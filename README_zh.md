# FDTD MCP

[Lumerical FDTD](https://www.ansys.com/products/optics/fdtd) 自动化 MCP Server。通过 Model Context Protocol 让 AI 助手读取、编辑、运行和分析 FDTD 仿真工程。

> [English](README.md)

## 架构

```
AI 助手 --MCP stdio--> server.py (系统 Python ≥3.10)
                            │ 子进程 stdin/stdout
                         bridge.py (Lumerical 内置 Python 3.6.8)
                            │ lumapi
                         Lumerical FDTD 引擎
```

双进程设计隔离了两个 Python 运行时的版本冲突：MCP 协议需要 Python ≥3.10，而 Lumerical 的 `lumapi` 只能在它自带的 Python 3.6.8 上运行。bridge 通过 stdin/stdout 以行分隔 JSON 与 server 通信。

## 安装

**前提：** Python ≥ 3.10，[Lumerical FDTD](https://www.ansys.com/products/optics/fdtd)

### 1. 安装包

```bash
git clone https://github.com/plaask/fdtd-mcp.git && cd fdtd-mcp
pip install .
```

### 2. 注册到 Claude Code

```bash
python install.py
```

自动发现 Lumerical 安装路径并打印注册命令，执行后重启 Claude Code 即可。

如果自动发现成功，打印的命令就是最简单的形式：

```
claude mcp add fdtd -- python -m fdtd_mcp.server
```

### 如果自动发现失败

Lumerical 装在非标准位置时，手动指定路径：

**方式 A — 注册时直接指定：**

```bash
claude mcp add fdtd -- python -m fdtd_mcp.server --lumerical-home "C:/Program Files/Lumerical/v241"
```

**方式 B — 设一次环境变量，一劳永逸：**

```powershell
[Environment]::SetEnvironmentVariable("LUMERICAL_HOME", "C:/Program Files/Lumerical/v241", "User")
# 重启终端后直接注册
claude mcp add fdtd -- python -m fdtd_mcp.server
```

### 其他 MCP 客户端（Cursor、VS Code 等）

和 `claude mcp add` 等价的 JSON 配置。自动发现同样生效：

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

自动发现失败时加上 `--lumerical-home`：

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

## 工具（21 个）

| 类别 | 工具 |
|------|------|
| 生命周期 | `new`, `open`, `close`, `save` |
| 审阅 | `get_scene_info`, `get_script`, `get_parameters`, `get_results`, `get_sweep_info` |
| 编辑 | `set_parameter`, `set_script` |
| 执行 | `execute`, `execute_file` |
| 材料 | `add_material`, `set_material`, `get_material` |
| 运行 | `run`, `run_sweep`, `get_sweep_result` |
| 数据 | `get_result_data`, `get_result_file` |

## 典型工作流

### 审阅已有工程
```
open("project.fsp")
get_scene_info()                     → 所有对象 + 属性
get_script("::model")                → setup + analysis 脚本
get_parameters()                     → 全部参数: {::model:{gap,...}, Cnorm:{...}, ...}
get_sweep_info("dpgap")              → 扫参配置
```

### 修改并保存
```
set_parameter("gap", 200e-9)
set_parameter("LR", 0, object="LR_tfsf")
set_script("::model", "analysis", new_code)
save("project_v2.fsp")
```

### 运行取结果
```
run()
get_results("::model")               → ["dpsource","P_L","P_R","g_lum"]
get_result_data("::model", "g_lum")  → {lambda:[...], ...}
get_result_file("DFT", "E", "fields.mat")
```

### 从零搭建
```
new(dimension="3D", x_span=1e-6, y_span=1e-6, z_span=2e-6)
execute("addtfsf()")
set_parameter("wavelength start", 400e-9, object="TFSF")
set_parameter("wavelength stop", 800e-9, object="TFSF")
execute("addpower()")
set_parameter("monitor type", "3D", object="power")
save("my_sim.fsp")
```

### 自定义材料
```
add_material(type="Sampled 3D data")                 → {name: "material_1"}
set_material("material_1", "nk data",
  [[300e-9,800e-9], [1.52,1.52], [0.001,0.001]])
set_material("material_1", "mesh order", 2)
# 内置材料直接使用：
execute("addrect()")
set_parameter("material", "Au (Gold) - Johnson and Christy", object="rectangle")
```

## 文件结构

```
fdtd-mcp/
├── README.md          # English
├── README_zh.md       # 中文
├── LICENSE
├── pyproject.toml
├── install.py
└── fdtd_mcp/
    ├── __init__.py
    ├── discovery.py   # Lumerical 路径自动发现
    ├── bridge.py      # JSON-RPC 桥接
    └── server.py      # MCP 服务器
```

## 依赖

- Python ≥ 3.10
- Lumerical FDTD（v202 或更高）
- `mcp`
