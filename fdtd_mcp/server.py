# -*- coding: utf-8 -*-
"""
FDTD MCP Server — 30 tools, 6 modules covering full FDTD workflow.

Architecture:
  AI Assistant --MCP stdio--> server.py (system Python >=3.10)
                                | subprocess stdin/stdout (line-delimited JSON)
                             bridge.py (Lumerical embed Python 3.6.8)
                                | lumapi
                             Lumerical FDTD engine
"""
import sys, os, json, subprocess, threading, re, queue, logging
import anyio
from collections import deque
from typing import Any
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from .discovery import find_lumerical, find_lumerical_python

# Per-call timeout (seconds). OFF by default so legitimate long simulations are
# never killed mid-run. Set FDTD_MCP_CALL_TIMEOUT (or pass timeout= on a single
# call to run/sweep_run/execute/execute_file) to bound a call: on expiry the
# bridge process tree is killed and auto-restarted on the next call, so a wedged
# engine cannot freeze the session forever. Note the kill discards unsaved
# in-memory engine state.
_CALL_TIMEOUT = None
try:
    _t = os.environ.get('FDTD_MCP_CALL_TIMEOUT')
    if _t is not None and _t.strip() != '':
        _CALL_TIMEOUT = float(_t)
        if _CALL_TIMEOUT <= 0:
            _CALL_TIMEOUT = None
except ValueError:
    pass

# ---- Bundled lumapi ref data (server-side only) ----
_CHEATSHEET_PATH = os.path.join(os.path.dirname(__file__), 'cheatsheet', 'lumapi_ref.json')
_LUMAPI_REF = {}
_LUMAPI_NAMES = set()
if os.path.exists(_CHEATSHEET_PATH):
    try:
        with open(_CHEATSHEET_PATH, 'r', encoding='utf-8') as _f:
            _LUMAPI_REF = json.load(_f)
        _LUMAPI_NAMES = set(k for k in _LUMAPI_REF if k != 'meta')
    except Exception:
        pass

# ---- Single source of truth for tool dispatch (shared with bridge.py) ----
# Maps MCP tool name -> bridge method name. Load-bearing: both processes read
# the same JSON, so the two dispatch tables can no longer drift. Tools listed
# under "_server_only" have no bridge round-trip (handled in server.py); tools
# under "_hidden" are still dispatchable but not advertised to the LLM.
_DISPATCH_PATH = os.path.join(os.path.dirname(__file__), 'dispatch.json')
with open(_DISPATCH_PATH, 'r', encoding='utf-8') as _f:
    _DISPATCH_RAW = json.load(_f)
_SERVER_ONLY_TOOLS = frozenset(_DISPATCH_RAW.get('_server_only', []))
_HIDDEN_TOOLS = frozenset(_DISPATCH_RAW.get('_hidden', []))
_DISPATCH = {k: v for k, v in _DISPATCH_RAW.items()
             if k not in ('_server_only', '_hidden')}


def _get_lumerical_home():
    """Discover Lumerical installation root. CLI arg > env var > auto-detect."""
    for i, arg in enumerate(sys.argv):
        if arg == '--lumerical-home' and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    env = os.environ.get('LUMERICAL_HOME')
    if env:
        return env
    return find_lumerical()


LUMERICAL_HOME = _get_lumerical_home()
LUMERICAL_PYTHON = find_lumerical_python(LUMERICAL_HOME)
BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), 'bridge.py')


class BridgeClient(object):
    """Subprocess bridge to the Lumerical FDTD engine.

    stdout is drained by a background reader thread into a queue so calls never
    block the event loop and EOF (crash) is detected immediately. stderr is
    drained by a separate thread to avoid a full pipe-buffer deadlock.
    """

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._resp_queue = None
        self._dead = False
        self._stderr_lines = deque(maxlen=200)

    def is_dead(self):
        if self._dead or self._proc is None:
            return True
        return self._proc.poll() is not None

    def start(self):
        env = os.environ.copy()
        env['PATH'] = LUMERICAL_HOME + os.pathsep + env.get('PATH', '')
        env['PYTHONIOENCODING'] = 'utf-8'
        self._proc = subprocess.Popen(
            [LUMERICAL_PYTHON, BRIDGE_SCRIPT, '--lumerical-home', LUMERICAL_HOME],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding='utf-8')
        self._dead = False
        self._resp_queue = queue.Queue()
        threading.Thread(target=self._read_loop, daemon=True,
                         name='bridge-reader').start()
        threading.Thread(target=self._stderr_loop, daemon=True,
                         name='bridge-stderr').start()
        try:
            line = self._resp_queue.get(timeout=30)
        except queue.Empty:
            self._dead = True
            raise RuntimeError('Bridge failed to send ready handshake')
        if line is None:
            self._dead = True
            raise RuntimeError('Bridge closed before ready handshake')
        try:
            ready = json.loads(line)
        except (ValueError, TypeError):
            self._dead = True
            raise RuntimeError('Bridge failed to start: bad handshake')
        if not (isinstance(ready, dict) and ready.get('ready')):
            self._dead = True
            raise RuntimeError('Bridge failed to start')

    def _read_loop(self):
        try:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    break
                self._resp_queue.put(line)
        except Exception:
            pass
        finally:
            self._dead = True
            self._resp_queue.put(None)  # EOF sentinel (responses are never None)

    def _stderr_loop(self):
        try:
            for line in self._proc.stderr:
                line = line.rstrip('\n')
                if line:
                    self._stderr_lines.append(line)
                    logging.debug('[bridge] %s', line)
        except Exception:
            pass

    def stderr_tail(self):
        return list(self._stderr_lines)

    def stop(self):
        proc = self._proc
        if not proc:
            return
        self._dead = True
        try:
            if proc.stdin:
                try:
                    proc.stdin.write(json.dumps(
                        {'id': self._req_id + 1, 'method': 'shutdown',
                         'params': {}}) + '\n')
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._proc = None
        self._resp_queue = None

    def call(self, method, params=None, timeout=None):
        with self._lock:
            return self._call(method, params or {}, timeout)

    def _kill_bridge_tree(self):
        """Kill the bridge and its children (taskkill /T) so a wedged run()
        does not leave an orphaned fdtd-solutions solver process behind."""
        proc = self._proc
        if proc is None:
            return
        try:
            subprocess.run(
                ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                capture_output=True, timeout=15)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    def _call(self, method, params, timeout=None):
        if timeout is None:
            timeout = _CALL_TIMEOUT  # resolve global at call time
        if self._dead or self._proc is None:
            raise RuntimeError('Bridge is not running')
        self._req_id += 1
        try:
            self._proc.stdin.write(json.dumps(
                {'id': self._req_id, 'method': method, 'params': params}) + '\n')
            self._proc.stdin.flush()
        except Exception as e:
            self._dead = True
            raise RuntimeError('Bridge write failed: ' + str(e))
        try:
            if timeout:
                line = self._resp_queue.get(timeout=timeout)
            else:
                line = self._resp_queue.get()
        except queue.Empty:
            # A timed-out call means the engine is wedged (e.g. blocked on a
            # license). Kill the bridge tree and mark dead so the next call
            # auto-restarts a fresh engine instead of queueing forever.
            self._dead = True
            self._kill_bridge_tree()
            raise RuntimeError(
                'Bridge call timed out after %.0fs (FDTD_MCP_CALL_TIMEOUT). '
                'The bridge was killed and will auto-restart; reopen your '
                'project if the engine state was lost.' % (timeout or 0))
        if line is None:
            self._dead = True
            raise RuntimeError('Bridge closed')
        resp = json.loads(line)
        if 'error' in resp:
            msg = resp['error'].get('message', str(resp['error']))
            if 'Traceback' in msg:
                msg = msg.split('\n')[0]
            raise RuntimeError(msg)
        return resp.get('result')

_bridge = BridgeClient()

# ---- 30 tools / 6 modules ----
# Each description embeds correct invocation patterns and common pitfalls.

TOOLS = [
    # ==================================================================
    # Module: session (5 tools)
    # ==================================================================
    types.Tool(name='session_open',
        description=(
            'Open a Lumerical FDTD project file (.fsp).\n'
            'Always call this first to load an existing project.\n'
            '\n'
            'Example:\n'
            '  session_open(path="C:/projects/my_sim.fsp")'
        ),
        inputSchema={'type':'object','properties':{
            'path':{'type':'string','description':'Path to .fsp file'}},
            'required':['path']}),
    types.Tool(name='session_new',
        description=(
            'Create a new blank FDTD project (no .fsp file needed).\n'
            'Optionally set FDTD region properties.\n'
            '\n'
            'Example:\n'
            '  session_new(dimension="3D", x_span=2e-6, y_span=2e-6, z_span=1e-6, mesh_accuracy=4)'
        ),
        inputSchema={'type':'object','properties':{
            'dimension':{'type':'string','description':'2D or 3D'},
            'x_span':{'type':'number'},'y_span':{'type':'number'},'z_span':{'type':'number'},
            'simulation_time':{'type':'number'},'mesh_accuracy':{'type':'number'}}}),
    types.Tool(name='session_close',
        description='Close the currently open FDTD project.',
        inputSchema={'type':'object','properties':{}}),
    types.Tool(name='session_save',
        description=(
            'Save the current project.\n'
            'If path is omitted, overwrites the current file (if previously saved).\n'
            '\n'
            'Examples:\n'
            '  session_save()                                  -> overwrite current file\n'
            '  session_save(path="C:/projects/my_sim.fsp")    -> save as new path'
        ),
        inputSchema={'type':'object','properties':{
            'path':{'type':'string','description':'Output .fsp path (optional, omit to overwrite current)'}}}),
    types.Tool(name='session_save_as',
        description=(
            'Save the current project with a new path.\n'
            '\n'
            'Example:\n'
            '  session_save_as(path="C:/projects/my_sim_v2.fsp")'
        ),
        inputSchema={'type':'object','properties':{
            'path':{'type':'string','description':'Output .fsp path'}},
            'required':['path']}),

    # ==================================================================
    # Module: model (6 tools)
    # ==================================================================
    types.Tool(name='model_info',
        description=(
            'ONE-CALL model self-introspection: object tree (recursive), materials list, '
            'model variables, FDTD summary, result names.\n'
            'Merges scene info + model overview + variables into a single result.\n'
            '\n'
            'Call this FIRST after opening/creating a project, and BEFORE you:\n'
            '  - Make assumptions about object types\n'
            '  - Use variable names not yet verified\n'
            '  - Assign materials to objects\n'
            '\n'
            'Disabled objects FILTERED OUT by default (enabled_only=true).\n'
            'Set include_full=true to get full per-object properties (slower, larger response).'
        ),
        inputSchema={'type':'object','properties':{
            'enabled_only':{'type':'boolean','description':'Skip disabled objects (default true)'},
            'include_full':{'type':'boolean','description':'Also return full per-object props (default false)'}}}),
    types.Tool(name='model_add',
        description=(
            'Add ANY object to the model tree.\n'
            '\n'
            'Supported types:\n'
            '  rectangle, circle, ring, polygon, sphere, pyramid, triangle, waveguide\n'
            '  fdtd, mesh\n'
            '  dipole, tfsf, plane, gaussian, mode_source\n'
            '  power_monitor, dft_monitor, index_monitor, field_monitor, movie_monitor\n'
            '  structure_group, analysis_group\n'
            '\n'
            'Examples:\n'
            '  model_add(type="rectangle", name="my_rect",\n'
            '    properties={"x span": 1e-6, "y span": 2e-6})\n'
            '  model_add(type="dipole", properties={"x": 0, "y": 0, "z": 0})\n'
            '\n'
            'If name is omitted, Lumerical auto-generates one.\n'
            'Use scope="::model" (default) to add at root level.'
        ),
        inputSchema={'type':'object','properties':{
            'type':{'type':'string','description':'Object type to create',
                'enum':['rectangle','circle','ring','polygon','sphere','pyramid','triangle',
                        'waveguide','fdtd','mesh','dipole','tfsf','plane','gaussian',
                        'mode_source','power_monitor','dft_monitor','index_monitor',
                        'field_monitor','movie_monitor','structure_group','analysis_group']},
            'name':{'type':'string','description':'Optional name (auto-generated if omitted)'},
            'properties':{'type':'object','description':'Optional property dict to set after creation'},
            'scope':{'type':'string','description':'Parent scope, default "::model"'}},
            'required':['type']}),
    types.Tool(name='model_get',
        description=(
            'Get full info for ONE named object: all properties, scripts (if group/::model),\n'
            'variables (if group/::model), children (if group).\n'
            '\n'
            'Example:\n'
            '  model_get(name="source_1")\n'
            '    -> {type: "Dipole", amplitude: 1.0, ...}'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Object name (required)'}},
            'required':['name']}),
    types.Tool(name='model_set',
        description=(
            'Set properties/variables on an object.\n'
            'For "::model": uses addvar/setvar for new properties.\n'
            'For groups: uses adduserprop/addanalysisprop.\n'
            'For ordinary objects: uses setnamed directly.\n'
            '\n'
            'Examples:\n'
            '  model_set(name="my_rect", properties={"x span": 2e-6, "y span": 1e-6})\n'
            '  model_set(name="::model", properties={"gap": 200e-9})'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Object name'},
            'properties':{'type':'object','description':'Dict of property=value pairs'}},
            'required':['name','properties']}),
    types.Tool(name='model_delete',
        description=(
            'Delete an object from the model tree by name.\n'
            '\n'
            'Example:\n'
            '  model_delete(name="my_rect")'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Object name to delete'}},
            'required':['name']}),
    types.Tool(name='model_script',
        description=(
            'Get or set scripts on objects with script properties.\n'
            '\n'
            'Actions:\n'
            '  action="get"  -> return current script text\n'
            '  action="set"  -> write new script content\n'
            '\n'
            'Script property names depend on object TYPE (auto-detected):\n'
            '  ::model          -> "setup script" / "analysis script"\n'
            '  Analysis Group   -> "setup script" / "analysis script"\n'
            '  Structure Group  -> "script"\n'
            '\n'
            'Examples:\n'
            '  model_script(name="::model", action="get")\n'
            '  model_script(name="Cnorm", action="get")\n'
            '  model_script(name="Cnorm", action="set", content="...new script...")\n'
            '  model_script(name="my_struct", action="set", script_type="script", content="...")\n'
            '\n'
            'IMPORTANT: Always use this tool for scripts. '
            'Do NOT use execute(\'set("setup script",...)\').'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Object name, default "::model"'},
            'action':{'type':'string','description':'"get" or "set"'},
            'script_type':{'type':'string','description':'"setup", "analysis", or "script" (auto-detected if omitted)'},
            'content':{'type':'string','description':'Script text (required for action="set")'}},
            'required':['name','action']}),

    # ==================================================================
    # Module: material (5 tools)
    # ==================================================================
    types.Tool(name='material_add',
        description=(
            'Create a new material from a model type template.\n'
            'Returns the auto-generated material NAME.\n'
            '\n'
            'Common types:\n'
            '  "Sampled 3D data"  -> tabulated nk data (use for polymers like PNIPAM)\n'
            '  "Dielectric"       -> constant refractive index\n'
            '  "Drude"            -> metal Drude model\n'
            '\n'
            'Workflow:\n'
            '  1. material_add(type="Sampled 3D data")  -> returns name like "material_1"\n'
            '  2. material_set(name="material_1", property="sampled 3d data", value=[[...]])\n'
            '  3. model_set(name="my_rect", properties={"material": "material_1"})\n'
            '\n'
            'Tip: Built-in database materials (e.g. "Au (Gold) - Johnson and Christy")\n'
            'need NO material_add -- just assign the name string via model_set.'
        ),
        inputSchema={'type':'object','properties':{
            'type':{'type':'string','description':'Material model type, default "Sampled 3D data"'}}}),
    types.Tool(name='material_get',
        description=(
            'Read material properties.\n'
            'If property is omitted, lists all available property names for the material.\n'
            '\n'
            'Examples:\n'
            '  material_get(name="material_1")                   -> list all property names\n'
            '  material_get(name="material_1", property="sampled 3d data")  -> return data'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string'},'property':{'type':'string','description':'Optional property name to read'}},
            'required':['name']}),
    types.Tool(name='material_set',
        description=(
            'Set a material property.\n'
            '\n'
            'Correct property names (depends on material type -- check with material_get first):\n'
            '  "sampled 3d data"  -> Nx2/3 array [[wl,n,ik],...] for Sampled 3D data\n'
            '  "Refractive Index" -> constant n (for Dielectric type)\n'
            '  "mesh order"       -> mesh priority override\n'
            '  "name"            -> rename the material\n'
            '  "color"           -> RGBA color\n'
            '\n'
            'Data format for sampled materials:\n'
            '  Nx2 array: [[wl1, n1+ik1], [wl2, n2+ik2], ...]\n'
            '  or [[freq1, eps1], [freq2, eps2], ...]\n'
            '\n'
            'Examples:\n'
            '  material_set(name="mat1", property="Refractive Index", value=1.5)\n'
            '  material_set(name="mat1", property="mesh order", value=2)\n'
            '  material_set(name="mat1", property="sampled 3d data",\n'
            '    value=[[300e-9,1.5,0],[800e-9,1.5,0]])\n'
            '\n'
            'For large nk datasets, prefer file import:\n'
            '  execute(\'importnk("C:/path/to/nk_data.txt")\')\n'
            'Tip: Call material_get(name) WITHOUT property first to see all settable property names.'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string'},'property':{'type':'string'},'value':{'description':'Property value: number, string, or numeric array'}},
            'required':['name','property','value']}),
    types.Tool(name='material_delete',
        description=(
            'Delete a material from the database.\n'
            '\n'
            'Example:\n'
            '  material_delete(name="material_1")'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string'}}, 'required':['name']}),
    types.Tool(name='material_exists',
        description=(
            'Check if a material exists in the database.\n'
            '\n'
            'Example:\n'
            '  material_exists(name="Au (Gold) - Johnson and Christy") -> {exists: true}'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string'}}, 'required':['name']}),

    # ==================================================================
    # Module: sweep (6 tools)
    # ==================================================================
    types.Tool(name='sweep_add',
        description=(
            'Create and configure a parameter sweep.\n'
            '\n'
            'Types: 0=parameter sweep, 1=optimization, 2=monte carlo, 3=s-parameter\n'
            '\n'
            'Parameter dicts use keys: name, parameter (the object property path to\n'
            'sweep, e.g. "::model::substrate::z span" — use "::" separators, NOT ">":\n'
            '"::model>gap" registers but does NOT resolve at runtime), start, stop,\n'
            'and points (number of sample points; applied at the sweep level).\n'
            'Sampling is Linear by default — do NOT set a per-parameter "type" sampling\n'
            'string; "type" is the parameter VALUE data type and should be omitted\n'
            '(or "Number"). Model variables (addvar) are gone in v202 — sweep a real\n'
            'object property instead.\n'
            '\n'
            'Example:\n'
            '  sweep_add(name="thickness_sweep", type=0,\n'
            '    parameters=[{"name":"t","parameter":"::model::substrate::z span",\n'
            '                 "start":100e-9,"stop":300e-9,"points":5}],\n'
            '    results=[{"name":"T","result":"::model::transmission_calc::T"}])'
        ),
        inputSchema={'type':'object','properties':{
            'type':{'type':'integer','description':'Sweep type: 0=parameter, 1=optimization, 2=monte carlo, 3=s-parameter (default 0)'},
            'name':{'type':'string','description':'Sweep name'},
            'parameters':{'type':'array','items':{'type':'object'},'description':'List of parameter dicts: {name, parameter, start, stop, points}'},
            'results':{'type':'array','items':{'type':'object'},'description':'List of result dicts: {name, result}'}},
            'required':['type','name']}),
    types.Tool(name='sweep_get',
        description=(
            'Get parameter sweep configuration.\n'
            'Tells you: whether the sweep exists, has results, and result structure.\n'
            '\n'
            'Example:\n'
            '  sweep_get(name="gap_sweep") -> {exists: true, has_results: false, note: "not yet run"}'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Sweep name'}},
            'required':['name']}),
    types.Tool(name='sweep_set',
        description=(
            'Modify sweep properties.\n'
            '\n'
            'Example:\n'
            '  sweep_set(name="gap_sweep", properties={"sweep type": 0})'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Sweep name'},
            'properties':{'type':'object','description':'Dict of property=value pairs'}},
            'required':['name','properties']}),
    types.Tool(name='sweep_delete',
        description=(
            'Delete a sweep by name.\n'
            '\n'
            'Example:\n'
            '  sweep_delete(name="gap_sweep")'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string'}}, 'required':['name']}),
    types.Tool(name='sweep_run',
        description=(
            'Run a parameter sweep by name. Blocks until completion.\n'
            'timeout (seconds) bounds the wait; on expiry the engine is '
            'restarted (set 0 to wait forever, default from FDTD_MCP_CALL_TIMEOUT).\n'
            '\n'
            'Example:\n'
            '  sweep_run(name="gap_sweep")'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string'},
            'timeout':{'type':'number','description':'Optional wait bound in seconds (0 = no timeout)'}},
            'required':['name']}),
    types.Tool(name='sweep_result',
        description=(
            'Get results from a completed parameter sweep.\n'
            '\n'
            'Examples:\n'
            '  sweep_result(name="gap_sweep")                 -> all results\n'
            '  sweep_result(name="gap_sweep", result="T")    -> specific result'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string'},'result':{'type':'string','description':'Optional result name'}},
            'required':['name']}),

    # ==================================================================
    # Module: result (4 tools)
    # ==================================================================
    types.Tool(name='result_list',
        description=(
            'List available results/data for a monitor, or list all monitors with results.\n'
            '\n'
            'Examples:\n'
            '  result_list()                          -> list all monitors with results\n'
            '  result_list(monitor="DFT")             -> list datasets on DFT monitor\n'
            '  result_list(monitor="DFT", data="E")   -> list field names in E dataset\n'
            '\n'
            'Call this BEFORE result_get to discover available field names.'
        ),
        inputSchema={'type':'object','properties':{
            'monitor':{'type':'string','description':'Monitor name (optional)'},
            'data':{'type':'string','description':'Dataset name, e.g. "E", "T" (optional)'}}}),
    types.Tool(name='result_get',
        description=(
            'Get result data from a monitor. Fields parameter is REQUIRED.\n'
            'Call result_list FIRST to discover available field names.\n'
            '\n'
            'Returns {fields_available: [...], values: {field: array}}.\n'
            'Common fields: Ex, Ey, Ez, Hx, Hy, Hz, x, y, z, T, R, P, power, f, lambda\n'
            '\n'
            'Examples:\n'
            '  result_get(monitor="DFT", fields=["Ex","Ey","f","lambda"])\n'
            '  result_get(monitor="::model", data="g_lum", fields=["lambda","T"])\n'
            '\n'
            'Arrays are capped per-field at cap (default 2000) -- '
            'use result_save for full .mat export.'
        ),
        inputSchema={'type':'object','properties':{
            'monitor':{'type':'string'},
            'data':{'type':'string','description':'Dataset name, e.g. "E", "T"'},
            'fields':{'type':'array','items':{'type':'string'},
                'description':'Field names to retrieve (use result_list first)'},
            'cap':{'type':'number','description':'Per-field array cap (default 2000)'}},
            'required':['monitor','fields']}),
    types.Tool(name='result_save',
        description=(
            'Save result data to .mat file for offline analysis.\n'
            '\n'
            'Examples:\n'
            '  result_save(monitor="DFT", data="E")\n'
            '  result_save(monitor="DFT", data="E", output="C:/data/fields.mat")'
        ),
        inputSchema={'type':'object','properties':{
            'monitor':{'type':'string'},'data':{'type':'string'},
            'output':{'type':'string','description':'Output .mat file path (optional)'}},
            'required':['monitor','data']}),
    types.Tool(name='result_has',
        description=(
            'Check whether a monitor/element has results WITHOUT throwing an exception.\n'
            'Returns {exists: true/false}. Call before result_get to avoid errors\n'
            'when a monitor has no results (e.g. before simulation is run).\n'
            '\n'
            'Example:\n'
            '  result_has(monitor="DFT") -> {exists: true}'
        ),
        inputSchema={'type':'object','properties':{
            'monitor':{'type':'string'}}, 'required':['monitor']}),

    # ==================================================================
    # Module: engine (4 tools)
    # ==================================================================
    types.Tool(name='run',
        description=(
            'Run the FDTD simulation once. Blocks until completion.\n'
            'timeout (seconds) bounds the wait; on expiry the engine is '
            'restarted (set 0 to wait forever, default from FDTD_MCP_CALL_TIMEOUT).'
        ),
        inputSchema={'type':'object','properties':{
            'timeout':{'type':'number','description':'Optional wait bound in seconds (0 = no timeout)'}}}),
    types.Tool(name='execute',
        description=(
            'Execute LSF script code directly. Supports full multi-line LSF scripts '
            'passed to the Lumerical engine via eval().\n'
            '\n'
            'PATTERNS:\n'
            '  Query value:    execute("?getnamed(\\"::model\\", \\"gap\\")")\n'
            '                    -> prefix with ? to capture return value\n'
            '  Delete object:  execute(\'select("obj_name"); delete();\')\n'
            '                    -> NOT delete("name") -- use select+delete form\n'
            '  Create object:  execute("addrect(); setnamed(\\"rect\\", \\"x span\\", 1e-6)")\n'
            '  Multi-line:     execute("for i=1:10; ?i; end")\n'
            '\n'
            'DO NOT use execute for:\n'
            '  - Editing scripts       -> use model_script tool instead\n'
            '  - Setting material props -> use material_set tool instead\n'
            '  - Opening/saving files  -> use session_open/session_save tools instead\n'
            '\n'
            'OK for execute:\n'
            '  - Material file import:  execute(\'importnk("C:/path/to/file.txt")\')'
        ),
        inputSchema={'type':'object','properties':{
            'code':{'type':'string','description':'LSF script code (multi-line supported)'},
            'timeout':{'type':'number','description':'Optional wait bound in seconds (0 = no timeout)'}},
            'required':['code']}),
    types.Tool(name='execute_file',
        description='Run a Lumerical script file (.lsf).',
        inputSchema={'type':'object','properties':{
            'path':{'type':'string'},
            'timeout':{'type':'number','description':'Optional wait bound in seconds (0 = no timeout)'}},
            'required':['path']}),
    types.Tool(name='reference_lookup',
        description=(
            'Look up VERIFIED Lumerical API signature/parameter ranges BEFORE writing scripts.\n'
            'This is a server-side tool (no bridge call) that reads a curated bundled cheatsheet.\n'
            '\n'
            'Modes:\n'
            '  reference_lookup(name="getresult")   -> full entry: signature, args, pitfalls\n'
            '  reference_lookup(list_only=true)      -> all known function names\n'
            '  reference_lookup(category="result")   -> all entries in one category\n'
            '\n'
            'Always call list_only=true first before referencing a Lumerical function\n'
            'to ensure it exists. Script validation will warn about unrecognized function names.'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Exact Lumerical API function name, e.g. getresult'},
            'category':{'type':'string','description':'Filter by category: result|source|material|object|analysis|variable|sweep|mesh|monitor|general'},
            'list_only':{'type':'boolean','description':'If true, return only function names (ignores name/category)'}}}),
]

# ---- Startup validation: TOOLS and dispatch.json must agree ----
# Hidden tools stay dispatchable (in dispatch.json) but are not advertised.
TOOLS = [t for t in TOOLS if t.name not in _HIDDEN_TOOLS]
_tool_names = {t.name for t in TOOLS}
_missing = _tool_names - _SERVER_ONLY_TOOLS - set(_DISPATCH.keys())
if _missing:
    raise RuntimeError('Tools missing from dispatch.json: ' + ', '.join(sorted(_missing)))
_extra = (set(_DISPATCH.keys()) | _SERVER_ONLY_TOOLS) - _tool_names - _HIDDEN_TOOLS
if _extra:
    raise RuntimeError('dispatch.json references unknown tools: ' + ', '.join(sorted(_extra)))

# ---- Tool annotations (Claude review criteria: read/write split) ----
_READ_ONLY_TOOLS = {
    'model_info', 'model_get', 'material_get', 'material_exists',
    'result_list', 'result_get', 'sweep_get', 'reference_lookup',
}
_DESTRUCTIVE_TOOLS = {
    'session_open', 'session_new', 'session_close',
    'model_delete', 'material_delete', 'sweep_delete',
    'execute', 'execute_file',
}
_LONG_RUNNING_TOOLS = frozenset({'run', 'sweep_run', 'execute', 'execute_file'})
for _t in TOOLS:
    if _t.name in _READ_ONLY_TOOLS:
        _t.annotations = types.ToolAnnotations(readOnlyHint=True)
    elif _t.name in _DESTRUCTIVE_TOOLS:
        _t.annotations = types.ToolAnnotations(destructiveHint=True)

app = Server('fdtd-mcp')

@app.list_tools()
async def list_tools(): return TOOLS

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]):
    # ----------------------------------------------------------------
    # Server-side tools (NO bridge round-trip)
    # ----------------------------------------------------------------
    if name == 'reference_lookup':
        return _handle_lumapi_ref(arguments)

    # ----------------------------------------------------------------
    # Bridge-dispatched tools
    # ----------------------------------------------------------------
    bm = _DISPATCH.get(name)
    if not bm:
        raise ValueError('Unknown tool: ' + name)

    _ensure_bridge()

    params = dict(arguments) if arguments else {}

    # ---- Parameter defaults ----
    if name == 'model_script':
        params['action'] = arguments.get('action', 'get')
        params['script_type'] = arguments.get('script_type', '')
    if name == 'sweep_add':
        params['type'] = arguments.get('type', 0)
    if name in ('result_get', 'result_list'):
        params['data'] = arguments.get('data', '')
    if name == 'result_save':
        params['output'] = arguments.get('output', '')
    if name == 'result_get':
        params['cap'] = arguments.get('cap', 2000)
        params['fields'] = arguments.get('fields', [])

    # Per-call timeout override for long-running tools (0 disables the bound).
    call_timeout = _CALL_TIMEOUT
    if name in _LONG_RUNNING_TOOLS:
        t = arguments.get('timeout')
        if t is not None:
            call_timeout = float(t) if t > 0 else 0.0
        params.pop('timeout', None)

    # ---- Script scan (advisory, non-blocking) ----
    warnings = []
    if _LUMAPI_NAMES:
        if name == 'execute':
            text = params.get('code', '')
            warnings = _scan_script_for_unknown_funcs(text)
        elif name == 'model_script' and params.get('action') == 'set':
            text = params.get('content', '')
            warnings = _scan_script_for_unknown_funcs(text)

    # Run the blocking bridge call off the event loop so the server stays
    # responsive (progress/cancellation/other requests) during long calls.
    result = await anyio.to_thread.run_sync(_bridge.call, bm, params, call_timeout)
    if result is None:
        result = {}
    if warnings:
        result['warnings'] = warnings
    return {'result': result}


def _handle_lumapi_ref(arguments):
    """Server-side lookup of the bundled cheatsheet."""
    args = dict(arguments) if arguments else {}
    if args.get('list_only'):
        names = sorted(_LUMAPI_NAMES)
        return {'result': {
            'function_count': len(names),
            'functions': names,
            'scope_note': _LUMAPI_REF.get('meta', {}).get('scope', '')
        }}
    name = args.get('name', '')
    category = args.get('category', '')
    if name:
        entry = _LUMAPI_REF.get(name)
        if not entry:
            return {'result': {'error': 'Function "' + name + '" not in cheatsheet. Call list_only=true to see all known functions.', 'hint': _LUMAPI_REF.get('meta', {}).get('scope', '')}}
        return {'result': {'function': name, 'entry': entry}}
    if category:
        results = {k: v for k, v in _LUMAPI_REF.items() if k != 'meta' and v.get('category') == category}
        return {'result': {'category': category, 'count': len(results), 'functions': results}}
    return {'result': {'error': 'Provide name=, category=, or list_only=true'}}


# ---- LSF builtins allowlist (never warn on these) ----
_LSF_BUILTINS = frozenset({
    # Control flow
    'for', 'while', 'if', 'else', 'elif', 'end', 'break', 'continue',
    'switch', 'case', 'select', 'selectall', 'groupscope',
    # Core get/set
    'set', 'get', 'setnamed', 'getnamed',
    # Material
    'setmaterial', 'getmaterial', 'addmaterial',
    'deletematerial', 'copymaterial', 'materialexists',
    'importnk', 'importdataset', 'exportdataset', 'importmaterialdb',
    'savematerial', 'getindex', 'getfdtdindex', 'geteps',
    # Object operations
    'delete', 'clear', 'copy', 'move', 'min', 'max', 'sqrt',
    'abs', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'atan2',
    'exp', 'log', 'log10', 'real', 'imag', 'conj',
    'length', 'size', 'find', 'sprintf', 'str2num', 'num2str',
    'type', 'eval', 'feval', 'run', 'runsweep',
    'write', 'read', 'matlabsave', 'load',
    'readdata', 'loaddata',
    # File I/O
    'fileexists', 'fopen', 'fclose', 'fread', 'fwrite', 'format',
    'saveh5', 'loadh5', 'matlabload', 'matlabget', 'matlabput',
    'readtable', 'readspectrum',
    # Layout / analysis mode
    'switchtolayout', 'layoutmode', 'analysismode',
    # Add primitives
    'addfdtd', 'addrect', 'addcircle', 'addring',
    'addpoly', 'add2dpoly', 'add2drect', 'addsphere',
    'addpyramid', 'addtriangle', 'addwaveguide',
    # Sources
    'addtfsf', 'addgaussian', 'addplane', 'adddipole',
    # Monitors
    'addpower', 'addmovie', 'addindex', 'addfield',
    'adddftmonitor',
    # Mesh
    'addmesh',
    # Groups
    'addstructuregroup', 'addanalysisgroup', 'addassemblygroup',
    'addgroup', 'addtogroup',
    # Ports / EME / import
    'addimport', 'addmode', 'addport', 'addeme',
    # Variables & user properties
    'addvar', 'setvar', 'getvar', 'addvarfdtd',
    'adduserprop', 'addanalysisprop', 'addanalysisresult',
    'addimportnk',
    # Sweep
    'addsweepparameter', 'addsweepresult',
    'removesweepparameter', 'removesweepresult',
    'getsweep', 'getsweepdata', 'deletesweep',
    'copysweep', 'pastesweep', 'insertsweep',
    'setsweep',
    # Results
    'getresult', 'getdata', 'getresultdata',
    'getelectric', 'getmagnetic',
    'listresults', 'findresult', 'clearresults', 'clearanalysis',
    'haveresult',
    # Farfield / transmission
    'farfield', 'farfield3d', 'farfieldpolar',
    'farfieldpolar2d', 'farfieldpolar3d',
    'transmission', 'dipolepower',
    # Analysis / setup run
    'runsetup', 'runanalysis',
    'getpath',
    # BC / EME profile
    'addbc', 'addemeprofile', 'addemeindex', 'addeffectiveindex',
    # Math / string
    'amax', 'amin',
    'substring', 'findstring', 'replace', 'replacestring',
    # System / misc
    'system', 'save', 'close',
})


def _scan_script_for_funcs(text):
    """Extract all \bFuncName( tokens, ignoring comments and string literals."""
    clean = re.sub(r'%.*?(\n|$)', ' ', text)  # LSF comments start with %
    clean = re.sub(r'"[^"]*"', ' ', clean)    # double-quoted strings
    clean = re.sub(r"'[^']*'", ' ', clean)    # single-quoted strings
    return set(re.findall(r'(?:\b)([A-Za-z_]\w*)\s*\(', clean))


def _scan_script_for_unknown_funcs(text):
    """Return advisory warnings for unrecognized function calls in script text."""
    if not text:
        return []
    called = _scan_script_for_funcs(text)
    unknown = called - _LUMAPI_NAMES - _LSF_BUILTINS
    if not unknown:
        return []
    sorted_u = sorted(unknown)
    msg = (
        'Script contains function names not recognized: '
        + ', '.join(sorted_u) + '. '
        'Not every unrecognized name is wrong (some are your custom functions). '
        'If you intended to call a Lumerical API function, first call '
        'reference_lookup(list_only=true) to check it exists and get the correct signature.'
    )
    return [msg]

_bridge_started = False
_bridge_start_lock = threading.Lock()

def _ensure_bridge():
    global _bridge_started
    with _bridge_start_lock:
        if not _bridge_started or _bridge.is_dead():
            if _bridge_started:
                _bridge.stop()
            _bridge.start()
            _bridge_started = True

def main():
    import atexit
    atexit.register(lambda: _bridge.stop() if _bridge_started else None)
    import anyio; anyio.run(_main)

async def _main():
    _ensure_bridge()
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

if __name__ == '__main__':
    main()
