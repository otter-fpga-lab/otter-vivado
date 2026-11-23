---
name: otter-vivado
description: 操作 AMD Vivado 工程、本地或远程综合与实现、查看真实进度和报告、诊断工程与约束；人和智能体共用本机会话或显式远程任务；指导智能体为消费者工程生成可视化。也支持无 Vivado 时读取 XPR、XCI、XDC 和已有报告。
---

# Otter Vivado

本 Skill 负责选择工作路径，当前 MCP 的工具 schema 负责参数与执行约定；CLI、MCP 和消费者可视化复用同一份 `vivado_mcp` 实现。使用客户端实际配置的工具前缀。读取指南或工程目录约定直接调用 `vivado_guide` / `get_project_layout`，不要猜宿主缓存的 JSON 路径。

本产品负责 Vivado 工程、执行、仿真、IP、状态与报告工具。RTL 设计和编码由 Coding 产品负责；此处保留已有检查器作为验证入口，并提供可以交给人用原生 Vivado GUI 继续操作的工程与证据。

**会话与编译（智能体易漏）**：`run_synthesis` / `run_implementation` 在**已有 session 的那一个 Vivado 进程**里 `launch_runs`，GUI 开着就是综合宿主，不是另占一份“编译名额”。不要为了编译再 `start_session(mode="tcl")` 开第二份。默认 `gui` 端口 9999：已有窗口则 attach 复用，换 `session_id` 也不会自动多开；要第二个独立 GUI 才用 `port=0`。同一 session 的 Tcl 串行，不同 session 才可并行。许可 seat 是 Xilinx 按 `vivado.exe` 进程计，插件不另设名额。

**目录与产物（每个工程先做一次，智能体易漏）**：开工先 `get_project_layout(project=<.xpr>)` 取约定；用户口头给过的目录偏好写进 .xpr 旁的 `otter-vivado.toml`，以后不再每轮重问。

- 过程文件（日志、串口记录、抓图、VCD、导出包）放 `debug_dir/YYYYMMDD_主题/`，自写 Tcl/Python 放 `scripts_dir`，关键结论只追加到 `notes_file`。`export_ila`、`create_debug_experiment`、`export_debug_bundle` 不传 `output_dir` 时自动落在这里。
- 构建按 Vivado 原生 `.runs/.gen` 编译覆盖，不建工作树、工程副本或 `%TEMP%` 备份。需要保留的多版本固件放 `download_dir`，并附说明。
- 不创建 `.work`、`.vmcp`、`.otter` 等隐藏目录，已有的也不当草稿箱（工程根已被 `.gitignore` 且有仓库脚本约定的 `.work/` 在 `get_project_layout` 中降级为 `info` 提示）。不给 run 挂指向 TEMP 或工程外的 `TCL.PRE/POST`；临时参数用完即还原。
- 过程目录用完移入 `trash_dir`（最近的 `99_垃圾堆`）并附清单；为 null 时留在 Debug，不擅自删除。
- Vivado 自身的 `.Xil/.jou/.log` 落在会话 cwd，默认与 GUI 相同的 `%APPDATA%\Xilinx\Vivado`，无须再 `cd` 到仓根或 TEMP。

**会话选择**：9999 上的 GUI 已打开同一工程 → 复用。它属于别的工程或别的线程 → 不要 attach 后切换它的工程；用 `start_session(port=0)` 开独立实例，或用 MCP 托管批处理（见下）。等待构建用 `wait=False` 发起，再用 `wait_run(until=...)` 或 run monitor 快照；不要 sleep 循环调用 `get_run_progress`。

**独立 OOC 综合与批处理 Tcl（省 Token、禁 shell sleep）**：
- 无 `.xpr` 独立综合优先用 `run_ooc_synth`（1 次调用生成 Tcl、可选时钟约束并自动解析 WNS/TNS/WHS/THS、LUT/FF/BRAM/DSP 与去重告警，返回 < 2 KB 紧凑 JSON）；跑批处理 Tcl 脚本优先用 `run_batch_tcl`。
- 禁止用裸 shell `vivado -mode batch` + `sleep` 轮询！两工具均支持服务端等待（`wait=True` 带进度上报；超时不杀 Vivado，返回 `wait_timed_out=True` 与 `job_id`，再用 `get_batch_job(job_id=..., wait_seconds=...)` 续等）且工作目录隔离在 `%LOCALAPPDATA%\otter-vivado\jobs\`。
- 需要可视化或零 Token 检视离线 `.xpr`/`.runs`/OOC 目录时，使用 `open_vivado_console`（Antigravity 侧栏「Vivado 控制台」）。

可视化职责：本插件提供接口、状态数据与生成方法，智能体为具体消费者工程生成页面和
交互。页面、布局、主题、业务控件语义保存在消费者工程；已有内置网页仅为可选参考实现，
不作为使用工具的前提。不要为了一个工程的风格需求改库内 `web/`。

## 只读当前任务需要的参考

以下路径相对于本 Skill 目录，包括通过符号链接加载时；不相对于用户工程。按需读取，不预先加载全部参考。

| 用户目标 | 读取内容 | 已有入口 |
|---|---|---|
| 接续工程、综合或实现 | [workflows.md](references/workflows.md#工程与构建) | `list_sessions`、`start_session`、`run_synthesis`、`run_implementation` |
| 独立 OOC 综合与托管 Batch Tcl | [workflows.md](references/workflows.md#独立-ooc-与批处理-tcl) | `run_ooc_synth`、`run_batch_tcl`、`get_batch_job` |
| Linux 远程构建、原生 Tcl、队列与结果交付 | [远程构建](../../docs/REMOTE_BUILD.md)、[原生 Tcl](../../docs/REMOTE_TCL.md) | `vivado-mcp remote`；当前没有远程 MCP 工具 |
| 看进度、日志、报告或 Vivado 控制台 | [workflows.md](references/workflows.md#进度与报告) | `open_vivado_console`、`open_run_monitor`、`get_run_snapshot`、`get_run_progress` |
| 按工程生成页面/可视化 | [visualization.md](references/visualization.md) | 状态/报告 JSON、`DebugService`；可选参考页 |
| 调试构建与产物交付 | [debug-delivery.md](references/debug-delivery.md) | 已有构建工具、`export_debug_bundle`、`check_debug_artifacts` |
| 工程控件、枚举、定点数、单位与预设 | [controls.md](references/controls.md) | `resolve_debug_controls`、`write_debug_control` |
| 本地实验、倒计时、动作标记、暂停/重做 | [experiment.md](references/experiment.md) | `create_debug_experiment`、`get_debug_experiment`、`debug_experiment_action` |
| 采样分析、状态/握手检查、下一轮取证 | [analysis.md](references/analysis.md) | `analyze_ila_capture`、`read_ila_waveform`、`debug_action` |
| 采样导出、离线 VCD、生成消费者波形页面 | [waveform.md](references/waveform.md) | `debug_action(export_ila)`、`read_ila_waveform` |
| 人机共用 ILA/VIO 板上调试、生成工程控件 | [hardware-debug.md](references/hardware-debug.md) | `open_debug_panel`、`get_debug_snapshot`、`debug_action` |
| 交付工程给人用 GUI 接续 | [workflows.md](references/workflows.md#原生-gui-交接) | 工程路径、实际会话、文件与报告证据 |
| 分析失败、约束或 IP；无 EDA 摸底 | [workflows.md](references/workflows.md#诊断与离线分析) | `get_critical_warnings`、`xdc_lint`、`inspect_ip_params`、`parse_xpr` |
| 判断报告可信度、核对版本或查官方资料 | [evidence-and-docs.md](references/evidence-and-docs.md) | 当前 Vivado 帮助、本地官方文档、AMD 官方站点 |
| 比较或配合 AMD Ross | [ross.md](references/ross.md) | 两套产品按任务并列选择 |

先从工程、已有会话和文件中发现输入，再补无法发现的信息。本地执行明确工程、实际 Vivado 版本、session、run 与目标阶段；远程执行明确主机配置、workspace、工程/清单与 handle，不套本地会话字段。所有完成结论都带真实状态或产物依据。

远程任务使用同一产品 CLI，可直接运行 `python -m vivado_mcp.remote_build`。先按任务区分本地离线检查与远程执行：`inspect`、`report --results` 不读主机配置、不连接 SSH；提交、查询远端和清理沿用任务已有的主机与动作授权。不要生成空 MCP 工具或为一次远程任务修改客户端配置。上表的产品 `docs/` 从 Skill 链接的真实目标定位仓根后读取。

## 原地接入与持续运行

已有接入直接使用；Codex 原生插件的引导入口通过只读 `vivado_guide` 取得本文件与按需参考。普通指南更新按次读取，不能因读取本 Skill 再创建一份普通 Skill/MCP 入口。

首次接入或改变源路径时，查看源根 `docs/SOURCE_CONNECTION.md` 选择 Codex 插件壳或现有 `connect` 源码引用；其它脚手架使用同源 CLI/MCP 与 Skill 引用，不复制业务。原生 GUI attach 与现场步骤见仓库 `docs/RUN_MONITOR.md`，从返回的源根或 Skill 链接真实目标定位仓库。

Skill 在下一次读取时使用新内容；Python 模块和模型上下文在实际加载边界更新。活动构建继续使用现有会话，重载 MCP 留到空闲时。刷新面板、查询或源码更新不应触发停止、reset、重跑或替换会话。一次接入不等于修改 Vivado init Tcl；真实设备操作需要明确授权。
