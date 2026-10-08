# Otter Vivado

供人和智能体共用的 AMD Vivado 工具：原生工程、Tcl、本地/远程构建、仿真、IP、真实状态
与报告，支持 GUI、无头 Tcl 和已有 GUI 的 attach。Skill、CLI/MCP 与本地面板共用本仓源码。
RTL 设计与编码交给 `otter-rtl-coding` 等编码工具；Ross 与 Otter 可独立选择。

本仓保留 NJ 的 [vivado-mcp](https://github.com/mapleleavessssssss-wq/vivado-mcp) 历史、作者
和 Apache-2.0 许可。Python 包与 CLI 名仍为 `vivado-mcp`；PyPI 同名包是上游发行版，
Otter 功能按下面的本仓源码安装。

## 客户端接入

<a id="环境要求"></a>
需要 Python 3.10+ 和已选定的客户端。Windows 为主要使用场景，兼顾 Linux；运行本地
EDA 需要本机 Vivado，远程构建需要指定 Linux 主机的安装，离线文件/报告检查无需 EDA。
依赖由 editable 安装按 [pyproject.toml](pyproject.toml) 准备，不使用 Vivado 自带 Python。

<a id="快速开始"></a>
### 首次安装

在自己的机器取得源码并准备私有环境；已有源码或可用 `.venv` 时复用：

```powershell
git clone -b main https://github.com/otter-fpga-lab/otter-vivado.git
Set-Location otter-vivado
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

然后按[接入指南](docs/SOURCE_CONNECTION.md#客户端速查)选择一个客户端：Codex、
Antigravity、WorkBuddy/CodeBuddy IDE 使用对应同源插件壳；Claude Code/Cursor 使用源
Skill/MCP 引用。同一宿主只选一种入口。生成壳后还需按该页原生登记安装；已有市场只合并
本产品条目，保留其他配置。CLI 可直接使用，不要求先接入客户端。

完整安装、Linux 示例、路径选择与卸载都以 [docs/SOURCE_CONNECTION.md](docs/SOURCE_CONNECTION.md)
为准；当前安装/发现记录见该页[已验证范围](docs/SOURCE_CONNECTION.md#已验证范围)。

## 首次使用

先从仓根用同一环境只读列出本机安装候选：

```powershell
.\.venv\Scripts\vivado-mcp.exe versions --json
```

在客户端先调用 `vivado_guide(section="overview")` 确认正式源，再用 `list_sessions`
读取现有会话。需要新 GUI 时，明确安装路径后使用 `start_session(mode="gui", port=0,
vivado_path="<实际可执行文件>")`，再执行 `version -short` 核对实际版本。
已有 GUI 的协议配置和 attach 见[接入指南](docs/SOURCE_CONNECTION.md#连接原生-vivado-gui)。

有工程任务时，按[原生 GUI 指南](docs/HUMAN_GUI_WORKFLOW.md)选择工程，再按
[运行观察](docs/RUN_MONITOR.md)查看同一 run。安装/连接异常可按需运行 `vivado-mcp doctor`
（默认只读）；普通任务无需先诊断。CLI 未激活环境时使用上面的 `.venv\Scripts\...` 路径。
活动构建完成后再重启拥有它的 MCP，关闭该 owner 会清理它拥有的 Vivado 进程。

## 日常更新

在正式源码目录执行 `git pull`，然后按变化类型处理：

| 变化 | 本机处理 |
|---|---|
| 普通源码、Skill 或指南 | 活动任务完成后正常重启宿主/MCP；无需复制业务或刷新插件壳 |
| 依赖声明 | 在原环境重跑 `.\.venv\Scripts\python.exe -m pip install -e .`，再正常重启 |
| 源目录、解释器、入口或插件元数据 | 修正源引用，或重新生成薄壳并按宿主原生方式更新 |

指南按次读取当前源；已经加载的 Python 模块和模型上下文不承诺热更新。详细说明见
[日常修改与检查](docs/SOURCE_CONNECTION.md#日常修改与检查)；固定交付包另按版本更新。

<a id="工具职责"></a>
<a id="特性"></a>
<a id="工具列表"></a>
<a id="工作流-prompts"></a>
<a id="会话模式"></a>
<a id="cli-参考"></a>
<a id="使用示例--一轮完整的调试闭环"></a>
<a id="可选claude-code-hook-配置示例"></a>
<a id="文档"></a>
## 使用文档

| 需要做什么 | 入口 |
|---|---|
| 查会话、工具、Prompt、CLI、历史示例和可选 Hook | [使用指南](docs/USAGE.md) |
| 打开、交接或归档原生 `.xpr` | [GUI 工程与交付](docs/HUMAN_GUI_WORKFLOW.md) |
| 查看真实状态、日志与报告 | [运行观察](docs/RUN_MONITOR.md) |
| 远程 Linux 构建、原生 Tcl 和离线报告 | [远程构建](docs/REMOTE_BUILD.md) / [原生 Tcl](docs/REMOTE_TCL.md) |
| 用 ILA/VIO 调试和读取波形 | [硬件调试](docs/HARDWARE_DEBUG.md) / [波形](docs/ILA_WAVEFORM.md) / [分析](docs/ILA_ANALYSIS.md) |
| 准备调试 IP、核对/导出 bit/ltx、编排实验 | [工程准备](docs/DEBUG_DESIGN.md) / [产物](docs/DEBUG_ARTIFACTS.md) / [导出](docs/DEBUG_BUNDLE.md) / [实验](docs/DEBUG_EXPERIMENT.md) / [控件](docs/DEBUG_CONTROLS.md) / [停止边界](docs/ILA_STOP.md) |
| 遇版本、IP 或能力边界问题 | [版本兼容](docs/VERSION_COMPATIBILITY.md) / [IP 实践](docs/IP_DEBUG_GUIDE.md) / [限制](PITFALLS.md) |

<a id="远程构建-cli"></a>
远程入口为 `vivado-mcp remote --help`；`inspect` 和 `report --results` 只读本地输入，
实际远程操作再使用明确的私人主机配置。当前没有独立远程 MCP 工具。

<a id="架构"></a>
<a id="开发"></a>
开发与协议说明见 [CONTRIBUTING](CONTRIBUTING.md)，版本变化见 [CHANGELOG](CHANGELOG.md)，
旧版迁移见[迁移指南](docs/MIGRATION_0.1_to_0.2.md)，历史缺陷见[审计报告](docs/AUDIT_REPORT.md)。
当前维护状态、已有证据和未测项见 [TASK](TASK.md)，安装成功不等于 EDA/板卡通过。

## 反馈与 Bug 提交

在[产品讨论 PR #1](https://github.com/otter-fpga-lab/otter-vivado/pull/1)或 [TASK](TASK.md)
对应的 Hub 议题反馈。提供源码提交、客户端/Python/Vivado 版本、期望/实际行为、复现工具
序列和相关日志；[反馈材料说明](CONTRIBUTING.md#反馈复现材料)可交给当前客户端整理。
优先使用已有输出，收集反馈不重新构建或操作设备。适合回馈原上游的通用修复按贡献指南处理。

## 许可证

[Apache License 2.0](LICENSE)
