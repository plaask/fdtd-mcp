# -*- coding: utf-8 -*-
"""
FDTD Bridge — JSON-RPC via stdin/stdout.

Runs on Lumerical embed Python 3.6.8.
Uses lumapi Python methods (appCall-backed) instead of eval() for reliability.
Single-line eval() ONLY for simple expressions.
"""
from __future__ import print_function
import sys, os, json, re, traceback

# ---- Lumerical path discovery ----
# Accept --lumerical-home CLI arg (preferred) or LUMERICAL_HOME env var.
_home = os.environ.get('LUMERICAL_HOME', '')
for i, arg in enumerate(sys.argv):
    if arg == '--lumerical-home' and i + 1 < len(sys.argv):
        _home = sys.argv[i + 1]
        break

if not _home:
    raise RuntimeError(
        'LUMERICAL_HOME not set. '
        'Pass --lumerical-home PATH or set LUMERICAL_HOME environment variable.'
    )

LUM_API = os.path.join(_home, 'api', 'python')
sys.path.insert(0, LUM_API)
os.environ['PATH'] = os.path.join(_home, 'bin') + os.pathsep + os.environ.get('PATH', '')

import lumapi
from lumapi import appCall


class FdtdBridge(object):

    def __init__(self):
        self._fsp = None
        self._path = None
        self._tmp_dir = os.environ.get('TEMP', os.environ.get('TMP', '/tmp'))

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle(self, request):
        req_id = request.get('id')
        method = request.get('method', '')
        params = request.get('params', {})
        try:
            handler = getattr(self, '_cmd_' + method, None)
            if handler is None:
                return self._error(req_id, 'Unknown method: ' + method)
            return self._ok(req_id, handler(params))
        except Exception as e:
            return self._error(req_id, str(e) + '\n' + traceback.format_exc())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _cmd_ping(self, p): return 'pong'

    def _cmd_open(self, p):
        self._fsp = lumapi.FDTD(p['path'], hide=True)
        self._path = p['path']
        s = {}
        for k in ['dimension','x span','y span','z span','simulation time',
                  'mesh accuracy','x min bc','x max bc','y min bc','y max bc',
                  'z min bc','z max bc']:
            try: s[k] = self._fsp.getnamed('FDTD', k)
            except Exception: s[k] = None
        return {'status':'ok','path':p['path'],'summary':s}

    def _cmd_close(self, p):
        if self._fsp: self._fsp.close()
        self._fsp = None; self._path = None
        return {'status':'closed'}

    def _cmd_save(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        self._fsp.save(p['path']); self._path = p['path']
        return {'status':'ok','path':p['path']}

    def _cmd_new(self, p):
        """Create a blank FDTD project (no .fsp file needed).

        Optional FDTD region config: dimension, x/y/z span, simulation time, mesh accuracy.
        """
        if self._fsp:
            self._fsp.close()
        self._fsp = lumapi.FDTD(hide=True)
        self._path = None
        appCall(self._fsp, 'addfdtd', [])
        cfg = {}
        for k in ['dimension','x span','y span','z span','simulation time','mesh accuracy']:
            v = p.get(k)
            if v is not None:
                try: self._fsp.setnamed('FDTD', k, v); cfg[k] = v
                except Exception: pass
        return {'status':'ok','config':cfg}

    # ------------------------------------------------------------------
    # execute — universal single-line tool
    # ------------------------------------------------------------------

    def _cmd_execute(self, p):
        """Execute a single-line Lumerical command or expression.

        Handles:
          - delete("name")  → select("name"); delete(); (delete takes no args)
          - ?expr           → eval _br_r=expr; getv; clear (captures return value)
          - func(args)      → appCall for return value capture
          - raw command     → eval (no return value)
        """
        if not self._fsp: raise RuntimeError('No open project')
        code = p['code']

        # ---- Special case: delete("name") → select + delete ----
        m_del = re.match(r'delete\(\s*"([^"]+)"\s*\)', code)
        if m_del:
            obj_name = m_del.group(1)
            self._fsp.eval('select("' + obj_name + '"); delete();')
            return {'status': 'ok', 'deleted': obj_name}

        # ---- Guard: reject raw set("script") calls → use set_script tool ----
        m_bad_set = re.match(
            r'set\(\s*"(setup script|analysis script|script)"\s*,', code)
        if m_bad_set:
            prop = m_bad_set.group(1)
            return {
                'status': 'error',
                'message': (
                    'Do NOT use execute() to set "' + prop + '". '
                    'Use the set_script tool instead: '
                    'set_script(name="<object>", type="setup|analysis", content="...")'
                )
            }

        # ---- ?expr query: appCall for functions, getv for bare names ----
        if code.startswith('?'):
            expr = code[1:].strip()
            # Function call: route through appCall (reliable)
            m = re.match(r'(\w+)\((.+)\)', expr)
            if m:
                func_name = m.group(1)
                raw_args = m.group(2)
                args = []
                for a in re.findall(r'"([^"]*)"|\'([^\']*)\'|([^,]+)', raw_args):
                    arg = a[0] or a[1] or a[2].strip()
                    try: arg = float(arg)
                    except ValueError: pass
                    args.append(arg)
                result = appCall(self._fsp, func_name, args)
                return {'status': 'ok', 'result': self._sanitize(result)}
            # Bare name: getv (workspace variable), fallback to getnamed on ::model
            try:
                result = self._fsp.getv(expr)
            except Exception:
                result = appCall(self._fsp, 'getnamed', ['::model', expr])
            return {'status': 'ok', 'result': self._sanitize(result)}

        # ---- func(args) via appCall (return value capture) ----
        m = re.match(r'(\w+)\((.+)\)', code)
        if m:
            func_name = m.group(1)
            raw_args = m.group(2)
            args = []
            for a in re.findall(r'"([^"]*)"|\'([^\']*)\'|([^,]+)', raw_args):
                arg = a[0] or a[1] or a[2].strip()
                try: arg = float(arg)
                except ValueError: pass
                args.append(arg)
            try:
                result = appCall(self._fsp, func_name, args)
                return {'status': 'ok', 'result': self._sanitize(result)}
            except Exception:
                pass

        # ---- Fallback: pure command via eval ----
        try:
            self._fsp.eval(code)
            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)[:500]}

    def _cmd_execute_file(self, p):
        """Run a .lsf script file."""
        if not self._fsp: raise RuntimeError('No open project')
        self._fsp.feval(p['path'])
        return {'status': 'ok'}

    # ------------------------------------------------------------------
    # Scene info
    # ------------------------------------------------------------------

    _SCENE_PROPS = ['type','x','y','z','x span','y span','z span','z min','z max',
                     'material','index','enabled','wavelength start','wavelength stop',
                     'monitor type','frequency points','wavelength center','wavelength span',
                     'dx','dy','dz','theta','phi','injection axis','direction',
                     'polarization angle','dipole type','amplitude',
                     'output Ex','output Ey','output Ez','output Hx','output Hy','output Hz',
                     'output power','x min bc','x max bc','y min bc','y max bc',
                     'z min bc','z max bc','pml layers','simulation time','dt',
                     'auto shutoff min','mesh accuracy']

    _LIGHT_PROPS = ['type', 'enabled', 'material', 'x', 'y', 'z']

    def _traverse(self, scope, prop_list, enabled_only, result_list, seen):
        """Recurse into scope, appending discovered objects to result_list.

        Shared between _cmd_get_scene_info and _cmd_get_model_overview.
        Always resets to ::model root before navigating to target scope.
        Uses appCall for getid (avoids eval-assignment + getv pattern).
        """
        # Navigate to target scope from root
        self._fsp.eval('groupscope("::model");')
        if scope != '::model':
            parts = scope.replace('::model::', '').split('::')
            for part in parts:
                if part:
                    self._fsp.eval('groupscope("' + part + '");')
        self._fsp.eval('selectall();')
        ids_raw = appCall(self._fsp, 'getid', [])
        ids_str = str(ids_raw) if ids_raw else ''
        if not ids_str:
            return
        for obj_id in ids_str.split('\n'):
            obj_id = obj_id.strip()
            if not obj_id or obj_id in seen:
                continue
            seen.add(obj_id)
            try:
                t = appCall(self._fsp, 'getnamed', [obj_id, 'type'])
                obj = {'name': obj_id, 'type': str(t)}
                for prop in prop_list:
                    try:
                        obj[prop] = self._sanitize(
                            appCall(self._fsp, 'getnamed', [obj_id, prop]))
                    except Exception:
                        pass
                # enabled_only filter (if enabled property was read)
                if enabled_only:
                    en = obj.get('enabled')
                    if en is not None and not en:
                        # Skip disabled objects (but still recurse groups)
                        if str(t) in ('Structure Group', 'Analysis Group'):
                            self._traverse(scope + '::' + obj_id, prop_list,
                                           enabled_only, result_list, seen)
                        continue
                result_list.append(obj)
                if str(t) in ('Structure Group', 'Analysis Group'):
                    self._traverse(scope + '::' + obj_id, prop_list,
                                   enabled_only, result_list, seen)
            except Exception:
                pass

    def _cmd_get_scene_info(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        enabled_only = bool(p.get('enabled_only', False))

        objects = []
        seen = set()
        self._traverse('::model', self._SCENE_PROPS, enabled_only, objects, seen)

        fdtd = {}
        for k in ['dimension','x span','y span','z span','simulation time','mesh accuracy']:
            try: fdtd[k] = self._fsp.getnamed('FDTD', k)
            except Exception: pass

        return {'objects': objects, 'fdtd_summary': fdtd, 'object_count': len(objects)}

    # ------------------------------------------------------------------
    # Model variables (P0-2) + Model overview (P0-1)
    # ------------------------------------------------------------------

    # Known model variable names from user's typical .fsp files.
    # These are probed in addition to regex-discovered names.
    _KNOWN_MODEL_VARS = [
        'LD', 'DBR', 'top_layer', 'au', 'pmma1', 'd',
        'n_DBR', 't_H', 't_L', 'fff', 'hfa',
    ]

    def _enum_model_variables(self, scope):
        """Enumerate model/GUI variables in the given scope.

        Uses multiple strategies since no single lumapi call is guaranteed:
          1. Try lumapi list-variables call (name unverified — try/except)
          2. Fallback: regex-scan setup+analysis scripts for addvar/adduserprop
          3. Append known model variable names from _KNOWN_MODEL_VARS
        Each name is resolved via getnamed (or getvar if getnamed fails).
        """
        names = []
        # Strategy 1: try lumapi function to list variables (if exists)
        for cand in ('getvars', 'listvars', 'dumpvars'):
            try:
                r = appCall(self._fsp, cand, [])
                raw = str(r) if r else ''
                if raw:
                    parsed = [n.strip() for n in raw.replace('\n', ',').split(',') if n.strip()]
                    if parsed:
                        names = parsed
                        break
            except Exception:
                pass

        # Strategy 2: regex scan scripts for addvar / adduserprop
        if not names:
            try:
                scr = self._cmd_get_script({'name': scope})
                text = (scr.get('setup_script', '') or '') + '\n' + (scr.get('analysis_script', '') or '')
                var_names = re.findall(r'addvar\(\s*"(\w+)"', text)
                prop_names = re.findall(r'adduserprop\(\s*"(\w+)"', text)
                names = list(set(var_names + prop_names))
            except Exception:
                pass

        # Strategy 3: always probe known names
        for kn in self._KNOWN_MODEL_VARS:
            if kn not in names:
                names.append(kn)

        # Resolve values via getnamed (getvar as fallback)
        out = {}
        for vn in names:
            if not vn:
                continue
            val = None
            for fn in ('getnamed', 'getvar'):
                try:
                    val = appCall(self._fsp, fn, [scope, vn])
                    val = self._sanitize(val)
                    if val is not None:
                        break
                except Exception:
                    pass
            if val is not None:
                out[vn] = val
        return out

    def _cmd_get_model_variables(self, p):
        """Read GUI Model Variables table."""
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p.get('name', '::model')
        variables = self._enum_model_variables(name)
        return {'scope': name, 'variables': variables,
                'count': len(variables)}

    def _cmd_get_model_overview(self, p):
        """One-call self-introspection: objects + variables + materials + FDTD summary.

        Designed to stop the LLM from skipping steps — everything needed for
        modeling + script writing in one dict. Scripts pulled separately via get_script.
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        enabled_only = bool(p.get('enabled_only', True))
        include_full = bool(p.get('include_full', False))

        prop_list = self._SCENE_PROPS if include_full else self._LIGHT_PROPS

        # 1) Objects (light unless include_full=true)
        objects = []
        seen = set()
        self._traverse('::model', prop_list, enabled_only, objects, seen)

        # 2) Model variables
        variables = self._enum_model_variables('::model')

        # 3) Materials — list known materials via probing
        materials = []
        try:
            # getmaterial with no specific name returns a list in some versions
            raw = appCall(self._fsp, 'getmaterial', [])
            if raw:
                raw_str = str(raw)
                for m in raw_str.replace('\n', ',').split(','):
                    m = m.strip()
                    if m and m != '():':
                        materials.append(m)
        except Exception:
            pass

        # 4) FDTD summary
        fdtd_summary = {}
        for k in ['dimension', 'x span', 'y span', 'z span',
                  'simulation time', 'mesh accuracy']:
            try:
                fdtd_summary[k] = self._fsp.getnamed('FDTD', k)
            except Exception:
                pass

        return {
            'objects': objects,
            'object_count': len(objects),
            'model_variables': variables,
            'variable_count': len(variables),
            'materials': materials,
            'fdtd_summary': fdtd_summary,
        }

    # ------------------------------------------------------------------
    # Single object info (P0-3)
    # ------------------------------------------------------------------

    def _classify_source(self, t):
        """Return explicit source_kind label from object type string."""
        if not t:
            return None
        s = t.lower()
        if 'dipole' in s:
            return 'dipole'
        if 'tfsf' in s:
            return 'tfsf'
        if 'plane' in s:
            return 'plane'
        if 'gaussian' in s:
            return 'gaussian'
        if 'mode' in s:
            return 'mode-source'
        return None

    def _cmd_get_object_info(self, p):
        """Get full properties of ONE named object with type discriminator."""
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p['name']
        out = {'name': name}
        try:
            t = appCall(self._fsp, 'getnamed', [name, 'type'])
            t_str = str(t)
            out['type'] = t_str
            sk = self._classify_source(t_str)
            if sk:
                out['source_kind'] = sk
        except Exception as e:
            return {'name': name, 'error': 'not found', 'detail': str(e)[:200]}
        for prop in self._SCENE_PROPS:
            try:
                out[prop] = self._sanitize(
                    appCall(self._fsp, 'getnamed', [name, prop]))
            except Exception:
                pass
        return out

    def _cmd_get_script(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        name = p.get('name', '::model')
        result = {}
        self._fsp.select(name)

        # ::model + Analysis Group have setup/analysis scripts.
        # Structure Group has a single 'script' property.
        if name == '::model':
            props = [('setup script','setup'), ('analysis script','analysis')]
        else:
            obj_type = str(appCall(self._fsp, 'get', ['type']))
            if obj_type == 'Analysis Group':
                props = [('setup script','setup'), ('analysis script','analysis')]
            elif obj_type == 'Structure Group':
                props = [('script','script')]
            else:
                props = []

        for stype, suffix in props:
            key = stype.replace(' ','_')
            try:
                # Write to temp file from Lumerical side to avoid C-layer encoding issues.
                # write() appends to existing files — remove first to avoid stale accumulation.
                tmp_path = self._tmp_dir + '/_br_' + suffix + '.txt'
                try: os.remove(tmp_path)
                except OSError: pass
                self._fsp.eval(
                    'write("' + tmp_path.replace('\\','\\\\') + '",'
                    'get("' + stype + '"));'
                )
                content = None
                for enc in ['utf-8', 'gbk', 'gb2312', 'gb18030']:
                    try:
                        with open(tmp_path, 'r', encoding=enc) as f:
                            content = f.read()
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                if content is None:
                    with open(tmp_path, 'r', encoding='utf-8',
                              errors='replace') as f:
                        content = f.read()
                result[key] = content
            except Exception as e:
                result[key] = None
                result[key+'_error'] = str(e)[:200]
        return result

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def _cmd_get_parameters(self, p):
        """Get parameters from model, analysis groups, and structure groups.

        If 'object' is specified, only query that object.
        Otherwise, scan ::model + all analysis/structure groups found in scene.
        User properties (adduserprop) are discovered from setup scripts.
        """
        if not self._fsp: raise RuntimeError('No open project')
        target = p.get('object', None)
        all_params = {}

        objects_to_scan = []
        if target:
            objects_to_scan = [target]
        else:
            objects_to_scan = ['::model']
            try:
                scene = self._cmd_get_scene_info({})
                for obj in scene.get('objects', []):
                    t = obj.get('type', '')
                    if t in ('Analysis Group', 'Structure Group'):
                        objects_to_scan.append(obj['name'])
            except Exception:
                pass

        for obj_name in objects_to_scan:
            obj_params = {}
            # 1. Probe common parameter names
            for pname in ['gap', 'd', 'LR', 'theta', 'phi', 'fff', 'hfa',
                          'swp_wv_flag', 'wave_start', 'wave_stop', 'swp_wv',
                          'x_span', 'y_span', 'z_span', 'NA',
                          'x span', 'y span', 'z span']:
                try:
                    val = appCall(self._fsp, 'getnamed', [obj_name, pname])
                    obj_params[pname] = self._sanitize(val)
                except Exception:
                    pass
            # 2. Discover user properties from setup script
            try:
                scr = self._cmd_get_script({'name': obj_name})
                script_text = scr.get('script', '') or scr.get('setup_script', '') or ''
                user_props = re.findall(r'adduserprop\("(\w+)"', script_text)
                for up in user_props:
                    if up not in obj_params:
                        try:
                            val = appCall(self._fsp, 'getnamed', [obj_name, up])
                            obj_params[up] = self._sanitize(val)
                        except Exception:
                            pass
            except Exception:
                pass
            if obj_params:
                all_params[obj_name] = obj_params

        return {'parameters': all_params}

    def _cmd_set_parameter(self, p):
        """Set a parameter value on an object.

        Args:
            name: parameter name
            value: new value
            object: target object name (default '::model' for model-level params)
        """
        if not self._fsp: raise RuntimeError('No open project')
        param_name = p['name']
        value = p['value']
        obj = p.get('object', '::model')
        appCall(self._fsp, 'setnamed', [obj, param_name, value])
        return {'status': 'ok', 'object': obj, 'name': param_name, 'value': value}

    # ------------------------------------------------------------------
    # Sweep info
    # ------------------------------------------------------------------

    def _cmd_get_sweep_info(self, p):
        """Get sweep configuration."""
        if not self._fsp: raise RuntimeError('No open project')
        name = p['name']
        info = {'name': name}
        try:
            result = appCall(self._fsp, 'getsweepresult', [name])
            info['has_results'] = True
            info['result_sample'] = str(result)[:500] if result else 'empty'
        except Exception as e:
            err = str(e)
            if 'no results' in err.lower():
                info['exists'] = True
                info['has_results'] = False
                info['note'] = 'Sweep defined but not yet run'
            else:
                info['exists'] = False
                info['error'] = err[:200]
        return info

    # ------------------------------------------------------------------
    # Materials
    # ------------------------------------------------------------------

    def _cmd_add_material(self, p):
        """Create a new material from a model type template.

        Args:
            type: material model type, e.g. 'Sampled 3D data', 'Dielectric', 'Drude'
        Returns the auto-generated material name.
        """
        if not self._fsp: raise RuntimeError('No open project')
        mat_type = p.get('type', 'Sampled 3D data')
        name = appCall(self._fsp, 'addmaterial', [mat_type])
        return {'status': 'ok', 'name': str(name), 'type': mat_type}

    def _cmd_set_material(self, p):
        """Set a material property.

        Args:
            name: material name
            property: property name, e.g. 'Refractive Index', 'nk data', 'mesh order'
            value: property value (number, string, or array for tabulated nk data)
        """
        if not self._fsp: raise RuntimeError('No open project')
        name = p['name']
        prop = p['property']
        value = self._to_lum_array(p['value'])
        appCall(self._fsp, 'setmaterial', [name, prop, value])
        return {'status': 'ok', 'name': name, 'property': prop}

    def _cmd_get_material(self, p):
        """Read material properties.

        Args:
            name: material name
            property: optional property name; if omitted, lists all property names
        """
        if not self._fsp: raise RuntimeError('No open project')
        name = p['name']
        prop = p.get('property', None)
        if prop:
            result = appCall(self._fsp, 'getmaterial', [name, prop])
        else:
            result = appCall(self._fsp, 'getmaterial', [name])
        return self._sanitize(result)

    # ------------------------------------------------------------------
    # Script editing
    # ------------------------------------------------------------------

    def _cmd_set_script(self, p):
        """Set setup/analysis script on an object. Supports multi-line content.

        ::model and Analysis Group use 'setup script' / 'analysis script'.
        Structure Group uses a single 'script' property.
        """
        if not self._fsp: raise RuntimeError('No open project')
        name = p.get('name', '::model')
        stype = p['type']  # 'setup' or 'analysis'
        content = p['content']

        self._fsp.select(name)
        if name == '::model':
            script_prop = stype + ' script'
        else:
            obj_type = str(appCall(self._fsp, 'get', ['type']))
            if obj_type == 'Analysis Group':
                script_prop = stype + ' script'
            else:
                script_prop = 'script'

        appCall(self._fsp, 'set', [script_prop, content])
        return {'status': 'ok', 'object': name, 'script_type': script_prop}

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    # Common known dataset field names (fallback if field enumeration fails)
    _COMMON_RESULT_FIELDS = [
        'f', 'lambda', 'x', 'y', 'z',
        'Ex', 'Ey', 'Ez', 'Hx', 'Hy', 'Hz',
        'T', 'R', 'P', 'power', 'T_total',
        'index', 'index_x', 'index_y', 'index_z',
        'intensity', 'E', 'H',
    ]

    def _cmd_get_results(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        name = p.get('name', 'FDTD')
        try:
            r = appCall(self._fsp, 'getresult', [name])
            return {'name': name, 'results': str(r).split('\n') if r else []}
        except Exception as e:
            return {'name': name, 'results': [], 'error': str(e)[:200]}

    def _cmd_list_result_fields(self, p):
        """List field names in a result dataset WITHOUT fetching full data.

        Probes common known field names via getv(dataset.field) to discover
        which fields are accessible. Returns just the field names (no data)
        so LLM can preview monitor structure before calling get_result_data.
        """
        if not self._fsp: raise RuntimeError('No open project')
        monitor = p['monitor']
        data = p.get('data', '')
        try:
            if data:
                self._fsp.eval('_br_lf = getresult("' + monitor + '","' + data + '");')
            else:
                self._fsp.eval('_br_lf = getresult("' + monitor + '");')
            # Probe common field names to see which are accessible
            present = []
            for cf in self._COMMON_RESULT_FIELDS:
                try:
                    v = self._fsp.getv('_br_lf.' + cf)
                    if v is not None:
                        present.append(cf)
                except Exception:
                    pass
            self._fsp.eval('clear(_br_lf);')
            return {'monitor': monitor, 'data': data, 'fields': present,
                    'count': len(present)}
        except Exception as e:
            return {'monitor': monitor, 'data': data,
                    'error': str(e)[:300], 'fields': []}

    def _cmd_get_result_data(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        monitor = p['monitor']
        data = p.get('data', '')
        fields = p.get('fields', None)
        cap = int(p.get('cap', 2000))
        try:
            if data:
                self._fsp.eval('_br_d = getresult("' + monitor + '","' + data + '");')
            else:
                self._fsp.eval('_br_d = getresult("' + monitor + '");')

            # If no explicit field list, probe common fields
            if not fields or len(fields) == 0:
                fields = []
                for cf in self._COMMON_RESULT_FIELDS:
                    try:
                        v = self._fsp.getv('_br_d.' + cf)
                        if v is not None:
                            fields.append(cf)
                    except Exception:
                        pass

            out = {'monitor': monitor, 'data': data,
                   'fields_available': fields, 'values': {}}
            truncated = {}
            for fld in fields:
                try:
                    v = self._fsp.getv('_br_d.' + fld)
                    if v is None:
                        continue
                    sv = self._sanitize(v)
                    if isinstance(sv, list) and len(sv) > cap:
                        out['values'][fld] = sv[:cap]
                        truncated[fld] = len(sv)
                    else:
                        out['values'][fld] = sv
                except Exception:
                    pass
            if truncated:
                out['truncated'] = truncated
                out['cap'] = cap
            self._fsp.eval('clear(_br_d);')
            return out
        except Exception as e:
            return {'error': str(e)[:300], 'monitor': monitor, 'data': data}

    def _cmd_get_result_file(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        monitor, data, out = p['monitor'], p.get('data', ''), p['output']
        try:
            ep = out.replace('\\', '\\\\')
            self._fsp.eval('_br_d = getresult("' + monitor + '","' + data + '");')
            self._fsp.eval('matlabsave("' + ep + '", _br_d);')
            self._fsp.eval('clear(_br_d);')
            return {'file': out, 'status': 'ok'}
        except Exception as e:
            return {'error': str(e)[:300]}

    def _cmd_has_result(self, p):
        """Check whether a monitor/element has results without throwing."""
        if not self._fsp: raise RuntimeError('No open project')
        name = p['name']
        # First try direct lumapi call (if it exists)
        for fn in ('haveresult', 'findresult', 'hasresult'):
            try:
                r = appCall(self._fsp, fn, [name])
                if r is not None:
                    return {'name': name, 'exists': bool(r), 'check_method': fn}
            except Exception:
                pass
        # Fallback: try getresult and see if it succeeds
        try:
            r = appCall(self._fsp, 'getresult', [name])
            return {'name': name, 'exists': r is not None and str(r).strip() != '',
                    'check_method': 'getresult_attempt'}
        except Exception:
            return {'name': name, 'exists': False, 'check_method': 'exception_caught'}

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def _cmd_run(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        self._fsp.run()
        return {'status': 'completed'}

    def _cmd_run_sweep(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        self._fsp.runsweep(p['name'])
        return {'status': 'completed'}

    def _cmd_get_sweep_result(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        try:
            r = appCall(self._fsp, 'getsweepresult', [p['name']])
            return self._sanitize(r)
        except Exception as e:
            return {'error': str(e)[:300]}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_lum_array(self, value):
        """Convert Python numeric lists to numpy ndarray for appCall.

        Lumapi's appCall marshals list -> MATLAB cell array (wrong for
        setmaterial/setnamed numeric matrix args), but ndarray -> numeric
        matrix. Only numeric leaf lists are converted; strings, empty lists,
        and non-numeric lists pass through unchanged.
        """
        if isinstance(value, list) and len(value) > 0:
            try:
                import numpy as np
                arr = np.array(value)
                if arr.dtype.kind in ('i', 'f', 'u'):
                    return arr
            except Exception:
                pass
        return value

    def _sanitize(self, value):
        if value is None: return None
        if isinstance(value, (bool, int, float)):
            if isinstance(value, float) and value != value: return None
            return value
        if isinstance(value, str): return value
        if isinstance(value, (list, tuple)):
            return [self._sanitize(v) for v in value]
        if isinstance(value, dict):
            return {str(k): self._sanitize(v) for k, v in value.items()}
        try:
            import numpy as np
            if isinstance(value, np.ndarray):
                return value.flatten()[:1000].tolist()
        except ImportError: pass
        try: return float(value)
        except (TypeError, ValueError): pass
        return str(value)[:10000]

    def _ok(self, rid, result):
        return {'id': rid, 'result': result}

    def _error(self, rid, message):
        return {'id': rid, 'error': {'code': -1, 'message': message[:2000]}}


def main():
    bridge = FdtdBridge()
    sys.stdout.write(json.dumps({'ready': True}) + '\n')
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: request = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({'error': 'Invalid JSON'}) + '\n')
            sys.stdout.flush(); continue
        if request.get('method') == 'shutdown':
            bridge._cmd_close({}); break
        resp = bridge.handle(request)
        sys.stdout.write(json.dumps(resp, default=str) + '\n')
        sys.stdout.flush()
    sys.exit(0)

if __name__ == '__main__':
    main()
