# -*- coding: utf-8 -*-
"""Scripted fake Lumerical engine for functional tests.

Injected into sys.modules as 'lumapi' so fdtd_mcp/bridge.py can be exercised
WITHOUT a real Lumerical installation. Backs an in-memory model/material/sweep/
result store so CRUD round-trips work:

    session_new -> model_add -> model_get -> model_set -> model_delete

Calls that cannot be emulated faithfully are logged (self._eval_log /
self._appcall_log) and either raise (so the bridge surfaces a clean error) or
no-op. See test_functional.py for the coverage matrix.
"""
import os
import re
import sys
import types


# ---------------------------------------------------------------------------
# LSF-ish mini-parsing helpers
# ---------------------------------------------------------------------------

def _split_statements(code):
    """Split LSF-ish code on ';' outside of quoted strings."""
    stmts = []
    cur = []
    in_dq = False
    in_sq = False
    for ch in code:
        if ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "'" and not in_dq:
            in_sq = not in_sq
        if ch == ';' and not in_dq and not in_sq:
            stmts.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        stmts.append(''.join(cur))
    return stmts


def _split_args(s):
    """Split comma-separated args at top level, respecting quotes/parens."""
    args = []
    cur = []
    depth = 0
    in_dq = False
    in_sq = False
    for ch in s:
        if ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "'" and not in_dq:
            in_sq = not in_sq
        if ch == '(' and not in_dq and not in_sq:
            depth += 1
        elif ch == ')' and not in_dq and not in_sq:
            depth -= 1
        if ch == ',' and depth == 0 and not in_dq and not in_sq:
            args.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if ''.join(cur).strip():
        args.append(''.join(cur).strip())
    return args


def _token_value(tok):
    """Turn a parsed argument token into a str/float/None."""
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1]
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return tok[1:-1]
    try:
        return float(tok)
    except ValueError:
        return tok


def _repr_lsf(v):
    """Best-effort LSF literal representation (used for addvar lines)."""
    if v is None:
        return '0'
    if isinstance(v, str):
        return '"%s"' % v
    if hasattr(v, 'tolist'):
        v = v.tolist()
    if isinstance(v, (list, tuple)):
        return repr(list(v))
    return repr(v)


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------

class _Obj(object):
    def __init__(self, type_str):
        self.type = type_str
        self.props = {}
        self.scope = '::model'


class FakeFDTD(object):
    """Emulates enough of a Lumerical FDTD project object for CRUD round-trips."""

    ADD = {
        'addrect': ('Rectangle', 'rect'),
        'addcircle': ('Circle', 'circle'),
        'addring': ('Ring', 'ring'),
        'addpoly': ('Polygon', 'poly'),
        'addsphere': ('Sphere', 'sphere'),
        'addpyramid': ('Pyramid', 'pyramid'),
        'addtriangle': ('Triangle', 'triangle'),
        'addwaveguide': ('Waveguide', 'waveguide'),
        'addfdtd': ('FDTD', 'FDTD'),
        'addmesh': ('Mesh', 'mesh'),
        'adddipole': ('Dipole', 'dipole'),
        'addtfsf': ('TFSF', 'tfsf'),
        'addplane': ('Plane Wave', 'plane'),
        'addgaussian': ('Gaussian', 'gaussian'),
        'addmode': ('Mode Source', 'mode'),
        'addpower': ('Power Monitor', 'power'),
        'addindex': ('Index Monitor', 'index'),
        'addfield': ('Field Monitor', 'field'),
        'addmovie': ('Movie Monitor', 'movie'),
        'addstructuregroup': ('Structure Group', 'structuregroup'),
        'addanalysisgroup': ('Analysis Group', 'analysisgroup'),
    }
    MONITOR_TYPES = ('Power Monitor', 'DFT Monitor', 'Index Monitor',
                     'Field Monitor', 'Movie Monitor')

    def __init__(self, *a, **k):
        self._objects = {}
        self._model = _Obj('Model')
        self._model.props['type'] = 'Model'
        self._model.props['setup script'] = ''
        self._model.props['analysis script'] = ''
        self._selected = None
        self._scope = '::model'
        self._materials = {}
        self._material_order = []
        self._sweeps = {}
        self._results = {}
        self._getv_vars = {}
        self._saved = []
        self._feval_calls = []
        self._run_count = 0
        self._closed = False
        self._eval_log = []
        self._appcall_log = []
        self._next_auto = {}
        self._material_counter = 0
        # A fresh project always contains an FDTD region (so summary reads work
        # for both session_open and session_new).
        self._create_object('addfdtd')

    # ---- naming / creation -------------------------------------------------

    def _next_name(self, base):
        n = self._next_auto.get(base, 0) + 1
        self._next_auto[base] = n
        cand = base if n == 1 else '%s_%d' % (base, n - 1)
        i = 0
        while cand in self._objects:
            i += 1
            cand = '%s_%d' % (base, n + i - 1)
        return cand

    def _create_object(self, func):
        if func == 'addfdtd' and 'FDTD' in self._objects:
            self._selected = 'FDTD'
            return 'FDTD'
        type_str, base = self.ADD[func]
        name = self._next_name(base)
        obj = _Obj(type_str)
        obj.scope = self._scope
        obj.props['name'] = name
        obj.props['enabled'] = True
        self._apply_defaults(obj)
        self._objects[name] = obj
        self._selected = name
        return name

    def _apply_defaults(self, obj):
        t = obj.type
        p = obj.props
        if t in ('Rectangle', 'Circle', 'Ring', 'Polygon', 'Sphere',
                 'Pyramid', 'Triangle', 'Waveguide', 'Mesh', 'Index Monitor'):
            p['material'] = 'Au (Gold) - Johnson and Christy'
            p['index'] = 1.5
            p['mesh order'] = 2
        if t != 'FDTD':
            p['x'] = 0.0
            p['y'] = 0.0
            p['z'] = 0.0
        if t in ('Rectangle', 'Circle', 'Ring', 'Polygon', 'Sphere',
                 'Pyramid', 'Triangle', 'Waveguide'):
            p['x span'] = 1e-6
            p['y span'] = 1e-6
            p['z span'] = 1e-6
        if t in ('Dipole', 'TFSF', 'Plane Wave', 'Gaussian', 'Mode Source'):
            p['amplitude'] = 1.0
            p['polarization angle'] = 0.0
            p['direction'] = 0
        if t in self.MONITOR_TYPES:
            p['frequency points'] = 100
            p['monitor type'] = 'Linear X'
            p['output Ex'] = True
            p['output Ey'] = True
            p['output Ez'] = True
            p['output power'] = True
        if t == 'FDTD':
            p['dimension'] = '3D'
            p['x span'] = 2e-6
            p['y span'] = 2e-6
            p['z span'] = 1e-6
            p['simulation time'] = 1000e-15
            p['mesh accuracy'] = 4
            for axis in ('x', 'y', 'z'):
                p['%s min bc' % axis] = 'PML'
                p['%s max bc' % axis] = 'PML'
        if t == 'Structure Group':
            p['script'] = ''
        if t == 'Analysis Group':
            p['setup script'] = ''
            p['analysis script'] = ''

    # ---- resolution --------------------------------------------------------

    def _resolve(self, name):
        if name == '::model':
            return self._model
        short = name
        if name.startswith('::model'):
            short = name[len('::model'):].lstrip(':')
        obj = self._objects.get(short)
        if obj is None:
            raise KeyError('Object not found: %s' % name)
        return obj

    def _selected_object(self):
        if not self._selected:
            raise KeyError('No selected object')
        return self._resolve(self._selected)

    def _set_scope(self, scope):
        if scope == '::model':
            self._scope = '::model'
        elif scope.startswith('::model'):
            self._scope = scope
        else:
            self._scope = '::model::' + scope.lstrip(':')

    def _delete_selected(self):
        name = self._selected
        if not name or name == '::model':
            raise KeyError('delete: nothing selected')
        self._objects.pop(name, None)
        self._selected = None

    # ---- lumapi project API ------------------------------------------------

    def close(self):
        self._closed = True

    def getnamed(self, name, prop):
        """Direct-method form used by session_new/open and model_info."""
        return self._appcall('getnamed', [name, prop])

    def setnamed(self, name, prop, value):
        """Direct-method form used by session_new/open."""
        return self._appcall('setnamed', [name, prop, value])

    def select(self, name):
        if name == '::model':
            self._selected = '::model'
            return
        self._resolve(name)
        self._selected = name

    def getv(self, expr):
        expr = expr.strip()
        if '.' in expr:
            var, field = expr.split('.', 1)
            ds = self._getv_vars.get(var)
            if ds is None:
                raise KeyError('Unknown variable: %s' % var)
            if field not in ds:
                raise KeyError('Field not found: %s' % field)
            return ds[field]
        if expr in self._getv_vars:
            return self._getv_vars[expr]
        if expr in self._model.props:
            return self._model.props[expr]
        raise KeyError('Unknown variable: %s' % expr)

    def eval(self, code):
        self._eval_log.append(code)
        for stmt in _split_statements(code):
            s = stmt.strip()
            if not s:
                continue
            if not self._eval_one(s):
                raise RuntimeError('FakeEngine: unsupported LSF: ' + s)

    def _eval_one(self, s):
        # write("path", get("prop"))
        m = re.match(
            r'write\s*\(\s*"([^"]*)"\s*,\s*get\s*\(\s*"([^"]*)"\s*\)\s*\)\s*$', s)
        if m:
            path, prop = m.group(1), m.group(2)
            obj = self._selected_object()
            content = obj.props.get(prop, '')
            if not isinstance(content, str):
                content = str(content)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            return True
        # VAR = getresult(...)
        m = re.match(r'(\w+)\s*=\s*getresult\s*\((.*)\)\s*$', s)
        if m:
            var, argstr = m.group(1), m.group(2)
            args = [_token_value(a) for a in _split_args(argstr)]
            ds = self._appcall('getresult', args)
            self._getv_vars[var] = ds
            return True
        # clear(VAR)
        m = re.match(r'clear\s*\(\s*(\w+)\s*\)\s*$', s)
        if m:
            self._getv_vars.pop(m.group(1), None)
            return True
        # matlabsave("path", VAR)
        m = re.match(r'matlabsave\s*\(\s*"([^"]*)"\s*,\s*(\w+)\s*\)\s*$', s)
        if m:
            path = m.group(1)
            with open(path, 'w', encoding='utf-8') as f:
                f.write('# fake mat result\n')
            return True
        # groupscope("...")
        m = re.match(r'groupscope\s*\(\s*"([^"]*)"\s*\)\s*$', s)
        if m:
            self._set_scope(m.group(1))
            return True
        if s.startswith('selectall'):
            return True
        # select("...")
        m = re.match(r'select\s*\(\s*"([^"]*)"\s*\)\s*$', s)
        if m:
            self.select(m.group(1))
            return True
        if re.match(r'delete\s*\(\s*\)\s*$', s):
            self._delete_selected()
            return True
        # generic func(...)
        m = re.match(r'(\w+)\s*\((.*)\)\s*$', s)
        if m:
            func = m.group(1)
            args = [_token_value(a) for a in _split_args(m.group(2))]
            self._appcall(func, args)
            return True
        return False

    def save(self, path):
        self._saved.append(path)
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('# fake fsp\n')

    def feval(self, path):
        if not os.path.exists(path):
            raise RuntimeError('script file not found: ' + path)
        self._feval_calls.append(path)

    def run(self):
        self._run_count += 1
        self._populate_results()

    def runsweep(self, name):
        sw = self._sweeps.get(name)
        if sw is None:
            raise KeyError('sweep not found: ' + name)
        import numpy as np
        res = {}
        for rname in sw['results_def']:
            res[rname] = {'gap': np.linspace(100e-9, 300e-9, 5),
                          rname: np.linspace(0.1, 0.9, 5)}
        sw['results'] = res

    # ---- simulation results ------------------------------------------------

    def _populate_results(self):
        import numpy as np
        self._results = {}
        wl = np.linspace(500e-9, 800e-9, 20)
        x = np.linspace(-1e-6, 1e-6, 10)
        for name, obj in self._objects.items():
            t = obj.type
            datasets = {}
            if t == 'Power Monitor':
                # A power monitor used as a DFT monitor computes E/H datasets
                # too (when E/H outputs are enabled), matching the real engine.
                datasets['T'] = {'f': 3e8 / wl, 'lambda': wl,
                                 'T': np.linspace(0, 1, 20),
                                 'R': np.linspace(1, 0, 20),
                                 'P': np.linspace(0.5, 1.0, 20)}
                datasets['E'] = {'f': 3e8 / wl, 'lambda': wl, 'x': x, 'y': x, 'z': x,
                                 'Ex': np.ones((10, 10, 10)),
                                 'Ey': np.ones((10, 10, 10)),
                                 'Ez': np.zeros((10, 10, 10))}
            elif t in ('Field Monitor',):
                datasets['E'] = {'f': 3e8 / wl, 'lambda': wl, 'x': x, 'y': x, 'z': x,
                                 'Ex': np.ones((10, 10, 10)),
                                 'Ey': np.ones((10, 10, 10)),
                                 'Ez': np.zeros((10, 10, 10))}
            elif t == 'Index Monitor':
                datasets['index'] = {'x': x, 'y': x, 'z': x,
                                     'index': np.ones((10, 10, 10)),
                                     'index_x': np.ones((10, 10, 10))}
            if datasets:
                self._results[name] = datasets
        if 'FDTD' in self._objects:
            self._results['FDTD'] = {
                'T': {'f': 3e8 / wl, 'lambda': wl, 'T': np.linspace(0, 1, 20)},
                'R': {'f': 3e8 / wl, 'lambda': wl, 'R': np.linspace(1, 0, 20)}}

    # ---- appCall dispatch ---------------------------------------------------

    def _appcall(self, func_name, args):
        self._appcall_log.append((func_name, list(args)))

        if func_name == 'getid':
            return '\n'.join(n for n, o in self._objects.items()
                             if o.scope == self._scope)

        if func_name == 'getnamed':
            obj = self._resolve(args[0])
            prop = args[1]
            if prop == 'type':
                return obj.type
            if prop not in obj.props:
                raise KeyError('Property not found: %s.%s' % (args[0], prop))
            return obj.props[prop]

        if func_name == 'setnamed':
            target, prop, value = args[0], args[1], args[2]
            obj = self._resolve(target)
            if target == '::model' and prop not in ('type', 'setup script',
                                                    'analysis script'):
                line = 'addvar("%s", %s)' % (prop, _repr_lsf(value))
                if line not in obj.props.get('setup script', ''):
                    obj.props['setup script'] = obj.props.get('setup script', '') \
                        + line + '\n'
            obj.props[prop] = value
            return None

        if func_name == 'get':
            if self._selected in self._sweeps:
                sw = self._sweeps[self._selected]
                prop = args[0]
                if prop == 'type':
                    return 'Sweep'
                if prop not in sw['props']:
                    raise KeyError('Property not found on selected sweep')
                return sw['props'][prop]
            obj = self._selected_object()
            prop = args[0]
            if prop == 'type':
                return obj.type
            if prop not in obj.props:
                raise KeyError('Property not found on selected: %s' % prop)
            return obj.props[prop]

        if func_name == 'set':
            prop, value = args[0], args[1]
            if self._selected in self._sweeps:
                if prop == 'name':
                    old = self._selected
                    if value != old:
                        self._sweeps[value] = self._sweeps.pop(old)
                        self._selected = value
                        self._sweeps[value]['props']['name'] = value
                else:
                    self._sweeps[self._selected]['props'][prop] = value
                return None
            obj = self._selected_object()
            if prop == 'name':
                newname = value
                oldname = self._selected
                if newname != oldname and oldname not in ('::model', None):
                    self._objects.pop(oldname, None)
                    self._objects[newname] = obj
                    self._selected = newname
                    obj.props['name'] = newname
            else:
                obj.props[prop] = value
            return None

        if func_name in self.ADD:
            self._create_object(func_name)
            return None

        if func_name in ('addvar', 'setvar'):
            if len(args) == 3:
                target, prop, value = args
            elif len(args) == 2:
                target, prop = args
                value = None
            else:
                raise TypeError('addvar/setvar expects 2 or 3 args')
            obj = self._resolve(target)
            obj.props[prop] = value
            if target == '::model' and prop not in ('type', 'setup script',
                                                    'analysis script'):
                line = 'addvar("%s", %s)' % (prop, _repr_lsf(value))
                if line not in obj.props.get('setup script', ''):
                    obj.props['setup script'] = obj.props.get('setup script', '') \
                        + line + '\n'
            return None

        if func_name in ('adduserprop', 'addanalysisprop'):
            target, prop = args[0], args[1]
            obj = self._resolve(target)
            if prop not in obj.props:
                obj.props[prop] = None
            return None

        # ---- materials ----
        if func_name == 'addmaterial':
            mtype = args[0] if args else 'Sampled 3D data'
            self._material_counter += 1
            name = 'material_%d' % self._material_counter
            mat = {'type': mtype, 'name': name, 'mesh order': 2}
            if mtype == 'Dielectric':
                mat['Refractive Index'] = 1.5
            elif mtype == 'Drude':
                mat['plasma frequency'] = 1.37e15
                mat['plasma frequency fit'] = False
            else:
                mat['sampled 3d data'] = None
            self._materials[name] = mat
            self._material_order.append(name)
            return name

        if func_name == 'setmaterial':
            name, prop, value = args[0], args[1], args[2]
            if name not in self._materials:
                raise KeyError('material not found: ' + name)
            self._materials[name][prop] = value
            return None

        if func_name == 'getmaterial':
            if len(args) == 0:
                return '\n'.join(self._material_order)
            if len(args) == 1:
                name = args[0]
                if name not in self._materials:
                    raise KeyError('material not found: ' + name)
                return list(self._materials[name].keys())
            name, prop = args[0], args[1]
            if name not in self._materials:
                raise KeyError('material not found: ' + name)
            if prop not in self._materials[name]:
                raise KeyError('material prop not found: %s' % prop)
            return self._materials[name][prop]

        if func_name == 'deletematerial':
            name = args[0]
            if name not in self._materials:
                raise KeyError('material not found: ' + name)
            del self._materials[name]
            if name in self._material_order:
                self._material_order.remove(name)
            return None

        if func_name == 'materialexists':
            return args[0] in self._materials

        # ---- sweeps ----
        if func_name == 'addsweep':
            stype = args[0] if args else 0
            self._sweeps['sweep'] = {'type': stype, 'props': {'name': 'sweep'},
                                     'parameters': {}, 'results_def': {},
                                     'results': None}
            return '::sweep'  # real engine returns the created sweep path

        if func_name == 'setsweep':
            self._setsweep(*args)
            return None

        if func_name == 'addsweepparameter':
            name, spec = args[0], args[1]
            sw = self._sweeps.get(name)
            if sw is None:
                raise KeyError('sweep not found: ' + name)
            if isinstance(spec, dict):
                pname = spec.get('Name') or spec.get('name')
                sw['parameters'][pname] = dict(spec)
            else:
                pname = spec
                sw['parameters'][pname] = {'name': pname}
            return None

        if func_name == 'addsweepresult':
            name, spec = args[0], args[1]
            sw = self._sweeps.get(name)
            if sw is None:
                raise KeyError('sweep not found: ' + name)
            if isinstance(spec, dict):
                rname = spec.get('Name') or spec.get('name')
                sw['results_def'][rname] = dict(spec)
            else:
                rname = spec
                sw['results_def'][rname] = {'name': rname}
            return None

        if func_name == 'insertsweep':
            return None

        if func_name == 'deletesweep':
            name = args[0]
            if name not in self._sweeps:
                raise KeyError('sweep not found: ' + name)
            del self._sweeps[name]
            return None

        if func_name == 'getsweepresult':
            name = args[0]
            sw = self._sweeps.get(name)
            if sw is None:
                raise KeyError('sweep not found: ' + name)
            if not sw['results']:
                raise RuntimeError('no results available for sweep: ' + name)
            if len(args) > 1:
                rname = args[1]
                if rname not in sw['results']:
                    raise KeyError('no result %s in sweep %s' % (rname, name))
                return {rname: sw['results'][rname]}
            return sw['results']

        # ---- results ----
        if func_name == 'getresult':
            if len(args) == 1:
                name = args[0]
                ds = self._results.get(name)
                if not ds:
                    raise KeyError('No results for ' + name)
                return '\n'.join(sorted(ds.keys()))
            name, dataset = args[0], args[1]
            ds = self._results.get(name)
            if not ds or dataset not in ds:
                raise KeyError('No dataset %s for %s' % (dataset, name))
            return ds[dataset]

        if func_name in ('haveresult', 'findresult', 'hasresult'):
            return bool(self._results.get(args[0]))

        self._eval_log.append('FAKE-UNSUPPORTED: %s%r' % (func_name, args))
        raise RuntimeError('FakeEngine: unsupported function: %s' % func_name)

    def _setsweep(self, *args):
        if len(args) == 3:
            name, dotted, value = args
            sw = self._sweeps.get(name)
            if sw is None:
                raise KeyError('sweep not found: ' + name)
            if '::' in dotted:
                obj, prop = dotted.split('::', 1)
                self._set_sweep_target(name, obj, prop, value)
            elif dotted == 'name':
                newname = value
                if newname != name:
                    self._sweeps[newname] = self._sweeps.pop(name)
                sw['props']['name'] = newname
            else:
                sw['props'][dotted] = value
        elif len(args) == 4:
            self._set_sweep_target(*args)
        else:
            raise TypeError('setsweep expects 3 or 4 args')

    def _set_sweep_target(self, name, obj, prop, value):
        sw = self._sweeps.get(name)
        if sw is None:
            raise KeyError('sweep not found: ' + name)
        if obj in sw['parameters']:
            sw['parameters'][obj][prop] = value
        elif obj in sw['results_def']:
            sw['results_def'][obj][prop] = value
        else:
            sw['props'][obj + '_' + prop] = value


# ---------------------------------------------------------------------------
# lumapi module shim
# ---------------------------------------------------------------------------

def appCall(fsp, func_name, args):
    """Mirror of lumapi.appCall(handle, func_name, args_list)."""
    if hasattr(fsp, '_appcall'):
        return fsp._appcall(func_name, list(args) if args else [])
    return None


def install():
    """Inject a fake 'lumapi' module into sys.modules."""
    lumapi = types.ModuleType('lumapi')
    lumapi.FDTD = FakeFDTD
    lumapi.appCall = appCall
    sys.modules['lumapi'] = lumapi
    return lumapi
