# DSH 插件集成（fdtd-mcp-dsh-plugin）

本目录下的 [`dsh-plugin/`](dsh-plugin/) 是一个 **DSH bundle 插件包**：它把本仓库的
fdtd-mcp MCP 服务器（Lumerical FDTD 仿真）桥接进 DeepSeek Harness（DSH），使 DSH 的
模型可以把 FDTD 工具当作原生工具直接调用。

- 桥接机制：DSH 官方插件 [`@deepseek-ai/dsh-mcp-client`](https://www.npmjs.com/package/@deepseek-ai/dsh-mcp-client)
  （MCP 客户端桥接插件，随每个 `dsh` 安装自带）。
- 工具命名：DSH 中每个工具显示为 `mcp__fdtd__<工具名>`，共 30 个，例如
  `mcp__fdtd__execute`、`mcp__fdtd__run`、`mcp__fdtd__model_add`、
  `mcp__fdtd__material_add`、`mcp__fdtd__session_new`、`mcp__fdtd__sweep_run`、
  `mcp__fdtd__result_get` 等。
- 对 fdtd-mcp 本身**零改动**：不修改 `fdtd_mcp/` 的任何代码，服务器仍可同时供
  Claude Code（`.mcp.json`）使用。

## 目录结构

```
dsh-plugin/
├── package.json        # 插件包清单：声明 dsh.bundle.patch（标准 bundle 形态）
└── cordis.patch.yml    # 补丁层：插入一个 dsh-mcp-client 实例，spawn fdtd-mcp 服务器
```

## 安装

```powershell
# 从仓库根目录执行（或任意目录，给出绝对路径）
npx @deepseek-ai/dsh plugin --profile web add D:/project/fdtd-mcp/dsh-plugin
```

命令内部会在 profile 目录 `$DSH_HOME/profiles/web` 调用 pnpm，把插件登记为 bundle 层
（写进 profile 的 `package.json` → `dsh.profile.bundles`），并以符号链接方式安装到
profile 的 `node_modules`——因此**以后直接改 `dsh-plugin/cordis.patch.yml` 即可，无需重新安装**。

> 注意：bundle 层在 DSH 启动时读取。安装后需要**重启 web GUI**（停止再重新运行
> `dsh web`）才会生效；DSH 只会对 profile 的 `cordis.patch.yml` 用户补丁层做热更新，
> 不会热加载新增的 bundle。

## 卸载

```powershell
npx @deepseek-ai/dsh plugin --profile web remove fdtd-mcp-dsh-plugin
```

## 修改配置

所有可调项都在 `dsh-plugin/cordis.patch.yml` 的 `config` 中：

| 字段 | 说明 | 默认值（对应 `.mcp.json`） |
|---|---|---|
| `command` | Python 解释器（需 ≥3.10，安装了 `mcp` 包） | `D:/coding/anaconda3/python.exe` |
| `args` | 服务器启动参数 | `['-m', 'fdtd_mcp.server']` |
| `env.PYTHONPATH` | fdtd-mcp 仓库路径 | `D:/project/fdtd-mcp` |
| `env.LUMERICAL_HOME` | Lumerical 安装路径 | `D:/Software/Lumerical/v202` |
| `cwd` | 服务器工作目录 | `D:/project/fdtd-mcp` |
| `serverName` | 工具命名空间（改它需同步重启） | `fdtd` |
| `failOnStartupError` | 服务器启动失败时是否拒绝 GUI 启动 | `false`（仅跳过工具注册并记日志） |

## 在其他 profile 使用（例如 headless）

```powershell
npx @deepseek-ai/dsh plugin --profile headless add D:/project/fdtd-mcp/dsh-plugin
```

## 已验证

- `dsh web --dump-config` 合成的配置树包含 `mcp-fdtd` 行（`# == fdtd-mcp-dsh-plugin`）。
- 用插件完全相同的 command/args/env/cwd 启动服务器，MCP initialize 握手成功，
  `tools/list` 返回全部 30 个工具；协议帧格式（换行分隔 JSON）与
  `@modelcontextprotocol/sdk`（dsh-mcp-client 所用）一致。
- `@deepseek-ai/dsh-mcp-client` 是 `dsh` 包自身的依赖，任何 profile 都能解析到它。
