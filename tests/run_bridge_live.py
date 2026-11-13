# -*- coding: utf-8 -*-
"""Live-engine integration test for FdtdBridge (opt-in, needs a real Lumerical).

Unlike the rest of the suite (which runs against tests/fake_engine.py), this
drives the real bridge handlers against a real FDTD engine, so it catches the
whole class of "the fake emulates an API that no longer exists" bugs.

Run with Lumerical's bundled Python (3.6.8):

    "%LUMERICAL_HOME%\\python-3.6.8-embed-amd64\\python.exe" tests/run_bridge_live.py

Exit code 0 = all checks passed. Creates no files outside the temp dir.
"""
from __future__ import print_function
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if not os.environ.get('LUMERICAL_HOME'):
    from fdtd_mcp.discovery import find_lumerical
    os.environ['LUMERICAL_HOME'] = find_lumerical()

import fdtd_mcp.bridge as bridge_mod  # noqa: E402

RESULTS = []


def check(label, cond, detail=''):
    RESULTS.append((label, bool(cond), detail))
    print('%s | %s%s' % ('PASS' if cond else 'FAIL', label,
                         ('  -> ' + str(detail)[:220]) if detail else ''))
    sys.stdout.flush()


def call(b, method, **params):
    """Send one request through the real dispatch path."""
    resp = b.handle({'id': 1, 'method': method, 'params': params})
    if 'error' in resp:
        return {'_error': resp['error']['message']}
    return resp['result']


def main():
    b = bridge_mod.FdtdBridge()
    print('--- live bridge test ---')

    r = call(b, 'session_new', dimension='3D', x_span=2e-6, y_span=2e-6,
             z_span=1e-6, mesh_accuracy=3)
    check('session_new', r.get('status') == 'ok', r)

    r = call(b, 'model_add', type='rectangle', name='sub',
             properties={'x span': 1e-6, 'y span': 1e-6, 'z span': 2e-7})
    check('model_add rectangle', r.get('name') == 'sub', r)

    # --- the reported bug: add a parameter (model variable) on ::model ------
    r = call(b, 'model_set', name='::model', properties={'gap': 200e-9})
    check('model_set ::model {gap} reports no failures',
          not r.get('properties_failed'), r)
    check('model_set ::model reports 1 property set',
          r.get('properties_set') == 1, r)

    r = call(b, 'execute', code='?getnamed("::model","gap")')
    check('::model::gap readable after model_set',
          r.get('result') == 200e-9, r)

    # a text-typed variable and a plain number
    r = call(b, 'model_set', name='::model',
             properties={'nsub': 3, 'label': 'demo'})
    check('model_set ::model {int, str}', not r.get('properties_failed'), r)

    r = call(b, 'model_info', enabled_only=True)
    mv = r.get('model_variables', {})
    check('model_info lists the new variables',
          mv.get('gap') == 200e-9 and 'nsub' in mv, mv)
    check('model_info still sees objects', r.get('object_count', 0) >= 1, r)

    # --- the variable must be usable as a sweep parameter ------------------
    r = call(b, 'sweep_add', type=0, name='gap_sweep',
             parameters=[{'name': 'gap', 'parameter': '::model::gap',
                          'start': 100e-9, 'stop': 300e-9, 'points': 3}])
    check('sweep_add parameter on ::model::gap', not r.get('parameter_errors'), r)

    r = call(b, 'execute', code='?getsweep("gap_sweep")')
    check('sweep lists the gap parameter',
          'gap' in str(r.get('result', '')), r)

    # --- setting an existing value must keep working -----------------------
    r = call(b, 'model_set', name='::model', properties={'gap': 300e-9})
    r = call(b, 'execute', code='?getnamed("::model","gap")')
    check('existing model variable value updated',
          r.get('result') == 300e-9, r)

    # --- analysis group parameter (the "analysis tab" analogue) ------------
    r = call(b, 'model_add', type='analysis_group', name='ag1')
    check('model_add analysis_group', r.get('name') == 'ag1', r)
    r = call(b, 'model_set', name='ag1', properties={'period': 1e-6})
    check('model_set analysis group new property',
          not r.get('properties_failed'), r)
    r = call(b, 'execute', code='?getnamed("ag1","period")')
    check('analysis group property readable', r.get('result') == 1e-6, r)

    # --- structure group property ------------------------------------------
    r = call(b, 'model_add', type='structure_group', name='sg1')
    r = call(b, 'model_set', name='sg1', properties={'thickness': 1e-7})
    check('model_set structure group new property',
          not r.get('properties_failed'), r)
    r = call(b, 'execute', code='?getnamed("sg1","thickness")')
    check('structure group property readable', r.get('result') == 1e-7, r)

    # --- a property that cannot exist must fail loudly ---------------------
    r = call(b, 'model_set', name='sub', properties={'no such property': 1})
    check('bogus property is reported as failed',
          r.get('properties_failed') == ['no such property'] or
          r.get('status') != 'ok', r)

    # --- scripts still work -------------------------------------------------
    # '#' is the LSF comment character on v202 ('%' makes a script fail).
    r = call(b, 'model_script', name='::model', action='set',
             script_type='analysis', content='# live test\nx = 1;')
    check('model_script set analysis', r.get('status') == 'ok', r)
    r = call(b, 'model_script', name='::model', action='get')
    check('model_script get analysis round-trip',
          'x = 1' in (r.get('analysis_script') or ''), r)

    # --- structure group: script + user properties + runsetup ---------------
    r = call(b, 'model_add', type='structure_group', name='dbr')
    check('model_add structure group', r.get('name') == 'dbr', r)
    r = call(b, 'model_script', name='dbr', action='set', script_type='script',
             content='# build children from a user property\n'
                     'adduserprop("period", 2, 100e-9);\n'
                     'addrect;\n'
                     'set("name", "layer1");\n'
                     'set("z span", period);\n'
                     'set("material", "Si (Silicon) - Palik");\n')
    check('structure group script set', r.get('status') == 'ok', r)
    r = call(b, 'model_set', name='dbr', properties={'period': 200e-9})
    check('structure group user property', not r.get('properties_failed'), r)
    r = call(b, 'execute', code='runsetup;')
    check('runsetup executes the group script', r.get('status') == 'ok', r)
    r = call(b, 'execute', code='?getnamed("::model::dbr::layer1","type")')
    check('group child created by its script', r.get('result') == 'Rectangle', r)

    # --- sweep config is readable, and a fake "solver" fails loudly --------
    r = call(b, 'sweep_get', name='gap_sweep')
    check('sweep_get exposes editable properties',
          'type' in (r.get('properties') or []) and r.get('sweep_type') == 0, r)
    check('sweep_get does not invent solver',
          'solver' not in (r.get('properties') or []), r)
    r = call(b, 'sweep_set', name='gap_sweep', properties={'solver': 'FDTD'})
    check('sweep_set rejects solver loudly',
          r.get('status') == 'error' and 'solver' in (r.get('failures') or {}), r)
    r = call(b, 'sweep_set', name='gap_sweep', properties={'type': 'Ranges'})
    check('sweep_set applies type=Ranges',
          r.get('status') == 'ok' and r.get('properties_set') == 1, r)

    # --- Attributes group --------------------------------------------------
    r = call(b, 'attribute_add', kind='lc orientation')
    check('attribute_add lc orientation', r.get('name') == 'LC attribute', r)
    r = call(b, 'attribute_add', kind='temperature', name='temp_map')
    check('attribute_add temperature rename', r.get('name') == 'temp_map', r)

    # --- v202 object adders the catalogue used to lack ---------------------
    for obj_type in ('port', 'mode_expansion', 'container_group', 'custom',
                     'planar_solid', 'stack', 'layer_builder'):
        name = 'live_' + obj_type
        r = call(b, 'model_add', type=obj_type, name=name)
        check('model_add ' + obj_type, r.get('name') == name, r)

    r = call(b, 'stack_report')
    check('stack_report finds layers',
          r.get('layer_count', 0) >= 1 and bool(r.get('fdtd')), r)

    # --- complex sampled material + anisotropic index ----------------------
    r = call(b, 'material_add', type='Sampled 3D data')
    mname = r.get('name')
    data = [[3e14, [4.0, 0.1], [9.0, 0.1], [16.0, 0.1]],
            [4e14, [4.0, 0.1], [9.0, 0.1], [16.0, 0.1]]]
    r = call(b, 'material_set', name=mname, property='sampled 3d data', value=data)
    check('material_set complex sampled data',
          r.get('status') == 'ok' and r.get('complex') is True, r)
    r = call(b, 'material_index', name=mname, frequencies=[3e14],
             component='yy')
    n = (r.get('n') or [None])[0]
    check('material_index yy component', n is not None and abs(n - 3.0) < 0.01, r)

    try:
        call(b, 'session_close')
    except Exception:
        pass

    failed = [lbl for lbl, ok, _ in RESULTS if not ok]
    print('--- %d/%d checks passed ---' % (len(RESULTS) - len(failed),
                                           len(RESULTS)))
    if failed:
        print('FAILED: ' + '; '.join(failed))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
