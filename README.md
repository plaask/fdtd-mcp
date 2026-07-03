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

```bash
# 1. Clone and install
git clone https://github.com/plaask/fdtd-mcp.git && cd fdtd-mcp
pip install .

# 2. Print registration command（auto-detects Lumerical path）
python install.py

# 3. Run the printed command, e.g.:
#   claude mcp add fdtd -- python -m fdtd_mcp.server --lumerical-home "D:/Software/Lumerical/v202"

# 4. Restart your MCP client
```

### Manual path specification

If auto-detection fails, specify the Lumerical installation path:

```bash
# Via environment variable
export LUMERICAL_HOME=/opt/lumerical/v202

# Or via CLI argument in the registration command
claude mcp add fdtd -- python -m fdtd_mcp.server --lumerical-home "D:/Software/Lumerical/v202"
```

### Other MCP clients

Add to your client's MCP server configuration:

```json
{
  "mcpServers": {
    "fdtd": {
      "command": "python",
      "args": ["-m", "fdtd_mcp.server", "--lumerical-home", "/opt/lumerical/v202"]
    }
  }
}
```

Adjust `--lumerical-home` to match your installation.

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

## Workflows

### Review an existing project
```
open("project.fsp")
get_scene_info()                     → all objects + properties
get_script("::model")                → setup + analysis scripts
get_parameters()                     → all params: {::model:{gap,...}, Cnorm:{...}, ...}
get_sweep_info("dpgap")              → sweep config
```

### Modify and save
```
set_parameter("gap", 200e-9)
set_parameter("LR", 0, object="LR_tfsf")
set_script("::model", "analysis", new_code)
save("project_v2.fsp")
```

### Run and extract results
```
run()
get_results("::model")               → ["dpsource","P_L","P_R","g_lum"]
get_result_data("::model", "g_lum")  → {lambda:[...], ...}
get_result_file("DFT", "E", "fields.mat")
```

### Build from scratch
```
new(dimension="3D", x_span=1e-6, y_span=1e-6, z_span=2e-6)
execute("addtfsf()")
set_parameter("wavelength start", 400e-9, object="TFSF")
set_parameter("wavelength stop", 800e-9, object="TFSF")
execute("addpower()")
set_parameter("monitor type", "3D", object="power")
save("my_sim.fsp")
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
