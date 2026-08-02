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

## Tools (30 tools, 6 modules)

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

engine (4)      run, execute, execute_file, reference_lookup
```

`run` / `sweep_run` auto-save an unsaved project to a temp path before solving,
so they never block on Lumerical's invisible "Save As" dialog when the engine is
run hidden. Note: model variables (`addvar`) are **not available in Lumerical
v202** — create them in the GUI, or `sweep_add` parameter paths like
`::model>gap` will not resolve.

### Module overview

| Module | Purpose | Tools |
|--------|---------|-------|
| **session** | Project file lifecycle | `session_open`, `session_new`, `session_close`, `session_save`, `session_save_as` |
| **model** | Object tree unified CRUD | `model_info`, `model_add`, `model_get`, `model_set`, `model_delete`, `model_script` |
| **material** | Material database | `material_add`, `material_get`, `material_set`, `material_delete`, `material_exists` |
| **sweep** | Parameter sweep lifecycle | `sweep_add`, `sweep_get`, `sweep_set`, `sweep_delete`, `sweep_run`, `sweep_result` |
| **result** | Simulation data | `result_list`, `result_get`, `result_save`, `result_has` |
| **engine** | Direct engine interaction | `run`, `execute`, `execute_file`, `reference_lookup` |

### model_add type table

| type | Lumerical command | Category |
|------|-------------------|----------|
| `rectangle`, `circle`, `ring`, `polygon`, `sphere`, `pyramid`, `triangle`, `waveguide` | `addrect`, `addcircle`, ... | Geometry |
| `fdtd` | `addfdtd` | Solver |
| `mesh` | `addmesh` | Mesh |
| `dipole`, `tfsf`, `plane`, `gaussian`, `mode_source` | `adddipole`, `addtfsf`, ... | Source |
| `power_monitor`, `dft_monitor`, `index_monitor`, `field_monitor`, `movie_monitor` | `addpower`, `adddftmonitor`, ... | Monitor |
| `structure_group`, `analysis_group` | `addstructuregroup`, `addanalysisgroup` | Group |

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
material_add(type="Sampled 3D data")                      → {name: "material_1"}
material_set("material_1", "name", "PA_RCP")
material_set("material_1", "sampled 3d data",
  [[300e-9, 1.5+0.001i], [800e-9, 1.5+0.001i]])          # Nx2 [wl, n+ik]
material_set("material_1", "mesh order", 2)

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

## Key design principles

- **Unified CRUD** — every module uses consistent `add`/`get`/`set`/`delete` naming
- **Single-source dispatch** — `dispatch.json` maps every tool to its bridge handler; both processes read the same table, so the two sides can't drift (and a test enforces it)
- **Execute is transparent** — `execute(code)` passes LSF directly to the engine with no parsing
- **model_set handles variables automatically** — uses `addvar`/`addanalysisprop`/`adduserprop` based on object type
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
│   ├── dispatch.json     # single source of truth for tool -> bridge handler
│   ├── bridge.py         # JSON-RPC bridge (Lumerical Python 3.6.8)
│   ├── server.py         # MCP server (system Python)
│   └── cheatsheet/
│       └── lumapi_ref.json  # Lumerical API reference
└── tests/                # pytest suite (no Lumerical needed)
```

## Requirements

- Python ≥ 3.10
- Lumerical FDTD (v202 or later)
- `mcp`

## Timeouts for long-running calls

`run`, `sweep_run`, `execute`, `execute_file` block until the engine finishes.
By default there is **no timeout**, so legitimate long simulations are never cut
short. If you want a bound (e.g. to stop a hung engine freezing the session),
set the `FDTD_MCP_CALL_TIMEOUT` env var in seconds, or pass `timeout=<seconds>`
to a single call. On expiry the bridge is killed and auto-restarted on the next
call — note this discards unsaved in-memory engine state, so save your project
before running long simulations.
