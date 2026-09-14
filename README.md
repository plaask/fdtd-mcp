# FDTD MCP

MCP server for [Lumerical FDTD](https://www.ansys.com/products/optics/fdtd) automation. Let AI assistants read, edit, run, and analyze FDTD simulations through the Model Context Protocol.

> [中文说明](README_zh.md)

## Architecture

```
AI Assistant --MCP stdio--> server.py (system Python ≥3.10)
                                │ subprocess stdin/stdout
                             bridge.py (Lumerical embed Python 3.6.8)
                                │ lumapi
                             Lumerical FDTD engine
```

The dual-process design isolates the MCP protocol (which needs modern Python) from the Lumerical API (which only runs on the bundled Python 3.6.8). The bridge communicates via line-delimited JSON over stdin/stdout.

## Installation

**Prerequisites:** Python ≥ 3.10, [Lumerical FDTD](https://www.ansys.com/products/optics/fdtd)

### 1. Install the package

```bash
git clone https://github.com/plaask/fdtd-mcp.git && cd fdtd-mcp
pip install .
```

### 2. Register with Claude Code

```bash
python install.py
```

This auto-detects your Lumerical installation and prints the registration command (which includes the detected `--lumerical-home`). Run the printed command, restart Claude Code, done.

If `fdtd-mcp` is not on your PATH, replace it with `python -m fdtd_mcp.server` in the printed command.

### If auto-detection fails

If Lumerical is installed at a non-standard location, specify the path manually:

**Option A — pass it in the registration command:**

```bash
claude mcp add fdtd -- python -m fdtd_mcp.server --lumerical-home "C:/Program Files/Lumerical/v241"
```

**Option B — set the env var once:**

```powershell
[Environment]::SetEnvironmentVariable("LUMERICAL_HOME", "C:/Program Files/Lumerical/v241", "User")
# Restart the terminal, then just:
claude mcp add fdtd -- python -m fdtd_mcp.server
```

### Other MCP clients (Cursor, VS Code, etc.)

The JSON equivalent of `claude mcp add`. Auto-detection works here too:

```json
{
  "mcpServers": {
    "fdtd": {
      "command": "python",
      "args": ["-m", "fdtd_mcp.server"]
    }
  }
}
```

If auto-detection fails, add the `--lumerical-home` argument:

```json
{
  "mcpServers": {
    "fdtd": {
      "command": "python",
      "args": ["-m", "fdtd_mcp.server", "--lumerical-home", "C:/Program Files/Lumerical/v241"]
    }
  }
}
```

### DeepSeek Harness (DSH)

The MCP server is bridged into DSH through its official plugin
`@deepseek-ai/dsh-mcp-client` (ships with every `dsh` install). Tools are exposed
to DSH as `mcp__fdtd__*` (e.g. `mcp__fdtd__execute`, `mcp__fdtd__run`,
`mcp__fdtd__model_add`; 34 tools total).

**Registration steps:**

1. Open the web profile's user patch layer `$DSH_HOME/profiles/web/cordis.patch.yml`
   (`$DSH_HOME` defaults to `C:\Users\<you>\.dsh`) and append to the top-level array:

```yaml
- insert:
    - id: mcp-fdtd
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: fdtd
        transport: stdio
        command: D:/coding/anaconda3/python.exe   # ← your Python ≥3.10 (with the mcp package)
        args: ['-m', 'fdtd_mcp.server']
        env:
          PYTHONPATH: D:/project/fdtd-mcp          # ← your checkout; can be removed after pip install .
        cwd: D:/project/fdtd-mcp                   # ← same
        failOnStartupError: false                  # skip tool registration, never block GUI boot
```

2. **No Lumerical path configuration needed**: the server auto-detects it
   (`--lumerical-home` arg > `LUMERICAL_HOME` env var > scanning common install
   directories, newest version wins). For non-standard installs, change `args` to
   `['-m', 'fdtd_mcp.server', '--lumerical-home', '<your-path>']` or set the
   `LUMERICAL_HOME` environment variable. Do NOT hardcode a nonexistent
   `LUMERICAL_HOME` in the plugin config — the server trusts it without
   validation and auto-detection would be short-circuited.
3. The profile patch layer is watched live by DSH, so a restart is usually **not
   needed**; if the tools do not appear, restart `dsh web`.
4. Verify with `dsh web --dump-config` — the output should contain the `mcp-fdtd` row.

For other profiles (e.g. headless), add the same snippet to
`$DSH_HOME/profiles/<name>/cordis.patch.yml`, or to the home-level
`$DSH_HOME/cordis.patch.yml` which applies to every profile. This integration does
not affect the Claude Code `.mcp.json` registration.

## Tools (34 tools, 7 modules)

```
session (5)     session_open, session_new, session_close,
                session_save, session_save_as

model (6)       model_info, model_add, model_get, model_set,
                model_delete, model_script

material (5)    material_add, material_get, material_set,
                material_delete, material_exists

sweep (6)       sweep_add, sweep_get, sweep_set, sweep_delete,
                sweep_run, sweep_result

result (4)      result_list, result_get, result_save, result_has

engine (6)      run, execute, execute_file, reference_lookup, engine_status,
                engine_cleanup

library (2)     library_list, library_insert
```

`run` / `sweep_run` auto-save an unsaved project to a temp path before solving,
so they never block on Lumerical's invisible "Save As" dialog when the engine is
run hidden. That temp path is reported in `saved_to` but never masquerades as the
project path — `session_save()` with no path still tells you the project is
unsaved.

**Creating model parameters on v202** — verified against a live engine:
`addvar`/`setvar`/`getvar` were removed. The supported route is
`addanalysisprop("name", type_code, value)` for **`::model` and Analysis Groups**
and `adduserprop(...)` for **Structure Groups**, with the target **selected**
(`type_code` is an integer: 0=number, 1=text, 2=length, 3=time, 4=frequency,
5=material). The split matters: the GUI's *Edit analysis group* dialog lists
**analysis** properties in its Analysis ▸ Variables table, so a `::model`
parameter has to go through `addanalysisprop` to appear there — a property
created with `adduserprop` does not show up in that dialog. Analysis *results*
(the dialog's Results table) are declared with `addanalysisresult("name")`.
`model_set` does both:

```
model_set(name="::model", properties={"gap": 200e-9})                  # new model parameter
model_set(name="::model", properties={"gap": 200e-9},
          property_types={"gap": "length"})                             # GUI unit type
model_set(name="::model", results=["T_spectrum", "R_spectrum"])         # Results table rows

sweep_add(..., parameters=[{"name": "gap", "parameter": "::model::gap",
                            "start": 100e-9, "stop": 300e-9, "points": 5}])
```

Declaration is not idempotent and the analysis/user namespaces share names:
re-declaring `gap` creates `gap2`, so declare once and update with the same
`model_set` call afterwards. `select("::model"); queryanalysisprop("::model")`
and `queryanalysisresult("::model")` report exactly what that dialog shows.

`addsweepparameter` does not validate that the parameter path exists — a typo
only fails when the sweep runs, so `model_get` the target first.

### Module overview

| Module | Purpose | Tools |
|--------|---------|-------|
| **session** | Project file lifecycle | `session_open`, `session_new`, `session_close`, `session_save`, `session_save_as` |
| **model** | Object tree unified CRUD | `model_info`, `model_add`, `model_get`, `model_set`, `model_delete`, `model_script` |
| **material** | Material database | `material_add`, `material_get`, `material_set`, `material_delete`, `material_exists` |
| **sweep** | Parameter sweep lifecycle | `sweep_add`, `sweep_get`, `sweep_set`, `sweep_delete`, `sweep_run`, `sweep_result` |
| **result** | Simulation data | `result_list`, `result_get`, `result_save`, `result_has` |
| **engine** | Direct engine interaction | `run`, `execute`, `execute_file`, `reference_lookup`, `engine_status`, `engine_cleanup` |
| **library** | Lumerical object library | `library_list`, `library_insert` |

### model_add type table

| type | Lumerical command | Category |
|------|-------------------|----------|
| `rectangle`, `circle`, `ring`, `polygon`, `sphere`, `pyramid`, `triangle`, `waveguide` | `addrect`, `addcircle`, ... | Geometry |
| `fdtd` | `addfdtd` | Solver |
| `mesh` | `addmesh` | Mesh |
| `dipole`, `tfsf`, `plane`, `gaussian`, `mode_source` | `adddipole`, `addtfsf`, ... | Source |
| `power_monitor`, `dft_monitor`, `index_monitor`, `field_monitor`, `movie_monitor` | `addpower`, `addindex`, `addmovie` — every frequency-domain monitor is an `addpower` object on v202 (`addfield`/`adddftmonitor` do not exist) | Monitor |
| `structure_group`, `analysis_group` | `addstructuregroup`, `addanalysisgroup` | Group |

### Object library (structure groups and analysis groups)

`model_add` only creates *empty* groups. Ready-made library items — 129 of them,
shipped with the install in `<LUMERICAL_HOME>/Resources/fdtd-library` (96
`Structures` + 33 `Analysis`) — are inserted with `library_insert`, which wraps
Lumerical's `addobject("<catalog id>")`: the item's `.fsp` is copied in and its
setup script re-runs, so a library analysis group arrives with its monitors
built and its analysis script intact.

```
library_list(type="Analysis")            → 33 analysis groups, with categories
library_list(category="Gratings")        → one catalog category
library_list(id="Qanalysis")             → one entry
library_insert(name="Qanalysis")         → Analysis Group "Qanalysis" + monitors t1..t4
library_insert(name="ring_resonator")    → Structure Group "ring resonator" + 8 waveguides
```

Ids are catalog ids and the created object keeps the item's own name (spaces
included: `"ring resonator"`). Verified live on v202: some items are primitives
rather than groups (`sphere`), and an unknown id is a **silent no-op** inside the
engine — which is why `library_insert` validates the id against `catalog.xml`
first and then reports exactly which objects exist afterwards (it raises instead
of claiming success when the engine rolls an insert back).

### Engine process hygiene

`lumapi.FDTD()` spawns `fdtd-solutions.exe` as a **child of the bridge process**,
and Windows does not reap children when a parent dies — so every crash, timeout or
MCP-server recycle used to leave a hidden engine behind, holding an ANSYS license
seat and a few hundred MB. The engine pid is now tracked and bound to a
kill-on-close job object (a hard kill of the bridge takes the engine with it),
the bridge closes its session on the EOF path, and stale engines left by a
previous server generation are reaped at startup. Classification is the safety
rule:

| Kind | Meaning | Action |
|------|---------|--------|
| `owned` / `bridge` | this session's engine | freed by closing/restarting the session |
| `orphan` | parent process is gone — unreachable | **safe to kill**, reaped automatically |
| `foreign` | another live client (e.g. your Lumerical GUI) | **never touched** |

```
engine_status()                          → engine_processes: owned/orphan/foreign +
                                           stale_code (is the bridge running old code?)
engine_cleanup(dry_run=true)             → list what would be killed
engine_cleanup(restart_bridge=true)      → fresh bridge + fresh engine when wedged
```

Set `FDTD_MCP_ORPHAN_SWEEP=0` to disable the startup sweep.

## Usage examples

### Open and inspect

```
session_open("D:/project/my_sim.fsp")
model_info()                           → objects, materials, variables, FDTD summary (one call)
model_get("FDTD")                      → full properties of the FDTD region
model_script("::model", action="get")  → setup + analysis scripts
```

### Build from scratch

```
session_new(dimension="3D", x_span=2e-6, y_span=2e-6, mesh_accuracy=4)

# Add objects
model_add(type="fdtd")
model_add(type="rectangle", name="substrate",
          properties={"x span": 2e-6, "y span": 2e-6, "z span": 200e-9})
model_add(type="dipole", name="source_1")

# Set properties
model_set("substrate", {"material": "Si (Silicon) - Palik"})
model_set("source_1", {"x": 0, "y": 0, "z": 100e-9, "wavelength start": 500e-9})
session_save("new_sim.fsp")
```

### Custom materials

```
material_add(type="Sampled 3D data")                      → {name: "New material 1"}
material_set("New material 1", "name", "PA_RCP")
material_set("PA_RCP", "sampled 3d data",
  [[300e-9, 1.52], [800e-9, 1.46]])                       # Nx2 [wl, n] or Nx4 [wl, n, k, 0]
material_set("PA_RCP", "mesh order", 2)                   # v202 rejects 3 columns

# Assign to an object
model_set("substrate", {"material": "PA_RCP"})

# Or import from file
execute('importnk("D:/data/nk_data.txt")')
model_set("substrate", {"material": "nk_data"})
```

### Structure groups and analysis groups

```
# Create a structure group with script
model_add(type="structure_group", name="dbr_stack")
model_set("dbr_stack", {"x": 0, "y": 0})
model_script("dbr_stack", action="set", script_type="script",
  content="addrect(); set('name', 'layer'); set('x span', 2e-6);")

# Create an analysis group
model_add(type="analysis_group", name="transmission_calc")
model_script("transmission_calc", action="set", script_type="setup",
  content="addpower(); set('name', 'monitor');")
model_script("transmission_calc", action="set", script_type="analysis",
  content="T = transmission('monitor');")
```

### Run and get data

```
run()
result_has("monitor")                  → check before fetching
result_list("monitor")                 → discover available datasets
result_get("monitor", data="E",
  fields=["Ex", "Ey", "f"])           → get specific fields (fields is REQUIRED)
result_save("monitor", data="E",
  output="C:/data/fields.mat")        → export to .mat file
```

### Parameter sweeps

```
# Create and run
sweep_add(type=0, name="thickness_sweep",
  parameters=[{"name": "t", "parameter": "::model::substrate::z span",
               "start": 50e-9, "stop": 300e-9, "points": 6}],
  results=[{"name": "T", "result": "::model::monitor::T"}])
sweep_run(name="thickness_sweep")
sweep_result(name="thickness_sweep", result="T")   → get sweep data
```

### Anti-hallucination

```
reference_lookup(list_only=true)       → verify function names exist
reference_lookup(name="addrect")       → get signature + pitfalls
execute("?getnamed('FDTD', 'dimension')")  → ?expr captures return value
```

### Diagnostics — "is the running code actually my code?"

```
engine_status()
  → {bridge_code_id, disk_code_id, stale_code, project_path, autosave_path,
     session_generation, engine: {install_version, verified_against},
     recent_calls: [...], server: {server_code_id, tool_count, ...}}
```

`stale_code: true` means the bridge process predates your edit: bridge-side
changes (`bridge.py`, `handlers/`, `engine/`) only apply once that process
restarts. `engine_status` also works with no project open, and reports the last
50 calls with timings and errors.

## Key design principles

- **Unified CRUD** — every module uses consistent `add`/`get`/`set`/`delete` naming
- **Single-source dispatch** — `dispatch.json` maps every tool to its bridge handler; both processes read the same table, so the two sides can't drift (and a test enforces it)
- **Execute is transparent** — `execute(code)` passes LSF directly to the engine with no parsing
- **model_set declares missing properties** — updates go through `setnamed`; a property that does not exist yet is declared with `adduserprop`/`addanalysisprop` (integer type code) on the model root or a group, and anything that still fails is reported in `properties_failed`/`failures` with `status: partial|error` instead of a silent `ok`
- **result_get requires fields** — call `result_list` first to discover available fields, then request only what you need
- **Short names resolve** — `model_get("FDTD")` works without the `::model::` prefix

## Files

```
fdtd-mcp/
├── README.md
├── README_zh.md
├── LICENSE
├── pyproject.toml
├── install.py
├── fdtd_mcp/
│   ├── __init__.py
│   ├── discovery.py      # auto-detect Lumerical installation
│   ├── toolspec.py       # declarative tool catalogue (schema, description,
│   │                     #   annotations, defaults, timeout class)
│   ├── dispatch.json     # single source of truth for tool -> bridge handler
│   ├── bridge.py         # JSON-RPC bridge shell (Lumerical Python 3.6.8)
│   ├── engine/           # bridge-side primitives (Python 3.6.8)
│   │   ├── constants.py  #   caps, LSF facts, run() regex
│   │   ├── errors.py     #   BridgeError — the expected-failure channel
│   │   ├── lsf.py        #   quoting, temp vars, `?expr` parsing
│   │   ├── capabilities.py  # verified engine facts + code fingerprint
│   │   ├── proctree.py   #   engine-process bookkeeping (owned/orphan/foreign,
│   │   │                 #     job-object binding, TerminateProcess)
│   │   ├── library.py    #   object library catalog (Resources/fdtd-library)
│   │   └── base.py       #   shared mix-in (_call_lsf, sanitising, name resolve)
│   ├── handlers/         # one mix-in per module: session, model, material,
│   │                     #   sweep, result, library, engine_tools
│   ├── server.py         # MCP server (system Python)
│   └── cheatsheet/
│       └── lumapi_ref.json  # Lumerical API reference (verified_against per entry)
└── tests/
    ├── test_*.py            # pytest suite against tests/fake_engine.py (no Lumerical needed)
    ├── test_capability_guard.py  # code <-> cheatsheet <-> allowlist consistency
    ├── test_toolspec.py     # the declarative catalogue stays consistent
    ├── test_proctree.py     # which engine processes may be killed (never foreign)
    ├── test_engine_reaping.py    # session transitions / exit never orphan an engine
    ├── test_library.py      # object-library catalog + insert (fake and real layout)
    ├── run_bridge_live.py   # opt-in real-engine smoke test (needs a Lumerical install)
    ├── live_orphan_check.py # opt-in: EOF / hard-kill / close leave no engine behind
    └── test_live_smoke.py   # pytest wrapper for it (FDTD_MCP_LIVE=1)
```

Run the offline suite (no Lumerical needed) and the real-engine check:

```
python -m pytest tests -q                                  # fake engine
$env:FDTD_MCP_LIVE=1; python -m pytest tests/test_live_smoke.py   # real engine
"%LUMERICAL_HOME%\python-3.6.8-embed-amd64\python.exe" tests/run_bridge_live.py
```

## Requirements

- Python ≥ 3.10
- Lumerical FDTD (v202 or later)
- `mcp`

## Timeouts

`run`, `sweep_run`, `execute`, `execute_file` block until the engine finishes.
By default there is **no timeout**, so legitimate long simulations are never cut
short. If you want a bound (e.g. to stop a hung engine freezing the session),
set the `FDTD_MCP_CALL_TIMEOUT` env var in seconds, or pass `timeout=<seconds>`
to a single call. On expiry the bridge is killed and auto-restarted on the next
call — note this discards unsaved in-memory engine state, so save your project
before running long simulations (the caller is warned on the next call).

Engine launches (`session_open`, `session_new`, `session_close`) additionally get
a short bound — `FDTD_MCP_LAUNCH_TIMEOUT`, default 300 s — so a wedged launch
reports a diagnosable error instead of hanging the session for minutes.
