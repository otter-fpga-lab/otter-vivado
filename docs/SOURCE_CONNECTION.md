# 一份源码与客户端入口

Otter Vivado 的插件体验由 **Skill + MCP + 运行面板**组成。Skill 与参考文件来自本仓
`skills/otter-vivado/`，MCP/CLI/UI 共用本仓 Python 实现。客户端只保留目录链接和 MCP
启动配置，或 Codex/Antigravity/WorkBuddy 原生插件的简短入口壳；日常修改源码后无需到各客户端复制业务。

源码目录引用与原生插件壳都是接入方式，按宿主选择一种，不并行注册重复能力；没有同步器或第二份业务库。
本仓继续保留 NJ 的 vivado-mcp 作者、Apache-2.0 许可、fork 和上游贡献关系。
同机安装多个 Vivado 时，按 [版本兼容说明](VERSION_COMPATIBILITY.md) 选择工程所需版本；
客户端接入本身不要求升级 Vivado。

## 客户端速查

| 客户端 | 采用入口 | 实现与验证状态 |
|---|---|---|
| Codex | [原生同源插件壳](#codex-native-plugin)，与普通源绑定择一 | 2026-10-07 本机原生安装、Skill/MCP 发现和缓存入口 SDK 已验证；模型采用、EDA 另验。 |
| Claude Code | `connect --client claude-code`，源 Skill + MCP | 2026-10-08 本机配置/Skill/CLI 健康检查已有证据，重启后当前会话发现已报告；实际工具调用回执尚未确认。 |
| Cursor | `connect --client cursor`，源 Skill + MCP | 同源引用已有实现与自动化；真实宿主发现、加载未测。 |
| Antigravity | [原生同源插件壳](#antigravity-native-plugin)；也可选 `connect` 源引用 | 本机 CLI 安装、四文本缓存 SDK 及源指南读取 PASS；IDE/模型采用另验，见 [TASK](../TASK.md)。 |
| WorkBuddy | [原生同源插件壳](#workbuddy-native-plugin) | 本机原生安装/启用及缓存 SDK 读源 PASS；会话/模型采用另验，见 [TASK](../TASK.md)。 |

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

<a id="antigravity-native-plugin"></a>
## Antigravity 原生插件

使用本仓私有 editable 环境，生成到尚不存在的绝对目录，再用本机 Antigravity CLI 安装：

```powershell
.\.venv\Scripts\python.exe -B -m vivado_mcp plugin --client antigravity --output "$env:USERPROFILE\.gemini\local-plugins\otter-vivado"
& "$env:USERPROFILE\.gemini\bin\agy.exe" plugin install "$env:USERPROFILE\.gemini\local-plugins\otter-vivado"
& "$env:USERPROFILE\.gemini\bin\agy.exe" plugin list
```

按 [Antigravity 官方插件格式](https://antigravity.google/docs/plugins)，只生成 `plugin.json`、
`mcp_config.json`、`skills/otter-vivado/SKILL.md` 和 README。manifest 只有 `name/description`；
MCP 保留 `otter-vivado` 的私有 Python 和 `-B -m vivado_mcp`。短 Skill 与 Codex 共用当前
bootstrap，由 `vivado_guide` 按次读取正式源，无业务副本或嵌套目录链接。
生成器不安装宿主、不改客户端配置；目标已有内容时另选新目录。已有 Codex 生成结果不变。

同一 Antigravity 宿主选择此插件或普通 Skill/MCP 接入；安装插件后不重复运行该宿主的 `connect`。
`plugin list` 的注册、实际缓存配置和 `vivado_guide` 的源读取分别核对；`mcp list` 的全局条目
不包含插件内部 MCP，不能据此判定插件失效。普通更新与活动会话边界沿用上文，实际证据见 [TASK](../TASK.md)。

<a id="workbuddy-native-plugin"></a>
## WorkBuddy 原生插件

本机采用 WorkBuddy 随包 CodeBuddy CLI 的[原生插件机制](https://www.codebuddy.ai/docs/cli/plugins-reference)。
下面复用已登记且含本插件条目的 `otter-local`；首次接入按[官方市场方法](https://www.codebuddy.ai/docs/cli/plugin-marketplaces)
登记市场与插件条目，再执行 `plugin marketplace add <市场根> --name otter-local`。本产品生成器只生成壳，不写市场或用户配置。

```powershell
$marketRoot = "$env:USERPROFILE\.workbuddy\local-marketplaces\otter-local"
$pluginDir = Join-Path $marketRoot 'plugins\otter-vivado'
.\.venv\Scripts\python.exe -B -m vivado_mcp plugin --client workbuddy --output $pluginDir
$workbuddyCli = "$env:LOCALAPPDATA\Programs\WorkBuddy\resources\app.asar.unpacked\cli\bin\codebuddy"
$env:CODEBUDDY_CONFIG_DIR = "$env:USERPROFILE\.workbuddy"
$env:WORKBUDDY_CONFIG_DIR = $env:CODEBUDDY_CONFIG_DIR
$env:CODEBUDDY_FORCE_HEADLESS_BUNDLE = '1'
node $workbuddyCli plugin validate $pluginDir
node $workbuddyCli plugin install otter-vivado@otter-local --scope user
node $workbuddyCli plugin list --json
```

四文本为 `.codebuddy-plugin/plugin.json`、`.mcp.json`、短 Skill 和 README；manifest 只含
`name/version/description/skills/mcpServers`，不带 Codex `interface`。MCP 与 bootstrap 复用原源路径，
`vivado_guide` 仍按次读正式源。实际缓存为 `~/.workbuddy/plugins/cache/otter-local/otter-vivado/<版本>`。
已有内容时选择新输出；同一宿主选插件或普通 Skill/MCP，避免重复。Codex/Antigravity 输出保持原样。

普通指南/MCP 代码修改按既有空闲边界正常重启，不刷新壳。WorkBuddy 同版本缓存不覆盖；
只有入口、引导协议或元数据变化时升产品版本、重新生成并 `plugin update otter-vivado@otter-local --scope user`。
新会话或 `/reload-plugins` 应用插件变更，活动 Vivado 会话按原生命周期处理。实际宿主证据见 [TASK](../TASK.md)。

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

WorkBuddy 当前采用上文原生插件。旧 `~/.workbuddy/mcp.json` 的 `mcpServers` 和 `~/.workbuddy/skills`
曾作为普通源引用；生成器不会接管或清理这些历史入口。`connect` 的客户端枚举不含 WorkBuddy，
不能用其他客户端的 `--config/--skills-dir` 覆盖参数代替原生插件接入。

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

遇到接入问题时可选用 `doctor`，不要求每个任务先运行。它对客户端只检查配置登记/启用，
以 `runtime_verified=false` 明确实际 MCP 运行与工具调用未验证；已有 Vivado TCP 协议检查
另行报告。原生插件与可验证直接登记重复、或多个可验证直接登记时只告警；`--fix`
保持客户端配置原样，由使用者核对是否有意保留。Codex 按 `enabled=false` 排除停用条目；
Claude Code 只核对用户级登记，不把条目内的 `enabled/disabled` 当作真实启停开关，
其[项目停用列表](https://code.claude.com/docs/en/mcp#disable-a-server-without-removing-it)未采样。
损坏配置仍报告错误；同配置的 `vivado/vivado-mcp` 常用键名下无效启动条目也会报告。

接入命令不注入 Vivado、不探测端口、不重启 MCP，也不启动/停止 Vivado。GUI/attach
需要的协议配置见 [运行观察](RUN_MONITOR.md#一次源码接入)；活动运行不要为了刷新面板
或更新 Skill 而重启拥有它的 MCP。

## 客户端兼容与验证边界

客户端 Skill 目录发现规则与原生插件目录规则不同。Ross 的原生插件安装在部分客户端会
复制缓存，Cursor 原生插件还限制外部目录链接；本入口使用 Skill 发现目录，不向插件缓存
放置越界链接。Ross 与 Otter 可按任务并列选择，取舍与固定版本依据见
[Ross 参考](../skills/otter-vivado/references/ross.md)。同一客户端避免同时安装两份同名 Skill。

连接器已有配置保留、路径选择、共享链接和冲突处理的自动化验证；Codex 原生插件的本机
安装、发现和源 SDK 验证见上文与 TASK。Claude Code 的本机检查与重启后发现报告也见 TASK，
当前会话工具调用回执尚未确认；Antigravity 原生入口结果见 TASK，Cursor 与普通连接器的实际宿主加载按需核对。
使用时按实际需要确认客户端版本、Skill 发现、MCP 握手、Windows
symlink/junction 解析，以及修改一个参考文件后新会话能读取原位内容；仍在运行的构建应
保持原 session/run。详细自动化结果和接续点见 [TASK.md](../TASK.md)。

2026-10-03 核对的官方入口与证据范围：

| 客户端 | 官方资料与本轮确认 |
|---|---|
| Codex | [Skills](https://learn.chatgpt.com/docs/build-skills) 明确支持 symlink Skill 目录；[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 说明用户级 TOML 配置。 |
| Cursor | [Skills](https://cursor.com/docs/skills) 说明 `.agents/skills`、`.cursor/skills` 等发现目录；[MCP](https://cursor.com/docs/mcp) 说明用户级 JSON。目录文档未明确承诺 symlink/junction。 |
| Claude Code | [官方 CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)：2.0.62 修复 Skill 目录 symlink，2.1.0 加入 Skill 热重载；2.1.257 限制插件组件越界链接。本轮官方文档站返回 403，未独立复核其 MCP 页面正文；配置路径沿用已有实现。 |
| Antigravity | [Skills](https://antigravity.google/docs/skills) 区分 IDE 与 CLI 目录；[MCP](https://antigravity.google/docs/mcp) 说明新配置路径。文档未明确承诺 symlink/junction 或新版读取旧 MCP 路径。 |
