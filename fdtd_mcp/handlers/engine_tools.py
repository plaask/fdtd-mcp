# -*- coding: utf-8 -*-
"""Raw LSF execution and engine status.

Split out of bridge.py; runs on Lumerical's bundled Python 3.6.8, so keep the
syntax 3.6-compatible (no f-strings, no type hints).
"""
from __future__ import print_function
import os
import re
import time

from ..engine.base import BridgeBase
from ..engine.capabilities import (DEAD_COMMANDS, VERIFIED_AGAINST, code_id,
                                   code_id_of, engine_facts)
from ..engine.constants import ERR_MSG as _ERR_MSG
from ..engine.constants import RUN_COMMAND as _RUN_RE
from ..engine.errors import BridgeError
from ..engine.lsf import lsf_quote, parse_query, strip_comments_and_strings

# `set("setup script", ...)` in any spacing/quoting — script editing has its own
# tool. Normalising whitespace first closes the `set ("analysis script", ...)`
# bypass the old regex left open.
_SCRIPT_SET = re.compile(
    r'\bset\s*\(\s*[\'"]?\s*(setup\s+script|analysis\s+script|script)\s*[\'"]?\s*,')
_WS = re.compile(r'\s+')


def _rewrite_query_lines(code, path):
    """Rewrite LSF `?expr` lines into `write(path, num2str(expr))`.

    lumapi.eval() does not return the text printed by `?`, so multi-statement
    scripts cannot capture query values otherwise. Returns (script, count).
    Only lines whose first non-space character is `?` are rewritten; `?`
    inside a string or comment is left alone.
    """
    out = []
    count = 0
    for line in code.split('\n'):
        stripped = line.lstrip()
        if stripped.startswith('?'):
            expr = stripped[1:].split('#', 1)[0].strip()
            if expr.endswith(';'):
                expr = expr[:-1].rstrip()
            if expr:
                indent = line[:len(line) - len(stripped)]
                out.append('%swrite(%s, num2str(%s));'
                           % (indent, lsf_quote(path), expr))
                count += 1
                continue
        out.append(line)
    return '\n'.join(out), count


class EngineHandlers(BridgeBase):
    """Raw LSF execution and engine status."""

    def _reset_to_model_root(self):
        """Put the engine's current group scope back at ::model.

        Verified live on v202: after a group setup script runs, the engine's
        current scope can be left inside that group, so a later raw script that
        creates an object (addrect; set("name", ...)) silently creates it as a
        child of that group — and deleting the group takes it along. Raw
        execution therefore always starts from the model root.
        """
        try:
            self._fsp.eval('groupscope("::model");')
        except Exception:
            pass

    def _cmd_execute(self, p):
        """Execute LSF code from the model root scope.

        `?expr` captures a return value (function call or bare name); anything
        else is passed to the engine verbatim, with no parsing or splitting.
        """
        self._require_project()
        code = p['code']

        if _SCRIPT_SET.search(_WS.sub(' ', code)):
            raise BridgeError(
                'Do NOT use execute() to set scripts. '
                'Use model_script(action="set", ...) instead.')

        if p.get('capture'):
            return self._execute_capture(code)

        if code.startswith('?'):
            return self._query(code[1:].strip())

        # Only a real run()/runsweep() call needs the project saved first — a
        # `# run this later` comment must not trigger a disk write.
        if _RUN_RE.search(strip_comments_and_strings(code)):
            self._ensure_project_saved()
        self._reset_to_model_root()
        try:
            self._fsp.eval(code)
        except Exception as e:
            msg = str(e)[:_ERR_MSG]
            if 'Failed to evaluate code' in msg:
                msg += (' | LSF rejected the script (comments start with #, not '
                        '%); use execute_file for line numbers, or execute('
                        'capture=True) to capture ? values.')
            raise BridgeError(msg)
        return {'status': 'ok'}

    def _execute_capture(self, code):
        """Run a script and capture its `?expr` lines via a temp file."""
        tmp = os.path.join(self._tmp_dir, '_mcp_capture_%d.txt' % os.getpid())
        try:
            os.remove(tmp)
        except OSError:
            pass
        script, count = _rewrite_query_lines(code, tmp)
        if not count:
            if _RUN_RE.search(strip_comments_and_strings(code)):
                self._ensure_project_saved()
            self._reset_to_model_root()
            try:
                self._fsp.eval(code)
            except Exception as e:
                raise BridgeError(str(e)[:_ERR_MSG])
            return {'status': 'ok', 'captured': [],
                    'note': 'no lines starting with "?"; nothing to capture'}

        if _RUN_RE.search(strip_comments_and_strings(script)):
            self._ensure_project_saved()
        self._reset_to_model_root()
        try:
            self._fsp.eval(script)
        except Exception as e:
            raise BridgeError('capture failed: %s' % str(e)[:_ERR_MSG])
        try:
            text = None
            for enc in ('utf-8', 'gbk', 'gb18030'):
                try:
                    with open(tmp, 'r', encoding=enc) as f:
                        text = f.read()
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if text is None:
                with open(tmp, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            lines = [ln for ln in text.splitlines() if ln.strip()]
        except Exception as e:
            raise BridgeError('captured file could not be read: %s'
                              % str(e)[:_ERR_MSG])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return {'status': 'ok', 'query_count': count, 'captured': lines,
                'stdout': '\n'.join(lines)}

    def _query(self, expr):
        """Resolve a `?expr` query into a returned value."""
        parsed = parse_query(expr)
        if parsed is not None:
            _kind, (func_name, args) = parsed
            if func_name in DEAD_COMMANDS:
                raise BridgeError(
                    '%s does not exist on this engine version (%s). See '
                    'reference_lookup for the working replacement.'
                    % (func_name, VERIFIED_AGAINST))
            try:
                result = self._call_lsf(func_name, *args)
            except Exception as e:
                raise BridgeError('%s(%s) failed: %s'
                                  % (func_name, ', '.join(repr(a) for a in args),
                                     str(e)[:_ERR_MSG]))
            return {'status': 'ok', 'result': self._sanitize(result),
                    'query': func_name}

        # Bare name: engine variable first, then a model property.
        try:
            result = self._fsp.getv(expr)
            return {'status': 'ok', 'result': self._sanitize(result),
                    'query': 'getv'}
        except Exception:
            pass
        try:
            result = self._call_lsf('getnamed', '::model', expr)
            return {'status': 'ok', 'result': self._sanitize(result),
                    'query': 'getnamed'}
        except Exception as e:
            raise BridgeError("could not read '%s': %s" % (expr, str(e)[:_ERR_MSG]))

    def _cmd_execute_file(self, p):
        """Run a .lsf script file, starting from the model root scope."""
        self._require_project()
        self._ensure_project_saved()  # the file may contain a run/runsweep command
        self._reset_to_model_root()
        try:
            self._fsp.feval(p['path'])
        except Exception as e:
            raise BridgeError('execute_file failed: %s' % str(e)[:_ERR_MSG])
        return {'status': 'ok', 'path': p['path']}

    def _cmd_engine_status(self, p):
        """Diagnostics: which code answered, what the engine is holding.

        Answers the two questions that otherwise need guesswork:
          * is the bridge running the code on disk? (`stale_code` compares the
            running fingerprint with the files on disk),
          * is the engine alive, and what does the bridge remember about it?
        """
        disk_id = None
        try:
            disk_id = code_id_of(_package_dir())
        except Exception:
            pass
        running = code_id()

        status = {
            'status': 'ok',
            'bridge_pid': os.getpid(),
            'bridge_uptime_s': round(_uptime(self), 1),
            'bridge_code_id': running,
            'disk_code_id': disk_id,
            'stale_code': bool(disk_id and disk_id != running),
            'verified_against': VERIFIED_AGAINST,
            'calls_handled': getattr(self, '_calls', 0),
            'lsf_calls': getattr(self, '_lsf_call_count', 0),
            'engine': engine_facts(),
            'project_open': bool(self._fsp),
            'project_path': self._path,
            'autosave_path': self._autosave_path,
            'session_generation': getattr(self, '_session_generation', 0),
            'session_engine_pids': list(getattr(self, '_engine_pids', []) or []),
            'engine_job': getattr(self, '_engine_job', None),
            'engine_processes': self.engine_processes(),
            'recent_calls': list(getattr(self, '_trace', []) or []),
        }
        if self._fsp:
            status['layout_mode'] = self._layout_mode()
            status['mode'] = self._mode_name()
            summary = {}
            for key in ('dimension', 'x span', 'y span', 'z span',
                        'simulation time', 'mesh accuracy'):
                try:
                    summary[key] = self._fsp.getnamed('FDTD', key)
                except Exception:
                    pass
            status['fdtd_summary'] = summary
        if status['stale_code']:
            status['note'] = (
                'The running bridge predates the code on disk: restart the '
                'bridge process (or the MCP server) for bridge-side changes to '
                'take effect.')
        return status


def _package_dir():
    """Directory of the fdtd_mcp package (holds bridge.py + dispatch.json)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _uptime(bridge):
    started = getattr(bridge, '_started_at', None)
    return time.time() - started if started else 0.0
