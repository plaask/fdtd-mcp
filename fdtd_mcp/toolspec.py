# -*- coding: utf-8 -*-
"""Declarative tool catalogue — the single source for the MCP surface.

Everything the server needs about a tool lives here: its schema and description
(what the model sees), its bridge method, its annotations, its parameter
defaults and how its timeout should behave. server.py turns these into
`mcp.types.Tool` objects, so the previous hand-maintained `_READ_ONLY_TOOLS` /
`_DESTRUCTIVE_TOOLS` / `_LONG_RUNNING_TOOLS` sets and the per-tool `if name ==
...` default block can no longer drift out of sync with the tool list.

`fdtd_mcp/dispatch.json` stays the single source for tool -> bridge method, and
tests/test_dispatch.py + tests/test_toolspec.py keep the two in agreement.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSpec(object):
    """One MCP tool."""

    name: str
    module: str
    description: str
    schema: dict
    # Bridge method name; defaults to the tool name (== the `_cmd_<method>`
    # handler on FdtdBridge).
    method: str = ''
    # Parameters filled in when the caller omits them.
    defaults: dict = field(default_factory=dict)
    read_only: bool = False
    destructive: bool = False
    # May block for minutes (solve / arbitrary script): accepts a per-call
    # `timeout` override.
    long_running: bool = False
    # Starts or stops a hidden engine instance: gets a short default timeout so
    # a wedged launch fails fast instead of hanging the session.
    launch: bool = False
    # Handled inside server.py with no bridge round-trip.
    server_only: bool = False
    # Dispatchable but not advertised to the model.
    hidden: bool = False

    @property
    def bridge_method(self) -> str:
        return self.method or self.name


SPECS = [
    ToolSpec(
        name='session_open',
        module='session',
        description=(
'Open a Lumerical FDTD project file (.fsp).\n'
        'Always call this first to load an existing project.\n'
        '\n'
        'Example:\n'
        '  session_open(path="C:/projects/my_sim.fsp")'
        ),
        schema={'type':'object','properties':{
        'path':{'type':'string','description':'Path to .fsp file'}},
        'required':['path']},
        destructive=True, launch=True,
    ),
    ToolSpec(
        name='session_new',
        module='session',
        description=(
'Create a new blank FDTD project (no .fsp file needed).\n'
        'Optionally set FDTD region properties.\n'
        '\n'
        'Example:\n'
        '  session_new(dimension="3D", x_span=2e-6, y_span=2e-6, z_span=1e-6, mesh_accuracy=4)'
        ),
        schema={'type':'object','properties':{
        'dimension':{'type':'string','description':'2D or 3D'},
        'x_span':{'type':'number'},'y_span':{'type':'number'},'z_span':{'type':'number'},
        'simulation_time':{'type':'number'},'mesh_accuracy':{'type':'number'}}},
        destructive=True, launch=True,
    ),
    ToolSpec(
        name='session_close',
        module='session',
        description=(
'Close the currently open FDTD project.'
        ),
        schema={'type':'object','properties':{}},
        destructive=True, launch=True,
    ),
    ToolSpec(
        name='session_save',
        module='session',
        description=(
'Save the current project.\n'
        'If path is omitted, overwrites the current file (if previously saved).\n'
        '\n'
        'Examples:\n'
        '  session_save()                                  -> overwrite current file\n'
        '  session_save(path="C:/projects/my_sim.fsp")    -> save as new path'
        ),
        schema={'type':'object','properties':{
        'path':{'type':'string','description':'Output .fsp path (optional, omit to overwrite current)'}}},
    ),
    ToolSpec(
        name='session_save_as',
        module='session',
        method='session_save',
        description=(
'Save the current project with a new path.\n'
        '\n'
        'Example:\n'
        '  session_save_as(path="C:/projects/my_sim_v2.fsp")'
        ),
        schema={'type':'object','properties':{
        'path':{'type':'string','description':'Output .fsp path'}},
        'required':['path']},
    ),
    ToolSpec(
        name='model_info',
        module='model',
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
        schema={'type':'object','properties':{
        'enabled_only':{'type':'boolean','description':'Skip disabled objects (default true)'},
        'include_full':{'type':'boolean','description':'Also return full per-object props (default false)'}}},
        read_only=True,
    ),
    ToolSpec(
        name='model_add',
        module='model',
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
        schema={'type':'object','properties':{
        'type':{'type':'string','description':'Object type to create',
            'enum':['rectangle','circle','ring','polygon','sphere','pyramid','triangle',
                    'waveguide','fdtd','mesh','dipole','tfsf','plane','gaussian',
                    'mode_source','power_monitor','dft_monitor','index_monitor',
                    'field_monitor','movie_monitor','structure_group','analysis_group']},
        'name':{'type':'string','description':'Optional name (auto-generated if omitted)'},
        'properties':{'type':'object','description':'Optional property dict to set after creation'},
        'scope':{'type':'string','description':'Parent scope, default "::model"'}},
        'required':['type']},
    ),
    ToolSpec(
        name='model_get',
        module='model',
        description=(
'Get full info for ONE named object: all properties, scripts (if group/::model),\n'
        'variables (if group/::model), user-declared properties (model variables and\n'
        'group parameters), children (if group).\n'
        '\n'
        'Example:\n'
        '  model_get(name="source_1")\n'
        '    -> {type: "Dipole", amplitude: 1.0, ...}'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string','description':'Object name (required)'}},
        'required':['name']},
        read_only=True,
    ),
    ToolSpec(
        name='model_set',
        module='model',
        description=(
'Set parameters/properties on an object, and declare analysis results.\n'
        'Existing properties are updated directly (setnamed).\n'
        'A name that does NOT exist yet is declared automatically when the target\n'
        'is the model root ("::model"), a Structure Group or an Analysis Group\n'
        '(addanalysisprop for the model root and analysis groups, adduserprop for\n'
        'structure groups, both with an INTEGER type code — addvar no longer\n'
        'exists). Parameters declared on "::model" appear in the Analysis >\n'
        'Variables table of the GUI\'s "Edit analysis group" dialog, which is\n'
        'exactly what that dialog shows; "results" fills its Results table.\n'
        'On ordinary objects (geometry, sources, monitors) only existing\n'
        'properties can be set; failures come back in properties_failed with a\n'
        'reason per property and status becomes "partial" or "error".\n'
        '\n'
        'Property names are shared between the analysis- and user-property\n'
        'namespaces, and the declaration commands are NOT idempotent: declaring a\n'
        'name that already exists silently creates a suffixed clone ("gap" ->\n'
        '"gap2") instead of updating it, so declare each name once (setnamed\n'
        'updates an existing one).\n'
        '\n'
        'Examples:\n'
        '  model_set(name="my_rect", properties={"x span": 2e-6, "y span": 1e-6})\n'
        '  model_set(name="::model", properties={"gap": 200e-9})   -> model parameter\n'
        '  model_set(name="::model", properties={"gap": 200e-9},\n'
        '            property_types={"gap": "length"})             -> GUI length unit\n'
        '  model_set(name="::model", results=["T_spectrum", "R_spectrum"])\n'
        '            -> rows in the Analysis tab\'s Results table\n'
        '\n'
        'property_types maps a property to a type: 0/number, 1/text, 2/length,\n'
        '3/time, 4/frequency, 5/material (default: text for strings, number\n'
        'otherwise). Parameters declared on ::model are usable as sweep\n'
        'parameters (parameter="::model::gap").'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string','description':'Object name'},
        'properties':{'type':'object','description':'Dict of property=value pairs'},
        'property_types':{'type':'object','description':'Optional {property: type code or name} for newly declared properties'},
        'results':{'type':'array','items':{'type':'string'},
            'description':'Analysis result names to declare (the dialog\'s Results table)'}},
        'required':['name']},
    ),
    ToolSpec(
        name='model_delete',
        module='model',
        description=(
'Delete an object from the model tree by name.\n'
        '\n'
        'Example:\n'
        '  model_delete(name="my_rect")'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string','description':'Object name to delete'}},
        'required':['name']},
        destructive=True,
    ),
    ToolSpec(
        name='model_script',
        module='model',
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
        schema={'type':'object','properties':{
        'name':{'type':'string','description':'Object name, default "::model"'},
        'action':{'type':'string','description':'"get" (default) or "set"'},
        'script_type':{'type':'string','description':'"setup", "analysis", or "script" (auto-detected if omitted)'},
        'content':{'type':'string','description':'Script text (required for action="set")'}},
        'required':['name']},
        defaults={'action': 'get', 'script_type': ''},
    ),
    ToolSpec(
        name='material_add',
        module='material',
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
        schema={'type':'object','properties':{
        'type':{'type':'string','description':'Material model type, default "Sampled 3D data"'}}},
    ),
    ToolSpec(
        name='material_get',
        module='material',
        description=(
'Read material properties.\n'
        'If property is omitted, lists all available property names for the material.\n'
        '\n'
        'Examples:\n'
        '  material_get(name="material_1")                   -> list all property names\n'
        '  material_get(name="material_1", property="sampled 3d data")  -> return data'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string'},'property':{'type':'string','description':'Optional property name to read'}},
        'required':['name']},
        read_only=True,
    ),
    ToolSpec(
        name='material_set',
        module='material',
        description=(
'Set a material property.\n'
        '\n'
        'Correct property names (depends on material type -- check with material_get first):\n'
        '  "sampled 3d data"  -> Nx2 or Nx4 array for Sampled 3D data (v202 rejects 3 columns)\n'
        '  "Refractive Index" -> constant n (for Dielectric type)\n'
        '  "mesh order"       -> mesh priority override\n'
        '  "name"            -> rename the material\n'
        '  "color"           -> RGBA color\n'
        '\n'
        'Data format for sampled materials:\n'
        '  Nx2: [[wl1, n1], [wl2, n2], ...]          (real index only)\n'
        '  Nx4: [[wl1, n1, k1, 0], ...]              (with extinction)\n'
        '  or [[freq1, eps1], [freq2, eps2], ...]\n'
        'Errors are returned verbatim, e.g. "the number of columns ... should be\n'
        'either 2 or 4".\n'
        '\n'
        'Examples:\n'
        '  material_set(name="mat1", property="Refractive Index", value=1.5)\n'
        '  material_set(name="mat1", property="mesh order", value=2)\n'
        '  material_set(name="mat1", property="sampled 3d data",\n'
        '    value=[[300e-9,1.52],[800e-9,1.46]])\n'
        '\n'
        'For large nk datasets, prefer file import:\n'
        '  execute(\'importnk("C:/path/to/nk_data.txt")\')\n'
        'Tip: Call material_get(name) WITHOUT property first to see all settable property names.'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string'},'property':{'type':'string'},'value':{'description':'Property value: number, string, or numeric array'}},
        'required':['name','property','value']},
    ),
    ToolSpec(
        name='material_delete',
        module='material',
        description=(
'Delete a material from the database.\n'
        '\n'
        'Example:\n'
        '  material_delete(name="material_1")'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string'}}, 'required':['name']},
        destructive=True,
    ),
    ToolSpec(
        name='material_exists',
        module='material',
        description=(
'Check if a material exists in the database.\n'
        '\n'
        'Example:\n'
        '  material_exists(name="Au (Gold) - Johnson and Christy") -> {exists: true}'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string'}}, 'required':['name']},
        read_only=True,
    ),
    ToolSpec(
        name='sweep_add',
        module='sweep',
        description=(
'Create and configure a parameter sweep.\n'
        '\n'
        'types: 0=parameter sweep, 1=optimization, 2=monte carlo, 3=s-parameter\n'
        '\n'
        'Parameter dicts use keys: name, parameter (the object property path to\n'
        'sweep, e.g. "::model::substrate::z span" — use "::" separators, NOT ">":\n'
        '"::model>gap" registers but does NOT resolve at runtime), start, stop,\n'
        'and points (number of sample points; applied at the sweep level).\n'
        'Sampling is Linear by default — do NOT set a per-parameter "type" sampling\n'
        'string; "type" is the parameter VALUE data type and should be omitted\n'
        '(or "Number").\n'
        '\n'
        'Model variables work as sweep targets: create them first with\n'
        'model_set(name="::model", properties={"gap": 200e-9}) and sweep\n'
        '"::model::gap" (addvar no longer exists in v202).\n'
        'WARNING: addsweepparameter accepts a path to a property that does not\n'
        'exist, so a typo only shows up when the sweep runs — model_get the\n'
        'target first if you are not sure it exists.\n'
        '\n'
        'Example:\n'
        '  sweep_add(name="thickness_sweep", type=0,\n'
        '    parameters=[{"name":"t","parameter":"::model::substrate::z span",\n'
        '                 "start":100e-9,"stop":300e-9,"points":5}],\n'
        '    results=[{"name":"T","result":"::model::transmission_calc::T"}])'
        ),
        schema={'type':'object','properties':{
        'type':{'type':'integer','description':'Sweep type: 0=parameter, 1=optimization, 2=monte carlo, 3=s-parameter (default 0)'},
        'name':{'type':'string','description':'Sweep name'},
        'parameters':{'type':'array','items':{'type':'object'},'description':'List of parameter dicts: {name, parameter, start, stop, points}'},
        'results':{'type':'array','items':{'type':'object'},'description':'List of result dicts: {name, result}'}},
        'required':['name']},
        defaults={'type': 0},
    ),
    ToolSpec(
        name='sweep_get',
        module='sweep',
        description=(
'Get parameter sweep configuration.\n'
        'Tells you: whether the sweep exists, has results, and result structure.\n'
        '\n'
        'Example:\n'
        '  sweep_get(name="gap_sweep") -> {exists: true, has_results: false, note: "not yet run"}'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string','description':'Sweep name'}},
        'required':['name']},
        read_only=True,
    ),
    ToolSpec(
        name='sweep_set',
        module='sweep',
        description=(
'Modify sweep properties.\n'
        '\n'
        'Example:\n'
        '  sweep_set(name="gap_sweep", properties={"sweep type": 0})'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string','description':'Sweep name'},
        'properties':{'type':'object','description':'Dict of property=value pairs'}},
        'required':['name','properties']},
    ),
    ToolSpec(
        name='sweep_delete',
        module='sweep',
        description=(
'Delete a sweep by name.\n'
        '\n'
        'Example:\n'
        '  sweep_delete(name="gap_sweep")'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string'}}, 'required':['name']},
        destructive=True,
    ),
    ToolSpec(
        name='sweep_run',
        module='sweep',
        description=(
'Run a parameter sweep by name. Blocks until completion.\n'
        'timeout (seconds) bounds the wait; on expiry the engine is '
        'restarted (set 0 to wait forever, default from FDTD_MCP_CALL_TIMEOUT).\n'
        '\n'
        'Example:\n'
        '  sweep_run(name="gap_sweep")'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string'},
        'timeout':{'type':'number','description':'Optional wait bound in seconds (0 = no timeout)'}},
        'required':['name']},
        long_running=True,
    ),
    ToolSpec(
        name='sweep_result',
        module='sweep',
        description=(
'Get results from a completed parameter sweep.\n'
        '\n'
        'Examples:\n'
        '  sweep_result(name="gap_sweep")                 -> all results\n'
        '  sweep_result(name="gap_sweep", result="T")    -> specific result'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string'},'result':{'type':'string','description':'Optional result name'}},
        'required':['name']},
    ),
    ToolSpec(
        name='result_list',
        module='result',
        description=(
'List available result datasets for a monitor, with the field names\n'
        'present in each dataset.\n'
        '\n'
        'Examples:\n'
        '  result_list(monitor="DFT")             -> datasets + fields on that monitor\n'
        '  result_list(all=true)                  -> every object that has results\n'
        '  result_list()                          -> datasets of the FDTD object\n'
        '\n'
        'Pass result_has(monitor=...) afterwards, or before, to check whether a\n'
        'monitor has results at all. Call this BEFORE result_get to discover the\n'
        'field names available.'
        ),
        schema={'type':'object','properties':{
        'monitor':{'type':'string','description':'Monitor/object name (default "FDTD")'},
        'data':{'type':'string','description':'Dataset name, e.g. "E", "T" (optional)'},
        'all':{'type':'boolean','description':'With no monitor: list every object that has results'}}},
        defaults={'data': ''},
        read_only=True,
    ),
    ToolSpec(
        name='result_get',
        module='result',
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
        schema={'type':'object','properties':{
        'monitor':{'type':'string'},
        'data':{'type':'string','description':'Dataset name, e.g. "E", "T"'},
        'fields':{'type':'array','items':{'type':'string'},
            'description':'Field names to retrieve (use result_list first)'},
        'cap':{'type':'number','description':'Per-field array cap (default 2000)'}},
        'required':['monitor','fields']},
        defaults={'data': '', 'cap': 2000},
        read_only=True,
    ),
    ToolSpec(
        name='result_save',
        module='result',
        description=(
'Save result data to .mat file for offline analysis.\n'
        '\n'
        'Examples:\n'
        '  result_save(monitor="DFT", data="E")\n'
        '  result_save(monitor="DFT", data="E", output="C:/data/fields.mat")'
        ),
        schema={'type':'object','properties':{
        'monitor':{'type':'string'},'data':{'type':'string'},
        'output':{'type':'string','description':'Output .mat file path (optional)'}},
        'required':['monitor','data']},
        defaults={'output': ''},
    ),
    ToolSpec(
        name='result_has',
        module='result',
        description=(
'Check whether a monitor/element has results WITHOUT throwing an exception.\n'
        'Returns {exists: true/false}. Call before result_get to avoid errors\n'
        'when a monitor has no results (e.g. before simulation is run).\n'
        '\n'
        'Example:\n'
        '  result_has(monitor="DFT") -> {exists: true}'
        ),
        schema={'type':'object','properties':{
        'monitor':{'type':'string'}}, 'required':['monitor']},
    ),
    ToolSpec(
        name='run',
        module='engine',
        description=(
'Run the FDTD simulation once. Blocks until completion.\n'
        'timeout (seconds) bounds the wait; on expiry the engine is '
        'restarted (set 0 to wait forever, default from FDTD_MCP_CALL_TIMEOUT).'
        ),
        schema={'type':'object','properties':{
        'timeout':{'type':'number','description':'Optional wait bound in seconds (0 = no timeout)'}}},
        long_running=True,
    ),
    ToolSpec(
        name='execute',
        module='engine',
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
        schema={'type':'object','properties':{
        'code':{'type':'string','description':'LSF script code (multi-line supported)'},
        'timeout':{'type':'number','description':'Optional wait bound in seconds (0 = no timeout)'}},
        'required':['code']},
        destructive=True, long_running=True,
    ),
    ToolSpec(
        name='execute_file',
        module='engine',
        description=(
'Run a Lumerical script file (.lsf).'
        ),
        schema={'type':'object','properties':{
        'path':{'type':'string'},
        'timeout':{'type':'number','description':'Optional wait bound in seconds (0 = no timeout)'}},
        'required':['path']},
        destructive=True, long_running=True,
    ),
    ToolSpec(
        name='library_list',
        module='library',
        description=(
'List the Lumerical OBJECT LIBRARY (Resources/fdtd-library + catalog.xml):\n'
        '129 ready-made items, split into Structures (96) and Analysis (33).\n'
        'Server-side tool: no bridge call, no open project needed.\n'
        '\n'
        'Use it before library_insert — ids are catalog ids such as "sphere",\n'
        '"ring_resonator", "straight_wg" (Structures) or "Qanalysis",\n'
        '"grating_transmission" (Analysis).\n'
        '\n'
        'Filters:\n'
        '  library_list(type="Structures")     -> geometry items only\n'
        '  library_list(type="Analysis")       -> analysis groups only\n'
        '  library_list(category="Gratings")   -> one catalog category\n'
        '  library_list(id="Qanalysis")        -> one entry\n'
        '\n'
        'Example:\n'
        '  library_list(type="Analysis", limit=50)'
        ),
        schema={'type':'object','properties':{
        'type':{'type':'string','description':'"Structures" or "Analysis" (omit for both)'},
        'category':{'type':'string','description':'Catalog category, e.g. "Gratings", "Cylinders", "Advanced analysis"'},
        'id':{'type':'string','description':'Return exactly one entry with this catalog id'},
        'limit':{'type':'number','description':'Max items returned (default 60)'}}},
        read_only=True, server_only=True,
    ),
    ToolSpec(
        name='library_insert',
        module='library',
        description=(
'Insert an OBJECT LIBRARY item into the open project — a structure group or an\n'
        'analysis group. The engine copies the item in and re-runs its setup script,\n'
        'so a library analysis group arrives with its monitors built and its\n'
        'analysis script intact.\n'
        '\n'
        'Call library_list first to get a valid id.\n'
        '\n'
        'Verified live on v202:\n'
        '  library_insert(name="Qanalysis")       -> Analysis Group "Qanalysis"\n'
        '                                            + time monitors t1..t4\n'
        '  library_insert(name="ring_resonator")  -> Structure Group "ring resonator"\n'
        '                                            with 8 child waveguides\n'
        '  library_insert(name="sphere")          -> a plain Sphere (some items are\n'
        '                                            primitives, not groups)\n'
        '\n'
        'The new object keeps the library item\'s own name (spaces included, e.g.\n'
        '"ring resonator"); the reply lists what was actually added.'
        ),
        schema={'type':'object','properties':{
        'name':{'type':'string','description':'Object library catalog id, e.g. "Qanalysis" or "ring_resonator"'}},
        'required':['name']},
        destructive=True,
    ),
    ToolSpec(
        name='reference_lookup',
        module='engine',
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
        schema={'type':'object','properties':{
        'name':{'type':'string','description':'Exact Lumerical API function name, e.g. getresult'},
        'category':{'type':'string','description':'Filter by category: result|source|material|object|analysis|variable|sweep|mesh|monitor|general'},
        'list_only':{'type':'boolean','description':'If true, return only function names (ignores name/category)'}}},
        read_only=True, server_only=True,
    ),
    ToolSpec(
        name='engine_cleanup',
        module='engine',
        description=(
'Clean up Lumerical engine processes (fdtd-solutions.exe). Server-side, no bridge call.\n'
        '\n'
        'Why engines pile up: the engine is a CHILD of the bridge python, and Windows\n'
        'kills nothing when a parent dies. If the bridge is killed without its tree, or\n'
        'the MCP server is recycled while a project is open, the engine survives as a\n'
        'windowless process that holds an ANSYS license seat and ~200 MB of RAM.\n'
        '\n'
        'What it does:\n'
        '  * kills ORPHAN engines — parent process is gone, so no client can ever\n'
        '    reach them again; always safe\n'
        '  * never touches FOREIGN engines — parent is alive (e.g. your Lumerical GUI),\n'
        '    so that session is left alone\n'
        '  * restart_bridge=true also restarts the bridge (kills the whole tree and\n'
        '    respawns): use it when the engine is wedged, e.g. after a license timeout\n'
        '\n'
        'Examples:\n'
        '  engine_cleanup(dry_run=true)             -> list what would be killed\n'
        '  engine_cleanup()                          -> reap orphans, report before/after\n'
        '  engine_cleanup(restart_bridge=true)       -> fresh bridge + fresh engine'
        ),
        schema={'type':'object','properties':{
        'dry_run':{'type':'boolean','description':'Only report what would be killed (default false)'},
        'include_orphans':{'type':'boolean','description':'Kill engines whose parent process is gone (default true)'},
        'restart_bridge':{'type':'boolean','description':'Also restart the bridge and its engine (default false)'}}},
        destructive=True, server_only=True,
    ),
    ToolSpec(
        name='engine_status',
        module='engine',
        description=(
'Diagnostics for the whole MCP <-> bridge <-> engine chain. Call this when\n'
        'something looks stale, dead or inconsistent — it answers:\n'
        '  * is the running bridge executing the code on disk? (stale_code /\n'
        '    bridge_code_id vs disk_code_id — after editing bridge.py the bridge\n'
        '    process must restart before the change is live)\n'
        '  * is a project open, where is it, and is it autosaved to a temp path?\n'
        '  * which engine build and which verified-capability set is in use?\n'
        '  * how many fdtd-solutions.exe processes exist, and which are orphans?\n'
        '  * what did the last calls do (method, duration, error)?\n'
        '\n'
        'Works without an open project. Example:\n'
        '  engine_status()\n'
        '\n'
        'If engine_processes shows orphans, call engine_cleanup to reap them.'
        ),
        schema={'type':'object','properties':{}},
        read_only=True,
    ),
]


def spec_by_name():
    return {s.name: s for s in SPECS}


def visible_specs():
    return [s for s in SPECS if not s.hidden]
