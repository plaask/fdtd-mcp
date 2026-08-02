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

# Unified error channel: handlers raise BridgeError; handle() turns it into the
# single {'error': {code, message}} shape. Generic exceptions are the second,
# traceback-derived channel (used for unexpected engine errors).
class BridgeError(RuntimeError):
    pass

_ERR_MSG = 300          # cap for error-message strings
_TRUNC = 1000           # default per-array element cap for _sanitize

# LSF commands that start a solve; executing any of these on an unsaved project
# pops an invisible 'Save As' dialog that wedges the hidden engine.
_RUN_COMMAND = re.compile(r'(?<![A-Za-z_])(run|runsweep|runanalysis|runsweepanalysis|runmany)\b')


class FdtdBridge(object):

    def __init__(self):
        self._fsp = None
        self._path = None
        self._tmp_dir = os.environ.get('TEMP', os.environ.get('TMP', '/tmp'))
        # Derive the dispatch table from dispatch.json; fail fast if a
        # referenced method has no _cmd_<method> handler on this class.
        self._method_map = {}
        for _tool, _method in _DISPATCH.items():
            _handler = getattr(self, '_cmd_' + _method, None)
            if _handler is None:
                raise RuntimeError(
                    'dispatch.json references missing handler: _cmd_' + _method)
            self._method_map[_method] = _handler

    def _require_project(self):
        """Reject a handler call when no FDTD project is open."""
        if not self._fsp:
            raise BridgeError('No open project')

    def _select_by_name(self, name):
        """Select an object, falling back to the ::model:: scoped path (bare
        names often fail select() on v202 even when they resolve for getnamed)."""
        try:
            self._fsp.select(name)
        except Exception:
            if name.startswith('::'):
                raise
            self._fsp.select('::model::' + name)

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
        except BridgeError as e:
            return self._error(req_id, str(e))
        except Exception as e:
            tb = traceback.format_exc()
            if tb and tb.strip():
                msg = tb.strip().split('\n')[-1][:_ERR_MSG]
            else:
                msg = str(e)[:_ERR_MSG]
            return self._error(req_id, msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _cmd_session_open(self, p):
        if self._fsp:
            try:
                self._fsp.close()
            except Exception:
                pass
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

    def _cmd_session_close(self, p):
        if self._fsp:
            self._fsp.close()
        self._fsp = None
        self._path = None
        return {'status':'closed'}

    def _cmd_session_save(self, p):
        self._require_project()
        path = p.get('path') or self._path
        if not path:
            raise RuntimeError(
                'Project has no current file path. Pass path= to session_save.')
        self._fsp.save(path)
        self._path = path
        return {'status':'ok','path':path}

    def _cmd_session_new(self, p):
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
            v = p.get(k, p.get(k.replace(' ', '_')))
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
        self._require_project()
        code = p['code']

        # Guard: reject set("...script") anywhere in the code (single or double
        # quotes) — script editing must go through model_script.
        if re.search(r'\bset\(\s*[\'"]?(setup script|analysis script|script)[\'"]?\s*,', code):
            raise BridgeError(
                'Do NOT use execute() to set scripts. '
                'Use model_script(action="set", ...) instead.')

        # ?expr: capture return value
        if code.startswith('?'):
            expr = code[1:].strip()
            m = re.match(r'(\w+)\((.+)\)', expr)
            if m:
                func_name = m.group(1)
                raw_args = m.group(2)
                args = []
                for a in re.findall(r'"([^"]*)"|\'([^\']*)\'|([^,\s]+)', raw_args):
                    arg = a[0] or a[1] or a[2]
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
        if _RUN_COMMAND.search(code):
            self._ensure_project_saved()
        try:
            self._fsp.eval(code)
            return {'status': 'ok'}
        except Exception as e:
            raise BridgeError(str(e)[:_ERR_MSG])

    def _cmd_execute_file(self, p):
        """Run a .lsf script file."""
        self._require_project()
        self._ensure_project_saved()  # the file may contain a run/runsweep command
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

    # ------------------------------------------------------------------
    # Model variables + model overview
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

    def _cmd_model_info(self, p):
        """One-call self-introspection: objects + variables + materials + FDTD summary.

        Designed to stop the LLM from skipping steps — everything needed for
        modeling + script writing in one dict. Scripts pulled separately via get_script.
        """
        self._require_project()
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

    def _cmd_model_get(self, p):
        """Get full properties of ONE named object with type discriminator.
        For groups and ::model, also returns scripts and variables.
        """
        self._require_project()
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
            raise BridgeError('Object not found: %s (%s)' % (name, str(e)[:_ERR_MSG]))
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
        self._require_project()
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
            'power_monitor': 'addpower', 'dft_monitor': 'addpower',
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
        self._require_project()
        name = self._resolve_name(p['name'])
        self._fsp.eval('select("' + name + '"); delete();')
        return {'status': 'ok', 'deleted': name}

    def _set_object_prop(self, obj, prop, value):
        """Set one property on an object, handling ::model and groups.

        ::model         -> setnamed, falling back to addvar/setvar.
        Analysis Group  -> setnamed, falling back to addanalysisprop.
        Structure Group -> setnamed, falling back to adduserprop.
        Anything else   -> setnamed directly.

        Returns True if the property was set, False if every attempt failed
        (e.g. the property does not exist on the object).
        """
        if obj == '::model':
            for call in (('setnamed', obj, prop, value),
                         ('addvar', obj, prop, value),
                         ('setvar', obj, prop, value)):
                try:
                    self._call_lsf(*call)
                    return True
                except Exception:
                    continue
            return False
        try:
            obj_type = str(self._call_lsf('getnamed', obj, 'type'))
        except Exception:
            try:
                self._call_lsf('setnamed', obj, prop, value)
                return True
            except Exception:
                return False
        if 'Analysis' in obj_type:
            try:
                self._call_lsf('setnamed', obj, prop, value)
                return True
            except Exception:
                try:
                    self._call_lsf('addanalysisprop', obj, prop)
                    self._call_lsf('setnamed', obj, prop, value)
                    return True
                except Exception:
                    return False
        if 'Structure' in obj_type:
            try:
                self._call_lsf('setnamed', obj, prop, value)
                return True
            except Exception:
                try:
                    self._call_lsf('adduserprop', obj, prop)
                    self._call_lsf('setnamed', obj, prop, value)
                    return True
                except Exception:
                    return False
        try:
            self._call_lsf('setnamed', obj, prop, value)
            return True
        except Exception:
            return False

    def _cmd_model_set(self, p):
        """Set properties on an object (batch via 'properties', or single via
        'property'/'value'). Object type is auto-detected (see _set_object_prop).
        """
        self._require_project()
        if 'properties' in p:
            obj = self._resolve_name(p.get('name', '::model'))
            set_count = 0
            failed = []
            for prop, value in p['properties'].items():
                if self._set_object_prop(obj, prop, value):
                    set_count += 1
                else:
                    failed.append(prop)
            result = {'status': 'ok', 'object': obj, 'properties_set': set_count}
            if failed:
                result['properties_failed'] = failed
                result['hint'] = (
                    'The failed properties likely do not exist on the object. '
                    'Group/::model user properties must be declared in the object\'s '
                    'script first (model_script), e.g. adduserprop("name", type_code, value). '
                    'type_code is an integer: 0=number, 1=text, 2=length, 3=time, '
                    '4=frequency, 5=material (addanalysisprop mirrors this).')
            return result
        # Support both new API (name=object, property=prop) and old API (name=prop, object=target)
        if 'object' in p:
            obj = p['object']
            prop = p['name']
        else:
            obj = p.get('name', '::model')
            prop = p.get('property', p.get('name'))
        value = p['value']
        self._set_object_prop(obj, prop, value)
        return {'status': 'ok', 'object': obj, 'property': prop, 'value': value}

    # ------------------------------------------------------------------
    # Script editing
    # ------------------------------------------------------------------

    def _cmd_get_script(self, p):
        self._require_project()
        name = p.get('name', '::model')
        result = {}
        self._select_by_name(name)

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
                result[key+'_error'] = str(e)[:_ERR_MSG]
        return result

    def _cmd_set_script(self, p):
        """Set setup/analysis script on an object. Supports multi-line content.

        ::model and Analysis Group use 'setup script' / 'analysis script'.
        Structure Group uses a single 'script' property.
        """
        self._require_project()
        name = p.get('name', '::model')
        stype = p.get('script_type') or p.get('type')  # 'setup' or 'analysis'
        content = p['content']

        self._select_by_name(name)
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

    def _cmd_sweep_get(self, p):
        """Get sweep configuration."""
        self._require_project()
        name = p['name']
        info = {'name': name}
        try:
            result = self._call_lsf('getsweepresult', name)
            info['exists'] = True
            info['has_results'] = True
            info['result_sample'] = str(result)[:_ERR_MSG] if result else 'empty'
        except Exception as e:
            err = str(e)
            if 'no results' in err.lower():
                info['exists'] = True
                info['has_results'] = False
                info['note'] = 'Sweep defined but not yet run'
            else:
                info['exists'] = False
                info['error'] = err[:_ERR_MSG]
        return info

    # ------------------------------------------------------------------
    # Sweep management
    # ------------------------------------------------------------------

    def _sweep_param_struct(self, param):
        """Build the Lumerical sweep-parameter struct from a tool param dict.

        Verified against the real engine (v202): addsweepparameter takes a
        struct with mixed-case keys (Name / parameter / type / start / stop);
        lumapi's appCall converts a Python dict to that struct. 'points' is a
        sweep-level property, not per-parameter.

        NOTE: the per-parameter 'type' field is the parameter VALUE data type
        (numeric code, or the string 'Number'), NOT the sampling type. The
        tool's 'type':'Linear'/'Logarithmic' is the sampling intent — the
        sweep samples start..stop with the sweep's number of points (Linear by
        default) — so sampling strings are dropped and numeric/'Number' values
        pass through.
        """
        s = {'Name': param.get('name')}
        for key in ('parameter', 'start', 'stop'):
            if key in param and param[key] is not None:
                s[key] = param[key]
        t = param.get('type')
        if isinstance(t, int) and not isinstance(t, bool):
            s['type'] = t
        elif isinstance(t, str) and t.strip().lower() == 'number':
            s['type'] = t.strip()
        return s

    def _sweep_result_struct(self, result):
        s = {'Name': result.get('name')}
        if result.get('result'):
            s['Result'] = result['result']
        return s

    def _cmd_sweep_add(self, p):
        """Add a new sweep with optional parameters and results."""
        self._require_project()
        sweep_type = p.get('type', 0)
        name = p.get('name', 'sweep')
        parameters = p.get('parameters', [])
        results = p.get('results', [])

        # addsweep returns the created sweep path (e.g. '::sweep', '::sweep1' —
        # the auto-name increments when sweeps already exist). Sweeps are NOT
        # model-tree objects: rename via the sweep API, not get/set on the
        # selection (addsweep does not auto-select).
        created_raw = self._call_lsf('addsweep', sweep_type)
        created_name = str(created_raw).split('::')[-1] if created_raw else 'sweep'
        if name:
            self._call_lsf('setsweep', created_name, 'name', name)

        param_errors = []
        result_errors = []
        for param in parameters:
            if not isinstance(param, dict) or not param.get('name'):
                param_errors.append('invalid parameter dict: ' + str(param))
                continue
            pname = param['name']
            try:
                self._call_lsf('addsweepparameter', name, self._sweep_param_struct(param))
            except Exception as e:
                param_errors.append(pname + ': add: ' + str(e)[:_ERR_MSG])
            if param.get('points'):
                try:
                    self._call_lsf('setsweep', name, 'number of points', param['points'])
                except Exception as e:
                    param_errors.append(pname + '.points: ' + str(e)[:_ERR_MSG])

        for result in results:
            if not isinstance(result, dict) or not result.get('name'):
                result_errors.append('invalid result dict: ' + str(result))
                continue
            rname = result['name']
            try:
                self._call_lsf('addsweepresult', name, self._sweep_result_struct(result))
            except Exception as e:
                result_errors.append(rname + ': add: ' + str(e)[:_ERR_MSG])

        return {'status': 'ok', 'name': name, 'type': sweep_type,
                'parameter_count': len(parameters), 'result_count': len(results),
                'parameter_errors': param_errors, 'result_errors': result_errors}

    def _cmd_sweep_set(self, p):
        """Set sweep properties."""
        self._require_project()
        name = p['name']
        for prop, val in p.get('properties', {}).items():
            self._call_lsf('setsweep', name, prop, val)
        return {'status': 'ok', 'name': name}

    def _cmd_sweep_delete(self, p):
        """Delete a sweep by name."""
        self._require_project()
        self._call_lsf('deletesweep', p['name'])
        return {'status': 'ok', 'deleted': p['name']}

    # ------------------------------------------------------------------
    # Materials
    # ------------------------------------------------------------------

    def _cmd_material_add(self, p):
        """Create a new material from a model type template.

        Args:
            type: material model type, e.g. 'Sampled 3D data', 'Dielectric', 'Drude'
        Returns the auto-generated material name.
        """
        self._require_project()
        mat_type = p.get('type', 'Sampled 3D data')
        name = self._call_lsf('addmaterial', mat_type)
        return {'status': 'ok', 'name': str(name), 'type': mat_type}

    def _cmd_material_set(self, p):
        """Set a material property.

        Args:
            name: material name
            property: property name, e.g. 'Refractive Index', 'nk data', 'mesh order'
            value: property value (number, string, or array for tabulated nk data)
        """
        self._require_project()
        name = p['name']
        prop = p['property']
        value = p['value']
        self._call_lsf('setmaterial', name, prop, value)
        return {'status': 'ok', 'name': name, 'property': prop}

    def _cmd_material_get(self, p):
        """Read material properties.

        Args:
            name: material name
            property: optional property name; if omitted, lists all property names
        """
        self._require_project()
        name = p['name']
        prop = p.get('property', None)
        if prop:
            result = self._call_lsf('getmaterial', name, prop)
        else:
            result = self._call_lsf('getmaterial', name)
        return self._sanitize(result)

    def _cmd_material_delete(self, p):
        """Delete a material by name."""
        self._require_project()
        self._call_lsf('deletematerial', p['name'])
        return {'status': 'ok', 'deleted': p['name']}

    def _cmd_material_exists(self, p):
        """Check if a material exists."""
        self._require_project()
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

    def _cmd_result_list(self, p):
        """List result datasets AND probe field names in one call.

        Returns available result datasets for a monitor/object,
        with field names probed for each dataset.
        """
        self._require_project()
        name = p.get('monitor') or p.get('name') or 'FDTD'

        # Get result datasets
        try:
            r = self._call_lsf('getresult', name)
            datasets = str(r).split('\n') if r else []
        except Exception as e:
            return {'name': name, 'datasets': [], 'error': str(e)[:_ERR_MSG]}

        # Probe fields for each dataset
        datasets_with_fields = []
        for dataset in datasets:
            dataset = dataset.strip()
            if not dataset:
                continue
            try:
                self._fsp.eval('brrl = getresult("' + name + '","' + dataset + '");')
                fields = []
                for cf in self._COMMON_RESULT_FIELDS:
                    try:
                        v = self._fsp.getv('brrl.' + cf)
                        if v is not None:
                            fields.append(cf)
                    except Exception:
                        pass
                self._fsp.eval('clear(brrl);')
                datasets_with_fields.append({'data': dataset, 'fields': fields,
                                             'field_count': len(fields)})
            except Exception:
                datasets_with_fields.append({'data': dataset,
                                             'error': 'could not load'})

        return {'monitor': name, 'datasets': datasets_with_fields,
                'dataset_count': len(datasets_with_fields)}

    def _cmd_result_get(self, p):
        """Fetch result data for specific fields from a monitor.

        Args:
            monitor: monitor name
            data: optional result data name (e.g. 'T', 'R')
            fields: list of field names to retrieve (REQUIRED)
            cap: max elements per field (default 2000)
        """
        self._require_project()
        monitor = p['monitor']
        data = p.get('data', '')
        fields = p.get('fields', None)
        if not fields:
            raise RuntimeError('fields parameter is required. Use result_list first to discover available fields.')
        cap = int(p.get('cap', 2000))

        # Load result (Lumerical 2020 R2 rejects identifiers starting with
        # '_', so temp vars must not be underscore-prefixed).
        if data:
            self._fsp.eval('brd = getresult("' + monitor + '","' + data + '");')
        else:
            self._fsp.eval('brd = getresult("' + monitor + '");')

        # Extract requested fields only
        values = {}
        truncated = {}
        for field in fields:
            try:
                v = self._fsp.getv('brd.' + field)
                sanitized = self._sanitize(v, cap)
                values[field] = sanitized
                if isinstance(sanitized, dict) and sanitized.get('truncated'):
                    truncated[field] = sanitized.get('length')
            except Exception as e:
                values[field] = {'error': str(e)[:_ERR_MSG]}

        self._fsp.eval('clear(brd);')
        return {'monitor': monitor, 'data': data, 'values': values,
                'fields_requested': fields, 'truncated': truncated}

    def _cmd_result_save(self, p):
        self._require_project()
        monitor, data = p['monitor'], p.get('data', '')
        out = p.get('output')
        if not out:
            safe_mon = re.sub(r'[^\w.-]+', '_', monitor).strip('_') or 'monitor'
            safe_data = re.sub(r'[^\w.-]+', '_', data or 'result').strip('_') or 'result'
            out = os.path.join(self._tmp_dir,
                               'result_%s_%s.mat' % (safe_mon, safe_data))
        try:
            ep = out.replace('\\', '\\\\')
            self._fsp.eval('brd = getresult("' + monitor + '","' + data + '");')
            self._fsp.eval('matlabsave("' + ep + '", brd);')
            self._fsp.eval('clear(brd);')
            return {'file': out, 'status': 'ok'}
        except Exception as e:
            raise BridgeError('result_save failed: %s' % str(e)[:_ERR_MSG])

    def _cmd_result_has(self, p):
        """Check whether a monitor/element has results without throwing."""
        self._require_project()
        name = p.get('monitor') or p.get('name')
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

    def _ensure_project_saved(self):
        """Save an unsaved project to a temp path before run().

        Lumerical shows a modal 'Save As' dialog when running an unnamed
        project; with a hidden instance that dialog is invisible and wedges the
        solver. Giving the project a path first avoids the prompt entirely.
        """
        if not self._path:
            self._path = os.path.join(
                self._tmp_dir, '_mcp_auto_%d.fsp' % os.getpid())
            self._fsp.save(self._path)
        return self._path

    def _cmd_run(self, p):
        self._require_project()
        self._ensure_project_saved()
        self._fsp.run()
        return {'status': 'completed', 'saved_to': self._path}

    def _cmd_sweep_run(self, p):
        self._require_project()
        self._ensure_project_saved()
        self._fsp.runsweep(p['name'])
        return {'status': 'completed', 'saved_to': self._path}

    def _cmd_sweep_result(self, p):
        self._require_project()
        try:
            if p.get('result'):
                r = self._call_lsf('getsweepresult', p['name'], p['result'])
            else:
                r = self._call_lsf('getsweepresult', p['name'])
            return self._sanitize(r)
        except Exception as e:
            raise BridgeError('sweep_result failed: %s' % str(e)[:_ERR_MSG])

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

    def _sanitize(self, value, cap=_TRUNC):
        """Sanitize values for JSON serialization.

        Preserves ndarray shape for small arrays (nested lists), represents
        complex values as [re, im] pairs (instead of '(1.5+0.001j)' strings),
        and reports shape/length/truncation for large arrays.
        """
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
                if value.size <= cap:
                    data = value.tolist()
                    if value.dtype.kind == 'c':
                        data = self._complex_to_lists(data)
                    return {'data': data, 'shape': list(value.shape),
                            'length': value.size}
                flat = value.flatten()[:cap]
                if flat.dtype.kind == 'c':
                    data = [[float(v.real), float(v.imag)] for v in flat.tolist()]
                else:
                    data = flat.tolist()
                return {'data': data, 'shape': list(value.shape),
                        'length': value.size, 'truncated': True}
        except ImportError:
            pass
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
        if isinstance(value, complex):
            return [value.real, value.imag]
        s = str(value)
        return s[:10000] if len(s) > 10000 else s

    def _complex_to_lists(self, data):
        """Recursively convert Python complex numbers to [re, im] pairs."""
        if isinstance(data, complex):
            return [data.real, data.imag]
        if isinstance(data, list):
            return [self._complex_to_lists(v) for v in data]
        return data

    def _ok(self, rid, result):
        return {'id': rid, 'result': result}

    def _error(self, rid, message):
        return {'id': rid, 'error': {'code': -1, 'message': message[:_ERR_MSG]}}


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
            bridge._cmd_session_close({})
            break
        resp = bridge.handle(request)
        sys.stdout.write(json.dumps(resp, default=str) + '\n')
        sys.stdout.flush()
    sys.exit(0)


if __name__ == '__main__':
    main()
