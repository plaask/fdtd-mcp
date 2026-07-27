# -*- coding: utf-8 -*-
"""
FDTD MCP Server — 21 tools covering full FDTD workflow.

Architecture:
  Claude Code --MCP stdio--> server.py (system Python)
                                | subprocess stdin/stdout
                             bridge.py (Lumerical embed Python 3.6.8)
                                | lumapi
                             Lumerical FDTD engine
"""
import sys, os, json, subprocess, threading, re
from typing import Any
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from .discovery import find_lumerical, find_lumerical_python

# ---- Cheatsheet (bundled lumapi ref data, server-side only) ----
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
    def __init__(self):
        self._proc = None; self._lock = threading.Lock(); self._req_id = 0

    def start(self):
        env = os.environ.copy()
        env['PATH'] = LUMERICAL_HOME + os.pathsep + env.get('PATH', '')
        env['PYTHONIOENCODING'] = 'utf-8'
        self._proc = subprocess.Popen(
            [LUMERICAL_PYTHON, BRIDGE_SCRIPT, '--lumerical-home', LUMERICAL_HOME],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding='utf-8')
        if not json.loads(self._proc.stdout.readline()).get('ready'):
            raise RuntimeError('Bridge failed to start')

    def stop(self):
        if not self._proc: return
        try: self._call('shutdown', {})
        except Exception: pass
        try: self._proc.stdin.close(); self._proc.wait(timeout=5)
        except Exception: self._proc.kill()
        self._proc = None

    def call(self, method, params=None):
        with self._lock: return self._call(method, params or {})

    def _call(self, method, params):
        self._req_id += 1
        self._proc.stdin.write(json.dumps({'id': self._req_id, 'method': method, 'params': params}) + '\n')
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line: raise RuntimeError('Bridge closed')
        resp = json.loads(line)
        if 'error' in resp:
            msg = resp['error'].get('message', str(resp['error']))
            if 'Traceback' in msg: msg = msg.split('\n')[0]
            raise RuntimeError(msg)
        return resp.get('result')

_bridge = BridgeClient()

# ---- 21 tools ----
# Each description embeds correct invocation patterns and common pitfalls.
# Claude reads these as its only guide — make every word count.

TOOLS = [
    # ==================================================================
    # Universal (2)
    # ==================================================================
    types.Tool(name='execute',
        description=(
            'Execute a single-line Lumerical command or expression.\n'
            '\n'
            'PATTERNS (use exactly these forms):\n'
            '  Create object:  execute("addtfsf()") / execute("addpower()") / execute("addrect()") etc.\n'
            '     → Then configure with set_parameter.\n'
            '  Delete object:  execute(\'delete("obj_name")\')  — wraps select+delete internally.\n'
            '  Query value:    execute("?getnamed(\"::model\", \"gap\")")  — prefix with ? to capture return.\n'
            '  Set property:   execute(\'set("property", value)\') — for simple properties only.\n'
            '  Raw script:     execute("code; more_code;") — multi-command single line.\n'
            '\n'
            'DO NOT use execute for:\n'
            '  - Editing scripts → use set_script tool instead.\n'
            '  - Setting material props → use set_material tool instead.\n'
            '  - Opening/saving files → use open/save tools instead.'
        ),
        inputSchema={'type':'object','properties':{'code':{'type':'string','description':'Single-line Lumerical command or expression'}},'required':['code']}),
    types.Tool(name='execute_file',
        description='Run a Lumerical script file (.lsf). Use for multi-line scripts.',
        inputSchema={'type':'object','properties':{'path':{'type':'string'}},'required':['path']}),

    # ==================================================================
    # Scene (2)
    # ==================================================================
    types.Tool(name='get_scene_info',
        description=(
            'Get all objects with full properties + FDTD summary in one call.\n'
            'Recursively traverses all structure/analysis groups — nested children are included.\n'
            'Use this as the FIRST tool after opening a project to understand its structure.\n'
            '\n'
            'HINT: Pass enabled_only=true to skip disabled objects and reduce output noise.\n'
            '      Or use get_model_overview() for a lighter single-call alternative\n'
            '      that also includes model variables and materials.'
        ),
        inputSchema={'type':'object','properties':{
            'enabled_only':{'type':'boolean','description':'Skip disabled objects (default false)'}},
        }),
    types.Tool(name='get_script',
        description=(
            'Get setup and analysis scripts from an object.\n'
            '\n'
            'Script property names depend on object TYPE:\n'
            '  ::model          → returns "setup_script" + "analysis_script"\n'
            '  Analysis Group   → returns "setup_script" + "analysis_script"\n'
            '  Structure Group  → returns "script"\n'
            '\n'
            'Examples:\n'
            '  get_script()                    → root model scripts\n'
            '  get_script(name="Cnorm")        → analysis group scripts\n'
            '  get_script(name="my_structure") → structure group script'
        ),
        inputSchema={'type':'object','properties':{'name':{'type':'string','description':'Object name, default "::model"'}}}),

    # ==================================================================
    # Parameters (2)
    # ==================================================================
    types.Tool(name='get_parameters',
        description=(
            'Get all model and analysis group parameters with values.\n'
            'Discovers both built-in params AND user-defined properties (adduserprop).\n'
            '\n'
            'Examples:\n'
            '  get_parameters()                → all params from ::model + all groups\n'
            '  get_parameters(object="Cnorm")  → only Cnorm group params'
        ),
        inputSchema={'type':'object','properties':{'object':{'type':'string','description':'Object name (default: scan all)'}}}),
    types.Tool(name='set_parameter',
        description=(
            'Set a parameter value on an object.\n'
            '\n'
            'Examples:\n'
            '  set_parameter(name="gap", value=200e-9)              → model-level param\n'
            '  set_parameter(name="LR", value=0, object="LR_tfsf")  → analysis group param\n'
            '\n'
            'Works on ::model (default), Analysis Groups, and Structure Groups.'
        ),
        inputSchema={'type':'object','properties':{'name':{'type':'string'},'value':{'type':'number'},'object':{'type':'string','description':'Target object, default "::model"'}},'required':['name','value']}),

    # ==================================================================
    # Sweep (1)
    # ==================================================================
    types.Tool(name='get_sweep_info',
        description=(
            'Get parameter sweep configuration.\n'
            'Tells you: whether the sweep exists, has results, and result structure.\n'
            '\n'
            'Example:\n'
            '  get_sweep_info(name="dpgap") → {exists: true, has_results: false, note: "not yet run"}'
        ),
        inputSchema={'type':'object','properties':{'name':{'type':'string','description':'Sweep name, e.g. "dpgap"'}},'required':['name']}),

    # ==================================================================
    # Script editing (1)
    # ==================================================================
    types.Tool(name='set_script',
        description=(
            'Set the setup or analysis script of an object. Supports multi-line content.\n'
            '\n'
            'Script property names depend on object TYPE:\n'
            '  ::model          → type="setup" sets "setup script", type="analysis" sets "analysis script"\n'
            '  Analysis Group   → same as ::model (setup/analysis scripts)\n'
            '  Structure Group  → type is ignored, sets the single "script" property\n'
            '\n'
            'Examples:\n'
            '  set_script(type="setup", content="...")                → model setup script\n'
            '  set_script(name="Cnorm", type="analysis", content="…") → analysis group analysis script\n'
            '  set_script(name="my_struct", content="…")              → structure group script\n'
            '\n'
            'IMPORTANT: Always use this tool for scripts. Do NOT use execute(\'set("setup script",…)\').'
        ),
        inputSchema={'type':'object','properties':{'name':{'type':'string','description':'Object name, default "::model"'},'type':{'type':'string','description':'"setup" or "analysis"'},'content':{'type':'string','description':'Script text (multi-line supported)'}},'required':['type','content']}),

    # ==================================================================
    # Materials (3)
    # ==================================================================
    types.Tool(name='add_material',
        description=(
            'Create a new material from a model type template.\n'
            'Returns the auto-generated material NAME — save this, you need it for set_material.\n'
            '\n'
            'Common types:\n'
            '  "Sampled 3D data" — tabulated nk data (use for polymers like PNIPAM)\n'
            '  "Dielectric"      — constant refractive index\n'
            '  "Drude"           — metal Drude model\n'
            '\n'
            'Workflow:\n'
            '  1. add_material(type="Sampled 3D data")  → returns name like "material_1"\n'
            '  2. set_material(name="material_1", property="nk data", value=[...])\n'
            '  3. set_parameter(object="rect", name="material", value="material_1")\n'
            '\n'
            'Tip: Built-in database materials like "Au (Gold) - Johnson and Christy" need NO add_material —\n'
            '     just assign the name string directly via set_parameter.'
        ),
        inputSchema={'type':'object','properties':{'type':{'type':'string','description':'Material model type, default "Sampled 3D data"'}},'required':[]}),
    types.Tool(name='set_material',
        description=(
            'Set a material property.\n'
            '\n'
            'Common properties (name depends on material type — check with get_material first):\n'
            '  "Refractive Index"  \u2192 constant n (for Dielectric type)\n'
            '  "sampled 3d data"   \u2192 Nx2 array [[wl,n],...] with optional k (for Sampled 3D data)\n'
            '  "mesh order"        \u2192 mesh priority override\n'
            '  "name"             \u2192 rename the material\n'
            '\n'
            'Examples:\n'
            '  set_material(name="mat1", property="Refractive Index", value=1.5)\n'
            '  set_material(name="mat1", property="mesh order", value=2)\n'
            '  set_material(name="mat1", property="sampled 3d data", value=[[300e-9,1.5,0],[800e-9,1.5,0]])\n'
            '\n'
            'For large nk datasets, prefer file import:\n'
            '  execute("importnk(\\"C:/path/to/nk_data.txt\\")")\n'
            'Tip: Call get_material(name) WITHOUT property first to see all settable property names.'
        ),
        inputSchema={'type':'object','properties':{'name':{'type':'string'},'property':{'type':'string','description':'e.g. "Refractive Index", "sampled 3d data", "mesh order", "name"'},'value':{'description':'Property value: number, string, or numeric array'}},'required':['name','property','value']}),
    types.Tool(name='get_material',
        description=(
            'Read material properties.\n'
            'If property is omitted, lists all available property names for the material.\n'
            'If property is given, returns that property value.'
        ),
        inputSchema={'type':'object','properties':{'name':{'type':'string'},'property':{'type':'string','description':'Optional property name to read'}},'required':['name']}),

    # ==================================================================
    # Results (3)
    # ==================================================================
    types.Tool(name='get_results',
        description=(
            'List available result names from a monitor or "FDTD".\n'
            'Use this to discover what datasets exist before calling get_result_data.\n'
            '\n'
            'Examples:\n'
            '  get_results()              → lists FDTD-level results\n'
            '  get_results(name="DFT")    → lists DFT monitor results'
        ),
        inputSchema={'type':'object','properties':{'name':{'type':'string','description':'Monitor name, default "FDTD"'}}}),
    types.Tool(name='get_result_data',
        description=(
            'Get full result dataset from a monitor or "FDTD".\n'
            'Returns {fields_available: [...], values: {field: array}} — NOT just f/lambda.\n'
            'Fields include: Ex, Ey, Ez, Hx, Hy, Hz, x, y, z, T, R, P, power, f, lambda, ...\n'
            '\n'
            'Use fields=["Ex","T"] to limit expensive data transfer.\n'
            'Arrays are capped per-field at cap (default 2000) — use get_result_file for full export.\n'
            '\n'
            'BEST PRACTICE: Call list_result_fields(monitor, data) FIRST to discover\n'
            'available field names without transferring data. Then call get_result_data\n'
            'with only the fields you need.\n'
            '\n'
            'Examples:\n'
            '  get_result_data(monitor="DFT", data="E")       → all available fields\n'
            '  get_result_data(monitor="::model", data="g_lum") → dataset fields\n'
            '  get_result_data(monitor="DFT", data="T", fields=["T","f","lambda"])'
        ),
        inputSchema={'type':'object','properties':{
            'monitor':{'type':'string'},'data':{'type':'string'},
            'fields':{'type':'array','items':{'type':'string'},'description':'Optional explicit field list to reduce data transfer'},
            'cap':{'type':'number','description':'Per-field array cap (default 2000)'}},
            'required':['monitor']}),
    types.Tool(name='get_result_file',
        description=(
            'Extract a result dataset to .mat file for offline analysis.\n'
            '\n'
            'Example:\n'
            '  get_result_file(monitor="DFT", data="E", output="C:/data/fields.mat")'
        ),
        inputSchema={'type':'object','properties':{'monitor':{'type':'string'},'data':{'type':'string'},'output':{'type':'string'}},'required':['monitor','data','output']}),

    # ==================================================================
    # Run (3)
    # ==================================================================
    types.Tool(name='run',
        description='Run the FDTD simulation once. Blocks until completion.',
        inputSchema={'type':'object','properties':{}}),
    types.Tool(name='run_sweep',
        description='Run a parameter sweep by name. Blocks until completion.',
        inputSchema={'type':'object','properties':{'name':{'type':'string'}},'required':['name']}),
    types.Tool(name='get_sweep_result',
        description='Get results from a completed parameter sweep.',
        inputSchema={'type':'object','properties':{'name':{'type':'string'}},'required':['name']}),

    # ==================================================================
    # Lifecycle (4)
    # ==================================================================
    types.Tool(name='open',
        description='Open a Lumerical FDTD project file (.fsp). Always call this first.',
        inputSchema={'type':'object','properties':{'path':{'type':'string'}},'required':['path']}),
    types.Tool(name='new',
        description=(
            'Create a new blank FDTD project (no .fsp file needed).\n'
            'Optionally set FDTD region properties.\n'
            '\n'
            'Example:\n'
            '  new(dimension="3D", x_span=2e-6, y_span=2e-6, z_span=1e-6, mesh_accuracy=4)'
        ),
        inputSchema={'type':'object','properties':{
            'dimension':{'type':'string','description':'2D or 3D'},
            'x span':{'type':'number'},'y span':{'type':'number'},'z span':{'type':'number'},
            'simulation time':{'type':'number'},'mesh accuracy':{'type':'number'}}}),
    types.Tool(name='close',
        description='Close the currently open FDTD project.',
        inputSchema={'type':'object','properties':{}}),
    types.Tool(name='save',
        description='Save current project to .fsp file.',
        inputSchema={'type':'object','properties':{'path':{'type':'string'}},'required':['path']}),

    # ==================================================================
    # Anti-hallucination / Infra — server-side, NO bridge round-trip (1)
    # ==================================================================
    types.Tool(name='get_lumapi_ref',
        description=(
            'Look up VERIFIED Lumerical API signature/parameter ranges BEFORE writing scripts.\n'
            'This is a server-side tool (no bridge call) that reads a curated bundled cheatsheet.\n'
            '\n'
            'Modes:\n'
            '  get_lumapi_ref(name="getresult")  → full entry: signature, args, pitfalls\n'
            '  get_lumapi_ref(list_only=true)     → all known function names\n'
            '  get_lumapi_ref(category="result")  → all entries in one category\n'
            '\n'
            'IMPORTANT: Always call list_only=true first before referencing a Lumerical function\n'
            'to ensure it exists. The cheatsheet covers commonly-used functions only — if a name\n'
            'is not listed, do NOT guess its signature; ask the user or check Lumerical docs.\n'
            'Script validation (execute/set_script) will warn you about unknown function names.'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Exact lumapi/LSF function name, e.g. getresult, farfield3d'},
            'category':{'type':'string','description':'Filter by category: result|source|material|object|analysis|variable|sweep|mesh|monitor|general'},
            'list_only':{'type':'boolean','description':'If true, return only function names (ignores name/category)'}},
        },
    ),
]

# ---- New anti-hallucination tools (via bridge) ----
_NEW_TOOLS = [
    types.Tool(name='get_model_overview',
        description=(
            'ONE-CALL self-introspection to stop skipping steps.\n'
            'Returns ALL information needed for modeling + script writing in ONE dict:\n'
            '  {objects: [...(name,type,enabled,material)], model_variables: {...}, materials: [...], notes: ["..."]}\n'
            '\n'
            'Disabled objects FILTERED OUT by default (enabled_only=true).\n'
            'Scripts are NOT included — call get_script on demand.\n'
            '\n'
            'Call this FIRST after opening a project, and BEFORE you:\n'
            '  - Make assumptions about object types (dipole vs plane source?)\n'
            '  - Use variable names in scripts (LD/DBR/top_layer/au values are here)\n'
            '  - Assign materials to objects (material list is here)\n'
            '  - Read/edit scripts (scripts pulled separately via get_script)\n'
            '\n'
            'NOT a replacement for get_lumapi_ref — consult that BEFORE writing script code.'
        ),
        inputSchema={'type':'object','properties':{
            'enabled_only':{'type':'boolean','description':'Skip disabled objects (default true)'},
            'include_full':{'type':'boolean','description':'Also return full per-object props (default false)'}},
        },
    ),
    types.Tool(name='get_model_variables',
        description=(
            'Read GUI Model Variables table (LD, DBR, top_layer, au, pmma1, d, n_DBR, t_H, t_L, ...).\n'
            'These DO NOT appear via get_parameters — they live in the Variables table,\n'
            'separate from the ::model property list.\n'
            '\n'
            'Call this whenever a setup script references a variable whose value is unknown,\n'
            'or after opening a project to understand current geometry/material configuration.\n'
            'Example variables this tool finds: LD (chirality), DBR (center wavelength),\n'
            'top_layer (spacer thickness), au (gold layer thickness).'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Scope (default "::model")'}},
        },
    ),
    types.Tool(name='get_object_info',
        description=(
            'Get full properties of ONE named object with an explicit TYPE discriminator.\n'
            'Use this instead of grep-ing the giant get_scene_info JSON.\n'
            '\n'
            'Example:\n'
            '  get_object_info(name="source_1")\n'
            '    → {type: "Dipole Source", source_kind: "dipole", amplitude: 1.0, ...}\n'
            '\n'
            'The source_kind field explicitly tells you what KIND of source (dipole/plane/tfsf/gaussian)\n'
            'so you never confuse a Dipole for a PlaneSource. Always call this before asserting\n'
            'an object\'s type or properties.'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Object name (required)'}},
            'required':['name'],
        },
    ),
    types.Tool(name='list_result_fields',
        description=(
            'List available field names in a result dataset BEFORE fetching data.\n'
            'Cheaper than get_result_data — no data transfer, just field names.\n'
            'Call this FIRST to discover what fields (Ex, Ey, T, lambda, x, etc.) exist\n'
            'for a monitor before requesting them with get_result_data.\n'
            '\n'
            'Examples:\n'
            '  list_result_fields(monitor="DFT", data="E") → ["Ex","Ey","Ez","f","lambda","x","y","z"]\n'
            '  list_result_fields(monitor="FDTD") → ["source power","lambda","f"]'
        ),
        inputSchema={'type':'object','properties':{
            'monitor':{'type':'string','description':'Monitor name (required)'},
            'data':{'type':'string','description':'Dataset name, e.g. "E", "T", "P". If omitted, no second arg passed to getresult.'}},
            'required':['monitor'],
        },
    ),
    types.Tool(name='has_result',
        description=(
            'Check whether a monitor has results WITHOUT throwing an exception.\n'
            'Returns {exists: true/false}. Call before get_result_data to avoid errors\n'
            'when a monitor has no results (e.g. before simulation is run).\n'
            '\n'
            'Examples:\n'
            '  has_result(name="DFT") → {exists: true}'
        ),
        inputSchema={'type':'object','properties':{
            'name':{'type':'string','description':'Monitor or element name (required)'}},
            'required':['name'],
        },
    ),
]

app = Server('fdtd-mcp')

# Combine original tools with new tools for the listing
_ALL_TOOLS = TOOLS + _NEW_TOOLS

@app.list_tools()
async def list_tools(): return _ALL_TOOLS

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]):
    _ensure_bridge()

    # ----------------------------------------------------------------
    # Server-side tools (NO bridge round-trip)
    # ----------------------------------------------------------------
    if name == 'get_lumapi_ref':
        return _handle_lumapi_ref(arguments)

    # ----------------------------------------------------------------
    # Bridge-dispatched tools
    # ----------------------------------------------------------------
    method_map = {
        'execute':'execute', 'execute_file':'execute_file',
        'open':'open', 'new':'new', 'close':'close', 'save':'save',
        'get_scene_info':'get_scene_info', 'get_script':'get_script',
        'get_parameters':'get_parameters', 'set_parameter':'set_parameter',
        'get_sweep_info':'get_sweep_info', 'set_script':'set_script',
        'add_material':'add_material', 'set_material':'set_material', 'get_material':'get_material',
        'get_results':'get_results', 'get_result_data':'get_result_data',
        'get_result_file':'get_result_file',
        'run':'run', 'run_sweep':'run_sweep', 'get_sweep_result':'get_sweep_result',
        # New anti-hallucination tools
        'get_model_overview':'get_model_overview', 'get_model_variables':'get_model_variables',
        'get_object_info':'get_object_info',
        'list_result_fields':'list_result_fields', 'has_result':'has_result',
    }
    bm = method_map.get(name)
    if not bm: raise ValueError('Unknown tool: ' + name)

    params = dict(arguments) if arguments else {}

    # ---- Parameter defaults ----
    if name == 'set_script':
        params['type'] = arguments.get('type', 'setup')
    if name in ('get_result_data', 'list_result_fields'):
        params['data'] = arguments.get('data', '')
    if name == 'get_result_file':
        params['output'] = arguments.get('output', '')

    # ---- Script scan (advisory, non-blocking) ----
    warnings = []
    if _LUMAPI_NAMES:
        if name == 'execute':
            text = params.get('code', '')
            warnings = _scan_script_for_unknown_funcs(text)
        elif name == 'set_script':
            text = params.get('content', '')
            warnings = _scan_script_for_unknown_funcs(text)

    result = _bridge.call(bm, params)
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
    'for', 'while', 'if', 'else', 'elif', 'end', 'break', 'continue',
    'switch', 'case', 'select', 'selectall', 'groupscope',
    'set', 'get', 'setnamed', 'getnamed',
    'setmaterial', 'getmaterial', 'addmaterial',
    'delete', 'clear', 'copy', 'move', 'min', 'max', 'sqrt',
    'abs', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'atan2',
    'exp', 'log', 'log10', 'real', 'imag', 'conj',
    'length', 'size', 'find', 'sprintf', 'str2num', 'num2str',
    'type', 'eval', 'feval', 'run', 'runsweep',
    'write', 'read', 'matlabsave', 'load',
    'readdata', 'loaddata', 'importdataset', 'importnk', 'readspectrum',
    'switchtolayout', 'layoutmode', 'analysismode',
    'addfdtd', 'addrect', 'addcircle', 'addring',
    'addtfsf', 'addgaussian', 'addplane', 'adddipole',
    'addpower', 'addmovie', 'addindex', 'addfield',
    'addmesh', 'addvar', 'setvar', 'getvar', 'adduserprop',
    'farfield', 'farfield3d', 'farfieldpolar', 'farfieldpolar2d', 'farfieldpolar3d',
    'transmission', 'dipolepower',
    'getresult', 'getdata', 'haveresult', 'findresult',
    'getpath', 'save', 'close',
})


def _scan_script_for_funcs(text):
    """Extract all \bFuncName( tokens from script text."""
    return set(re.findall(r'(?:\b)([A-Za-z_]\w*)\s*\(', text))


def _scan_script_for_unknown_funcs(text):
    """Return advisory warnings for unrecognized function calls in script text."""
    if not text:
        return []
    called = _scan_script_for_funcs(text)
    # Only flag calls NOT in known set AND NOT in LSF builtins
    unknown = called - _LUMAPI_NAMES - _LSF_BUILTINS
    if not unknown:
        return []
    sorted_u = sorted(unknown)
    msg = (
        'Script contains function names not recognized: '
        + ', '.join(sorted_u) + '. '
        'Not every unrecognized name is wrong (some are your custom functions). '
        'If you intended to call a Lumerical API function, first call '
        'get_lumapi_ref(list_only=true) to check it exists and get the correct signature.'
    )
    return [msg]

_bridge_started = False

def _ensure_bridge():
    global _bridge_started
    if not _bridge_started:
        _bridge.start(); _bridge_started = True

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
