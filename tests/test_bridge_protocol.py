# -*- coding: utf-8 -*-
"""Test the bridge protocol without a Lumerical installation.

A subprocess runs bridge.main() with a fake lumapi injected; the test drives
it over real stdin/stdout pipes. Verifies the ready handshake, dispatch.json
wiring (fail-fast handler derivation), the unified error channel, and the
session_save / session_save_as fixes.
"""
import json
import os
import subprocess
import sys
import types
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
_RUNNER = str(Path(__file__).resolve().parent / 'run_bridge_inprocess.py')


def _run_requests(requests):
    proc = subprocess.Popen([sys.executable, _RUNNER],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True, encoding='utf-8')
    out, err = proc.communicate(
        '\n'.join(json.dumps(r) for r in requests) + '\n')
    assert proc.returncode == 0, 'bridge runner failed: ' + err
    return [json.loads(l) for l in out.splitlines() if l.strip()]


def test_ready_handshake_and_unknown_method():
    out = _run_requests([{'id': 1, 'method': 'no_such_method', 'params': {}}])
    assert out[0] == {'ready': True}
    assert out[1]['id'] == 1
    assert 'error' in out[1]
    assert 'Unknown method' in out[1]['error']['message']


def test_dispatch_json_derives_handler_map():
    os.environ['LUMERICAL_HOME'] = str(_PKG)
    lumapi = types.ModuleType('lumapi')
    lumapi.FDTD = lambda *a, **k: None
    lumapi.appCall = lambda *a, **k: None
    sys.modules['lumapi'] = lumapi
    import fdtd_mcp.bridge as bridge

    inst = bridge.FdtdBridge()  # constructor fails fast if a handler is missing
    unique_methods = {m for m in bridge._DISPATCH.values()}
    assert len(inst._method_map) == len(unique_methods)
    for method in unique_methods:
        assert method in inst._method_map
        assert callable(inst._method_map[method])


def test_no_open_project_error_channel():
    out = _run_requests([{'id': 1, 'method': 'model_info', 'params': {}}])
    resp = out[1]
    assert 'error' in resp
    assert 'No open project' in resp['error']['message']


def test_session_new_then_save_without_path_errors():
    out = _run_requests([
        {'id': 1, 'method': 'session_new', 'params': {}},
        {'id': 2, 'method': 'session_save', 'params': {}},
    ])
    assert out[1]['result']['status'] == 'ok'
    assert 'no current file path' in out[2]['error']['message']


def test_session_save_as_maps_to_session_save():
    os.environ['LUMERICAL_HOME'] = str(_PKG)
    lumapi = types.ModuleType('lumapi')
    lumapi.FDTD = lambda *a, **k: None
    lumapi.appCall = lambda *a, **k: None
    sys.modules['lumapi'] = lumapi
    import fdtd_mcp.bridge as bridge

    # dispatch.json maps the session_save_as tool onto the session_save method.
    assert bridge._DISPATCH['session_save_as'] == 'session_save'
    assert '_cmd_session_save' in dir(bridge.FdtdBridge)


def test_session_save_with_path_ok():
    out = _run_requests([
        {'id': 1, 'method': 'session_new', 'params': {}},
        {'id': 2, 'method': 'session_save', 'params': {'path': 'D:/y.fsp'}},
    ])
    assert out[2]['result']['status'] == 'ok'
    assert out[2]['result']['path'] == 'D:/y.fsp'
