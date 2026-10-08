# 一份源码与客户端入口

Otter Vivado 的插件体验由 **Skill + MCP + 运行面板**组成。Skill 与参考文件来自本仓
`skills/otter-vivado/`，MCP/CLI/UI 共用本仓 Python 实现。客户端只保留目录链接和 MCP
启动配置，或 Codex 原生插件的简短入口壳；日常修改源码后无需到各客户端复制业务。

源码目录引用与 Codex 原生插件壳都是接入方式，按宿主选择一种，不并行注册重复能力；没有同步器或第二份业务库。
本仓继续保留 NJ 的 vivado-mcp 作者、Apache-2.0 许可、fork 和上游贡献关系。
同机安装多个 Vivado 时，按 [版本兼容说明](VERSION_COMPATIBILITY.md) 选择工程所需版本；
客户端接入本身不要求升级 Vivado。

## 客户端速查

| 客户端 | 采用入口 | 实现与验证状态 |
|---|---|---|
| Codex | [原生同源插件壳](#codex-native-plugin)，与普通源绑定择一 | 2026-10-07 本机原生安装、Skill/MCP 发现和缓存入口 SDK 已验证；模型采用、EDA 另验。 |
| Claude Code | `connect --client claude-code`，源 Skill + MCP | 连接器已有配置生成与保护验证；真实宿主发现、加载未测。 |
| Cursor | `connect --client cursor`，源 Skill + MCP | 同源引用已有实现与自动化；真实宿主发现、加载未测。 |
| Antigravity | `connect --client antigravity`，按实际 IDE/CLI 路径选择 | 新旧路径处理已有实现与自动化；真实宿主发现、加载未测。 |
| WorkBuddy | 无 `workbuddy` 适配；按实际宿主入口引用源 MCP/Skill | 只有本机历史配置观察；当前接入未测，不声称支持原生插件格式。 |

表内 `connect` 使用下文同一个私有 Python；完整参数和路径见下文。Claude Code 与 Claude Desktop 是不同宿主，本连接器的 `claude-code` 不代表 Desktop 适配。

<a id="codex-native-plugin"></a>
## Codex 原生插件

使用产品私有环境生成一个新的绝对目录：

```powershell
.\.venv\Scripts\vivado-mcp.exe plugin --client codex --output <新的绝对插件目录>
```

生成器只创建 `.codex-plugin/plugin.json`、`.mcp.json`、简短 `skills/otter-vivado/SKILL.md` 和 README。目标已存在时拒绝覆盖，不安装到宿主、不写全局配置。MCP 使用本仓 `.venv` 的绝对 Python 路径与 `-B -m vivado_mcp`；这是已验证的 editable 源，运行目录即使是宿主缓存，也不改用缓存业务树。已有 `connect` 是另一种接入方式，不需同时运行。

宿主按其本机 marketplace 与原生插件入口安装这个壳。2026-10-07 本机已用 `codex plugin add otter-vivado@personal --json` 安装；实测缓存位置为 `~/.codex/plugins/cache/personal/otter-vivado/0.3.25`，不是源壳目录。实际配置只增加本插件启用项，没有另外创建普通 Skill 链接或独立 `mcp_servers` 项；生成、原生发现、协议与模型采用分别以 [TASK](../TASK.md) 的证据为准。

`vivado_guide` 按次读取当前源：`overview` 返回主 Skill、真实源根与同源 CLI；`remote/remote-tcl` 返回相应指南；`reference` 只接受工具返回的白名单文件名。它没有任意读文件参数，拒绝越界或重定向的源文件，不读 hosts、客户端配置或凭据。普通指南更新无需重装插件；Python 业务在正常空闲边界重载 MCP，不监听文件热更新，也不为刷新文字中断本地 Vivado 会话。源位置、解释器或引导协议改变时重新生成明确的新入口。

兼容布局与 manifest 的 `./skills/`、`./.mcp.json` 引用依据 [OpenAI 官方插件文档](https://developers.openai.com/plugins/build/plugins)。这不是公开发布包，也不推定其他客户端支持同一原生格式。

## 源码 Skill/MCP 接入

Codex 已在所选配置中启用 `otter-vivado` 原生插件时，`connect`（含 `--check/--skills-only`）会拒绝新增普通入口并给出指引，不把已有插件误报为连接器 `ready`。需要接入其他客户端时显式指定它们，例如 `--client cursor claude-code antigravity`；`all` 包含 Codex，不能用于绕过这个重复入口检查。连接器不会禁用或删除已有插件。

已有本仓源码时直接进入该目录；首次取得默认 `main` 源码可用：

```bash
git clone -b main https://github.com/otter-fpga-lab/otter-vivado.git
cd otter-vivado
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m vivado_mcp connect --client claude-code --check
.\.venv\Scripts\python -m vivado_mcp connect --client claude-code
```

Linux：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m vivado_mcp connect --client claude-code --check
.venv/bin/python -m vivado_mcp connect --client claude-code
```

已有虚拟环境可直接使用，不必重建。`connect` 会确认当前解释器确实以 editable 方式加载
这份源码，然后把该解释器的绝对路径写入 MCP 配置。只用部分客户端时可指定列表，例如：

```bash
python -m vivado_mcp connect --client cursor claude-code antigravity
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

WorkBuddy 的本机历史观察是 `~/.workbuddy/mcp.json` 顶层 `mcpServers`，Skill 在 `~/.workbuddy/skills`，曾用目录 junction 与 Python/Node 的源码命令。这支持按实际宿主配置使用同源 `python -m vivado_mcp` 和 Skill 引用，不证明原生插件格式或当前新入口已经实测。本轮没有修改 WorkBuddy 配置，也未新增 `workbuddy` client 枚举；现有 `--config/--skills-dir` 仅覆盖已支持客户端的路径与格式，不能当作 WorkBuddy 适配认证。

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
python -m vivado_mcp connect --client claude-code --skills-only
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
python -m vivado_mcp connect --client claude-code --check
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

连接器已有配置保留、路径选择、共享链接和冲突处理的自动化验证；Codex 原生插件的本机
安装、发现和源 SDK 验证见上文与 TASK。其他宿主发现和 Windows 连接器链接仍未现场验证；使用时确认客户端版本、Skill 发现、MCP 握手、Windows
symlink/junction 解析，以及修改一个参考文件后新会话能读取原位内容；仍在运行的构建应
保持原 session/run。详细自动化结果和接续点见 [TASK.md](../TASK.md)。

2026-10-03 核对的官方入口与证据范围：

| 客户端 | 官方资料与本轮确认 |
|---|---|
| Codex | [Skills](https://learn.chatgpt.com/docs/build-skills) 明确支持 symlink Skill 目录；[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 说明用户级 TOML 配置。 |
| Cursor | [Skills](https://cursor.com/docs/skills) 说明 `.agents/skills`、`.cursor/skills` 等发现目录；[MCP](https://cursor.com/docs/mcp) 说明用户级 JSON。目录文档未明确承诺 symlink/junction。 |
| Claude Code | [官方 CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)：2.0.62 修复 Skill 目录 symlink，2.1.0 加入 Skill 热重载；2.1.257 限制插件组件越界链接。本轮官方文档站返回 403，未独立复核其 MCP 页面正文；配置路径沿用已有实现。 |
| Antigravity | [Skills](https://antigravity.google/docs/skills) 区分 IDE 与 CLI 目录；[MCP](https://antigravity.google/docs/mcp) 说明新配置路径。文档未明确承诺 symlink/junction 或新版读取旧 MCP 路径。 |
