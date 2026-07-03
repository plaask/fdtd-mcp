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

This auto-detects your Lumerical installation and prints the registration command. Run it, restart Claude Code, done.

If auto-detection succeeds, the printed command will be as simple as:

```
claude mcp add fdtd -- python -m fdtd_mcp.server
```

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

## Tools (21)

| Category | Tools |
|----------|-------|
| Lifecycle | `new`, `open`, `close`, `save` |
| Inspect | `get_scene_info`, `get_script`, `get_parameters`, `get_results`, `get_sweep_info` |
| Edit | `set_parameter`, `set_script` |
| Execute | `execute`, `execute_file` |
| Materials | `add_material`, `set_material`, `get_material` |
| Run | `run`, `run_sweep`, `get_sweep_result` |
| Data | `get_result_data`, `get_result_file` |

## Usage examples

### Open and inspect

```
open("my_sim.fsp")
get_scene_info()                 → all objects + properties
get_script("::model")            → setup + analysis scripts
get_parameters()                 → all model & group parameters
```

### Edit and save

```
set_parameter("wavelength", 1550e-9)
set_script("::model", "analysis", "plot_spectrum();")
save("my_sim_v2.fsp")
```

### Run and get data

```
run()
get_results("monitor1")          → list available datasets
get_result_data("monitor1", "T") → wavelength/transmission
```

### Build from scratch

```
new(dimension="3D", x_span=2e-6, y_span=2e-6, z_span=2e-6, mesh_accuracy=4)
execute("addrect()")
set_parameter("x span", 500e-9, object="rectangle")
set_parameter("material", "Si (Silicon) - Palik", object="rectangle")
execute("addfdtd()")
save("new_sim.fsp")
```

### Custom materials

```
add_material(type="Sampled 3D data")                 → {name: "material_1"}
set_material("material_1", "nk data",
  [[300e-9,800e-9], [1.52,1.52], [0.001,0.001]])
set_material("material_1", "mesh order", 2)
# Built-in materials need no setup:
execute("addrect()")
set_parameter("material", "Au (Gold) - Johnson and Christy", object="rectangle")
```

## Files

```
fdtd-mcp/
├── README.md
├── README_zh.md
├── LICENSE
├── pyproject.toml
├── install.py
└── fdtd_mcp/
    ├── __init__.py
    ├── discovery.py      # auto-detect Lumerical installation
    ├── bridge.py         # JSON-RPC bridge (Lumerical Python 3.6.8)
    └── server.py         # MCP server (system Python)
```

## Requirements

- Python ≥ 3.10
- Lumerical FDTD (v202 or later)
- `mcp`
