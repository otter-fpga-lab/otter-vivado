# 一份源码接入多个客户端

Otter Vivado 的插件体验由 **Skill + MCP + 运行面板**组成。Skill 与参考文件来自本仓
`skills/otter-vivado/`，MCP/CLI/UI 共用本仓 Python 实现。客户端只保留目录链接和 MCP
启动配置；日常修改源码后无需到 Cursor、Claude Code、Codex、Antigravity 各更新一遍。

这种原地引用就是正式接入方式，不要求客户端原生插件市场，也没有同步器或第二份业务库。
本仓继续保留 NJ 的 vivado-mcp 作者、Apache-2.0 许可、fork 和上游贡献关系。

## 一次接入四个客户端

已有本仓源码时直接进入该目录；首次取得本分支源码可用：

```bash
git clone -b feat/progress-view-study https://github.com/otter-fpga-lab/otter-vivado.git
cd otter-vivado
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m vivado_mcp connect --client all --check
.\.venv\Scripts\python -m vivado_mcp connect --client all
```

Linux：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m vivado_mcp connect --client all --check
.venv/bin/python -m vivado_mcp connect --client all
```

已有虚拟环境可直接使用，不必重建。`connect` 会确认当前解释器确实以 editable 方式加载
这份源码，然后把该解释器的绝对路径写入 MCP 配置。只用部分客户端时可指定列表，例如：

```bash
python -m vivado_mcp connect --client cursor claude-code codex
```

本文后续的 `python` 均指上面同一个虚拟环境解释器。`--check` 只读核对，不创建目录、
链接或配置；正常接入先预检全部所选客户端，再创建链接并备份、更新缺失的本产品 MCP 条目。
其他 MCP、环境参数及用户设置保留。已有同名 Skill 普通目录或指向其他源码的入口会报冲突，
不会自动删除或覆盖。

## 默认接入位置

`~` 表示当前用户主目录。链接名称均为 `otter-vivado`，目标均为本仓
`skills/otter-vivado`；不是把整个客户端配置目录换成仓库链接。

| 客户端 | Skill 链接的父目录 | MCP 配置 |
|---|---|---|
| Cursor | `~/.agents/skills`；已有 `~/.cursor/skills/otter-vivado` 时接续它 | `~/.cursor/mcp.json` |
| Claude Code | `~/.claude/skills` | `~/.claude.json` |
| Codex | `~/.agents/skills` | `$CODEX_HOME/config.toml`，未设置时 `~/.codex/config.toml` |
| Antigravity IDE | `~/.gemini/config/skills` | `~/.gemini/config/mcp_config.json` |

Cursor 和 Codex 默认共用同一个 Skill 链接，避免重复入口。已有配置文件本身是符号链接时，
保留该链接并更新其真实目标；多个 JSON 配置共用同一真实文件时只写一次。

Antigravity 若仅存在旧版 `~/.gemini/antigravity/mcp_config.json`，会接续旧布局的
`skills` 目录并提示版本需核对；这不承诺新版 IDE 仍发现旧路径。新旧 MCP 配置同时存在时
请单独指定实际使用的文件。Antigravity CLI 的 Skill 路径也请显式指定，避免套用 IDE 布局。
使用自定义路径时只选择一个客户端：

```bash
python -m vivado_mcp connect --client antigravity --config /actual/mcp_config.json --skills-dir /actual/skills
```

例如当前 Antigravity CLI 的官方 Skill 目录是 `~/.gemini/antigravity-cli/skills`，可将其
作为 `--skills-dir`；其 MCP 配置仍使用 `~/.gemini/config/mcp_config.json`。

`--skills-dir` 是父目录，命令会在其下建立 `otter-vivado` 链接。若已有自行维护的 MCP，
只需接入 Skill，可使用：

```bash
python -m vivado_mcp connect --client all --skills-only
```

`--skills-only` 不检查也不修改 MCP，不能据此判定工具已可调用。已有本产品 MCP 被停用，
或带 `cwd`/`PYTHONPATH`/`PYTHONHOME` 搜索路径覆盖时，普通接入会原样保留并返回
`needs_attention`，由使用者核对实际加载来源。它不会替你启用被停用的服务。

Windows 默认 `--link-mode auto`：先建立目录 symlink，仅遇到缺少链接特权的错误
（1314）时改用目录 junction；也可显式选 `--link-mode symlink` 或 `--link-mode junction`。
两种方式都引用原目录，不复制内容。其他错误直接报告；路径和权限按输出处理。

## 日常修改与检查

改本仓源码或 `git pull` 后不需要再运行 `connect`，也不需要重新复制 Skill。
Skill 和 references 在客户端下一次读取时取得新内容；已加载的模型上下文和 Python 模块
不承诺热更新。让活动构建继续完成，在客户端正常空闲边界新建会话或重载 MCP 后使用新代码。
源码目录或 Python 环境位置改变时，才需要核对并调整接入路径。

需要确认链接仍有效时运行：

```bash
python -m vivado_mcp connect --client all --check
```

输出逐客户端列出 `source`、`python`、`skill_source`、`skill`、`config` 与状态。
`ready` 表示本地链接与配置满足检查；`needs_connection` 表示仍缺入口；`needs_attention`
表示已有设置需要核对。检查不启动客户端，因此 `ready` 不代表 GUI 已发现 Skill 或 MCP
已成功握手。随后在实际客户端确认 `otter-vivado` Skill 与 Vivado 工具可见，先调用
`list_sessions`，再按 [运行观察流程](RUN_MONITOR.md) 选工程与会话。

接入命令不注入 Vivado、不探测端口、不重启 MCP，也不启动/停止 Vivado。GUI/attach
需要的协议配置见 [运行观察](RUN_MONITOR.md#一次源码接入)；活动运行不要为了刷新面板
或更新 Skill 而重启拥有它的 MCP。

## 客户端兼容与验证边界

客户端 Skill 目录发现规则与原生插件目录规则不同。Ross 的原生插件安装在部分客户端会
复制缓存，Cursor 原生插件还限制外部目录链接；本入口使用 Skill 发现目录，不向插件缓存
放置越界链接。Ross 与 Otter 可按任务并列选择，取舍与固定版本依据见
[Ross 参考](../skills/otter-vivado/references/ross.md)。同一客户端避免同时安装两份同名 Skill。

当前验证覆盖连接器的配置保留、路径选择、共享链接和冲突处理；没有在真实 Windows 或
四个客户端 GUI 中完成安装验收。现场应确认客户端版本、Skill 发现、MCP 握手、Windows
symlink/junction 解析，以及修改一个参考文件后新会话能读取原位内容；仍在运行的构建应
保持原 session/run。详细自动化结果和接续点见 [TASK.md](../TASK.md)。

2026-10-03 核对的官方入口与证据范围：

| 客户端 | 官方资料与本轮确认 |
|---|---|
| Codex | [Skills](https://learn.chatgpt.com/docs/build-skills) 明确支持 symlink Skill 目录；[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 说明用户级 TOML 配置。 |
| Cursor | [Skills](https://cursor.com/docs/skills) 说明 `.agents/skills`、`.cursor/skills` 等发现目录；[MCP](https://cursor.com/docs/mcp) 说明用户级 JSON。目录文档未明确承诺 symlink/junction。 |
| Claude Code | [官方 CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)：2.0.62 修复 Skill 目录 symlink，2.1.0 加入 Skill 热重载；2.1.257 限制插件组件越界链接。本轮官方文档站返回 403，未独立复核其 MCP 页面正文；配置路径沿用已有实现。 |
| Antigravity | [Skills](https://antigravity.google/docs/skills) 区分 IDE 与 CLI 目录；[MCP](https://antigravity.google/docs/mcp) 说明新配置路径。文档未明确承诺 symlink/junction 或新版读取旧 MCP 路径。 |
