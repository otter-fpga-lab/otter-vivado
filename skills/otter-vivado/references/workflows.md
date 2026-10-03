# Vivado 任务路径

按任务选择下列路径。工具的完整参数、默认值和版本差异以当前 MCP schema 为准；这里不维护第二份 schema 或执行脚本。

## 工程与构建

1. 读用户工程已有说明；用 `list_sessions` 查看现有会话。已有 `.xpr` 可先用 `parse_xpr` 离线确认器件、顶层、文件集和 run 名称。
2. 需要执行时复用正确的会话。没有会话时选择 `start_session` 的 `gui`、`tcl` 或 `attach` 模式，记录实际 `vivado_path`。`gui` 默认端口可能复用已有 GUI；独立实例使用 schema 支持的 `port=0`。`attach` 需要目标 GUI 已有本仓 TCP 协议。
3. 用 `get_project_info` 与 `run_tcl(command="version -short")` 核对工程和版本。仅在尚未打开目标工程且会话可用时打开工程；含特殊字符的路径用 `safe_tcl` 参数模板。不要为了看进度切走用户当前工程。
4. 明确本次目标：综合、布局布线或生成 bitstream。读取当前 run 状态，沿用实际 run 名称和同一个 session ID；运行中直接转观察路径。
5. 发起已授权综合或实现时使用异步入口，避免让模型一直等待：

   ```text
   run_synthesis(run_name="synth_1", session_id="default", wait=False)
   open_run_monitor(run_name="synth_1", target_step="synth_design", session_id="default")
   ```

   综合目标真实完成后，按请求启动实现：

   ```text
   run_implementation(run_name="impl_1", session_id="default", wait=False)
   open_run_monitor(run_name="impl_1", target_step="route_design", session_id="default")
   ```

   上面名称仅为示例；替换为第 1–4 步核对的对象。返回 job ID 表示已提交，成功结论来自后续状态与产物。

6. 使用下一节核对终态。生成 bitstream 是单独的构建目标，使用当前 `generate_bitstream` schema；生成文件与 `program_device` 写入硬件是不同操作，设备操作需明确授权。

同一 Vivado Tcl 会话顺序执行命令。长任务期间让已有运行继续，观察超时后保留 session/run，继续读状态；无需为取得结果重新 launch。

## 原生 GUI 交接

用户需要新建且可由 GUI 接续的工程时，默认使用磁盘 Project Mode，显式指定新目录、part/top、源和约束；不以 `-in_memory` 或临时 Tcl 状态代替持久工程。可运行样例是仓库 `examples/progress/create_demo.tcl`，现有工程则保留其原组织方式。

Windows 本机操作优先使用用户实际安装的 Vivado GUI。交付时记录可打开的 `.xpr` 路径，或非工程流的 `.dcp`/重建 Tcl、源码与约束位置；同时给出 Vivado 版本、part/top、会话和完成阶段。检查外部文件引用是否仍可用，不把临时目录、浏览器 URL 或聊天记录当作唯一工程交付。

GUI 使用已有 `start_session` 的 `gui`/`attach` 模式和 Vivado 工程命令，具体步骤读取仓库 `docs/HUMAN_GUI_WORKFLOW.md`（从 Skill 链接真实目标定位仓库）。打开工程、Block Design、原理图与波形使用原生 GUI；网页面板读取同一 run 的状态、日志与报告。界面能打开、目标阶段完成、时序通过与功能正确分别留证据，未在真实 Windows/Vivado 执行的项目明确列为待测。

发现需要 RTL 功能修改时，将原始消息、源文件位置与复现条件交给负责该设计的 Coding 工作流；Vivado 工具侧继续提供项目操作与复测能力。已有 `.xpr`/源码不可因交付目的被自动重建或覆盖。

## 进度与报告

需要人机共同观察时，`open_run_monitor` 返回运行 MCP 那台机器上的本机 URL；`get_run_snapshot` 读取同一缓存。两者只登记/读取观察器，不启动构建。首次 `waiting` 允许等待采样；`busy`、失联或错误时先看最后成功采样时间，保留原始原因。

判断顺序：

1. 核对 session、run、目标阶段与 `connection`、`observed_at`。
2. 读取 Vivado 原始 STATUS、PROGRESS 和日志。PROGRESS 缺失即未知，不根据时间、日志行数或动画推算。
3. 区分目标完成、失败、取消与仅中间阶段完成：place 完成不代表 route 完成，route 完成不代表 bitstream 完成。
4. 查看报告来源与陈旧、设计不匹配等标记。面板中的文件只是该 run 目录中的候选报告，不能仅凭时间较新确认它属于本轮。

`get_run_progress` 适合单次状态查询。重复刷新优先使用快照，避免用户界面和模型各自密集查询 Tcl。面板扫描文件数量、大小、日志窗口都有边界；完整规则见仓库 `docs/RUN_MONITOR.md`，摘要里的 0 条错误不代表完整日志无错误。

运行状态与报告各自采样；`reports_status` 为 loading/error/stale 时可能显示旧报告，应同时核对 `reports_observed_at` 和 `reports_source`。慢报告不会阻塞 STATUS 更新。不再需要面板时用 `close_run_monitor` 释放指定 session/run/目标的观察器；它不停止 Vivado 或 run。只关浏览器标签会保留观察器以便再看。

需要重新生成时序或资源报告时，先等会话空闲，核对当前打开的 design。`get_timing_report`、`get_utilization_report` 和 `get_io_report` 针对当前 design；异步构建完成并不自动证明当前 design 就是该 run。确认目标后显式打开相应已完成 run，再调用报告工具。面板仅浏览已有报告，不隐式执行 `open_run` 或 `report_*`。

报告结论按 [evidence-and-docs.md](evidence-and-docs.md) 留证据。`completed` 表示请求阶段完成，时序、资源、约束和功能是否满足需求分别验证。

## 诊断与离线分析

| 当前证据/目标 | 工具与后续动作 |
|---|---|
| 综合/实现失败或关键告警 | 先保留快照中的首个相关错误与日志路径，再用 `get_critical_warnings` 查询明确的 run；它会写诊断快照，支持下次对比。读对应真实源码/XDC 后提出修复，再重跑受影响阶段验证。 |
| 正在运行或只允许观察 | 使用快照/已有日志。`get_critical_warnings` 的 `sim_*` 模式在特定失败下可能回退执行仿真批处理，不作为纯只读轮询。 |
| XDC 引脚、IOSTANDARD、时钟问题 | `xdc_lint(xdc_paths=[...])` 可离线检查。`xdc_auto_fix(dry_run=True)` 可生成候选补丁；依据实际板卡、电压和时钟核对后再按已授权修改范围应用，默认值不是板卡事实。 |
| IP 参数差异或修改未生效 | 活会话用 `inspect_ip_params` 读回实际配置；两个文件用 `compare_xci` 离线对照。写入成功还需核对读回值与目标行为，必要时在已授权范围生成/验证 IP。 |
| 无 Vivado 的工程摸底 | `parse_xpr`、`compare_xci`、显式路径的 `xdc_lint` 读取已有文件；`parse_bit_header` 和 `parse_ltx` 可核对已有产物。它们不证明综合、实现或硬件通过。 |
| 已授权修改 RTL 的快速语法检查 | `verilog_compile_check` 使用已安装的 iverilog/verilator；SKIP 记录为未执行。结果不能代替 Vivado 综合与功能验证。 |

遇到版本相关 Tcl/报告格式、工具崩溃或许可/库问题，按官方文档路径核对具体版本和错误签名。修复结果用同一输入与受影响阶段复测，记录消除、保留和新增问题。没有 EDA 时继续文件分析及可执行步骤准备，将必须现场验证的命令、前提和通过条件具体列出。
