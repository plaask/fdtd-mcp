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

## 工具（27 个）

| 类别 | 工具 |
|------|------|
| 生命周期 | `new`, `open`, `close`, `save` |
| 总览 | `get_model_overview` |
| 审阅 | `get_scene_info`, `get_object_info`, `get_model_variables`, `get_script`, `get_parameters`, `get_sweep_info` |
| 知识库 | `get_lumapi_ref` |
| 编辑 | `set_parameter`, `set_script` |
| 执行 | `execute`, `execute_file` |
| 材料 | `add_material`, `set_material`, `get_material` |
| 运行 | `run`, `run_sweep`, `get_sweep_result` |
| 数据 | `get_results`, `list_result_fields`, `get_result_data`, `get_result_file`, `has_result` |

## 使用示例

### 打开并审阅

```
open("my_sim.fsp")
get_model_overview()                 → 对象、变量、材料（单次自省，防跳步）
get_script("::model")                → setup + analysis 脚本
get_parameters()                     → 全部模型和分组参数
```

### 抗幻觉工作流

大模型对 Lumerical FDTD 的训练语料稀缺，容易臆造函数名、对象类型和变量名。以下工具帮助解决：

```
get_model_overview()                 → 对象（默认过滤禁用）、
                                        GUI 模型变量（LD, DBR, top_layer, au, ...）、
                                        材料清单 — 一次拿到全部上下文

get_lumapi_ref(list_only=true)       → 写脚本前确认函数名是否存在
                                        （内置 32 条常用 API 签名库）

get_object_info("source_1")          → 显式类型 + source_kind 判别器
                                        （dipole / plane / tfsf / gaussian）

脚本扫描                              → execute() 和 set_script() 对不能识别的
                                        函数名返回非阻塞警告
```

### 修改并保存

```
set_parameter("wavelength", 1550e-9)
set_script("::model", "analysis", "plot_spectrum();")
save("my_sim_v2.fsp")
```

### 运行取结果

```
run()
get_results("monitor1")              → 列出可用数据集
list_result_fields("monitor1", "E")  → 预览字段名（Ex, Ey, Ez, ...）
get_result_data("monitor1", "E", fields=["Ex","T","f"])
                                     → 多字段数据（不再限于 f/lambda）
has_result("monitor1")               → 安全存在性检查，取数据前先调用
get_result_file("monitor1", "E", "C:/data/fields.mat")
                                     → 完整导出为 .mat 文件
```

### 从零搭建

```
new(dimension="3D", x_span=2e-6, y_span=2e-6, z_span=2e-6, mesh_accuracy=4)
execute("addrect()")
set_parameter("x span", 500e-9, object="rectangle")
set_parameter("material", "Si (Silicon) - Palik", object="rectangle")
execute("addfdtd()")
save("new_sim.fsp")
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
    ├── server.py      # MCP 服务器
    └── cheatsheet/
        └── lumapi_ref.json  # 32 条 Lumerical API 参考
```

## 依赖

- Python ≥ 3.10
- Lumerical FDTD（v202 或更高）
- `mcp`
