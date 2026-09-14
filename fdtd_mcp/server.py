# -*- coding: utf-8 -*-
"""FDTD MCP Server — Lumerical FDTD automation over MCP stdio.

Architecture:
  AI Assistant --MCP stdio--> server.py (system Python >=3.10)
                                | subprocess stdin/stdout (line-delimited JSON)
                             bridge.py (Lumerical embed Python 3.6.8)
                                | lumapi
                             Lumerical FDTD engine

Sources of truth (deliberately only three, and they are cross-checked):
  * fdtd_mcp/toolspec.py   — every tool's schema, description, annotations,
                             defaults and timeout class (the MCP surface),
  * fdtd_mcp/dispatch.json — tool -> bridge method, shared with the bridge,
  * fdtd_mcp/bridge.py + engine/ + handlers/ — the behaviour itself.
tests/test_dispatch.py, tests/test_toolspec.py and tests/test_capability_guard.py
fail when those disagree, so no second hand-maintained table can drift.
"""
import sys, os, json, hashlib, subprocess, threading, re, queue, logging
import anyio
from collections import deque
from typing import Any
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from .discovery import find_lumerical, find_lumerical_python
from .engine import proctree
from .engine import library
from .toolspec import SPECS, spec_by_name, visible_specs
from .engine.capabilities import DEAD_COMMANDS, VERIFIED_AGAINST

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
# Per-call timeout (seconds). OFF by default so legitimate long simulations are
# never killed mid-run. Set FDTD_MCP_CALL_TIMEOUT (or pass timeout= on a single
# call to run/sweep_run/execute/execute_file) to bound a call: on expiry the
# bridge process tree is killed and auto-restarted on the next call, so a wedged
# engine cannot freeze the session forever. Note the kill discards unsaved
# in-memory engine state.
_CALL_TIMEOUT = None
try:
    _t = os.environ.get('FDTD_MCP_CALL_TIMEOUT')
    if _t is not None and _t.strip() != '':
        _CALL_TIMEOUT = float(_t)
        if _CALL_TIMEOUT <= 0:
            _CALL_TIMEOUT = None
except ValueError:
    pass

# Launching/closing a hidden engine can block for minutes (license checkout, an
# invisible modal dialog). Bounding it turns a 9-minute silent hang into a fast,
# diagnosable error. Override with FDTD_MCP_LAUNCH_TIMEOUT (0 disables).
try:
    _LAUNCH_TIMEOUT = float(os.environ.get('FDTD_MCP_LAUNCH_TIMEOUT', '300') or 300)
except ValueError:
    _LAUNCH_TIMEOUT = 300.0

# ---- Bundled lumapi ref data (server-side only) ----
_CHEATSHEET_PATH = os.path.join(os.path.dirname(__file__), 'cheatsheet', 'lumapi_ref.json')
_LUMAPI_REF = {}
_LUMAPI_NAMES = set()
if os.path.exists(_CHEATSHEET_PATH):
    try:
        with open(_CHEATSHEET_PATH, 'r', encoding='utf-8') as _f:
            _LUMAPI_REF = json.load(_f)
        _LUMAPI_NAMES = set(k for k in _LUMAPI_REF if k != 'meta')
    except Exception:
        pass

# ---- Single source of truth for tool dispatch (shared with bridge.py) ----
# Maps MCP tool name -> bridge method name. Load-bearing: both processes read
# the same JSON, so the two dispatch tables can no longer drift. Tools listed
# under "_server_only" have no bridge round-trip (handled in server.py); tools
# under "_hidden" are still dispatchable but not advertised to the LLM.
_DISPATCH_PATH = os.path.join(os.path.dirname(__file__), 'dispatch.json')
with open(_DISPATCH_PATH, 'r', encoding='utf-8') as _f:
    _DISPATCH_RAW = json.load(_f)
_SERVER_ONLY_TOOLS = frozenset(_DISPATCH_RAW.get('_server_only', []))
_HIDDEN_TOOLS = frozenset(_DISPATCH_RAW.get('_hidden', []))
_DISPATCH = {k: v for k, v in _DISPATCH_RAW.items()
             if k not in ('_server_only', '_hidden')}


def _get_lumerical_home():
    """Discover Lumerical installation root. CLI arg > env var > auto-detect."""
    for i, arg in enumerate(sys.argv):
        if arg == '--lumerical-home' and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    env = os.environ.get('LUMERICAL_HOME')
    if env:
        return env
    return find_lumerical()


LUMERICAL_HOME = _get_lumerical_home()
LUMERICAL_PYTHON = find_lumerical_python(LUMERICAL_HOME)
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
# Command used to spawn the bridge. Kept as one overridable list so tests can
# point the client at a fake bridge process.
BRIDGE_ARGS = [LUMERICAL_PYTHON, '-m', 'fdtd_mcp.bridge',
               '--lumerical-home', LUMERICAL_HOME]


def _code_id(*paths):
    """Short fingerprint of a set of files (for staleness diagnostics)."""
    h = hashlib.sha1()
    for path in paths:
        if os.path.isfile(path):
            h.update(os.path.basename(path).encode('utf-8'))
            with open(path, 'rb') as f:
                h.update(f.read())
    return h.hexdigest()[:12]


_SERVER_CODE_ID = _code_id(
    os.path.join(_PACKAGE_DIR, 'server.py'),
    os.path.join(_PACKAGE_DIR, 'toolspec.py'),
    _DISPATCH_PATH,
    _CHEATSHEET_PATH,
)


class BridgeClient(object):
    """Subprocess bridge to the Lumerical FDTD engine.

    stdout is drained by a background reader thread into a queue so calls never
    block the event loop and EOF (crash) is detected immediately. stderr is
    drained by a separate thread to avoid a full pipe-buffer deadlock.
    Responses are correlated by request id: a line that does not carry our id
    (engine chatter, a malformed request echo, a late reply) is kept for
    diagnostics instead of being mistaken for the answer, which used to shift
    every later response by one.
    """

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._resp_queue = None
        self._dead = False
        self._stderr_lines = deque(maxlen=200)
        self._unsolicited = deque(maxlen=50)
        self._ever_started = False
        self._restart_notice = None

    def is_dead(self):
        if self._dead or self._proc is None:
            return True
        return self._proc.poll() is not None

    def start(self):
        env = os.environ.copy()
        env['PATH'] = LUMERICAL_HOME + os.pathsep + env.get('PATH', '')
        env['PYTHONIOENCODING'] = 'utf-8'
        # The bridge lives in this package, so make sure the repo root is
        # importable no matter how the server itself was started.
        root = os.path.dirname(_PACKAGE_DIR)
        env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
        self._proc = subprocess.Popen(
            list(BRIDGE_ARGS),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=root, text=True, encoding='utf-8')
        self._dead = False
        self._resp_queue = queue.Queue()
        threading.Thread(target=self._read_loop, daemon=True,
                         name='bridge-reader').start()
        threading.Thread(target=self._stderr_loop, daemon=True,
                         name='bridge-stderr').start()
        self._handshake()
        if self._ever_started:
            self._restart_notice = (
                'The bridge/engine process was restarted, so any unsaved '
                'in-memory engine state is gone. Reopen the project with '
                'session_open if needed.')
        self._ever_started = True

    def _handshake(self, timeout=30):
        """Wait for the bridge's ready line, tolerating other output."""
        deadline = timeout
        while True:
            try:
                line = self._resp_queue.get(timeout=deadline)
            except queue.Empty:
                self._dead = True
                raise RuntimeError('Bridge failed to send ready handshake')
            if line is None:
                self._dead = True
                raise RuntimeError('Bridge closed before ready handshake')
            try:
                ready = json.loads(line)
            except (ValueError, TypeError):
                self._unsolicited.append(line.strip()[:200])
                continue
            if isinstance(ready, dict) and ready.get('ready'):
                self.bridge_code_id = ready.get('code_id')
                return ready
            self._unsolicited.append(json.dumps(ready)[:200])

    def _read_loop(self):
        try:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    break
                q = self._resp_queue
                if q is None:      # stop() already tore the queue down
                    break
                q.put(line)
        except Exception:
            pass
        finally:
            self._dead = True
            q = self._resp_queue
            if q is not None:
                q.put(None)        # EOF sentinel (responses are never None)

    def _stderr_loop(self):
        try:
            for line in self._proc.stderr:
                line = line.rstrip('\n')
                if line:
                    self._stderr_lines.append(line)
                    logging.debug('[bridge] %s', line)
        except Exception:
            pass

    def stderr_tail(self):
        return list(self._stderr_lines)

    def unsolicited(self):
        return list(self._unsolicited)

    def take_restart_notice(self):
        """Return (and clear) the one-shot 'the engine restarted' warning."""
        notice, self._restart_notice = self._restart_notice, None
        return notice

    def stop(self):
        proc = self._proc
        if not proc:
            return
        self._dead = True
        try:
            if proc.stdin:
                try:
                    proc.stdin.write(json.dumps(
                        {'id': self._req_id + 1, 'method': 'shutdown',
                         'params': {}}) + '\n')
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            # Never leave the engine behind: taskkill /T takes the whole tree
            # (bridge python + its fdtd-solutions.exe child), proc.kill() would
            # only take the python and orphan the engine.
            self._kill_bridge_tree()
        self._proc = None
        self._resp_queue = None

    def call(self, method, params=None, timeout=None):
        with self._lock:
            return self._call(method, params or {}, timeout)

    def _kill_bridge_tree(self):
        """Kill the bridge and its children so a wedged run() does not leave an
        orphaned fdtd-solutions solver process behind.

        Goes through proctree (TerminateProcess) rather than the taskkill
        binary: taskkill can be refused by an outer sandbox/policy, and a silent
        failure here is one of the ways engines get orphaned.
        """
        proc = self._proc
        if proc is None:
            return
        try:
            proctree.kill_pid(proc.pid, tree=True)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _next_line(self, timeout):
        """Read one stdout line, or raise on timeout / EOF."""
        try:
            if timeout:
                line = self._resp_queue.get(timeout=timeout)
            else:
                line = self._resp_queue.get()
        except queue.Empty:
            # A timed-out call means the engine is wedged (e.g. blocked on a
            # license). Kill the bridge tree and mark dead so the next call
            # auto-restarts a fresh engine instead of queueing forever.
            self._dead = True
            self._kill_bridge_tree()
            raise RuntimeError(
                'Bridge call timed out after %.0fs. The bridge was killed and '
                'will auto-restart; reopen your project if the engine state was '
                'lost. (bounds: FDTD_MCP_CALL_TIMEOUT / FDTD_MCP_LAUNCH_TIMEOUT)'
                % (timeout or 0))
        if line is None:
            self._dead = True
            raise RuntimeError('Bridge closed')
        return line

    def _call(self, method, params, timeout=None):
        if timeout is None:
            timeout = _CALL_TIMEOUT  # resolve global at call time
        if self._dead or self._proc is None:
            raise RuntimeError('Bridge is not running')
        self._req_id += 1
        req_id = self._req_id
        try:
            self._proc.stdin.write(json.dumps(
                {'id': req_id, 'method': method, 'params': params}) + '\n')
            self._proc.stdin.flush()
        except Exception as e:
            self._dead = True
            raise RuntimeError('Bridge write failed: ' + str(e))

        resp = self._await_response(req_id, timeout)
        if 'error' in resp:
            msg = resp['error'].get('message', str(resp['error']))
            if 'Traceback' in msg:
                msg = msg.split('\n')[0]
            raise RuntimeError(msg)
        return resp.get('result')

    def _await_response(self, req_id, timeout):
        """Return the response carrying `req_id`, ignoring unrelated lines."""
        while True:
            line = self._next_line(timeout)
            try:
                resp = json.loads(line)
            except (ValueError, TypeError):
                self._unsolicited.append(line.strip()[:200])
                continue
            if not isinstance(resp, dict):
                self._unsolicited.append(str(resp)[:200])
                continue
            if resp.get('id') != req_id:
                # Not ours: engine chatter, a stray echo, or a late reply for a
                # call that already timed out. Never treat it as the answer.
                self._unsolicited.append(json.dumps(resp)[:200])
                logging.debug('[bridge] unsolicited response: %s', resp)
                continue
            return resp


_bridge = BridgeClient()

# ---- Orphaned engine processes -------------------------------------------
# fdtd-solutions.exe is a CHILD of the bridge python, and Windows kills nothing
# when a parent dies. Two paths therefore orphan it: the bridge being killed
# without /T, and the MCP server being recycled while a project is open (the
# bridge sees EOF and exits). An orphan keeps an ANSYS license seat and a few
# hundred MB, and no client can ever reach it again — so it is always safe to
# kill. Engines of a live foreign process (the Lumerical GUI) are never touched.
_ORPHAN_SWEEP = os.environ.get('FDTD_MCP_ORPHAN_SWEEP', '1').lower() not in (
    '0', 'false', 'no', 'off')
_orphan_sweep_report = None
_orphan_sweep_lock = threading.Lock()


def _sweep_orphan_engines(force=False):
    """Kill engines whose parent process is gone. Idempotent, never raises."""
    global _orphan_sweep_report
    with _orphan_sweep_lock:
        if _orphan_sweep_report is not None and not force:
            return _orphan_sweep_report
        if not _ORPHAN_SWEEP:
            _orphan_sweep_report = {'enabled': False,
                                    'note': 'set FDTD_MCP_ORPHAN_SWEEP=1 to enable'}
            return _orphan_sweep_report
        try:
            report = proctree.sweep_orphans()
        except Exception as e:                      # diagnostics only
            report = {'error': str(e)[:300]}
        _orphan_sweep_report = report
        return report


def _engine_process_status():
    """Engine-process picture for engine_status (works with a dead bridge)."""
    bridge_pid = None
    proc = getattr(_bridge, '_proc', None)
    if proc is not None and not _bridge.is_dead():
        bridge_pid = proc.pid
    try:
        inv = proctree.inventory(bridge_pid=bridge_pid)
    except Exception as e:
        return {'error': str(e)[:300]}
    return {
        'server_pid': inv['own_pid'],
        'bridge_pid': bridge_pid,
        'counts': inv['counts'],
        'bridge_engine_pids': [i['pid'] for i in inv['bridge']],
        'orphan_pids': [i['pid'] for i in inv['orphans']],
        'foreign_pids': [i['pid'] for i in inv['foreign']],
        'notes': {
            'bridge': "the live session's engine (child of this server's bridge)"
                      ' — freed by closing or restarting the session',
            'orphan': 'parent process is gone — unreachable, holds a license seat, safe to kill',
            'foreign': 'engine of another LIVE client (e.g. the Lumerical GUI) — never killed',
        },
        'swept_at_start': _orphan_sweep_report,
    }


def _handle_engine_cleanup(arguments):
    """Server-side engine_cleanup: reap orphans, optionally restart the bridge."""
    args = dict(arguments) if arguments else {}
    dry_run = bool(args.get('dry_run'))
    include_orphans = args.get('include_orphans', True)
    if include_orphans is None:
        include_orphans = True
    before = _engine_process_status()
    if dry_run:
        return {'result': {
            'dry_run': True,
            'would_kill': before.get('orphan_pids', []),
            'kept_foreign': before.get('foreign_pids', []),
            'before': before,
        }}
    result = {'before': before}
    if args.get('restart_bridge'):
        _bridge.stop()          # taskkill /T: bridge python + its engine
        _ensure_bridge()
        result['bridge_restarted'] = {'generation': getattr(
            _bridge, 'bridge_code_id', None)}
    if include_orphans:
        result['sweep'] = _sweep_orphan_engines(force=True)
    result['after'] = _engine_process_status()
    result['status'] = 'ok'
    return {'result': result}


# ---- Tools, annotations and validation derived from toolspec.SPECS ----
_VISIBLE = visible_specs()
TOOLS = [types.Tool(name=s.name, description=s.description, inputSchema=s.schema)
         for s in _VISIBLE]
for _spec, _tool in zip(_VISIBLE, TOOLS):
    if _spec.read_only:
        _tool.annotations = types.ToolAnnotations(readOnlyHint=True)
    elif _spec.destructive:
        _tool.annotations = types.ToolAnnotations(destructiveHint=True)

_TOOL_NAMES = {t.name for t in TOOLS}
_SPEC_BY_NAME = spec_by_name()

# Startup validation: specs <-> dispatch.json <-> server-only set.
_missing = _TOOL_NAMES - _SERVER_ONLY_TOOLS - set(_DISPATCH.keys()) - _HIDDEN_TOOLS
if _missing:
    raise RuntimeError('Tools missing from dispatch.json: ' + ', '.join(sorted(_missing)))
_extra = (set(_DISPATCH.keys()) | _SERVER_ONLY_TOOLS) - set(_SPEC_BY_NAME)
if _extra:
    raise RuntimeError('dispatch.json references unknown tools: ' + ', '.join(sorted(_extra)))
for _name, _method in _DISPATCH.items():
    _spec = _SPEC_BY_NAME.get(_name)
    if _spec is not None and _spec.bridge_method != _method:
        raise RuntimeError(
            'toolspec/dispatch disagree for %s: spec says %s, dispatch.json says %s'
            % (_name, _spec.bridge_method, _method))

app = Server('fdtd-mcp')

@app.list_tools()
async def list_tools(): return TOOLS

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]):
    spec = _SPEC_BY_NAME.get(name)
    if spec is None:
        raise ValueError('Unknown tool: ' + name)

    # ----------------------------------------------------------------
    # Server-side tools (NO bridge round-trip)
    # ----------------------------------------------------------------
    if spec.server_only:
        if name == 'reference_lookup':
            return _handle_lumapi_ref(arguments)
        if name == 'engine_cleanup':
            return _handle_engine_cleanup(arguments)
        if name == 'library_list':
            return _handle_library_list(arguments)
        raise ValueError('No server-side handler for ' + name)

    # ----------------------------------------------------------------
    # Bridge-dispatched tools
    # ----------------------------------------------------------------
    _ensure_bridge()

    params = dict(arguments) if arguments else {}
    for key, value in spec.defaults.items():
        params.setdefault(key, value)

    # Per-call timeout override for long-running tools (0 disables the bound).
    call_timeout = _CALL_TIMEOUT
    if spec.launch:
        launch_bound = _LAUNCH_TIMEOUT if _LAUNCH_TIMEOUT > 0 else None
        if call_timeout is None:
            call_timeout = launch_bound
        elif launch_bound is not None:
            call_timeout = min(call_timeout, launch_bound)
    if spec.long_running:
        t = arguments.get('timeout')
        if t is not None:
            call_timeout = float(t) if t > 0 else 0.0
    params.pop('timeout', None)

    # ---- Script scan (advisory, non-blocking) ----
    warnings = []
    if _LUMAPI_NAMES:
        if name == 'execute':
            warnings = _scan_script_for_unknown_funcs(params.get('code', ''))
        elif name == 'model_script' and params.get('action') == 'set':
            warnings = _scan_script_for_unknown_funcs(params.get('content', ''))

    try:
        # Run the blocking bridge call off the event loop so the server stays
        # responsive (progress/cancellation/other requests) during long calls.
        result = await anyio.to_thread.run_sync(_bridge.call, spec.bridge_method,
                                                params, call_timeout)
    except RuntimeError as e:
        notice = _bridge.take_restart_notice()
        if notice:
            raise RuntimeError('%s (%s)' % (e, notice))
        raise

    if result is None:
        result = {}
    if spec.name == 'engine_status' and isinstance(result, dict):
        result['server'] = _server_status()
    if warnings:
        result['warnings'] = warnings
    notice = _bridge.take_restart_notice()
    if notice:
        result.setdefault('warnings', []).append(notice)
    return {'result': result}


def _server_status():
    """Server-side half of engine_status."""
    return {
        'server_code_id': _SERVER_CODE_ID,
        'tool_count': len(TOOLS),
        'spec_count': len(SPECS),
        'cheatsheet_functions': len(_LUMAPI_NAMES),
        'verified_against': VERIFIED_AGAINST,
        'call_timeout_s': _CALL_TIMEOUT,
        'launch_timeout_s': _LAUNCH_TIMEOUT,
        'bridge_alive': not _bridge.is_dead(),
        'bridge_code_id': getattr(_bridge, 'bridge_code_id', None),
        'bridge_unsolicited_output': _bridge.unsolicited()[-5:],
        'lumerical_home': LUMERICAL_HOME,
        'engine_processes': _engine_process_status(),
    }


def _library_item(entry):
    """Compact catalog entry for library_list."""
    summary = entry.get('summary') or ''
    if len(summary) > 100:
        summary = summary[:97] + '...'
    return {'id': entry['id'], 'type': entry['type'],
            'category': entry['category'], 'summary': summary,
            'installed': entry.get('installed', False)}


def _handle_library_list(arguments):
    """Server-side object-library listing (reads the install's catalog.xml)."""
    args = dict(arguments) if arguments else {}
    base = library.library_dir(LUMERICAL_HOME)
    items = library.catalog(LUMERICAL_HOME)
    if not items:
        return {'result': {
            'library_dir': base, 'count': 0, 'total': 0,
            'error': ('No object library catalog found (looked for %s under '
                      'LUMERICAL_HOME=%s).' % (library.LIBRARY_SUBDIR,
                                               LUMERICAL_HOME or '<unset>'))}}

    want_id = (args.get('id') or '').strip()
    if want_id:
        entry = library.find(want_id, LUMERICAL_HOME)
        if entry is None:
            return {'result': {
                'library_dir': base, 'total': len(items), 'count': 0,
                'error': 'No object-library item with id "%s".' % want_id,
                'did_you_mean': library.suggest(want_id, LUMERICAL_HOME)}}
        return {'result': {'library_dir': base, 'total': len(items),
                           'count': 1, 'items': [_library_item(entry)]}}

    want_type = (args.get('type') or '').strip().lower()
    want_cat = (args.get('category') or '').strip().lower()
    try:
        limit = int(args.get('limit') or 60)
    except (TypeError, ValueError):
        limit = 60
    limit = max(1, limit)

    matched = [i for i in items
               if (not want_type or want_type in i['type'].lower())
               and (not want_cat or want_cat in i['category'].lower())]
    return {'result': {
        'library_dir': base,
        'total': len(items),
        'counts_by_type': library.counts(items),
        'matched': len(matched),
        'returned': min(len(matched), limit),
        'truncated': len(matched) > limit,
        'categories': sorted(set(i['category'] for i in matched)),
        'items': [_library_item(i) for i in matched[:limit]],
        'next': 'library_insert(name="<id>") to insert one into the open project',
    }}


def _handle_lumapi_ref(arguments):
    """Server-side lookup of the bundled cheatsheet."""
    args = dict(arguments) if arguments else {}
    if args.get('list_only'):
        names = sorted(_LUMAPI_NAMES)
        return {'result': {
            'function_count': len(names),
            'functions': names,
            'scope_note': _LUMAPI_REF.get('meta', {}).get('scope', '')
        }}
    name = args.get('name', '')
    category = args.get('category', '')
    if name:
        entry = _LUMAPI_REF.get(name)
        if not entry:
            return {'result': {'error': 'Function "' + name + '" not in cheatsheet. Call list_only=true to see all known functions.', 'hint': _LUMAPI_REF.get('meta', {}).get('scope', '')}}
        return {'result': {'function': name, 'entry': entry}}
    if category:
        results = {k: v for k, v in _LUMAPI_REF.items() if k != 'meta' and v.get('category') == category}
        return {'result': {'category': category, 'count': len(results), 'functions': results}}
    return {'result': {'error': 'Provide name=, category=, or list_only=true'}}


# ---- LSF builtins allowlist (never warn on these) ----
_LSF_BUILTINS = frozenset({
    # Control flow
    'for', 'while', 'if', 'else', 'elif', 'end', 'break', 'continue',
    'switch', 'case', 'select', 'selectall', 'groupscope',
    # Core get/set
    'set', 'get', 'setnamed', 'getnamed',
    # Material
    'setmaterial', 'getmaterial', 'addmaterial',
    'deletematerial', 'copymaterial', 'materialexists',
    'importnk', 'importdataset', 'exportdataset', 'importmaterialdb',
    'savematerial', 'getindex', 'getfdtdindex', 'geteps',
    # Object operations
    'delete', 'clear', 'copy', 'move', 'min', 'max', 'sqrt',
    'abs', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'atan2',
    'exp', 'log', 'log10', 'real', 'imag', 'conj',
    'length', 'size', 'find', 'sprintf', 'str2num', 'num2str',
    'type', 'eval', 'feval', 'run', 'runsweep',
    'write', 'read', 'matlabsave', 'load',
    'readdata', 'loaddata',
    # File I/O
    'fileexists', 'fopen', 'fclose', 'fread', 'fwrite', 'format',
    'saveh5', 'loadh5', 'matlabload', 'matlabget', 'matlabput',
    'readtable', 'readspectrum',
    # Layout / analysis mode
    'switchtolayout', 'layoutmode', 'analysismode',
    # Add primitives
    'addfdtd', 'addrect', 'addcircle', 'addring',
    'addpoly', 'add2dpoly', 'add2drect', 'addsphere',
    'addpyramid', 'addtriangle', 'addwaveguide',
    # Sources
    'addtfsf', 'addgaussian', 'addplane', 'adddipole',
    # Monitors.
    # NOTE: addfield/adddftmonitor do not exist in v202 (verified live) — every
    # frequency-domain monitor is an addpower object — so they are not
    # allowlisted and a script using them raises the advisory warning.
    'addpower', 'addmovie', 'addindex', 'addprofile',
    # Mesh
    'addmesh',
    # Groups
    'addstructuregroup', 'addanalysisgroup', 'addassemblygroup',
    'addgroup', 'addtogroup',
    # Ports / EME / import
    'addimport', 'addmode', 'addport', 'addeme',
    # Variables & user properties.
    # NOTE: addvar/setvar/getvar/addvarfdtd were removed in v202 (verified live)
    # and are deliberately NOT allowlisted — a script using them must raise the
    # advisory warning so the model looks up the real replacement (adduserprop
    # on the selected model root / group).
    'adduserprop', 'addanalysisprop', 'addanalysisresult',
    'queryuserprop', 'queryanalysisprop',
    'addimportnk',
    # Sweep
    'addsweep', 'addsweepparameter', 'addsweepresult',
    'removesweepparameter', 'removesweepresult',
    'getsweep', 'getsweepdata', 'deletesweep',
    'copysweep', 'pastesweep', 'insertsweep',
    'setsweep',
    # Results.
    # NOTE: findresult/hasresult/listresults do not exist in v202 (verified
    # live) — only haveresult does, so they are not allowlisted.
    'getresult', 'getdata', 'getresultdata',
    'getelectric', 'getmagnetic',
    'clearresults', 'clearanalysis',
    'haveresult',
    # Farfield / transmission
    'farfield', 'farfield3d', 'farfieldpolar',
    'farfieldpolar2d', 'farfieldpolar3d',
    'transmission', 'dipolepower',
    # Analysis / setup run
    'runsetup', 'runanalysis',
    'getpath',
    # BC / EME profile
    'addbc', 'addemeprofile', 'addemeindex', 'addeffectiveindex',
    # Math / string
    'amax', 'amin',
    'substring', 'findstring', 'replace', 'replacestring',
    # System / misc
    'system', 'save', 'close',
})

# Runtime invariant (the static guard test also checks it): the allowlist must
# never bless a command the engine is known not to have.
_blessed_dead = sorted(_LSF_BUILTINS & DEAD_COMMANDS)
if _blessed_dead:
    raise RuntimeError('_LSF_BUILTINS allowlists unavailable commands: '
                       + ', '.join(_blessed_dead))


def _scan_script_for_funcs(text):
    """Extract all \\bFuncName( tokens, ignoring comments and string literals.

    Verified live on v202: `#` is the LSF comment character. (`%` is NOT — a
    `%` line makes the whole script fail — so it is only blanked here to keep
    the token scan quiet; `_percent_comment_warning` reports it separately.)
    """
    clean = re.sub(r'#.*', ' ', text)         # LSF comments start with #
    clean = re.sub(r'%.*', ' ', clean)        # not a comment on v202; don't scan it
    clean = re.sub(r'"[^"]*"', ' ', clean)    # double-quoted strings
    clean = re.sub(r"'[^']*'", ' ', clean)    # single-quoted strings
    return set(re.findall(r'(?:\b)([A-Za-z_]\w*)\s*\(', clean))


def _percent_comment_warning(text):
    """Warn about `%` line comments, which v202 rejects.

    This closes a documented-by-usage trap: `%` looks like a comment in many
    Lumerical examples, but a script containing one fails with the unhelpful
    'Failed to evaluate code' the moment it runs (runsetup / run).
    """
    if not text:
        return []
    lines = [n for n, line in enumerate(text.split('\n'), 1)
             if line.strip().startswith('%')]
    if not lines:
        return []
    return ['Script uses "%" line comments on line(s) '
            + ', '.join(str(n) for n in lines)
            + ', but LSF comments start with "#" — on v202 a "%" line makes the '
              'whole script fail with "Failed to evaluate code". Replace them '
              'with "#".']


def _scan_script_for_unknown_funcs(text):
    """Return advisory warnings for unrecognized function calls in script text."""
    if not text:
        return []
    warnings = _percent_comment_warning(text)
    called = _scan_script_for_funcs(text)
    unknown = called - _LUMAPI_NAMES - _LSF_BUILTINS
    if not unknown:
        return warnings
    sorted_u = sorted(unknown)
    msg = (
        'Script contains function names not recognized: '
        + ', '.join(sorted_u) + '. '
        'Not every unrecognized name is wrong (some are your custom functions). '
        'If you intended to call a Lumerical API function, first call '
        'reference_lookup(list_only=true) to check it exists and get the correct signature.'
    )
    warnings.append(msg)
    return warnings

_bridge_started = False
_bridge_start_lock = threading.Lock()

def _ensure_bridge():
    global _bridge_started
    with _bridge_start_lock:
        if not _bridge_started or _bridge.is_dead():
            if _bridge_started:
                _bridge.stop()
            _bridge.start()
            _bridge_started = True

def main():
    import atexit
    atexit.register(lambda: _bridge.stop() if _bridge_started else None)
    import anyio; anyio.run(_main)

async def _main():
    # Before the new bridge exists: sweep engines orphaned by the previous
    # server generation (its bridge is dead, so they can never be reached).
    _sweep_orphan_engines()
    _ensure_bridge()
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

if __name__ == '__main__':
    main()
