# -*- coding: utf-8 -*-
"""Comprehensive functional test pass over all 30 MCP tools.

Two layers:
  A) Bridge-level CRUD round-trips driven over real stdin/stdout pipes against
     bridge.main() with the scripted fake_engine lumapi (tests/fake_engine.py).
  B) Server-side logic exercised in-process: reference_lookup, call_tool
     parameter defaults, the anti-hallucination script scanner, tool schema
     validity, annotations, and the BridgeClient FDTD_MCP_CALL_TIMEOUT path.

Test files added by this pass:
  tests/fake_engine.py           shared fake lumapi engine
  tests/run_bridge_functional.py subprocess runner (installs fake_engine)
  tests/test_functional.py       this file
  tests/fake_bridge.py           (modified) added a 'sleep' method for the
                                 timeout test
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

_RUNNER = str(Path(__file__).resolve().parent / 'run_bridge_functional.py')
_FAKE = str(Path(__file__).resolve().parent / 'fake_bridge.py')

import fdtd_mcp.server as srv  # noqa: E402


# ---------------------------------------------------------------------------
# Bridge subprocess session (one per module, state reset via session_new)
# ---------------------------------------------------------------------------

class _BridgeSession(object):
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, _RUNNER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8')
        line = self.proc.stdout.readline()
        self.ready = json.loads(line)
        self._id = 0

    def send(self, method, params=None):
        self._id += 1
        req = {'id': self._id, 'method': method, 'params': params or {}}
        self.proc.stdin.write(json.dumps(req) + '\n')
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def fresh(self, **kwargs):
        """Start a blank project (resets the in-memory engine state)."""
        return self.send('session_new', kwargs)

    def close(self):
        try:
            self.proc.stdin.write(
                json.dumps({'id': 999, 'method': 'shutdown', 'params': {}}) + '\n')
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


@pytest.fixture(scope='module')
def bs():
    s = _BridgeSession()
    assert s.ready == {'ready': True}
    yield s
    s.close()


# ---------------------------------------------------------------------------
# session module (5 tools)
# ---------------------------------------------------------------------------

def test_session_new_ok(bs):
    r = bs.fresh(dimension='3D', mesh_accuracy=4)
    assert r['result']['status'] == 'ok'
    assert r['result']['config']['dimension'] == '3D'


def test_session_new_config_keys_applied(bs):
    # Regression: the schema uses underscore keys (x_span, mesh_accuracy); the
    # bridge now accepts both underscore and space-key forms.
    r = bs.fresh(dimension='3D', x_span=2e-6, mesh_accuracy=4)
    assert r['result']['status'] == 'ok'
    assert r['result']['config'] == {
        'dimension': '3D', 'x span': 2e-6, 'mesh accuracy': 4}


def test_session_open(bs):
    r = bs.send('session_open', {'path': 'D:/fake/proj.fsp'})
    assert r['result']['status'] == 'ok'
    assert r['result']['path'] == 'D:/fake/proj.fsp'
    assert r['result']['summary']['dimension'] == '3D'


def test_session_close(bs):
    bs.fresh()
    r = bs.send('session_close', {})
    assert r['result']['status'] == 'closed'
    r2 = bs.send('model_info', {})
    assert 'error' in r2 and 'No open project' in r2['error']['message']


def test_session_save(bs, tmp_path):
    bs.fresh()
    # no path yet -> must error
    r = bs.send('session_save', {})
    assert 'error' in r and 'no current file path' in r['error']['message']
    # explicit path
    p = tmp_path / 'p.fsp'
    r = bs.send('session_save', {'path': str(p)})
    assert r['result']['status'] == 'ok'
    assert p.exists()
    # now overwrite works
    r = bs.send('session_save', {})
    assert r['result']['status'] == 'ok'
    # session_save_as -> dispatch.json maps it onto the same _cmd_session_save
    # handler (verified in test_dispatch.py); drive the underlying method here.
    p2 = tmp_path / 'p2.fsp'
    r = bs.send('session_save', {'path': str(p2)})
    assert r['result']['status'] == 'ok'
    assert p2.exists()


# ---------------------------------------------------------------------------
# model module (6 tools)
# ---------------------------------------------------------------------------

def test_model_add_get_set_delete(bs):
    bs.fresh()
    r = bs.send('model_add', {'type': 'rectangle', 'name': 'r1',
                              'properties': {'x span': 2e-6, 'y span': 1e-6}})
    assert r['result']['status'] == 'ok'
    assert r['result']['name'] == 'r1'
    assert r['result']['type'] == 'rectangle'

    r = bs.send('model_get', {'name': 'r1'})
    res = r['result']
    assert res['name'] == 'r1'
    assert res['type'] == 'Rectangle'
    assert res['x span'] == 2e-6
    assert res['y span'] == 1e-6
    assert res['enabled'] is True

    r = bs.send('model_set', {'name': 'r1',
                              'properties': {'x span': 3e-6, 'material': 'Silicon'}})
    assert r['result']['properties_set'] == 2

    r = bs.send('model_get', {'name': 'r1'})
    assert r['result']['x span'] == 3e-6
    assert r['result']['material'] == 'Silicon'

    r = bs.send('model_delete', {'name': 'r1'})
    assert r['result']['status'] == 'ok'
    r = bs.send('model_get', {'name': 'r1'})
    assert 'error' in r and 'Object not found' in r['error']['message']


def test_model_add_auto_name_source_kind_unknown_type(bs):
    bs.fresh()
    r = bs.send('model_add', {'type': 'dipole'})
    name = r['result']['name']
    assert name and r['result']['type'] == 'dipole'
    r = bs.send('model_get', {'name': name})
    assert r['result']['type'] == 'Dipole'
    assert r['result']['source_kind'] == 'dipole'

    r = bs.send('model_add', {'type': 'bogus'})
    assert 'error' in r and 'Unknown object type' in r['error']['message']


def test_model_info(bs):
    bs.fresh()
    bs.send('model_add', {'type': 'rectangle', 'name': 'r1'})
    bs.send('model_add', {'type': 'dipole', 'name': 'src1'})
    bs.send('model_add', {'type': 'circle', 'name': 'c1',
                          'properties': {'enabled': False}})
    bs.send('model_set', {'name': '::model', 'properties': {'gap': 200e-9}})
    bs.send('material_add', {'type': 'Dielectric'})

    r = bs.send('model_info', {})
    res = r['result']
    names = [o['name'] for o in res['objects']]
    assert 'r1' in names and 'src1' in names
    assert 'c1' not in names  # disabled filtered by default
    assert res['model_variables']['gap'] == 2e-07
    assert res['fdtd_summary']['dimension'] == '3D'
    assert any('material' in m for m in res['materials'])

    # include_full exposes the full prop set
    r = bs.send('model_info', {'include_full': True})
    full = {o['name']: o for o in r['result']['objects']}
    assert 'x span' in full['r1']

    # enabled_only=false shows disabled objects
    r = bs.send('model_info', {'enabled_only': False})
    names2 = [o['name'] for o in r['result']['objects']]
    assert 'c1' in names2


def test_model_info_group_children(bs):
    bs.fresh()
    bs.send('model_add', {'type': 'structure_group', 'name': 'grp'})
    bs.send('model_add', {'type': 'rectangle', 'name': 'inner', 'scope': 'grp'})
    r = bs.send('model_info', {})
    names = [o['name'] for o in r['result']['objects']]
    assert 'grp' in names and 'inner' in names
    r = bs.send('model_get', {'name': 'inner'})
    assert r['result']['type'] == 'Rectangle'


def test_model_script_get_and_set(bs):
    bs.fresh()
    r = bs.send('model_script', {'name': '::model', 'action': 'get'})
    assert 'setup_script' in r['result'] and 'analysis_script' in r['result']

    bs.send('model_add', {'type': 'structure_group', 'name': 'myg'})
    r = bs.send('model_script', {'name': 'myg', 'action': 'get'})
    assert 'script' in r['result']

    # Regression: the server sends script_type=; _cmd_set_script now accepts it.
    r = bs.send('model_script', {'name': '::model', 'action': 'set',
                                 'script_type': 'setup',
                                 'content': 'addvar("gap", 200e-9)'})
    assert r['result']['status'] == 'ok'
    assert r['result']['script_type'] == 'setup script'
    r = bs.send('model_script', {'name': '::model', 'action': 'get'})
    assert 'addvar("gap"' in r['result']['setup_script']

    # Legacy 'type' key is still accepted.
    r = bs.send('model_script', {'name': '::model', 'action': 'set',
                                 'type': 'setup',
                                 'content': 'addvar("gap2", 1e-9)'})
    assert r['result']['status'] == 'ok'


# ---------------------------------------------------------------------------
# material module (5 tools)
# ---------------------------------------------------------------------------

def test_material_crud(bs):
    bs.fresh()
    r = bs.send('material_add', {'type': 'Dielectric'})
    assert r['result']['status'] == 'ok'
    name = r['result']['name']
    assert name

    r = bs.send('material_get', {'name': name})
    props = r['result']
    assert isinstance(props, list) and 'Refractive Index' in props

    r = bs.send('material_set', {'name': name, 'property': 'Refractive Index',
                                 'value': 1.5})
    assert r['result']['status'] == 'ok'
    r = bs.send('material_get', {'name': name, 'property': 'Refractive Index'})
    assert r['result'] == 1.5

    r = bs.send('material_exists', {'name': name})
    assert r['result']['exists'] is True
    r = bs.send('material_exists', {'name': 'DoesNotExist'})
    assert r['result']['exists'] is False

    r = bs.send('material_delete', {'name': name})
    assert r['result']['status'] == 'ok'
    r = bs.send('material_exists', {'name': name})
    assert r['result']['exists'] is False
    r = bs.send('material_get', {'name': name})
    assert 'error' in r


def test_material_sampled_data(bs):
    bs.fresh()
    r = bs.send('material_add', {})  # default 'Sampled 3D data'
    name = r['result']['name']
    data = [[300e-9, 1.5, 0], [800e-9, 1.5, 0]]
    r = bs.send('material_set', {'name': name, 'property': 'sampled 3d data',
                                 'value': data})
    assert r['result']['status'] == 'ok'
    r = bs.send('material_get', {'name': name, 'property': 'sampled 3d data'})
    out = r['result']
    assert out['shape'] == [2, 3]
    assert out['data'][0] == [3e-07, 1.5, 0.0]


# ---------------------------------------------------------------------------
# sweep module (6 tools)
# ---------------------------------------------------------------------------

def test_sweep_crud(bs):
    bs.fresh()
    params = [{'name': 'gap', 'parameter': '::model>gap', 'type': 'Linear',
               'start': 100e-9, 'stop': 300e-9, 'points': 5}]
    results = [{'name': 'T', 'result': 'DFT>T'}]
    r = bs.send('sweep_add', {'type': 0, 'name': 'gap_sweep',
                              'parameters': params, 'results': results})
    res = r['result']
    assert res['status'] == 'ok'
    assert res['parameter_count'] == 1 and res['result_count'] == 1
    assert not res['parameter_errors'] and not res['result_errors']

    r = bs.send('sweep_get', {'name': 'gap_sweep'})
    assert r['result']['exists'] is True
    assert r['result']['has_results'] is False

    r = bs.send('sweep_run', {'name': 'gap_sweep'})
    assert r['result']['status'] == 'completed'

    r = bs.send('sweep_get', {'name': 'gap_sweep'})
    assert r['result']['has_results'] is True
    assert r['result']['exists'] is True  # exists present on success too

    r = bs.send('sweep_result', {'name': 'gap_sweep'})
    assert r['result']['T']['T'] is not None
    # Regression: the optional result= param is honored (filtered result).
    r = bs.send('sweep_result', {'name': 'gap_sweep', 'result': 'T'})
    assert set(r['result'].keys()) == {'T'}

    r = bs.send('sweep_set', {'name': 'gap_sweep', 'properties': {'sweep type': 1}})
    assert r['result']['status'] == 'ok'

    r = bs.send('sweep_delete', {'name': 'gap_sweep'})
    assert r['result']['status'] == 'ok'
    r = bs.send('sweep_get', {'name': 'gap_sweep'})
    assert r['result']['exists'] is False


# ---------------------------------------------------------------------------
# result module (4 tools) + run
# ---------------------------------------------------------------------------

def test_result_lifecycle(bs, tmp_path):
    bs.fresh()
    bs.send('model_add', {'type': 'dft_monitor', 'name': 'DFT'})
    bs.send('model_add', {'type': 'power_monitor', 'name': 'PWR'})

    r = bs.send('result_has', {'monitor': 'DFT'})
    assert r['result']['exists'] is False

    r = bs.send('run', {})
    assert r['result']['status'] == 'completed'

    r = bs.send('result_has', {'monitor': 'DFT'})
    assert r['result']['exists'] is True

    r = bs.send('result_list', {'monitor': 'DFT'})
    res = r['result']
    assert res['monitor'] == 'DFT'
    e_ds = [d for d in res['datasets'] if d['data'] == 'E']
    assert e_ds and 'Ex' in e_ds[0]['fields']

    r = bs.send('result_list', {})
    assert r['result']['monitor'] == 'FDTD'
    assert r['result']['dataset_count'] >= 1

    r = bs.send('result_get', {'monitor': 'DFT', 'data': 'E',
                               'fields': ['Ex', 'Ey', 'f', 'lambda']})
    res = r['result']
    for f in ('Ex', 'Ey', 'f', 'lambda'):
        assert f in res['values'] and 'error' not in res['values'][f]
    assert res['truncated'] == {}

    # small cap -> truncation meta reported
    r = bs.send('result_get', {'monitor': 'DFT', 'data': 'E',
                               'fields': ['Ex'], 'cap': 5})
    assert r['result']['values']['Ex']['truncated'] is True
    assert r['result']['truncated']['Ex'] == 1000

    # unknown field -> per-field error, not a crash
    r = bs.send('result_get', {'monitor': 'DFT', 'data': 'E', 'fields': ['nope']})
    assert 'error' in r['result']['values']['nope']

    # empty fields -> clean error
    r = bs.send('result_get', {'monitor': 'DFT', 'fields': []})
    assert 'error' in r and 'fields' in r['error']['message']

    out = tmp_path / 'fields.mat'
    r = bs.send('result_save', {'monitor': 'DFT', 'data': 'E', 'output': str(out)})
    assert r['result']['status'] == 'ok'
    assert out.exists()


def test_result_save_without_output(bs):
    # Regression: output is optional; the bridge now generates a default .mat
    # path in the temp dir instead of failing on matlabsave("").
    bs.fresh()
    bs.send('model_add', {'type': 'dft_monitor', 'name': 'DFT'})
    bs.send('run', {})
    r = bs.send('result_save', {'monitor': 'DFT', 'data': 'E', 'output': ''})
    assert r['result']['status'] == 'ok'
    assert r['result']['file'].endswith('.mat')


# ---------------------------------------------------------------------------
# engine module: execute / execute_file
# ---------------------------------------------------------------------------

def test_execute(bs):
    bs.fresh()
    # transparent eval
    r = bs.send('execute', {'code': 'addrect();'})
    assert r['result']['status'] == 'ok'

    # ?expr function-call capture (works when quoted args are comma-adjacent;
    # see test_execute_expr_spaced_args_bug for the spaced-arg regression)
    r = bs.send('execute', {'code': '?getnamed("rect","type")'})
    assert r['result']['result'] == 'Rectangle'

    # ?bare model variable via getv
    bs.send('model_set', {'name': '::model', 'properties': {'gap': 200e-9}})
    r = bs.send('execute', {'code': '?gap'})
    assert r['result']['result'] == 2e-07
    r = bs.send('execute', {'code': '?getnamed("::model","gap")'})
    assert r['result']['result'] == 2e-07

    # script-edit guard
    r = bs.send('execute', {'code': 'set("setup script", "x=1;")'})
    assert 'error' in r and 'Do NOT use execute' in r['error']['message']


def test_execute_expr_spaced_args(bs):
    """Regression: execute('?getnamed("a", "b")') must parse the spaced quoted
    arg correctly (the old arg tokenizer swallowed it as a bare token)."""
    bs.fresh()
    bs.send('execute', {'code': 'addrect();'})
    r = bs.send('execute', {'code': '?getnamed("rect", "type")'})
    assert r['result']['status'] == 'ok'
    assert r['result']['result'] == 'Rectangle'


def test_execute_file(bs, tmp_path):
    bs.fresh()
    p = tmp_path / 's.lsf'
    p.write_text('addrect();', encoding='utf-8')
    r = bs.send('execute_file', {'path': str(p)})
    assert r['result']['status'] == 'ok'
    r = bs.send('execute_file', {'path': str(tmp_path / 'missing.lsf')})
    assert 'error' in r


# ---------------------------------------------------------------------------
# Server-side: reference_lookup (all modes)
# ---------------------------------------------------------------------------

def test_ref_list_only():
    r = srv._handle_lumapi_ref({'list_only': True})
    res = r['result']
    assert res['function_count'] == 32
    assert 'getresult' in res['functions']


def test_ref_by_name():
    r = srv._handle_lumapi_ref({'name': 'getresult'})
    res = r['result']
    assert res['function'] == 'getresult'
    assert 'signature' in res['entry']


def test_ref_unknown_name():
    r = srv._handle_lumapi_ref({'name': 'definitely_not_real'})
    assert 'error' in r['result']


def test_ref_by_category():
    r = srv._handle_lumapi_ref({'category': 'result'})
    res = r['result']
    assert res['category'] == 'result'
    assert res['count'] >= 1
    assert 'getresult' in res['functions']


def test_ref_no_args():
    r = srv._handle_lumapi_ref({})
    assert 'error' in r['result']


def test_ref_via_call_tool():
    r = asyncio.run(srv.call_tool('reference_lookup', {'name': 'getresult'}))
    assert r['result']['function'] == 'getresult'


# ---------------------------------------------------------------------------
# Server-side: call_tool parameter defaults + warnings wiring
# ---------------------------------------------------------------------------

class _Recorder(object):
    def __init__(self):
        self.calls = []

    def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        return {}

    def is_dead(self):
        return False


def _patch_bridge(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(srv, '_bridge', rec)
    monkeypatch.setattr(srv, '_ensure_bridge', lambda: None)
    return rec


def test_call_tool_param_defaults(monkeypatch):
    rec = _patch_bridge(monkeypatch)

    asyncio.run(srv.call_tool('model_script', {'name': '::model'}))
    assert rec.calls[-1][1]['action'] == 'get'
    assert rec.calls[-1][1]['script_type'] == ''

    asyncio.run(srv.call_tool('sweep_add', {'name': 'sweep1'}))
    assert rec.calls[-1][1]['type'] == 0

    asyncio.run(srv.call_tool('result_get', {'monitor': 'DFT', 'fields': ['T']}))
    p = rec.calls[-1][1]
    assert p['cap'] == 2000 and p['data'] == '' and p['fields'] == ['T']

    asyncio.run(srv.call_tool('result_list', {}))
    assert rec.calls[-1][1]['data'] == ''

    asyncio.run(srv.call_tool('result_save', {'monitor': 'DFT', 'data': 'T'}))
    assert rec.calls[-1][1]['output'] == ''


def test_call_tool_unknown_tool(monkeypatch):
    _patch_bridge(monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(srv.call_tool('no_such_tool', {}))


def test_call_tool_script_warnings(monkeypatch):
    rec = _patch_bridge(monkeypatch)
    r = asyncio.run(srv.call_tool('execute', {'code': 'totallyUnknownFunc("x");'}))
    assert 'warnings' in r['result']
    assert 'totallyUnknownFunc' in r['result']['warnings'][0]

    rec.calls.clear()
    r = asyncio.run(srv.call_tool('execute', {'code': 'addrect();'}))
    assert 'warnings' not in r['result']


# ---------------------------------------------------------------------------
# Server-side: anti-hallucination script scanner
# ---------------------------------------------------------------------------

def test_scan_recognized():
    assert srv._scan_script_for_unknown_funcs('getresult("DFT", "T")') == []
    assert srv._scan_script_for_unknown_funcs(
        'addrect(); setnamed("r1", "x span", 1e-6)') == []


def test_scan_ignores_comments():
    assert srv._scan_script_for_unknown_funcs('% myfunc("x");') == []


def test_scan_ignores_string_literals_with_parens():
    assert srv._scan_script_for_unknown_funcs(
        'setnamed("r1", "x span (um)", 1)') == []


def test_scan_flags_unknown():
    warns = srv._scan_script_for_unknown_funcs('mycustomfunc("a");')
    assert len(warns) == 1 and 'mycustomfunc' in warns[0]


def test_scan_empty():
    assert srv._scan_script_for_unknown_funcs('') == []
    assert srv._scan_script_for_unknown_funcs(None) == []


# ---------------------------------------------------------------------------
# Tool schema validity + annotations
# ---------------------------------------------------------------------------

def test_tools_schema_valid():
    assert len(srv.TOOLS) == 30 - len(srv._HIDDEN_TOOLS)
    for t in srv.TOOLS:
        assert t.name, 'tool without name'
        assert t.description, t.name
        schema = t.inputSchema
        assert schema['type'] == 'object', t.name
        props = schema.get('properties', {})
        for req in schema.get('required', []):
            assert req in props, '%s required=%s not in properties' % (t.name, req)


def test_tool_annotations_split():
    ro = {t.name for t in srv.TOOLS if t.annotations and t.annotations.readOnlyHint}
    ds = {t.name for t in srv.TOOLS if t.annotations and t.annotations.destructiveHint}
    assert ro == srv._READ_ONLY_TOOLS
    assert ds == srv._DESTRUCTIVE_TOOLS
    assert not (ro & ds)


def test_dispatch_structural_consistency():
    """30 tools = 29 bridge-dispatched + 1 server-only (no hidden tools)."""
    assert len(srv.TOOLS) == (len(srv._DISPATCH) + len(srv._SERVER_ONLY_TOOLS)
                              - len(srv._HIDDEN_TOOLS))
    assert srv._SERVER_ONLY_TOOLS == frozenset({'reference_lookup'})
    assert srv._HIDDEN_TOOLS == frozenset()
    assert len(srv.TOOLS) == 30


# ---------------------------------------------------------------------------
# BridgeClient FDTD_MCP_CALL_TIMEOUT path
# ---------------------------------------------------------------------------

def test_bridge_call_timeout(monkeypatch):
    monkeypatch.setattr(srv, 'LUMERICAL_PYTHON', sys.executable)
    monkeypatch.setattr(srv, 'BRIDGE_SCRIPT', _FAKE)
    monkeypatch.setattr(srv, '_CALL_TIMEOUT', 0.5)
    srv._bridge_started = False
    b = srv.BridgeClient()
    b.start()
    try:
        t0 = time.time()
        with pytest.raises(RuntimeError, match='timed out'):
            b.call('sleep', {'duration': 2})
        assert time.time() - t0 < 2.0
        # On timeout the wedged bridge is killed (dead) so the next call
        # auto-restarts a fresh one instead of queueing behind a hung engine.
        assert b.is_dead()
        with pytest.raises(RuntimeError, match='not running'):
            b.call('anything', {})
    finally:
        b.stop()


def test_bridge_call_timeout_then_ensure_recovers(monkeypatch):
    monkeypatch.setattr(srv, 'LUMERICAL_PYTHON', sys.executable)
    monkeypatch.setattr(srv, 'BRIDGE_SCRIPT', _FAKE)
    monkeypatch.setattr(srv, '_CALL_TIMEOUT', 0.5)
    srv._bridge_started = False
    srv._ensure_bridge()
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            srv._bridge.call('sleep', {'duration': 2})
        assert srv._bridge.is_dead()
        # _ensure_bridge respawns a fresh live bridge on the next call.
        srv._ensure_bridge()
        r = srv._bridge.call('anything', {'ok': 1})
        assert r == {'echo': {'ok': 1}}
    finally:
        srv._bridge.stop()
