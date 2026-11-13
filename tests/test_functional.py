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
    assert s.ready.get('ready') is True
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


def test_model_add_v202_objects(bs):
    """Port/mode-expansion/container/custom/planar-solid/STACK/layer-builder."""
    bs.fresh()
    cases = [
        ('port', 'Port'),
        ('mode_expansion', 'Mode Expansion Monitor'),
        ('container_group', 'Container Group'),
        ('custom', 'Custom'),
        ('planar_solid', 'Planar Solid'),
        ('stack', 'STACK'),
        ('layer_builder', 'Layer Builder'),
    ]
    for obj_type, expected in cases:
        name = 'x_' + obj_type
        r = bs.send('model_add', {'type': obj_type, 'name': name})
        assert r['result']['status'] == 'ok', r
        r = bs.send('model_get', {'name': name})
        assert r['result']['type'] == expected, r


def test_attribute_add(bs):
    bs.fresh()
    r = bs.send('attribute_add', {'kind': 'lc orientation'})
    assert r['result']['status'] == 'ok'
    assert r['result']['name'] == 'LC attribute'
    assert r['result']['type']

    r = bs.send('attribute_add', {'kind': 'temperature', 'name': 'temp_map'})
    assert r['result']['name'] == 'temp_map'
    r = bs.send('model_get', {'name': 'temp_map'})
    assert r['result']['type'] == 'Temperature'

    r = bs.send('attribute_add', {'kind': 'bogus'})
    assert 'error' in r and 'Unknown grid attribute' in r['error']['message']


def test_import_add_kinds(bs):
    bs.fresh()
    r = bs.send('import_add', {'path': 'chip.gds', 'cell': 'TOP', 'layer': 1})
    assert r['result']['status'] == 'ok'
    assert r['result']['count'] == 1
    assert r['result']['created']

    r = bs.send('import_add', {'path': 'chip.gds', 'cell': 'TOP', 'layer': 1,
                               'zmin': -1e-7, 'zmax': 0})
    assert 'error' in r and 'material' in r['error']['message']

    # STL adds structure(s) directly.
    r = bs.send('import_add', {'kind': 'stl', 'path': 'part.stl'})
    assert r['result']['status'] == 'ok'
    assert r['result']['created']

    # surface/binary/nk create an import primitive and fill it.
    for kind in ('surface', 'binary', 'nk'):
        r = bs.send('import_add', {'kind': kind, 'path': kind + '.dat',
                                   'file_units': 'microns'})
        assert r['result']['status'] == 'ok', r
        assert r['result']['name']
        q = bs.send('model_get', {'name': r['result']['name']})
        assert q['result']['type'] == 'Import'

    r = bs.send('import_add', {'kind': 'surface', 'path': 'x.txt',
                               'cell': 'TOP'})
    assert 'error' in r and 'cell' in r['error']['message']

    r = bs.send('import_add', {'kind': 'step', 'path': 'part.step'})
    assert 'error' in r and 'stepimport' in r['error']['message']

    r = bs.send('import_add', {'kind': 'image', 'path': 'pic.png'})
    assert 'error' in r and 'importimage' in r['error']['message']


def test_model_get_include_all_properties(bs):
    bs.fresh()
    bs.send('model_add', {'type': 'rectangle', 'name': 'all_props_rect'})
    r = bs.send('model_get', {'name': 'all_props_rect'})
    assert 'override color opacity from material database' not in r['result']

    r = bs.send('model_get', {'name': 'all_props_rect',
                              'include_all_properties': True})
    assert 'override color opacity from material database' in r['result']
    assert r['result']['all_properties_count'] > 0


def test_result_list_marks_orphan_results(bs):
    bs.fresh()
    bs.send('model_add', {'type': 'dft_monitor', 'name': 'm_orphan'})
    bs.send('model_info', {})          # records the name in _seen_objects
    bs.send('run', {})
    bs.send('model_delete', {'name': 'm_orphan'})

    r = bs.send('result_list', {'all': True})
    orphans = [m for m in r['result']['monitors'] if m.get('orphan')]
    assert any(m['monitor'] == 'm_orphan' for m in orphans)
    assert r['result']['orphan_count'] == 1


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


def test_stack_report_flags_overlap_gap_and_pml(bs):
    bs.fresh()
    bs.send('model_add', {'type': 'rectangle', 'name': 'l1',
                          'properties': {'z': -300e-9, 'z span': 400e-9,
                                         'x span': 2e-6, 'y span': 2e-6}})
    bs.send('model_add', {'type': 'rectangle', 'name': 'l2',
                          'properties': {'z': -50e-9, 'z span': 200e-9}})
    bs.send('model_add', {'type': 'rectangle', 'name': 'l3',
                          'properties': {'z': 400e-9, 'z span': 100e-9}})

    r = bs.send('stack_report', {})
    res = r['result']
    assert [l['name'] for l in res['layers']] == ['l1', 'l2', 'l3']
    assert res['layers'][0]['thickness'] == pytest.approx(400e-9)
    assert res['overlaps'] and res['overlaps'][0]['below'] == 'l1'
    assert res['gaps'] and res['gaps'][0]['above'] == 'l3'
    assert any('transverse PML' in w for w in res['warnings'])

    r = bs.send('stack_report', {'include_disabled': True})
    assert r['result']['layer_count'] == 3


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
                                 'content': 'adduserprop("gap", 2, 200e-9);'})
    assert r['result']['status'] == 'ok'
    assert r['result']['script_type'] == 'setup script'
    r = bs.send('model_script', {'name': '::model', 'action': 'get'})
    assert 'adduserprop("gap"' in r['result']['setup_script']

    # Legacy 'type' key is still accepted.
    r = bs.send('model_script', {'name': '::model', 'action': 'set',
                                 'type': 'setup',
                                 'content': 'adduserprop("gap2", 2, 1e-9);'})
    assert r['result']['status'] == 'ok'


def test_model_set_creates_model_variable(bs):
    """The v202 way to add a model parameter: adduserprop on the selected
    model root. Regression guard for the addvar-based path, which silently
    failed on the real engine while the fake kept it green."""
    bs.fresh()
    r = bs.send('model_set', {'name': '::model', 'properties': {'gap': 200e-9}})
    assert r['result']['status'] == 'ok'
    assert r['result']['properties_set'] == 1
    assert 'properties_failed' not in r['result']

    r = bs.send('model_get', {'name': '::model'})
    assert r['result']['variables']['gap'] == 200e-9

    # ... and the value of an existing variable can be updated afterwards
    r = bs.send('model_set', {'name': '::model', 'properties': {'gap': 300e-9}})
    assert r['result']['properties_set'] == 1
    r = bs.send('model_get', {'name': '::model'})
    assert r['result']['variables']['gap'] == 300e-9


def test_model_set_types_and_groups(bs):
    bs.fresh()
    # text/number inference + explicit type code
    r = bs.send('model_set', {'name': '::model',
                              'properties': {'nsub': 3, 'tag': 'demo'},
                              'property_types': {'nsub': 'number'}})
    assert r['result']['status'] == 'ok', r

    # analysis group -> addanalysisprop, structure group -> adduserprop
    bs.send('model_add', {'type': 'analysis_group', 'name': 'ag1'})
    r = bs.send('model_set', {'name': 'ag1', 'properties': {'period': 1e-6}})
    assert r['result']['status'] == 'ok' and r['result']['properties_set'] == 1
    r = bs.send('model_get', {'name': 'ag1'})
    assert r['result']['period'] == 1e-6

    bs.send('model_add', {'type': 'structure_group', 'name': 'sg1'})
    r = bs.send('model_set', {'name': 'sg1', 'properties': {'thickness': 1e-7}})
    assert r['result']['status'] == 'ok' and r['result']['properties_set'] == 1
    r = bs.send('model_get', {'name': 'sg1'})
    assert r['result']['thickness'] == 1e-7


def test_model_set_failure_is_loud(bs):
    """A property that cannot be created must not come back as status=ok."""
    bs.fresh()
    r = bs.send('model_add', {'type': 'rectangle', 'name': 'r1'})
    r = bs.send('model_set', {'name': 'r1',
                              'properties': {'x span': 3e-6, 'no such prop': 1}})
    assert r['result']['status'] == 'partial'
    assert r['result']['properties_set'] == 1
    assert r['result']['properties_failed'] == ['no such prop']
    assert 'no such prop' in r['result']['failures']

    r = bs.send('model_set', {'name': 'r1', 'properties': {'no such prop': 1}})
    assert r['result']['status'] == 'error'
    assert r['result']['properties_set'] == 0


def test_dead_v202_commands_are_rejected(bs):
    """addvar/setvar are gone from v202; the fake must reject them so tests
    cannot certify the removed API again."""
    bs.fresh()
    r = bs.send('execute', {'code': 'addvar("gap", 200e-9);'})
    assert 'error' in r and 'not a valid function' in r['error']['message']
    r = bs.send('execute', {'code': 'setvar("gap", 1);'})
    assert 'error' in r and 'not a valid function' in r['error']['message']


# ---------------------------------------------------------------------------
# material module (5 tools)
# ---------------------------------------------------------------------------

def test_material_index_anisotropic_and_constant(bs):
    bs.fresh()
    name = bs.send('material_add', {})['result']['name']
    data = [[3e14, [4.0, 0.0], [9.0, 0.0], [16.0, 0.0]],
            [4e14, [4.0, 0.0], [9.0, 0.0], [16.0, 0.0]]]
    r = bs.send('material_set', {'name': name, 'property': 'sampled 3d data',
                                 'value': data})
    assert r['result']['status'] == 'ok'

    r = bs.send('material_index', {'name': name, 'frequencies': [3e14],
                                   'component': 'yy'})
    assert r['result']['n'] == pytest.approx([3.0])
    assert r['result']['k'] == pytest.approx([0.0])

    r = bs.send('material_index', {'name': name, 'frequencies': [3e14],
                                   'component': 'zz'})
    assert r['result']['n'] == pytest.approx([4.0])

    # out of the tabulated range -> null, not a clamp
    r = bs.send('material_index', {'name': name, 'frequencies': [1e14]})
    assert r['result']['n'] == [None]

    # wavelength input
    r = bs.send('material_index', {'name': name,
                                   'wavelengths': [299792458.0 / 3e14]})
    assert r['result']['n'] == pytest.approx([2.0])

    r = bs.send('material_index', {'name': name,
                                   'wavelengths': [1e-6], 'frequencies': [3e14]})
    assert 'error' in r and 'not both' in r['error']['message']

    # constant material
    r = bs.send('material_add', {'type': 'Dielectric'})
    dname = r['result']['name']
    r = bs.send('material_index', {'name': dname, 'wavelengths': [1e-6]})
    assert r['result']['n'] == pytest.approx([1.5])
    assert r['result']['k'] == pytest.approx([0.0])


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
    data = [[3e14, [4.0, 0.1], [4.0, 0.1], [4.0, 0.1]],
            [4e14, [4.1, 0.1], [4.1, 0.1], [4.1, 0.1]]]
    r = bs.send('material_set', {'name': name, 'property': 'sampled 3d data',
                                 'value': data})
    assert r['result']['status'] == 'ok'
    assert r['result']['columns'] == 4
    assert r['result']['complex'] is True

    r = bs.send('material_get', {'name': name, 'property': 'sampled 3d data'})
    out = r['result']
    assert out['shape'] == [2, 4]
    assert out['data'][0][0] == [3e14, 0.0]
    assert out['data'][0][1] == [4.0, 0.1]

    # material_get's {"data": ...} output can be fed straight back.
    r = bs.send('material_set', {'name': name, 'property': 'sampled 3d data',
                                 'value': out})
    assert r['result']['status'] == 'ok'

    # v202 rejects 3 columns; the tool catches it before the engine call.
    r = bs.send('material_set', {'name': name, 'property': 'sampled 3d data',
                                 'value': [[3e14, 1.5, 0], [4e14, 1.5, 0]]})
    assert 'error' in r and '2 columns' in r['error']['message']


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
    props = r['result']['properties']
    assert 'type' in props and 'number of points' in props
    assert 'solver' not in props            # v202 has no sweep solver
    assert r['result']['sweep_type'] == 0

    r = bs.send('sweep_run', {'name': 'gap_sweep'})
    assert r['result']['status'] == 'completed'
    assert 'diagnostics' in r['result']

    r = bs.send('sweep_get', {'name': 'gap_sweep'})
    assert r['result']['has_results'] is True
    assert r['result']['exists'] is True  # exists present on success too

    r = bs.send('sweep_result', {'name': 'gap_sweep'})
    assert r['result']['T']['T'] is not None
    # Regression: the optional result= param is honored (filtered result).
    r = bs.send('sweep_result', {'name': 'gap_sweep', 'result': 'T'})
    assert set(r['result'].keys()) == {'T'}

    r = bs.send('sweep_set', {'name': 'gap_sweep', 'properties': {'type': 'Ranges'}})
    assert r['result']['status'] == 'ok'
    assert r['result']['properties_set'] == 1

    # v202 setsweep returns 0.0 for unknown properties instead of raising.
    # That must surface as a failure, not a silent success.
    r = bs.send('sweep_set', {'name': 'gap_sweep', 'properties': {'sweep type': 1}})
    assert r['result']['status'] == 'error'
    assert 'sweep type' in r['result']['failures']
    assert 'Editable properties' in r['result']['failures']['sweep type']

    r = bs.send('sweep_set', {'name': 'gap_sweep', 'properties': {'solver': 'FDTD'}})
    assert r['result']['status'] == 'error'
    assert 'solver' in r['result']['failures']

    r = bs.send('sweep_delete', {'name': 'gap_sweep'})
    assert r['result']['status'] == 'ok'
    r = bs.send('sweep_get', {'name': 'gap_sweep'})
    assert r['result']['exists'] is False


def test_sweep_add_auto_names_and_sparam_warning(bs):
    bs.fresh()
    r1 = bs.send('sweep_add', {'name': 's1'})
    r2 = bs.send('sweep_add', {'name': 's2'})
    assert r1['result']['name'] == 's1'
    assert r2['result']['name'] == 's2'
    assert r2['result']['type'] == 0

    r = bs.send('sweep_get', {'name': 's2'})
    assert r['result']['exists'] is True
    assert r['result']['sweep_type'] == 0

    # S-parameter sweep without ports must be created but flagged loudly.
    r = bs.send('sweep_add', {'name': 's3', 'type': 3})
    assert r['result']['status'] == 'ok'
    assert r['result']['warnings']
    assert 'port' in r['result']['warnings'][0].lower()


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
    assert 'diagnostics' in r['result']

    r = bs.send('result_has', {'monitor': 'DFT'})
    assert r['result']['exists'] is True

    # Scoped names fall back to the short name (v202 accepts both in different
    # calls; the tool now reports which one actually worked).
    r = bs.send('result_get', {'monitor': '::model::DFT', 'data': 'T',
                               'fields': ['T']})
    assert r['result']['status'] == 'ok'
    assert r['result']['monitor'] == 'DFT'

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
    # Count against the bundled file instead of a frozen number, so adding a
    # verified entry does not fail the suite.
    import fdtd_mcp.cheatsheet  # noqa: F401  (package marker)
    import json as _json
    from pathlib import Path as _Path
    path = _Path(srv.__file__).resolve().parent / 'cheatsheet' / 'lumapi_ref.json'
    with open(str(path), 'r', encoding='utf-8') as f:
        entries = _json.load(f)
    assert res['function_count'] == len([k for k in entries if k != 'meta'])
    assert 'getresult' in res['functions']
    assert res['function_count'] > 0


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

    def take_restart_notice(self):
        return None

    def unsolicited(self):
        return []


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


def test_call_tool_rejects_unknown_or_missing_params(monkeypatch):
    """Hallucinated/typo parameters must fail before the bridge is touched."""
    rec = _patch_bridge(monkeypatch)

    with pytest.raises(ValueError, match='solver'):
        asyncio.run(srv.call_tool('sweep_add',
                                  {'name': 's', 'solver': 'FDTD'}))
    with pytest.raises(ValueError, match='unknown key'):
        asyncio.run(srv.call_tool('sweep_add', {
            'name': 's',
            'parameters': [{'name': 'x', 'solver': 'FDTD'}]}))
    with pytest.raises(ValueError, match='Unknown parameter'):
        asyncio.run(srv.call_tool('sweep_set', {
            'name': 's', 'properties': {'type': 'Ranges'}, 'solver': 'FDTD'}))
    with pytest.raises(ValueError, match='Missing required'):
        asyncio.run(srv.call_tool('sweep_add', {}))
    assert not rec.calls       # nothing reached the bridge


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
    # '#' is the LSF comment character on v202.
    assert srv._scan_script_for_unknown_funcs('# myfunc("x");') == []


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
    assert len(srv.TOOLS) == len(srv.SPECS) - len(srv._HIDDEN_TOOLS)
    for t in srv.TOOLS:
        assert t.name, 'tool without name'
        assert t.description, t.name
        schema = t.inputSchema
        assert schema['type'] == 'object', t.name
        props = schema.get('properties', {})
        for req in schema.get('required', []):
            assert req in props, '%s required=%s not in properties' % (t.name, req)


def test_tool_annotations_split():
    """Annotations come from the spec table, not from a second hand-written set."""
    ro = {t.name for t in srv.TOOLS if t.annotations and t.annotations.readOnlyHint}
    ds = {t.name for t in srv.TOOLS if t.annotations and t.annotations.destructiveHint}
    assert ro == {s.name for s in srv.SPECS if s.read_only and not s.hidden}
    assert ds == {s.name for s in srv.SPECS if s.destructive and not s.hidden}
    assert not (ro & ds)


def test_dispatch_structural_consistency():
    """Every advertised tool is either bridge-dispatched or explicitly server-only."""
    assert len(srv.TOOLS) == (len(srv._DISPATCH) + len(srv._SERVER_ONLY_TOOLS)
                              - len(srv._HIDDEN_TOOLS))
    assert srv._SERVER_ONLY_TOOLS == frozenset(
        {'reference_lookup', 'engine_cleanup', 'library_list'})
    assert srv._HIDDEN_TOOLS == frozenset()
    assert len(srv.TOOLS) == len(srv.SPECS)


# ---------------------------------------------------------------------------
# Regression coverage for the fixes of this review pass
# ---------------------------------------------------------------------------

def test_engine_status_reports_code_identity(bs):
    bs.fresh()
    r = bs.send('engine_status', {})
    st = r['result']
    assert st['status'] == 'ok'
    assert st['project_open'] is True
    assert st['bridge_code_id']
    assert st['disk_code_id']
    # The test bridge is running the code that is on disk right now.
    assert st['stale_code'] is False
    assert 'note' not in st
    assert st['recent_calls'], 'the call log should include earlier calls'
    # The log holds completed calls: this one is appended once it returns.
    assert 'session_new' in [c['method'] for c in st['recent_calls']]
    assert st['fdtd_summary']['dimension'] == '3D'
    assert st['mode'] == 'layout'
    assert st['layout_mode'] is True


def test_engine_status_mode_after_run(bs):
    bs.fresh()
    r = bs.send('run', {})
    assert r['result']['mode'] == 'analysis'
    r = bs.send('engine_status', {})
    assert r['result']['mode'] == 'analysis'
    assert r['result']['layout_mode'] is False


def test_engine_status_works_without_project(bs):
    bs.send('session_close', {})
    r = bs.send('engine_status', {})
    assert r['result']['project_open'] is False
    assert r['result']['status'] == 'ok'
    bs.fresh()


def test_execute_query_accepts_zero_arg_and_bare_args(bs):
    bs.fresh()
    # Bare (unquoted) argument stays a single string instead of being split.
    r = bs.send('execute', {'code': '?getnamed("FDTD", dimension)'})
    assert r['result']['result'] == '3D'
    assert r['result']['query'] == 'getnamed'
    # A bare name still resolves through getv/getnamed.
    r = bs.send('execute', {'code': '?type'})
    assert r['result']['result'] == 'ObjectModelRoot'
    assert r['result']['query'] in ('getv', 'getnamed')


def test_execute_rejects_script_set_with_space(bs):
    """`set ("setup script", ...)` used to slip past the guard regex."""
    bs.fresh()
    r = bs.send('execute', {'code': 'set ("setup script", "x = 1;");'})
    assert 'error' in r
    assert 'model_script' in r['error']['message']


def test_execute_query_explains_dead_commands(bs):
    bs.fresh()
    r = bs.send('execute', {'code': '?addvar("gap", 1)'})
    assert 'error' in r
    assert 'does not exist' in r['error']['message']
    assert 'reference_lookup' in r['error']['message']


def test_execute_autosave_ignores_commented_run(bs):
    """A `# run ...` comment must not trigger the pre-solve autosave."""
    bs.fresh()
    r = bs.send('execute', {'code': '# run this later\naddrect();'})
    assert r['result']['status'] == 'ok'
    st = bs.send('engine_status', {})['result']
    assert st['autosave_path'] is None

    # ... whereas a real run() does autosave the project first.
    r = bs.send('execute', {'code': 'run();'})
    assert r['result']['status'] == 'ok'
    st = bs.send('engine_status', {})['result']
    assert st['autosave_path']


def test_execute_autosave_covers_resume(bs):
    """resume() must autosave first, or a hidden engine pops a Save dialog."""
    bs.fresh()
    r = bs.send('execute', {'code': '# resume later\naddrect();'})
    assert r['result']['status'] == 'ok'
    assert bs.send('engine_status', {})['result']['autosave_path'] is None

    r = bs.send('execute', {'code': 'resume();'})
    assert r['result']['status'] == 'ok'
    assert bs.send('engine_status', {})['result']['autosave_path']


def test_percent_lines_are_flagged_and_rejected(bs):
    """`%` is not an LSF comment: warn at scan time, fail at run time."""
    bs.fresh()
    r = bs.send('execute', {'code': '% not a comment\naddrect();'})
    assert 'error' in r, r
    assert 'Failed to evaluate code' in r['error']['message']

    # The advisory scanner catches it before the engine does.
    warns = srv._percent_comment_warning('% oops\naddrect();')
    assert warns and 'line(s) 1' in warns[0] and '#' in warns[0]
    assert srv._percent_comment_warning('# fine\naddrect();') == []


def test_result_list_all_reports_monitors_with_results(bs):
    bs.fresh()
    r = bs.send('result_list', {'all': True})
    assert r['result']['monitor_count'] == 0

    bs.send('model_add', {'type': 'power_monitor', 'name': 'm1'})
    bs.send('run', {})
    r = bs.send('result_list', {'all': True})
    names = [m['monitor'] for m in r['result']['monitors']]
    assert 'm1' in names


def test_sweep_add_flags_unresolved_parameter_path(bs):
    bs.fresh()
    r = bs.send('sweep_add', {'name': 's1', 'type': 0, 'parameters': [
        {'name': 'typo', 'parameter': '::model::nope::x span',
         'start': 1e-6, 'stop': 2e-6}]})
    assert r['result']['parameter_warnings'], r['result']
    assert 'unresolved' in r['result']['parameter_warnings'][0]

    bs.send('model_add', {'type': 'rectangle', 'name': 'r1'})
    r = bs.send('sweep_add', {'name': 's2', 'type': 0, 'parameters': [
        {'name': 'w', 'parameter': '::model::r1::x span',
         'start': 1e-6, 'stop': 2e-6}]})
    assert not r['result'].get('parameter_warnings'), r['result']


def test_model_add_reports_property_failures(bs):
    """model_add used to swallow every property error and still say ok."""
    bs.fresh()
    r = bs.send('model_add', {'type': 'rectangle', 'name': 'r1',
                              'properties': {'x span': 2e-6, 'nope': 1}})
    assert r['result']['status'] == 'ok'
    assert r['result']['properties_failed'] == ['nope']
    assert 'nope' in r['result']['failures']


def test_session_new_reports_bad_config_key(bs):
    bs.fresh()
    r = bs.send('session_new', {'dimension': '3D', 'mesh accuracy': 4})
    assert r['result']['status'] == 'ok'
    assert r['result']['config']['mesh accuracy'] == 4
    assert r['result']['session_generation'] >= 1


def test_names_with_quotes_are_escaped_not_injected(bs):
    """A quote in an object name must not break the generated LSF."""
    bs.fresh()
    weird = 'rect "quoted"'
    r = bs.send('model_add', {'type': 'rectangle', 'name': weird})
    assert r['result']['name'] == weird
    r = bs.send('model_get', {'name': weird})
    assert r['result']['type'] == 'Rectangle'
    r = bs.send('model_delete', {'name': weird})
    assert r['result']['status'] == 'ok'


def test_model_set_reports_inactive_property_distinctly(bs):
    """A property that exists but is inactive must not read as 'no such property'."""
    bs.fresh()
    bs.send('model_add', {'type': 'rectangle', 'name': 'r1'})
    r = bs.send('model_set', {'name': 'r1', 'properties': {'mesh order': 3}})
    assert r['result']['status'] == 'error'
    reason = r['result']['failures']['mesh order']
    assert 'inactive' in reason and 'override' in reason

    # Switching the override on makes it writable (dict order is preserved).
    r = bs.send('model_set', {'name': 'r1', 'properties': {
        'override mesh order from material database': 1, 'mesh order': 3}})
    assert r['result']['properties_set'] == 2
    assert bs.send('model_get', {'name': 'r1'})['result']['mesh order'] == 3


def test_model_parameters_and_results_are_analysis_entries(bs):
    """::model parameters must land where the GUI shows them.

    The model's "Edit analysis group" dialog lists ANALYSIS properties in its
    Analysis > Variables table and addanalysisresult names in its Results table;
    a user property created with adduserprop does not appear there.
    """
    bs.fresh()
    r = bs.send('model_set', {'name': '::model',
                              'properties': {'gap': 2e-7, 'nsub': 3},
                              'property_types': {'gap': 'length'},
                              'results': ['T_spectrum', 'R_spectrum']})
    res = r['result']
    assert res['status'] == 'ok', res
    assert res['properties_set'] == 2
    assert res['results_added'] == ['T_spectrum', 'R_spectrum']

    q = bs.send('execute', {'code': '?queryanalysisprop("::model")'})['result']['result']
    assert q['name'] == ['gap', 'nsub']
    assert q['type'] == ['2', '0']
    q = bs.send('execute', {'code': '?queryanalysisresult("::model")'})['result']['result']
    assert q == ['T_spectrum', 'R_spectrum']


def test_structure_group_parameters_use_user_props(bs):
    bs.fresh()
    bs.send('model_add', {'type': 'structure_group', 'name': 'g1'})
    r = bs.send('model_set', {'name': 'g1', 'properties': {'period': 2e-7}})
    assert r['result']['properties_set'] == 1
    bs.send('execute', {'code': 'select("g1");'})
    q = bs.send('execute', {'code': '?queryuserprop()'})['result']['result']
    assert q['name'] == ['period']


def test_declaring_an_existing_name_creates_a_suffixed_clone(bs):
    """v202: addanalysisprop/adduserprop are not idempotent (documented pitfall)."""
    bs.fresh()
    bs.send('execute', {'code': 'select("::model"); addanalysisprop("dup", 2, 1e-7);'})
    bs.send('execute', {'code': 'select("::model"); addanalysisprop("dup", 2, 2e-7);'})
    q = bs.send('execute', {'code': '?queryanalysisprop("::model")'})['result']['result']
    assert q['name'] == ['dup', 'dup2']


def test_analysis_result_rejects_value_arguments(bs):
    """addanalysisresult takes ONE argument on the model root on v202."""
    bs.fresh()
    r = bs.send('execute', {'code': 'select("::model"); addanalysisresult("x", 3);'})
    assert 'error' in r


# ---------------------------------------------------------------------------
# BridgeClient FDTD_MCP_CALL_TIMEOUT path
# ---------------------------------------------------------------------------
def test_bridge_call_timeout(monkeypatch):
    monkeypatch.setattr(srv, 'BRIDGE_ARGS', [sys.executable, _FAKE])
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
    monkeypatch.setattr(srv, 'BRIDGE_ARGS', [sys.executable, _FAKE])
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


# ---------------------------------------------------------------------------
# Engine-process hygiene (orphaned fdtd-solutions.exe)
# ---------------------------------------------------------------------------
def test_bridge_closes_the_session_on_eof():
    """EOF on stdin == the MCP server was recycled while a project was open.

    The bridge used to exit(0) straight out of its stdin loop, leaving the
    engine process running forever (the production leak: 7 orphaned engines,
    every one with a dead bridge as its parent).
    """
    proc = subprocess.Popen(
        [sys.executable, _RUNNER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        assert json.loads(proc.stdout.readline())['ready'] is True
        proc.stdin.write(json.dumps(
            {'id': 1, 'method': 'session_new', 'params': {}}) + '\n')
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())['result']['status'] == 'ok'
        proc.stdin.close()                      # server gone
        proc.wait(timeout=30)
        err = proc.stderr.read()
        assert 'close() called' in err, err
    finally:
        if proc.poll() is None:
            proc.kill()


def test_engine_cleanup_sweeps_orphans_and_never_touches_foreign(monkeypatch):
    table = [
        {'pid': 1, 'ppid': 0, 'name': 'node.exe'},
        {'pid': 100, 'ppid': 1, 'name': 'python.exe'},
        {'pid': 300, 'ppid': 999, 'name': 'fdtd-solutions.exe'},   # orphan
        {'pid': 400, 'ppid': 1, 'name': 'fdtd-solutions.exe'},     # foreign
    ]
    killed = []
    monkeypatch.setattr(srv.proctree, 'process_table', lambda: table)
    monkeypatch.setattr(srv.proctree, 'kill_pid',
                        lambda pid, tree=True, wait=8.0: (killed.append(pid),
                                                          (True, 'ok'))[1])
    monkeypatch.setattr(srv, '_ORPHAN_SWEEP', True)
    monkeypatch.setattr(srv, '_orphan_sweep_report', None)

    dry = srv._handle_engine_cleanup({'dry_run': True})['result']
    assert dry['would_kill'] == [300]
    assert dry['kept_foreign'] == [400]
    assert killed == [], 'dry run must not kill anything'

    out = srv._handle_engine_cleanup({})['result']
    assert killed == [300]
    assert out['sweep']['counts']['foreign'] == 1
    assert 400 not in [e['pid'] for e in out['sweep']['killed']]


def test_server_status_reports_engine_processes(monkeypatch):
    monkeypatch.setattr(srv.proctree, 'process_table', lambda: [
        {'pid': 100, 'ppid': 999, 'name': 'fdtd-solutions.exe'}])
    status = srv._engine_process_status()
    assert status['counts']['orphan'] == 1
    assert status['orphan_pids'] == [100]
    assert 'never killed' in status['notes']['foreign']
