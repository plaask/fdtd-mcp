# -*- coding: utf-8 -*-
"""FDTD Bridge — JSON-RPC over stdin/stdout.

Runs on Lumerical embed Python 3.6.8 (no f-strings, no type hints anywhere in
this package's bridge-side modules).

Layered so each piece can be read, tested and replaced on its own:

    engine/constants.py     shared constants (caps, LSF facts, run() regex)
    engine/errors.py        BridgeError — the single expected-failure channel
    engine/lsf.py           LSF quoting, temp-variable hygiene, `?expr` parsing
    engine/capabilities.py  verified engine facts + code fingerprint
    engine/base.py          BridgeBase mix-in (_call_lsf, sanitise, name resolve)
    handlers/session.py     project lifecycle + run/sweep_run
    handlers/model.py       object tree, properties, scripts
    handlers/material.py    material database
    handlers/library.py     object library (structure/analysis groups)
    handlers/sweep.py       parameter sweeps
    handlers/result.py      monitor results
    handlers/engine_tools.py raw LSF execution + engine_status
    bridge.py               this file: argv/env, lumapi import, dispatch, loop

The tool -> method table lives in dispatch.json and is shared with server.py;
this module derives `_cmd_<method>` handlers from it and fails fast at startup
when one is missing.
"""
from __future__ import print_function
import sys
import os
import json
import time
import traceback
from collections import deque

# ---- Lumerical path discovery ----
# Accept --lumerical-home CLI arg (preferred) or LUMERICAL_HOME env var.
_home = os.environ.get('LUMERICAL_HOME', '')
for _i, _arg in enumerate(sys.argv):
    if _arg == '--lumerical-home' and _i + 1 < len(sys.argv):
        _home = sys.argv[_i + 1]
        break

if not _home:
    raise RuntimeError(
        'LUMERICAL_HOME not set. '
        'Pass --lumerical-home PATH or set LUMERICAL_HOME environment variable.'
    )

LUM_API = os.path.join(_home, 'api', 'python')
sys.path.insert(0, LUM_API)
os.environ['PATH'] = os.path.join(_home, 'bin') + os.pathsep + os.environ.get('PATH', '')
# The home may have arrived as --lumerical-home rather than through the
# environment; publish it so every later reader (engine_facts, diagnostics,
# child processes) sees the same value.
os.environ['LUMERICAL_HOME'] = _home

import lumapi                                    # noqa: E402
from lumapi import appCall                       # noqa: E402,F401  (handlers use it)

from fdtd_mcp.engine.base import BridgeBase                            # noqa: E402
from fdtd_mcp.engine.constants import ERR_MSG as _ERR_MSG_CAP          # noqa: E402
from fdtd_mcp.engine.errors import BridgeError                         # noqa: E402
from fdtd_mcp.engine.capabilities import code_id, VERIFIED_AGAINST     # noqa: E402
from fdtd_mcp.handlers.session import SessionHandlers                  # noqa: E402
from fdtd_mcp.handlers.model import ModelHandlers                      # noqa: E402
from fdtd_mcp.handlers.material import MaterialHandlers                # noqa: E402
from fdtd_mcp.handlers.attribute import AttributeHandlers              # noqa: E402
from fdtd_mcp.handlers.sweep import SweepHandlers                      # noqa: E402
from fdtd_mcp.handlers.result import ResultHandlers                    # noqa: E402
from fdtd_mcp.handlers.library import LibraryHandlers                  # noqa: E402
from fdtd_mcp.handlers.engine_tools import EngineHandlers              # noqa: E402

# ---- Single source of truth for tool dispatch (shared with server.py) ----
# Maps MCP tool name -> bridge method name. Convention: method name == the
# '_cmd_<method>' handler on FdtdBridge. Kept in JSON so the Python 3.6.8
# bridge and the >=3.10 server both read the same table with no drift.
# "_server_only" entries have no bridge round-trip and "_hidden" entries are
# not advertised to the LLM; both are skipped when building the dispatch map.
_DISPATCH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dispatch.json')
try:
    with open(_DISPATCH_PATH, 'r', encoding='utf-8') as _f:
        _DISPATCH_RAW = json.load(_f)
    _DISPATCH = {k: v for k, v in _DISPATCH_RAW.items()
                 if k not in ('_server_only', '_hidden')}
except Exception as _e:
    raise RuntimeError('Failed to load dispatch.json: ' + str(_e))

_TRACE_LIMIT = 50          # calls kept for engine_status


class FdtdBridge(SessionHandlers, ModelHandlers, MaterialHandlers,
                 AttributeHandlers, SweepHandlers, ResultHandlers,
                 LibraryHandlers, EngineHandlers, BridgeBase):
    """Dispatch table + session state; behaviour lives in the handler mix-ins."""

    def __init__(self):
        self._fsp = None
        self._path = None
        self._autosave_path = None
        self._tmp_dir = os.environ.get('TEMP', os.environ.get('TMP', '/tmp'))
        self._started_at = time.time()
        self._calls = 0
        self._lsf_call_count = 0
        self._session_generation = 0      # bumped on every open/new
        self._engine_pids = []            # engine processes launched by us
        self._engine_job = None           # kill-on-close job binding (if any)
        self._trace = deque(maxlen=_TRACE_LIMIT)
        # Derive the dispatch table from dispatch.json; fail fast if a
        # referenced method has no _cmd_<method> handler on this class.
        self._method_map = {}
        for _tool, _method in _DISPATCH.items():
            _handler = getattr(self, '_cmd_' + _method, None)
            if _handler is None:
                raise RuntimeError(
                    'dispatch.json references missing handler: _cmd_' + _method)
            self._method_map[_method] = _handler

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle(self, request):
        req_id = request.get('id')
        method = request.get('method', '')
        params = request.get('params', {})
        started = time.time()
        self._calls += 1
        try:
            handler = self._method_map.get(method)
            if handler is None:
                self._record(method, params, started, 'Unknown method: ' + method)
                return self._error(req_id, 'Unknown method: ' + method)
            result = handler(params)
            self._record(method, params, started, None)
            return self._ok(req_id, result)
        except BridgeError as e:
            self._record(method, params, started, str(e))
            return self._error(req_id, str(e))
        except Exception as e:
            tb = traceback.format_exc()
            if tb and tb.strip():
                msg = tb.strip().split('\n')[-1][:_ERR_MSG_CAP]
            else:
                msg = str(e)[:_ERR_MSG_CAP]
            self._record(method, params, started, msg)
            return self._error(req_id, msg)

    def _record(self, method, params, started, error):
        """Keep a bounded call log for engine_status (never raises)."""
        try:
            summary = json.dumps(params, default=str, sort_keys=True)
        except Exception:
            summary = str(params)
        if len(summary) > 200:
            summary = summary[:200] + '...'
        try:
            self._trace.append({
                'method': method,
                'params': summary,
                'ms': round((time.time() - started) * 1000.0, 1),
                'ok': error is None,
                'error': (error or '')[:200] or None,
            })
        except Exception:
            pass

    def _ok(self, rid, result):
        return {'id': rid, 'result': result}

    def _error(self, rid, message):
        return {'id': rid, 'error': {'code': -1, 'message': message[:_ERR_MSG_CAP]}}


def main():
    bridge = FdtdBridge()
    sys.stdout.write(json.dumps({
        'ready': True,
        'code_id': code_id(),
        'verified_against': VERIFIED_AGAINST,
    }) + '\n')
    sys.stdout.flush()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                # Answered with an id-less error: server.py correlates responses by
                # id and treats an id-less line as unsolicited output, so it no
                # longer desynchronises the request/response pairing.
                sys.stdout.write(json.dumps({'error': 'Invalid JSON'}) + '\n')
                sys.stdout.flush()
                continue
            if request.get('method') == 'shutdown':
                break
            resp = bridge.handle(request)
            sys.stdout.write(json.dumps(resp, default=str) + '\n')
            sys.stdout.flush()
    finally:
        # Runs on every exit path, including EOF on stdin when the MCP server
        # goes away (patch reload / `dsh web` restart). Without it each server
        # recycle orphans one fdtd-solutions.exe, which keeps an ANSYS license
        # seat and shows up in the task manager as a windowless FDTD instance.
        try:
            bridge.shutdown()
        except Exception:
            pass
    sys.exit(0)


if __name__ == '__main__':
    main()
