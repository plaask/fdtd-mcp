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


def _unescape(text):
    """Undo LSF string escaping (\\" and \\\\) inside a captured literal."""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text) and text[i + 1] in '\\"\'':
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _token_value(tok):
    """Turn a parsed argument token into a str/int/float/None.

    Integer-looking literals stay ints (LSF literals are typed, and commands
    such as addanalysisprop reject a float type code).
    """
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1]
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return tok[1:-1]
    if re.match(r'^[+-]?\d+$', tok):
        return int(tok)
    try:
        return float(tok)
    except ValueError:
        return tok


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------

class _Obj(object):
    def __init__(self, type_str):
        self.type = type_str
        self.props = {}
        self.prop_types = {}       # user properties (adduserprop)
        self.analysis_props = {}   # analysis properties (addanalysisprop)
        self.analysis_results = []  # addanalysisresult names
        self.scope = '::model'


def _suffixed(name):
    """v202's rename-on-collision rule: gap -> gap2 -> gap3."""
    base = name.rstrip('0123456789')
    digits = name[len(base):]
    return '%s%d' % (base or name, int(digits) + 1 if digits else 2)


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
        'addmovie': ('Movie Monitor', 'movie'),
        'addstructuregroup': ('Structure Group', 'structuregroup'),
        'addanalysisgroup': ('Analysis Group', 'analysisgroup'),
        'addport': ('Port', 'port'),
        'addmodeexpansion': ('Mode Expansion Monitor', 'mode expansion'),
        'addgroup': ('Container Group', 'group'),
        'addcustom': ('Custom', 'custom'),
        'addplanarsolid': ('Planar Solid', 'planar solid'),
        'addstack': ('STACK', 'STACK'),
        'addlayerbuilder': ('Layer Builder', 'layer builder'),
    }
    MONITOR_TYPES = ('Power Monitor', 'DFT Monitor', 'Index Monitor',
                     'Field Monitor', 'Movie Monitor')

    # v202 fidelity for addobject(): object-library items come from
    # <LUMERICAL_HOME>/Resources/fdtd-library and are copied in by id, then the
    # item's setup script re-runs (which is what creates the child objects).
    # The fake reproduces the three shapes the real library has: a structure
    # group with children, an analysis group with generated monitors, and a bare
    # primitive. An unknown id is silently ignored — the real engine does not
    # raise either, which is why library_insert validates against catalog.xml.
    LIBRARY = {
        'ring_resonator': ('Structure Group', 'ring resonator',
                           ['outer_top', 'outer_bottom', 'inner_top',
                            'inner_bottom', 'arc1', 'arc2', 'arc3', 'arc4']),
        'Qanalysis': ('Analysis Group', 'Qanalysis',
                      ['t1', 't2', 't3', 't4']),
        'sphere': ('Sphere', 'sphere', []),
    }

    def __init__(self, *a, **k):
        self._objects = {}
        self._model = _Obj('ObjectModelRoot')
        self._model.props['type'] = 'ObjectModelRoot'
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
        self._mode = 'layout'
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
            # Present but inactive until the override toggle is switched on.
            p['override mesh order from material database'] = False
            p['override color opacity from material database'] = False
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
        # Observable side effect for the EOF test: when the MCP server goes away
        # the bridge must still close the session before exiting, otherwise the
        # (real) engine process is orphaned. stderr is captured by the caller.
        sys.stderr.write('fake-engine: close() called\n')
        sys.stderr.flush()

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
        # v202 fidelity: '#' starts a comment and runs to end of line; '%' is
        # NOT a comment there — a '%' line makes the script fail outright.
        for line in code.split('\n'):
            if line.strip().startswith('%'):
                raise RuntimeError('Failed to evaluate code')
        stripped = '\n'.join(line.split('#', 1)[0] for line in code.split('\n'))
        for stmt in _split_statements(stripped):
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
        # select("...") — quotes inside the name arrive backslash-escaped.
        m = re.match(r'select\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)\s*$', s)
        if m:
            self.select(_unescape(m.group(1)))
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
        self._mode = 'analysis'

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

        if func_name == 'layoutmode':
            return 1.0 if self._mode == 'layout' else 0.0

        if func_name == 'switchtolayout':
            self._mode = 'layout'
            return None

        if func_name == 'getid':
            return '\n'.join(n for n, o in self._objects.items()
                             if o.scope == self._scope)

        if func_name == 'getnamed':
            obj = self._resolve(args[0])
            if len(args) == 1:
                # v202: getnamed(obj) returns the property-name list.
                return '\n'.join(sorted(set(obj.props) | {'type'}))
            prop = args[1]
            if prop == 'type':
                return obj.type
            if prop not in obj.props:
                raise KeyError('Property not found: %s.%s' % (args[0], prop))
            return obj.props[prop]

        if func_name == 'setnamed':
            target, prop, value = args[0], args[1], args[2]
            obj = self._resolve(target)
            # v202 fidelity: setnamed can only UPDATE an existing property. It
            # cannot create one ("in setnamed, the requested property was not
            # found") — the old fake silently created model variables here,
            # which hid the fact that addvar is gone from v202.
            if prop not in obj.props:
                raise KeyError('in setnamed, the requested property was not found')
            # v202 fidelity: a property that sits behind an "override ..."
            # toggle reports itself as inactive rather than missing.
            if prop == 'mesh order' and not obj.props.get(
                    'override mesh order from material database'):
                raise RuntimeError('in setnamed, the requested property is inactive')
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

        if func_name == 'addgridattribute':
            if len(args) != 1:
                raise RuntimeError(
                    'argument list for addgridattribute command is incorrect')
            table = {
                'lc orientation': ('LC Attribute', 'LC attribute'),
                'permittivity rotation': ('Permittivity Rotation',
                                          'Rotation attribute'),
                'matrix transform': ('Matrix Transform', 'Matrix attribute'),
                'np density': ('np Density', 'np density'),
                'temperature': ('Temperature', 'temperature'),
            }
            spec = table.get(str(args[0]).lower())
            if spec is None:
                raise RuntimeError('unknown grid attribute type: %s' % (args[0],))
            type_str, base = spec
            name = self._next_name(base)
            obj = _Obj(type_str)
            obj.scope = self._scope
            obj.props['name'] = name
            obj.props['enabled'] = True
            self._apply_defaults(obj)
            self._objects[name] = obj
            self._selected = name
            return None

        if func_name == 'addport' and 'FDTD' not in self._objects:
            raise RuntimeError('port requires an FDTD solver region')

        if func_name in self.ADD:
            self._create_object(func_name)
            return None

        if func_name == 'addimport':
            name = self._next_name('import')
            obj = _Obj('Import')
            obj.scope = self._scope
            obj.props['name'] = name
            obj.props['enabled'] = True
            self._apply_defaults(obj)
            self._objects[name] = obj
            self._selected = name
            return None

        if func_name == 'stlimport':
            name = self._next_name('stl')
            obj = _Obj('PlanarSolid')
            obj.scope = self._scope
            obj.props['name'] = name
            obj.props['enabled'] = True
            self._apply_defaults(obj)
            self._objects[name] = obj
            self._selected = name
            return None

        if func_name in ('importsurface', 'importbinary', 'importnk'):
            obj = self._selected_object()
            if obj.type != 'Import':
                raise RuntimeError('no import objects are currently selected')
            obj.props['import type'] = func_name
            return 1.0

        if func_name in ('stepimport', 'importimage'):
            raise RuntimeError(func_name + ' is not a valid function or variable name')

        if func_name == 'gdsimport':
            if len(args) < 3:
                raise RuntimeError('argument list for gdsimport command is incorrect')
            name = self._next_name('gds')
            obj = _Obj('Polygon')
            obj.scope = self._scope
            obj.props['name'] = name
            obj.props['enabled'] = True
            self._apply_defaults(obj)
            if len(args) >= 4 and args[3]:
                obj.props['material'] = args[3]
            self._objects[name] = obj
            self._selected = name
            return 1.0

        if func_name == 'addobject':
            if len(args) != 1:
                raise RuntimeError('argument list for addobject command is incorrect')
            spec = self.LIBRARY.get(str(args[0]))
            if spec is None:
                return None            # real engine: unknown id is a silent no-op
            type_str, name, children = spec
            obj = _Obj(type_str)
            obj.scope = self._scope
            obj.props['name'] = name
            obj.props['enabled'] = True
            self._apply_defaults(obj)
            if type_str == 'Structure Group':
                obj.props['script'] = ('addwaveguide; set("name","outer_top");\n'
                                       '# library setup script (fake)')
            elif type_str == 'Analysis Group':
                obj.props['setup script'] = ('addtime; set("name","t1");\n'
                                             '# library setup script (fake)')
                obj.props['analysis script'] = '# library analysis script (fake)'
            self._objects[name] = obj
            for child in children:
                kid = _Obj('Waveguide' if type_str == 'Structure Group'
                           else 'TimeMonitor')
                kid.scope = '%s::%s' % (self._scope, name)
                kid.props['name'] = child
                kid.props['enabled'] = True
                self._apply_defaults(kid)
                self._objects['%s::%s' % (name, child)] = kid
            self._selected = name
            return None

        # v202 fidelity: addvar/setvar/getvar were removed from the LSF command
        # table. The fake must NOT emulate them, or tests certify a dead API.
        if func_name in ('addvar', 'setvar', 'getvar', 'addvarfdtd'):
            raise RuntimeError(func_name + ' is not a valid function or variable name')

        if func_name in ('adduserprop', 'addanalysisprop'):
            # Real signature: adduserprop("name", type_code, value) — it acts on
            # the CURRENTLY SELECTED object and only accepts an INTEGER type code.
            if len(args) != 3:
                raise RuntimeError(
                    'argument list for %s command is incorrect' % func_name)
            prop, type_code, value = args[0], args[1], args[2]
            if not isinstance(type_code, int) or isinstance(type_code, bool) \
                    or not 0 <= type_code <= 5:
                raise RuntimeError(
                    'argument list for %s command is incorrect - type must be '
                    'the INTEGER code 0-5' % func_name)
            obj = self._selected_object()
            # v202: the model root and analysis groups are edited by the
            # "Edit analysis group" dialog, so their Analysis > Variables table
            # lists ANALYSIS properties; structure groups take user properties.
            if func_name == 'addanalysisprop':
                allowed = ('::model', 'ObjectModelRoot', 'Analysis Group')
                store = obj.analysis_props
            else:
                allowed = ('Structure Group',)
                store = obj.prop_types
            if obj.type not in allowed:
                raise RuntimeError(
                    '%s requires a single structure or analysis group object '
                    'to be selected' % func_name)
            # v202: the namespaces share names and the call is NOT idempotent —
            # declaring an existing name silently creates a suffixed clone.
            name = prop
            while name in obj.props:
                name = _suffixed(name)
            store[name] = type_code
            obj.props[name] = value
            return None

        if func_name == 'addanalysisresult':
            # v202: exactly one argument (name) on the model root.
            if len(args) != 1:
                raise RuntimeError(
                    'argument list for addanalysisresult command is incorrect')
            obj = self._selected_object()
            if obj.type not in ('::model', 'ObjectModelRoot', 'Analysis Group'):
                raise RuntimeError('addanalysisresult requires an analysis '
                                   'group object to be selected')
            if args[0] not in obj.analysis_results:
                obj.analysis_results.append(args[0])
            return None

        if func_name == 'queryanalysisresult':
            obj = self._resolve(args[0])
            return list(obj.analysis_results)

        if func_name == 'queryuserprop':
            obj = self._selected_object()
            if obj.type not in ('::model', 'ObjectModelRoot', 'Structure Group'):
                raise RuntimeError('NULL pointer access')
            return {'name': list(obj.prop_types.keys()),
                    'type': [str(v) for v in obj.prop_types.values()]}

        if func_name == 'queryanalysisprop':
            obj = self._resolve(args[0])
            return {'name': list(obj.analysis_props.keys()),
                    'type': [str(v) for v in obj.analysis_props.values()]}

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
            base = {0: 'sweep', 1: 'optimization',
                    2: 'Monte Carlo analysis', 3: 's-parameter sweep'}.get(
                        stype, 'sweep')
            name = base
            n = 1
            while name in self._sweeps:
                name = base + str(n)
                n += 1
            self._sweeps[name] = {'type': stype, 'props': {'name': name},
                                  'parameters': {}, 'results_def': {},
                                  'results': None}
            return '::' + name  # real engine returns the created sweep path

        if func_name == 'setsweep':
            return self._setsweep(*args)

        if func_name == 'getsweep':
            name = args[0]
            sw = self._sweeps.get(name)
            if sw is None:
                raise KeyError('sweep not found: ' + name)
            return self._sweep_props(sw)

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
            if func_name != 'haveresult':
                # v202 fidelity: only haveresult exists as a script command.
                raise RuntimeError(
                    func_name + ' is not a valid function or variable name')
            return bool(self._results.get(args[0]))

        # LSF `run;` / `runsweep("name");` are the script forms of the same
        # operations the bridge also calls through the project handle.
        if func_name == 'run':
            self.run()
            return None

        if func_name == 'runsweep':
            self.runsweep(args[0])
            return None

        if func_name == 'resume':
            return None

        self._eval_log.append('FAKE-UNSUPPORTED: %s%r' % (func_name, args))
        raise RuntimeError('FakeEngine: unsupported function: %s' % func_name)

    _SWEEP_DEFAULTS = {
        0: ['name', 'type', 'number of points', 'resave files after analysis'],
        1: ['name', 'algorithm', 'maximum generations', 'reset random generator',
            'type', 'generation size', 'tolerance', 'first generation script',
            'next generation script', 'use figure of merit script',
            'figure of merit script'],
        2: ['name', 'number of trials', 'variation', 'seed', 'enable seed',
            'individual trial', 'enable individual trial',
            'enable spatial correlations'],
        3: ['name', 'excite all ports', 'calculate group delay', 'invert sign',
            'map from', 'active', 'port', 'mode', 'map vector',
            'auto symmetry', 'export setup'],
    }

    def _sweep_props(self, sw):
        """Mirror ?setsweep(name) on v202: defaults + added parameter names."""
        props = list(self._SWEEP_DEFAULTS.get(sw.get('type', 0), []))
        for pname in sw.get('parameters', {}):
            if pname not in props:
                props.append(pname)
        return '\n'.join(props)

    def _setsweep(self, *args):
        if len(args) == 1:
            name = args[0]
            sw = self._sweeps.get(name)
            if sw is None:
                raise KeyError('sweep not found: ' + name)
            return self._sweep_props(sw)
        if len(args) == 3:
            name, prop, value = args
            sw = self._sweeps.get(name)
            if sw is None:
                raise KeyError('sweep not found: ' + name)
            editable = self._sweep_props(sw).split('\n')
            if prop == 'name':
                newname = value
                if newname != name:
                    self._sweeps[newname] = self._sweeps.pop(name)
                sw['props']['name'] = newname
                return 1.0
            if prop not in editable:
                # v202 fidelity: unknown property -> 0.0, no exception.
                return 0.0
            if prop == 'type' and value not in ('Ranges', 'Values'):
                return 0.0
            if prop == 'number of points':
                try:
                    if int(value) <= 0:
                        return 0.0
                except (TypeError, ValueError):
                    return 0.0
            if prop in sw['parameters']:
                sw['parameters'][prop]['value'] = value
            else:
                sw['props'][prop] = value
            return 1.0
        if len(args) == 4:
            ok = self._set_sweep_target(*args)
            return 1.0 if ok else 0.0
        raise TypeError('setsweep expects 1, 3 or 4 args')

    def _set_sweep_target(self, name, obj, prop, value):
        sw = self._sweeps.get(name)
        if sw is None:
            raise KeyError('sweep not found: ' + name)
        if obj in sw['parameters']:
            sw['parameters'][obj][prop] = value
            return True
        if obj in sw['results_def']:
            sw['results_def'][obj][prop] = value
            return True
        return False


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
