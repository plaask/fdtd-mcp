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

        # ---- ?expr query: eval to _br_r, getv, clear ----
        if code.startswith('?'):
            expr = code[1:].strip()
            self._fsp.eval('_br_r = ' + expr + ';')
            result = self._fsp.getv('_br_r')
            self._fsp.eval('clear(_br_r);')
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

    def _cmd_get_scene_info(self, p):
        if not self._fsp: raise RuntimeError('No open project')

        prop_list = ['type','x','y','z','x span','y span','z span','z min','z max',
                     'material','index','enabled','wavelength start','wavelength stop',
                     'monitor type','frequency points','wavelength center','wavelength span',
                     'dx','dy','dz','theta','phi','injection axis','direction',
                     'polarization angle','dipole type','amplitude',
                     'output Ex','output Ey','output Ez','output Hx','output Hy','output Hz',
                     'output power','x min bc','x max bc','y min bc','y max bc',
                     'z min bc','z max bc','pml layers','simulation time','dt',
                     'auto shutoff min','mesh accuracy']

        objects = []
        seen = set()

        def _traverse(scope):
            """Recurse into scope, discover all objects via groupscope+selectall+getid."""
            self._fsp.eval('groupscope("' + scope + '");')
            self._fsp.eval('selectall();')
            self._fsp.eval('_br_ids = getid();')
            ids_str = self._fsp.getv('_br_ids')
            self._fsp.eval('clear(_br_ids);')
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
                    objects.append(obj)
                    if str(t) in ('Structure Group', 'Analysis Group'):
                        _traverse(obj_id)
                except Exception:
                    pass

        _traverse('::model')

        fdtd = {}
        for k in ['dimension','x span','y span','z span','simulation time','mesh accuracy']:
            try: fdtd[k] = self._fsp.getnamed('FDTD', k)
            except Exception: pass

        return {'objects': objects, 'fdtd_summary': fdtd, 'object_count': len(objects)}

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
        value = p['value']
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

    def _cmd_get_results(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        name = p.get('name', 'FDTD')
        try:
            r = appCall(self._fsp, 'getresult', [name])
            return {'name': name, 'results': str(r).split('\n') if r else []}
        except Exception as e:
            return {'name': name, 'results': [], 'error': str(e)[:200]}

    def _cmd_get_result_data(self, p):
        if not self._fsp: raise RuntimeError('No open project')
        monitor, data = p['monitor'], p.get('data', '')
        try:
            self._fsp.eval('_br_d = getresult("' + monitor + '","' + data + '");')
            fv = self._fsp.getv('_br_d.f')
            lv = self._fsp.getv('_br_d.lambda')
            self._fsp.eval('clear(_br_d);')
            r = {'monitor': monitor, 'data': data}
            if fv is not None: r['f'] = self._sanitize(fv)
            if lv is not None: r['lambda'] = self._sanitize(lv)
            return r
        except Exception as e:
            return {'error': str(e)[:300]}

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
