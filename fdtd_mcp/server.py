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
import sys, os, json, subprocess, threading
from typing import Any
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from .discovery import find_lumerical, find_lumerical_python


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
            'Use this as the FIRST tool after opening a project to understand its structure.'
        ),
        inputSchema={'type':'object','properties':{}}),
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
            'Common properties:\n'
            '  "Refractive Index"  → constant n (for Dielectric type)\n'
            '  "nk data"           → tabulated [wavelengths, n, k] arrays (for Sampled 3D data)\n'
            '  "mesh order"        → mesh priority override\n'
            '\n'
            'Examples:\n'
            '  set_material(name="mat1", property="Refractive Index", value=1.5)\n'
            '  set_material(name="mat1", property="mesh order", value=2)\n'
            '  set_material(name="mat1", property="nk data", value=[[300e-9,800e-9],[1.5,1.5],[0,0]])\n'
            '\n'
            'Tip: Call set_material(name) WITHOUT property first to see all settable property names.'
        ),
        inputSchema={'type':'object','properties':{'name':{'type':'string'},'property':{'type':'string','description':'e.g. "Refractive Index", "nk data", "mesh order"'},'value':{'description':'Property value: number, string, or array'}},'required':['name','property','value']}),
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
            'Get frequency/wavelength arrays for a result dataset.\n'
            'Returns {"f": [...], "lambda": [...]}.\n'
            '\n'
            'Example:\n'
            '  get_result_data(monitor="::model", data="g_lum")'
        ),
        inputSchema={'type':'object','properties':{'monitor':{'type':'string'},'data':{'type':'string'}},'required':['monitor','data']}),
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
]

app = Server('fdtd-mcp')

@app.list_tools()
async def list_tools(): return TOOLS

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]):
    _ensure_bridge()
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
    }
    bm = method_map.get(name)
    if not bm: raise ValueError('Unknown tool: ' + name)

    params = dict(arguments) if arguments else {}
    if name == 'set_script':
        params['type'] = arguments.get('type', 'setup')
    if name == 'get_result_data':
        params['data'] = arguments.get('data', '')
    if name == 'get_result_file':
        params['output'] = arguments.get('output', '')

    result = _bridge.call(bm, params)
    return {'result': result}

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
