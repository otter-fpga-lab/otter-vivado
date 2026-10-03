---
name: otter-vivado
description: 操作 AMD Vivado 工程、启动综合与实现、查看真实运行进度和报告、诊断工程与约束；人和智能体共用本机会话；指导智能体为消费者工程生成可视化。也支持无 Vivado 时读取 XPR、XCI、XDC 等已有资产。
---

# Otter Vivado

本 Skill 负责选择工作路径，当前 MCP 的工具 schema 负责参数与执行约定；CLI、MCP 和消费者可视化复用同一份 `vivado_mcp` 实现。使用客户端实际配置的工具前缀。

本产品负责 Vivado 工程、执行、仿真、IP、状态与报告工具。RTL 设计和编码由 Coding 产品负责；此处保留已有检查器作为验证入口，并提供可以交给人用原生 Vivado GUI 继续操作的工程与证据。

可视化职责：本插件提供接口、状态数据与生成方法，智能体为具体消费者工程生成页面和
交互。页面、布局、主题、业务控件语义保存在消费者工程；已有内置网页仅为可选参考实现，
不作为使用工具的前提。不要为了一个工程的风格需求改库内 `web/`。

## 只读当前任务需要的参考

以下路径相对于本 Skill 目录，包括通过符号链接加载时；不相对于用户工程。按需读取，不预先加载全部参考。

| 用户目标 | 读取内容 | 已有入口 |
|---|---|---|
| 接续工程、综合或实现 | [workflows.md](references/workflows.md#工程与构建) | `list_sessions`、`start_session`、`run_synthesis`、`run_implementation` |
| 看进度、日志或报告 | [workflows.md](references/workflows.md#进度与报告) | `open_run_monitor`、`get_run_snapshot`、`get_run_progress` |
| 按工程生成页面/可视化 | [visualization.md](references/visualization.md) | 状态/报告 JSON、`DebugService`；可选参考页 |
| 调试构建与产物交付 | [debug-delivery.md](references/debug-delivery.md) | 已有构建工具、`export_debug_bundle`、`check_debug_artifacts` |
| 本地实验、倒计时、动作标记、暂停/重做 | [experiment.md](references/experiment.md) | `create_debug_experiment`、`get_debug_experiment`、`debug_experiment_action` |
| 采样分析、状态/握手检查、下一轮取证 | [analysis.md](references/analysis.md) | `analyze_ila_capture`、`read_ila_waveform`、`debug_action` |
| 采样导出、离线 VCD、生成消费者波形页面 | [waveform.md](references/waveform.md) | `debug_action(export_ila)`、`read_ila_waveform` |
| 人机共用 ILA/VIO 板上调试、生成工程控件 | [hardware-debug.md](references/hardware-debug.md) | `open_debug_panel`、`get_debug_snapshot`、`debug_action` |
| 交付工程给人用 GUI 接续 | [workflows.md](references/workflows.md#原生-gui-交接) | 工程路径、实际会话、文件与报告证据 |
| 分析失败、约束或 IP；无 EDA 摸底 | [workflows.md](references/workflows.md#诊断与离线分析) | `get_critical_warnings`、`xdc_lint`、`inspect_ip_params`、`parse_xpr` |
| 判断报告可信度、核对版本或查官方资料 | [evidence-and-docs.md](references/evidence-and-docs.md) | 当前 Vivado 帮助、本地官方文档、AMD 官方站点 |
| 比较或配合 AMD Ross | [ross.md](references/ross.md) | 两套产品按任务并列选择 |

先从工程、已有会话和文件中发现输入，再补无法发现的信息。执行前明确工程路径、实际 Vivado 版本、session、run 与目标阶段。所有完成结论都带真实状态或产物依据。

## 原地接入与持续运行

用同一源码目录的 Python 环境执行 `python -m pip install -e .`，再查看 `python -m vivado_mcp connect --help` 选择客户端。接入建立 Skill 目录引用并配置源码 MCP；普通更新只维护本仓。具体安装、GUI attach 与现场步骤见仓库 `docs/RUN_MONITOR.md`，从 Skill 链接的真实目标定位仓库。

Skill 在下一次读取时使用新内容；Python 模块和模型上下文在实际加载边界更新。活动构建继续使用现有会话，重载 MCP 留到空闲时。刷新面板、查询或源码更新不应触发停止、reset、重跑或替换会话。一次接入不等于修改 Vivado init Tcl；真实设备操作需要明确授权。
