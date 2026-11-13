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
`mcp__fdtd__run`、`mcp__fdtd__model_add`，共 38 个）。

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
        command: python   # ← 改成你的 Python ≥3.10（需装有 mcp 包）
        args: ['-m', 'fdtd_mcp.server']
        env:
          PYTHONPATH: <repo>          # ← 改成你的仓库目录；若已 pip install . 可删掉
        cwd: <repo>                   # ← 同上
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

## 工具（38 个，8 个模块）

```
session (5)     session_open, session_new, session_close,
                session_save, session_save_as

model (8)       model_info, stack_report, import_add, model_add, model_get,
                model_set, model_delete, model_script

material (6)    material_add, material_get, material_set, material_delete,
                material_exists, material_index

attribute (1)   attribute_add

sweep (6)       sweep_add, sweep_get, sweep_set, sweep_delete,
                sweep_run, sweep_result

result (4)      result_list, result_get, result_save, result_has

engine (6)      run, execute, execute_file, reference_lookup, engine_status,
                engine_cleanup

library (2)     library_list, library_insert
```

`run` / `sweep_run` 在求解前会自动把未保存的工程存到临时路径，避免隐藏引擎被 Lumerical 不可见的"Save As"对话框堵住（临时路径不会冒充工程路径，`session_save()` 无参数时仍会提示未保存）。

**v202 模型变量（参数）的创建方式**（已在真机验证）：`addvar`/`setvar`/`getvar` 在 v202 已被移除。正确做法是把 `::model` 选中后调用 `adduserprop("name", type_code, value)`（type_code 为整数：0=number, 1=text, 2=length, 3=time, 4=frequency, 5=material）；Analysis Group 用 `addanalysisprop`，Structure Group 用 `adduserprop`。`model_set` 已经自动完成这一步：

```
model_set(name="::model", properties={"gap": 200e-9})                 # 新建模型变量
model_set(name="::model", properties={"gap": 200e-9},
          property_types={"gap": "length"})                            # 指定 GUI 单位类型
sweep_add(..., parameters=[{"name": "gap", "parameter": "::model::gap",
                            "start": 100e-9, "stop": 300e-9, "points": 5}])
```

注意 `addsweepparameter` 不会校验参数路径是否存在——写错名字要等扫描运行时才报错，建议先 `model_get` 确认。

| 模块 | 用途 | 工具 |
|------|------|------|
| **session** | 工程文件生命周期 | `session_open`, `session_new`, `session_close`, `session_save`, `session_save_as` |
| **model** | 对象树统一 CRUD | `model_info`, `stack_report`, `import_add`, `model_add`, `model_get`, `model_set`, `model_delete`, `model_script` |
| **material** | 材料数据库 | `material_add`, `material_get`, `material_set`, `material_delete`, `material_exists`, `material_index` |
| **attribute** | 网格属性（Attributes 组） | `attribute_add` |
| **sweep** | 参数扫描生命周期 | `sweep_add`, `sweep_get`, `sweep_set`, `sweep_delete`, `sweep_run`, `sweep_result` |
| **result** | 仿真数据 | `result_list`, `result_get`, `result_save`, `result_has` |
| **engine** | 直接操作引擎 | `run`, `execute`, `execute_file`, `reference_lookup`, `engine_status`, `engine_cleanup` |
| **library** | Lumerical 对象库 | `library_list`, `library_insert` |

新增能力（v202 实测）：

* `attribute_add` 创建 GUI Attributes 组的 5 种 grid attribute（LC orientation / permittivity rotation / matrix transform / np density / temperature）。
* `model_add` 新增 `port`、`mode_expansion`、`container_group`、`custom`、`planar_solid`、`stack`、`layer_builder`；`port` 需要先有 FDTD 区域。
* `material_index` 按 xx/yy/zz 采样各向异性材料；`material_set` 的 `sampled 3d data` 4 列是 `[f, eps_xx, eps_yy, eps_zz]`（频率 Hz + 复介电常数，不是 n/k）。
* `stack_report` 按 z 排序并标出重叠、间隙、薄层、超出 FDTD z 范围、填满横向 PML 的结构。
* `import_add` 先用 GDSII（`gdsimport`）；STL/表面/Image 后续。
* `sweep_set` 会检查 `setsweep` 的 0/1 返回值；未知属性（如 `solver`）报 `status: error`，不再静默成功。

### 对象库（结构组 / 分析组）

`model_add` 只能建**空**组。安装自带的现成库元素共 129 个（`<LUMERICAL_HOME>/Resources/fdtd-library`，96 个 `Structures` + 33 个 `Analysis`）用 `library_insert` 插入，它封装了 Lumerical 的 `addobject("<catalog id>")`：把库元素的 `.fsp` 拷进来并**重跑其 setup 脚本**，所以库里的分析组插进来时监视器已经建好、analysis script 也完整。

```
library_list(type="Analysis")            → 33 个分析组（含分类）
library_list(category="Gratings")        → 按分类过滤
library_list(id="Qanalysis")             → 单个条目
library_insert(name="Qanalysis")         → Analysis Group "Qanalysis" + 监视器 t1..t4
library_insert(name="ring_resonator")    → Structure Group "ring resonator" + 8 个波导
```

id 就是 catalog 里的 id；插入后的对象沿用库元素自带名字（**含空格**，如 `"ring resonator"`）。真机实测：部分库元素本身就是基本体（如 `sphere`）；**未知 id 在引擎里是静默无效**的，所以 `library_insert` 会先按 `catalog.xml` 校验 id，再逐个核验对象确实存在（被引擎回滚时直接报错，而不是回 `ok`）。

### 引擎进程卫生

`lumapi.FDTD()` 把 `fdtd-solutions.exe` 生成为**桥进程的子进程**，而 Windows 不会因父进程死亡回收子进程——于是每次崩溃、超时或 MCP server 回收都会留下一个看不见的引擎，占着一个 ANSYS license seat 和几百 MB。现在引擎 pid 会被记录并加入 kill-on-close 的 job object（桥被强杀也会带走引擎），桥在 EOF 路径上主动关会话，上一代 server 留下的僵尸引擎在启动时清扫。分类是安全规则：

| 类别 | 含义 | 处理 |
|------|------|------|
| `owned` / `bridge` | 本会话的引擎 | 关闭/重启会话时释放 |
| `orphan` | 父进程已消失——再也无法访问 | **可安全杀掉**，启动时自动清扫 |
| `foreign` | 其他活着的客户端（如你开的 Lumerical GUI） | **绝不触碰** |

```
engine_status()                          → engine_processes: owned/orphan/foreign +
                                           stale_code（桥是否还在跑旧代码）
engine_cleanup(dry_run=true)             → 先看会杀谁
engine_cleanup(restart_bridge=true)      → 引擎卡死时换新桥+新引擎
```

设 `FDTD_MCP_ORPHAN_SWEEP=0` 可关闭启动清扫。

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
# 2 列 = [f(Hz), eps]（各向同性）；4 列 = [f, eps_xx, eps_yy, eps_zz]
# eps = n^2，复介电常数可以写成 [re, im]
material_set(name="material_1", property="sampled 3d data",
             value=[[3e14, [4.0, 0.0]], [4e14, [4.1, 0.05]]])
material_set(name="material_1", property="mesh order", value=2)
model_set(name="substrate", properties={"material": "PA_RCP"})

# importnk() 作用于 import 对象，不是材料；材料数据用上面的 set 写入
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

### 诊断：「我改的代码到底生效了没？」

```
engine_status()
  → {bridge_code_id, disk_code_id, stale_code, project_path, autosave_path,
     session_generation, engine: {install_version, verified_against},
     recent_calls: [...], server: {server_code_id, tool_count, ...}}
```

`stale_code: true` 表示正在运行的桥进程还是旧代码——桥侧改动（`bridge.py`、
`handlers/`、`engine/`）必须等该进程重启才会生效。这个工具在没打开工程时也能调用，
并返回最近 50 次调用（方法、耗时、错误）。

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
│   ├── toolspec.py    # 声明式工具目录（schema/描述/注解/默认值/超时类别）
│   ├── dispatch.json  # 工具名 -> bridge handler 的唯一映射表
│   ├── bridge.py      # JSON-RPC 桥接外壳（Lumerical Python 3.6.8）
│   ├── engine/        # 桥侧基础设施（Python 3.6.8）
│   │   ├── constants.py      # 上限、LSF 事实、run() 正则
│   │   ├── errors.py         # BridgeError 统一错误通道
│   │   ├── lsf.py            # 引号转义、临时变量、?expr 解析
│   │   ├── capabilities.py   # 实测引擎能力表 + 代码指纹
│   │   ├── proctree.py       # 引擎进程账本（owned/orphan/foreign、job object、
│   │   │                     #   TerminateProcess 强杀）
│   │   ├── library.py        # 对象库目录（Resources/fdtd-library）
│   │   └── base.py           # 共享 mix-in（_call_lsf / 清洗 / 名称解析）
│   ├── handlers/      # 按模块拆分的处理器：session/model/material/attribute/
│   │                  #   sweep/result/library/engine_tools
│   ├── server.py      # MCP 服务器（系统 Python）
│   └── cheatsheet/
│       └── lumapi_ref.json  # Lumerical API 参考（每条带 verified_against）
└── tests/
    ├── test_*.py            # 基于 fake engine 的 pytest（无需 Lumerical）
    ├── test_capability_guard.py  # 代码 / 能力表 / 白名单 一致性
    ├── test_toolspec.py     # 声明式工具目录自洽性
    ├── test_proctree.py     # 哪些引擎进程允许被杀（foreign 绝不误杀）
    ├── test_engine_reaping.py    # 会话切换/退出不留孤儿引擎
    ├── test_library.py      # 对象库目录与插入（fake 与真机布局一致）
    ├── run_bridge_live.py   # 真机冒烟（需要许可）
    ├── live_orphan_check.py # 真机：EOF / 硬杀 / 正常关闭都不留引擎
    └── test_live_smoke.py   # 上面脚本的 pytest 包装（FDTD_MCP_LIVE=1）
```

## 测试

```
python -m pytest tests -q                                        # 离线套件（fake engine）
$env:FDTD_MCP_LIVE=1; python -m pytest tests/test_live_smoke.py  # 真机冒烟
"%LUMERICAL_HOME%\python-3.6.8-embed-amd64\python.exe" tests/run_bridge_live.py
```

## 改动何时生效

| 改了什么 | 何时生效 |
|---|---|
| `handlers/*`、`engine/*`、`bridge.py` | **桥进程**重启后（超时/崩溃后被自动重启，或 MCP server 重启）。`engine_status()` 的 `stale_code` 可直接判断 |
| `server.py`、`toolspec.py`、`dispatch.json`、`cheatsheet/*.json` | **MCP server 进程**重启后（重载 DSH patch 层，或重启 `dsh web`） |
| `tests/*`、`README*` | 立即 |

## 依赖

- Python ≥ 3.10
- Lumerical FDTD（v202 或更高）
- `mcp`
