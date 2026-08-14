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

自动发现 Lumerical 安装路径并打印注册命令（含检测到的 `--lumerical-home`），执行后重启 Claude Code 即可。

如果 `fdtd-mcp` 不在 PATH 上，把打印出的命令里的 `fdtd-mcp` 换成 `python -m fdtd_mcp.server`。

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

### DeepSeek Harness（DSH）

通过 DSH 官方插件 `@deepseek-ai/dsh-mcp-client`（随每个 `dsh` 安装自带）桥接本
MCP 服务器，工具以 `mcp__fdtd__*` 形式供 DSH 调用（如 `mcp__fdtd__execute`、
`mcp__fdtd__run`、`mcp__fdtd__model_add`，共 30 个）。

**注册步骤：**

1. 打开 DSH web profile 的用户补丁层 `$DSH_HOME/profiles/web/cordis.patch.yml`
   （`$DSH_HOME` 默认是 `C:\Users\<你>\.dsh`），在顶层数组中追加：

```yaml
- insert:
    - id: mcp-fdtd
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: fdtd
        transport: stdio
        command: D:/coding/anaconda3/python.exe   # ← 改成你的 Python ≥3.10（需装有 mcp 包）
        args: ['-m', 'fdtd_mcp.server']
        env:
          PYTHONPATH: D:/project/fdtd-mcp          # ← 改成你的仓库目录；若已 pip install . 可删掉
        cwd: D:/project/fdtd-mcp                   # ← 同上
        failOnStartupError: false                  # Lumerical 未就绪时只跳过工具注册，不影响 GUI 启动
```

2. **Lumerical 路径无需配置**：服务器自动发现（`--lumerical-home` 参数 >
   `LUMERICAL_HOME` 环境变量 > 常见安装目录扫描，取最新版本）。非标准安装可把
   `args` 改成 `['-m', 'fdtd_mcp.server', '--lumerical-home', '<你的路径>']`，
   或直接设置 `LUMERICAL_HOME` 环境变量。注意不要给插件配置写死一个不存在的
   `LUMERICAL_HOME`——服务器对它不做有效性检查，会短路自动发现。
3. profile 补丁层会被 DSH 实时监视，一般**无需重启**；若工具未出现，重启
   `dsh web`。
4. 验证：`dsh web --dump-config`，确认输出中是否含 `mcp-fdtd` 行。

其他 profile（如 headless）把同一片段加进
`$DSH_HOME/profiles/<name>/cordis.patch.yml`，或加到对所有 profile 生效的
`$DSH_HOME/cordis.patch.yml` 即可。此集成与 Claude Code 的 `.mcp.json` 互不影响。

## 工具（30 个，6 个模块）

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

`run` / `sweep_run` 在求解前会自动把未保存的工程存到临时路径，避免隐藏引擎被 Lumerical 不可见的"Save As"对话框堵住。注意：`addvar` 在 v202 **已废弃**——模型变量需在 GUI 里建，否则 `sweep_add` 的参数路径（如 `::model>gap`）无法解析。

| 模块 | 用途 | 工具 |
|------|------|------|
| **session** | 工程文件生命周期 | `session_open`, `session_new`, `session_close`, `session_save`, `session_save_as` |
| **model** | 对象树统一 CRUD | `model_info`, `model_add`, `model_get`, `model_set`, `model_delete`, `model_script` |
| **material** | 材料数据库 | `material_add`, `material_get`, `material_set`, `material_delete`, `material_exists` |
| **sweep** | 参数扫描生命周期 | `sweep_add`, `sweep_get`, `sweep_set`, `sweep_delete`, `sweep_run`, `sweep_result` |
| **result** | 仿真数据 | `result_list`, `result_get`, `result_save`, `result_has` |
| **engine** | 直接操作引擎 | `run`, `execute`, `execute_file`, `reference_lookup` |

## 使用示例

### 打开并审阅

```
session_open("D:/project/my_sim.fsp")
model_info()                          → 对象、材料、变量、FDTD 摘要（单次自省，防跳步）
model_get("FDTD")                     → FDTD 区域完整属性
model_script("::model", action="get") → setup + analysis 脚本
```

### 从零搭建

```
session_new(dimension="3D", x_span=2e-6, y_span=2e-6, mesh_accuracy=4)

model_add(type="fdtd")
model_add(type="rectangle", name="substrate",
          properties={"x span": 2e-6, "y span": 2e-6, "z span": 200e-9})
model_add(type="dipole", name="source_1")

model_set(name="substrate", properties={"material": "Si (Silicon) - Palik"})
model_set(name="source_1", properties={"x": 0, "y": 0, "z": 100e-9})
session_save(path="new_sim.fsp")
```

### 自定义材料

```
material_add(type="Sampled 3D data")                 → {name: "material_1"}
material_set(name="material_1", property="name", value="PA_RCP")
material_set(name="material_1", property="sampled 3d data",
             value=[[300e-9,1.5,0],[800e-9,1.5,0]])
material_set(name="material_1", property="mesh order", value=2)
model_set(name="substrate", properties={"material": "PA_RCP"})

# 或者从文件导入
execute('importnk("D:/data/nk_data.txt")')
```

### 结构组与分析组

```
model_add(type="structure_group", name="dbr_stack")
model_script(name="dbr_stack", action="set", script_type="script",
             content="addrect(); set('name', 'layer'); set('x span', 2e-6);")

model_add(type="analysis_group", name="transmission_calc")
model_script(name="transmission_calc", action="set", script_type="analysis",
             content="T = transmission('monitor');")
```

### 运行取结果

```
run()
result_has(monitor="DFT")              → 安全存在性检查，取数据前先调用
result_list(monitor="DFT")             → 发现可用数据集
result_get(monitor="DFT", data="E",
           fields=["Ex","Ey","f"])    → 指定字段（fields 必填）
result_save(monitor="DFT", data="E", output="C:/data/fields.mat")
                                       → 完整导出 .mat
```

### 参数扫描

```
sweep_add(type=0, name="thickness_sweep",
  parameters=[{"name":"t","parameter":"::model::substrate::z span",
               "start":50e-9,"stop":300e-9,"points":6}],
  results=[{"name":"T","result":"::model::monitor::T"}])
sweep_run(name="thickness_sweep")
sweep_result(name="thickness_sweep", result="T")
```

### 抗幻觉工作流

大模型对 Lumerical FDTD 的训练语料稀缺，容易臆造函数名、对象类型和变量名。以下工具帮助解决：

```
reference_lookup(list_only=true)       → 写脚本前确认函数名是否存在
                                         （内置 32 条常用 API 签名库）
reference_lookup(name="addrect")       → 查看签名与陷阱
execute("?getnamed('FDTD', 'dimension')")  → ?expr 捕获返回值

model_info()                           → 对象、GUI 模型变量、材料清单一次拿全
model_get("source_1")                  → 显式类型 + source_kind 判别器

脚本扫描                               → execute() 和 model_script(action="set")
                                         对无法识别的函数名返回非阻塞警告
```

## 文件结构

```
fdtd-mcp/
├── README.md          # English
├── README_zh.md       # 中文
├── LICENSE
├── pyproject.toml
├── install.py
├── fdtd_mcp/
│   ├── __init__.py
│   ├── discovery.py   # Lumerical 路径自动发现
│   ├── dispatch.json  # 工具名 -> bridge handler 的唯一映射表
│   ├── bridge.py      # JSON-RPC 桥接（Lumerical Python 3.6.8）
│   ├── server.py      # MCP 服务器（系统 Python）
│   └── cheatsheet/
│       └── lumapi_ref.json  # 32 条 Lumerical API 参考
└── tests/             # pytest 测试（无需 Lumerical）
```

## 依赖

- Python ≥ 3.10
- Lumerical FDTD（v202 或更高）
- `mcp`
