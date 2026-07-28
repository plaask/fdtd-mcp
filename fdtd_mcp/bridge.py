# -*- coding: utf-8 -*-
"""
FDTD Bridge — JSON-RPC via stdin/stdout.

Runs on Lumerical embed Python 3.6.8.
Uses lumapi Python methods (appCall-backed) instead of eval() for reliability.
Uses _call_lsf as a unified entry point that auto-converts lists to ndarrays.
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
        self._method_map = {
            'ping': self._cmd_ping,
            'open': self._cmd_open,
            'close': self._cmd_close,
            'save': self._cmd_save,
            'new': self._cmd_new,
            'execute': self._cmd_execute,
            'execute_file': self._cmd_execute_file,
            'get_scene_info': self._cmd_get_scene_info,
            'get_model_variables': self._cmd_get_model_variables,
            'get_model_overview': self._cmd_get_model_overview,
            'model_info': self._cmd_get_model_overview,
            'get_object_info': self._cmd_get_object_info,
            'model_get': self._cmd_get_object_info,
            'model_add': self._cmd_model_add,
            'model_set': self._cmd_model_set,
            'model_delete': self._cmd_model_delete,
            'model_script': self._cmd_model_script,
            'get_script': self._cmd_get_script,
            'set_script': self._cmd_set_script,
            'get_parameters': self._cmd_get_object_info,
            'set_parameter': self._cmd_model_set,
            'get_sweep_info': self._cmd_get_sweep_info,
            'add_material': self._cmd_add_material,
            'set_material': self._cmd_set_material,
            'get_material': self._cmd_get_material,
            'material_delete': self._cmd_delete_material,
            'material_exists': self._cmd_material_exists,
            'sweep_add': self._cmd_sweep_add,
            'sweep_set': self._cmd_sweep_set,
            'sweep_delete': self._cmd_sweep_delete,
            'get_results': self._cmd_get_results,
            'result_list': self._cmd_result_list,
            'list_result_fields': self._cmd_list_result_fields,
            'get_result_data': self._cmd_result_get,
            'result_get': self._cmd_result_get,
            'get_result_file': self._cmd_get_result_file,
            'has_result': self._cmd_has_result,
            'run': self._cmd_run,
            'run_sweep': self._cmd_run_sweep,
            'get_sweep_result': self._cmd_get_sweep_result,
        }

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle(self, request):
        req_id = request.get('id')
        method = request.get('method', '')
        params = request.get('params', {})
        try:
            handler = self._method_map.get(method)
            if handler is None:
                return self._error(req_id, 'Unknown method: ' + method)
            return self._ok(req_id, handler(params))
        except Exception as e:
            tb = traceback.format_exc()
            if tb and tb.strip():
                msg = tb.strip().split('\n')[-1][:2000]
            else:
                msg = str(e)[:2000]
            return self._error(req_id, msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _cmd_ping(self, p):
        return 'pong'

    def _cmd_open(self, p):
        self._fsp = lumapi.FDTD(p['path'], hide=True)
        self._path = p['path']
        s = {}
        for k in ['dimension','x span','y span','z span','simulation time',
                  'mesh accuracy','x min bc','x max bc','y min bc','y max bc',
                  'z min bc','z max bc']:
            try:
                s[k] = self._fsp.getnamed('FDTD', k)
            except Exception:
                s[k] = None
        return {'status':'ok','path':p['path'],'summary':s}

    def _cmd_close(self, p):
        if self._fsp:
            self._fsp.close()
        self._fsp = None
        self._path = None
        return {'status':'closed'}

    def _cmd_save(self, p):
        if not self._fsp:
            raise RuntimeError('No open project')
        self._fsp.save(p['path'])
        self._path = p['path']
        return {'status':'ok','path':p['path']}

    def _cmd_new(self, p):
        """Create a blank FDTD project (no .fsp file needed).

        Optional FDTD region config: dimension, x/y/z span, simulation time, mesh accuracy.
        """
        if self._fsp:
            self._fsp.close()
        self._fsp = lumapi.FDTD(hide=True)
        self._path = None
        self._call_lsf('addfdtd')
        cfg = {}
        for k in ['dimension','x span','y span','z span','simulation time','mesh accuracy']:
            v = p.get(k)
            if v is not None:
                try:
                    self._fsp.setnamed('FDTD', k, v)
                    cfg[k] = v
                except Exception:
                    pass
        return {'status':'ok','config':cfg}

    # ------------------------------------------------------------------
    # execute — universal single-line tool (rewritten)
    # ------------------------------------------------------------------

    def _cmd_execute(self, p):
        """Execute LSF code. ?expr captures return value; everything else is transparent eval."""
        if not self._fsp:
            raise RuntimeError('No open project')
        code = p['code']

        # Guard: reject raw set("...script") calls -> use model_script tool
        if re.match(r'set\(\s*"(setup script|analysis script|script)"\s*,', code):
            return {
                'status': 'error',
                'message': 'Do NOT use execute() to set scripts. Use model_script(action="set", ...) instead.'
            }

        # ?expr: capture return value
        if code.startswith('?'):
            expr = code[1:].strip()
            m = re.match(r'(\w+)\((.+)\)', expr)
            if m:
                func_name = m.group(1)
                raw_args = m.group(2)
                args = []
                for a in re.findall(r'"([^"]*)"|\'([^\']*)\'|([^,]+)', raw_args):
                    arg = a[0] or a[1] or a[2].strip()
                    try:
                        arg = float(arg)
                    except ValueError:
                        pass
                    args.append(arg)
                result = self._call_lsf(func_name, *args)
                return {'status': 'ok', 'result': self._sanitize(result)}
            # Bare name: getv, fallback to getnamed
            try:
                result = self._fsp.getv(expr)
            except Exception:
                result = self._call_lsf('getnamed', '::model', expr)
            return {'status': 'ok', 'result': self._sanitize(result)}

        # Transparent eval (NO parsing, NO splitting on semicolons)
        try:
            self._fsp.eval(code)
            return {'status': 'ok'}
        except Exception as e:
            raise RuntimeError(str(e)[:500])

    def _cmd_execute_file(self, p):
        """Run a .lsf script file."""
        if not self._fsp:
            raise RuntimeError('No open project')
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
        Uses _call_lsf for getid (avoids eval-assignment + getv pattern).
        """
        # Navigate to target scope from root
        self._fsp.eval('groupscope("::model");')
        if scope != '::model':
            parts = scope.replace('::model::', '').split('::')
            for part in parts:
                if part:
                    self._fsp.eval('groupscope("' + part + '");')
        self._fsp.eval('selectall();')
        ids_raw = self._call_lsf('getid')
        ids_str = str(ids_raw) if ids_raw else ''
        if not ids_str:
            return
        for obj_id in ids_str.split('\n'):
            obj_id = obj_id.strip()
            if not obj_id or obj_id in seen:
                continue
            seen.add(obj_id)
            try:
                t = self._call_lsf('getnamed', obj_id, 'type')
                obj = {'name': obj_id, 'type': str(t)}
                for prop in prop_list:
                    try:
                        obj[prop] = self._sanitize(
                            self._call_lsf('getnamed', obj_id, prop))
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
        if not self._fsp:
            raise RuntimeError('No open project')
        enabled_only = bool(p.get('enabled_only', False))

        objects = []
        seen = set()
        self._traverse('::model', self._SCENE_PROPS, enabled_only, objects, seen)

        fdtd = {}
        for k in ['dimension','x span','y span','z span','simulation time','mesh accuracy']:
            try:
                fdtd[k] = self._fsp.getnamed('FDTD', k)
            except Exception:
                pass

        return {'objects': objects, 'fdtd_summary': fdtd, 'object_count': len(objects)}

    # ------------------------------------------------------------------
    # Model variables (P0-2) + Model overview (P0-1)
    # ------------------------------------------------------------------

    def _enum_model_variables(self, scope):
        """Enumerate model/GUI variables by scanning scripts for addvar/adduserprop."""
        names = []
        try:
            scr = self._cmd_get_script({'name': scope})
            text = (scr.get('setup_script', '') or '') + '\n' + (scr.get('analysis_script', '') or '')
            var_names = re.findall(r'addvar\(\s*"(\w+)"', text)
            prop_names = re.findall(r'adduserprop\(\s*"(\w+)"', text)
            names = list(set(var_names + prop_names))
        except Exception:
            pass

        # Resolve values via getnamed (getv as fallback)
        out = {}
        for vn in names:
            if not vn:
                continue
            val = None
            try:
                val = self._call_lsf('getnamed', scope, vn)
                val = self._sanitize(val)
            except Exception:
                try:
                    val = self._fsp.getv(vn)
                    val = self._sanitize(val)
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
            raw = self._call_lsf('getmaterial')
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
    # Single object info (P0-3) — enhanced for model_get
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
        """Get full properties of ONE named object with type discriminator.
        For groups and ::model, also returns scripts and variables.
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        name = self._resolve_name(p['name'])
        out = {'name': name}
        try:
            t = self._call_lsf('getnamed', name, 'type')
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
                    self._call_lsf('getnamed', name, prop))
            except Exception:
                pass
        # For groups and ::model, also include scripts and variables
        t_str = out.get('type', str(t)) if 'type' in out else ''
        if name == '::model' or t_str in ('Analysis Group', 'Structure Group'):
            try:
                scripts = self._cmd_get_script({'name': name})
                out['scripts'] = scripts
            except Exception:
                pass
            if name == '::model':
                try:
                    variables = self._enum_model_variables('::model')
                    if variables:
                        out['variables'] = variables
                except Exception:
                    pass
        return out

    # ------------------------------------------------------------------
    # Model add / delete / set
    # ------------------------------------------------------------------

    def _cmd_model_add(self, p):
        """Add any object type to the model tree."""
        if not self._fsp:
            raise RuntimeError('No open project')
        obj_type = p['type']
        name = p.get('name', None)
        properties = p.get('properties', {})
        scope = p.get('scope', '::model')

        # Map type string to Lumerical add* function
        TYPE_MAP = {
            'rectangle': 'addrect', 'circle': 'addcircle', 'ring': 'addring',
            'polygon': 'addpoly', 'sphere': 'addsphere', 'pyramid': 'addpyramid',
            'triangle': 'addtriangle', 'waveguide': 'addwaveguide',
            'fdtd': 'addfdtd', 'mesh': 'addmesh',
            'dipole': 'adddipole', 'tfsf': 'addtfsf', 'plane': 'addplane',
            'gaussian': 'addgaussian', 'mode_source': 'addmode',
            'power_monitor': 'addpower', 'dft_monitor': 'adddftmonitor',
            'index_monitor': 'addindex', 'field_monitor': 'addfield',
            'movie_monitor': 'addmovie',
            'structure_group': 'addstructuregroup',
            'analysis_group': 'addanalysisgroup',
        }
        func = TYPE_MAP.get(obj_type)
        if not func:
            raise RuntimeError('Unknown object type: ' + obj_type +
                               '. Use one of: ' + ', '.join(sorted(TYPE_MAP.keys())))

        # Navigate scope
        if scope != '::model':
            self._fsp.eval('groupscope("' + scope + '");')

        # Create object (Lumerical add* commands auto-select after creation, no name arg)
        self._call_lsf(func)
        # Get auto-generated name from the now-selected object
        try:
            created_name = str(self._call_lsf('get', 'name'))
        except Exception:
            created_name = obj_type

        # Rename if user specified a name
        if name and created_name != name:
            self._call_lsf('set', 'name', name)
            created_name = name

        # Set initial properties
        if properties:
            for prop, val in properties.items():
                try:
                    self._call_lsf('setnamed', created_name, prop, val)
                except Exception:
                    pass

        # Return to root
        if scope != '::model':
            self._fsp.eval('groupscope("::model");')

        return {'status': 'ok', 'name': created_name, 'type': obj_type}

    def _cmd_model_delete(self, p):
        """Delete an object by name."""
        if not self._fsp:
            raise RuntimeError('No open project')
        name = self._resolve_name(p['name'])
        self._fsp.eval('select("' + name + '"); delete();')
        return {'status': 'ok', 'deleted': name}

    def _cmd_model_set(self, p):
        """Set a property/parameter on an object, auto-handling different object types.

        Args:
            name: target object name (default '::model')
            property: property/parameter name (single)
            value: new value (single)
            properties: dict of property->value pairs (batch)
        Supports old set_parameter convention (object + name) as well.
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        # Support batch mode via 'properties' dict
        if 'properties' in p:
            obj = self._resolve_name(p.get('name', '::model'))
            props = p['properties']
            set_count = 0
            for prop, value in props.items():
                try:
                    if obj == '::model':
                        try:
                            self._call_lsf('setnamed', obj, prop, value)
                        except Exception:
                            try:
                                self._call_lsf('addvar', obj, prop, value)
                            except Exception:
                                self._call_lsf('setvar', obj, prop, value)
                    else:
                        try:
                            obj_type = str(self._call_lsf('getnamed', obj, 'type'))
                            if 'Analysis' in obj_type:
                                try:
                                    self._call_lsf('setnamed', obj, prop, value)
                                except Exception:
                                    self._call_lsf('addanalysisprop', obj, prop)
                                    self._call_lsf('setnamed', obj, prop, value)
                            elif 'Structure' in obj_type:
                                try:
                                    self._call_lsf('setnamed', obj, prop, value)
                                except Exception:
                                    self._call_lsf('adduserprop', obj, prop)
                                    self._call_lsf('setnamed', obj, prop, value)
                            else:
                                self._call_lsf('setnamed', obj, prop, value)
                        except Exception:
                            self._call_lsf('setnamed', obj, prop, value)
                    set_count += 1
                except Exception:
                    pass
            return {'status': 'ok', 'object': obj, 'properties_set': set_count}
        # Support both new API (name=object, property=prop) and old API (name=prop, object=target)
        if 'object' in p:
            obj = p['object']
            prop = p['name']
        else:
            obj = p.get('name', '::model')
            prop = p.get('property', p.get('name'))
        value = p['value']

        # Handle different object types
        if obj == '::model':
            # Try setnamed first, fall back to addvar/setvar
            try:
                self._call_lsf('setnamed', obj, prop, value)
            except Exception:
                try:
                    self._call_lsf('addvar', obj, prop, value)
                except Exception:
                    self._call_lsf('setvar', obj, prop, value)
        else:
            try:
                obj_type = str(self._call_lsf('getnamed', obj, 'type'))
                if 'Analysis' in obj_type:
                    try:
                        self._call_lsf('setnamed', obj, prop, value)
                    except Exception:
                        self._call_lsf('addanalysisprop', obj, prop)
                        self._call_lsf('setnamed', obj, prop, value)
                elif 'Structure' in obj_type:
                    try:
                        self._call_lsf('setnamed', obj, prop, value)
                    except Exception:
                        self._call_lsf('adduserprop', obj, prop)
                        self._call_lsf('setnamed', obj, prop, value)
                else:
                    self._call_lsf('setnamed', obj, prop, value)
            except Exception:
                self._call_lsf('setnamed', obj, prop, value)

        return {'status': 'ok', 'object': obj, 'property': prop, 'value': value}

    # ------------------------------------------------------------------
    # Script editing
    # ------------------------------------------------------------------

    def _cmd_get_script(self, p):
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p.get('name', '::model')
        result = {}
        self._fsp.select(name)

        # ::model + Analysis Group have setup/analysis scripts.
        # Structure Group has a single 'script' property.
        if name == '::model':
            props = [('setup script','setup'), ('analysis script','analysis')]
        else:
            obj_type = str(self._call_lsf('get', 'type'))
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
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
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

    def _cmd_set_script(self, p):
        """Set setup/analysis script on an object. Supports multi-line content.

        ::model and Analysis Group use 'setup script' / 'analysis script'.
        Structure Group uses a single 'script' property.
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p.get('name', '::model')
        stype = p['type']  # 'setup' or 'analysis'
        content = p['content']

        self._fsp.select(name)
        if name == '::model':
            script_prop = stype + ' script'
        else:
            obj_type = str(self._call_lsf('get', 'type'))
            if obj_type == 'Analysis Group':
                script_prop = stype + ' script'
            else:
                script_prop = 'script'

        self._call_lsf('set', script_prop, content)
        return {'status': 'ok', 'object': name, 'script_type': script_prop}

    def _cmd_model_script(self, p):
        """Unified script access. Action: 'get' (default) or 'set'.

        For 'get': returns scripts for the named object.
        For 'set': requires 'type' ('setup'/'analysis') and 'content'.
        """
        action = p.get('action', 'get')
        if action == 'get':
            return self._cmd_get_script(p)
        elif action == 'set':
            return self._cmd_set_script(p)
        else:
            raise RuntimeError('Unknown action: "' + action + '". Use "get" or "set".')

    # ------------------------------------------------------------------
    # Sweep info
    # ------------------------------------------------------------------

    def _cmd_get_sweep_info(self, p):
        """Get sweep configuration."""
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p['name']
        info = {'name': name}
        try:
            result = self._call_lsf('getsweepresult', name)
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
    # Sweep management (new)
    # ------------------------------------------------------------------

    def _cmd_sweep_add(self, p):
        """Add a new sweep with optional parameters and results."""
        if not self._fsp:
            raise RuntimeError('No open project')
        sweep_type = p.get('type', 0)
        name = p.get('name', 'sweep')
        parameters = p.get('parameters', [])
        results = p.get('results', [])

        # Create sweep
        self._call_lsf('addsweep', sweep_type)
        self._call_lsf('setsweep', 'sweep', 'name', name)

        # Configure parameters
        for param in parameters:
            self._call_lsf('addsweepparameter', name, param)

        # Configure results
        self._call_lsf('insertsweep', name)
        for result in results:
            self._call_lsf('addsweepresult', name, result)

        return {'status': 'ok', 'name': name, 'type': sweep_type,
                'parameter_count': len(parameters), 'result_count': len(results)}

    def _cmd_sweep_set(self, p):
        """Set sweep properties."""
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p['name']
        for prop, val in p.get('properties', {}).items():
            self._call_lsf('setsweep', name, prop, val)
        return {'status': 'ok', 'name': name}

    def _cmd_sweep_delete(self, p):
        """Delete a sweep by name."""
        if not self._fsp:
            raise RuntimeError('No open project')
        self._call_lsf('deletesweep', p['name'])
        return {'status': 'ok', 'deleted': p['name']}

    # ------------------------------------------------------------------
    # Materials
    # ------------------------------------------------------------------

    def _cmd_add_material(self, p):
        """Create a new material from a model type template.

        Args:
            type: material model type, e.g. 'Sampled 3D data', 'Dielectric', 'Drude'
        Returns the auto-generated material name.
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        mat_type = p.get('type', 'Sampled 3D data')
        name = self._call_lsf('addmaterial', mat_type)
        return {'status': 'ok', 'name': str(name), 'type': mat_type}

    def _cmd_set_material(self, p):
        """Set a material property.

        Args:
            name: material name
            property: property name, e.g. 'Refractive Index', 'nk data', 'mesh order'
            value: property value (number, string, or array for tabulated nk data)
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p['name']
        prop = p['property']
        value = p['value']
        self._call_lsf('setmaterial', name, prop, value)
        return {'status': 'ok', 'name': name, 'property': prop}

    def _cmd_get_material(self, p):
        """Read material properties.

        Args:
            name: material name
            property: optional property name; if omitted, lists all property names
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p['name']
        prop = p.get('property', None)
        if prop:
            result = self._call_lsf('getmaterial', name, prop)
        else:
            result = self._call_lsf('getmaterial', name)
        return self._sanitize(result)

    def _cmd_delete_material(self, p):
        """Delete a material by name."""
        if not self._fsp:
            raise RuntimeError('No open project')
        self._call_lsf('deletematerial', p['name'])
        return {'status': 'ok', 'deleted': p['name']}

    def _cmd_material_exists(self, p):
        """Check if a material exists."""
        if not self._fsp:
            raise RuntimeError('No open project')
        exists = self._call_lsf('materialexists', p['name'])
        return {'name': p['name'], 'exists': bool(exists)}

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
        'Px', 'Py', 'Pz', 'sigma', 'S', 'Sx', 'Sy', 'Sz',
        'n', 'k', 'epsilon', 'temp', 'heat',
        'T_forward', 'T_backward', 'R_forward', 'R_backward',
    ]

    def _cmd_get_results(self, p):
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p.get('name', 'FDTD')
        try:
            r = self._call_lsf('getresult', name)
            return {'name': name, 'results': str(r).split('\n') if r else []}
        except Exception as e:
            return {'name': name, 'results': [], 'error': str(e)[:200]}

    def _cmd_result_list(self, p):
        """List result datasets AND probe field names in one call.

        Returns available result datasets for a monitor/object,
        with field names probed for each dataset.
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p.get('name', 'FDTD')

        # Get result datasets
        try:
            r = self._call_lsf('getresult', name)
            datasets = str(r).split('\n') if r else []
        except Exception as e:
            return {'name': name, 'datasets': [], 'error': str(e)[:200]}

        # Probe fields for each dataset
        datasets_with_fields = []
        for dataset in datasets:
            dataset = dataset.strip()
            if not dataset:
                continue
            try:
                self._fsp.eval('_br_rl = getresult("' + name + '","' + dataset + '");')
                fields = []
                for cf in self._COMMON_RESULT_FIELDS:
                    try:
                        v = self._fsp.getv('_br_rl.' + cf)
                        if v is not None:
                            fields.append(cf)
                    except Exception:
                        pass
                self._fsp.eval('clear(_br_rl);')
                datasets_with_fields.append({'data': dataset, 'fields': fields,
                                             'field_count': len(fields)})
            except Exception:
                datasets_with_fields.append({'data': dataset,
                                             'error': 'could not load'})

        return {'monitor': name, 'datasets': datasets_with_fields,
                'dataset_count': len(datasets_with_fields)}

    def _cmd_list_result_fields(self, p):
        """List field names in a result dataset WITHOUT fetching full data.

        Probes common known field names via getv(dataset.field) to discover
        which fields are accessible. Returns just the field names (no data)
        so LLM can preview monitor structure before calling result_get.
        """
        if not self._fsp:
            raise RuntimeError('No open project')
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

    def _cmd_result_get(self, p):
        """Fetch result data for specific fields from a monitor.

        Unlike get_result_data, this requires explicit 'fields' parameter
        and only returns the requested fields (no auto-probing).

        Args:
            monitor: monitor name
            data: optional result data name (e.g. 'T', 'R')
            fields: list of field names to retrieve (REQUIRED)
            cap: max elements per field (default 2000)
        """
        if not self._fsp:
            raise RuntimeError('No open project')
        monitor = p['monitor']
        data = p.get('data', '')
        fields = p.get('fields', None)
        if not fields:
            raise RuntimeError('fields parameter is required. Use result_list first to discover available fields.')
        cap = int(p.get('cap', 2000))

        # Load result
        if data:
            self._fsp.eval('_br_d = getresult("' + monitor + '","' + data + '");')
        else:
            self._fsp.eval('_br_d = getresult("' + monitor + '");')

        # Extract requested fields only
        values = {}
        truncated = {}
        for field in fields:
            try:
                v = self._fsp.getv('_br_d.' + field)
                sanitized = self._sanitize(v, cap)
                values[field] = sanitized
                if isinstance(sanitized, dict) and sanitized.get('truncated'):
                    truncated[field] = sanitized.get('length')
            except Exception as e:
                values[field] = {'error': str(e)[:200]}

        self._fsp.eval('clear(_br_d);')
        return {'monitor': monitor, 'data': data, 'values': values,
                'fields_requested': fields, 'truncated': truncated}

    def _cmd_get_result_file(self, p):
        if not self._fsp:
            raise RuntimeError('No open project')
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
        if not self._fsp:
            raise RuntimeError('No open project')
        name = p['name']
        # First try direct lumapi call (if it exists)
        for fn in ('haveresult', 'findresult', 'hasresult'):
            try:
                r = self._call_lsf(fn, name)
                if r is not None:
                    return {'name': name, 'exists': bool(r), 'check_method': fn}
            except Exception:
                pass
        # Fallback: try getresult and see if it succeeds
        try:
            r = self._call_lsf('getresult', name)
            return {'name': name, 'exists': r is not None and str(r).strip() != '',
                    'check_method': 'getresult_attempt'}
        except Exception:
            return {'name': name, 'exists': False, 'check_method': 'exception_caught'}

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def _cmd_run(self, p):
        if not self._fsp:
            raise RuntimeError('No open project')
        self._fsp.run()
        return {'status': 'completed'}

    def _cmd_run_sweep(self, p):
        if not self._fsp:
            raise RuntimeError('No open project')
        self._fsp.runsweep(p['name'])
        return {'status': 'completed'}

    def _cmd_get_sweep_result(self, p):
        if not self._fsp:
            raise RuntimeError('No open project')
        try:
            r = self._call_lsf('getsweepresult', p['name'])
            return self._sanitize(r)
        except Exception as e:
            return {'error': str(e)[:300]}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_name(self, name):
        """Resolve a possibly-short object name to its full scoped path.

        Tries bare name first, then '::model::name' as fallback.
        Returns the resolved scoped name if found, or the original name.
        """
        if '::' in name:
            return name  # already scoped
        # Try bare name
        try:
            self._call_lsf('getnamed', name, 'type')
            return name
        except Exception:
            pass
        # Try ::model:: prefix
        scoped = '::model::' + name
        try:
            self._call_lsf('getnamed', scoped, 'type')
            return scoped
        except Exception:
            pass
        return name  # fallback: return original, let caller handle error

    def _call_lsf(self, func_name, *args):
        """Unified Lumerical function call. Auto-converts list->ndarray for correct MATLAB matrix marshaling."""
        converted = []
        for a in args:
            if isinstance(a, list) and len(a) > 0:
                try:
                    import numpy as np
                    arr = np.array(a)
                    if arr.dtype.kind in ('i', 'f', 'u'):
                        a = arr
                except Exception:
                    pass
            converted.append(a)
        return appCall(self._fsp, func_name, converted)

    def _sanitize(self, value, cap=1000):
        """Sanitize values for JSON serialization, preserving array shape and truncation info."""
        if value is None:
            return None
        if isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return None if value != value else value
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            total = len(value)
            truncated = total > cap
            items = [self._sanitize(v, cap) for v in (value[:cap] if truncated else value)]
            if truncated:
                return {'data': items, 'length': total, 'truncated': True}
            return items
        if isinstance(value, dict):
            return {str(k): self._sanitize(v, cap) for k, v in value.items()}
        try:
            import numpy as np
            if isinstance(value, np.ndarray):
                flat = value.flatten()
                total = len(flat)
                truncated = total > cap
                data = flat[:cap].tolist() if truncated else flat.tolist()
                result = {'data': data, 'shape': list(value.shape), 'length': total}
                if truncated:
                    result['truncated'] = True
                return result
        except ImportError:
            pass
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
        s = str(value)
        return s[:10000] if len(s) > 10000 else s

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
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({'error': 'Invalid JSON'}) + '\n')
            sys.stdout.flush()
            continue
        if request.get('method') == 'shutdown':
            bridge._cmd_close({})
            break
        resp = bridge.handle(request)
        sys.stdout.write(json.dumps(resp, default=str) + '\n')
        sys.stdout.flush()
    sys.exit(0)


if __name__ == '__main__':
    main()
