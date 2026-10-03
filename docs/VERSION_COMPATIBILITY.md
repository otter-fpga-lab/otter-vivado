# Vivado 版本兼容

常用目标为 **2018.3、2020.2、2022.2、2024.2**，其中 **2018.3 与 2024.2 优先维护**。
2019.1 保留为上游历史基线。这里的“目标”是代码维护与验证范围，实际通过记录以
[TASK.md](../TASK.md) 为准；本轮没有启动这四版商业 Vivado 或操作板卡。

## 在多版本机器上选对工具

每个工程先确定自己的 Vivado 版本、器件与 IP 依赖，不以“找到的最新版”替代用户选择。
用同一 Otter Python 环境只读列出安装候选：

```powershell
.\.venv\Scripts\python -m vivado_mcp versions --json
```

磁盘路径推断的版本只用于发现候选，不能证明二进制能启动、许可可用或工程兼容。
JSON 中 `version_evidence=installation_path_only` 明确这一边界；`selected_path`、
`selection_error` 与各项 `version_from_path` 用于核对选择。清单只检查配置路径、PATH
与已知安装目录，不扫描全盘；自定义安装不在清单时直接指定实际路径。
实际执行时明确 `vivado_path`，会话内再用 `version -short` 核对。例如：

```text
start_session(session_id="v2018_3", mode="gui", port=0, vivado_path="D:/Xilinx/Vivado/2018.3/bin/vivado.bat")
run_tcl(command="version -short", session_id="v2018_3")
```

2024.2 同样用独立 session ID、`port=0` 和实际安装路径。2020.2、2022.2 沿用相同步骤。
路径仅为示例；同一工程只由一个可写会话管理。已运行的 GUI 需要接续时，显式选择其
`attach` 端口并核对版本；不要用启动另一个版本的请求去重置已有会话或活动 run。
显式路径或 `VIVADO_PATH` 无效时会报错，保留该选择，不静默改用另一版；显式选择的版本
与端口已有 GUI 不一致或无法核实时，也会保留已有 GUI 并提示独立实例或显式 attach。

选择具体版本后，原生 GUI、无头 Tcl 与浏览器观察的使用入口分别见
[GUI 工程交接](HUMAN_GUI_WORKFLOW.md)、[源码接入](SOURCE_CONNECTION.md) 和
[运行观察](RUN_MONITOR.md)。普通 Skill/源码更新仍只维护一份，不为四个 Vivado 版本
建立四套业务实现。

## 已核对的官方命令范围

2026-10-03 实际读取了下列固定版本的 UG835 正文：

- [2018.3 UG835（官方 PDF）](https://docs.amd.com/v/u/2018.3-English/ug835-vivado-tcl-commands)，2018-12-05。
- [2020.2 UG835](https://docs.amd.com/r/2020.2-English/ug835-vivado-tcl-commands)。
- [2022.2 UG835](https://docs.amd.com/r/2022.2-English/ug835-vivado-tcl-commands)。
- [2024.2 UG835](https://docs.amd.com/r/2024.2-English/ug835-vivado-tcl-commands)，2024-11-13。

| 本仓使用路径 | 四版官方文档核对结果 | 运行时仍需核对 |
|---|---|---|
| 原生工程 | `create_project`、`open_project`、`import_files -fileset … -flat`、`archive_project` 已有对应命令 | 实际 `.xpr` 位置、已安装器件、外部源码与 IP 依赖；GUI/Tcl 创建目录差异 |
| 综合/实现 | `get_runs`、`get_property`、`launch_runs -jobs` / `-to_step`、`reset_runs` 已有对应命令 | 实际 run 名称和 STATUS；中间 step 完成不代表目标完成 |
| 报告 | `report_timing_summary -return_string`、`report_utilization -return_string` 已有对应选项 | 当前打开的 design、报告阶段、表头/行名/数值与解析结果 |
| 版本 | `version -short` 返回当前运行工具的版本号 | 与选中的安装路径及工程版本是否一致 |

文档中有命令不等于完整流程已实测。`get_property` 可以返回空值，原生 PROGRESS、耗时或
可选属性缺失时保留未知；已有 STATUS 有效时，可选元数据缺失不应把运行包装成失败。
`launch_runs -to_step` 明确允许中途停止，不能仅看任意 `Complete!` 就宣告 route/bitstream 完成。

## Windows 与工程交付

[2018.3 UG973](https://docs.amd.com/v/u/2018.3-English/ug973-vivado-release-notes-install-license)
列出的 Windows 范围是 64 位 Windows 7 SP1 与 Windows 10 1803/1809；
[2024.2 UG973](https://docs.amd.com/r/2024.2-English/ug973-vivado-release-notes-install-license/Supported-Operating-Systems)
列出 Windows 10 22H2 与 Windows 11 23H2。旧版 Vivado 在更现代 Windows 上能否正常
启动、运行脚本与仿真，需要按用户实际 OS 构建号验证，不能由 Python 单测推断。

本仓 Python 环境独立于 Vivado 自带工具，仍要求 Python ≥ 3.10。2018.3 官方命名约定要求
保守的工程/文件名，并指出 Windows 路径长度风险；新建消费样例优先使用短、纯 ASCII
目录，例如 `D:/vivado_work`。Tcl 参数能传递空格或 Unicode，只证明传输/转义边界，
不代表具体 Vivado 版本及其子工具支持任意路径。已有工程不自动搬动或改名。

原生 `.xpr` 优先用原版本重新打开。遇到 project/IP 升级提示先保留具体信息，查看
`get_ip_status` 或原生 IP Status；不批量 `upgrade_ip`，不覆盖旧工程以试探兼容。
跨版本迁移作为明确的单独任务进行，保留旧版可运行工程，按 GUI 指南归档并在新目录验证。
`open_project -read_only` 在上述版本有官方定义，可用于适合只读的检查；它不能保证新版
工具接受旧 IP，也不能替代原版本运行验证。

## 报告、可视化与 Ross 的边界

资源报告同时受版本、器件系列和设计阶段影响。2024.2
[report_utilization](https://docs.amd.com/r/2024.2-English/ug835-vivado-tcl-commands/report_utilization)
明确表名会随器件与阶段变化；[UG906](https://docs.amd.com/r/2024.2-English/ug906-vivado-design-analysis/Report-Utilization)
说明资源数也可能随优化改变。不要把 7 系列的 `Slice` 命名当作所有器件的唯一格式，
也不要将小数资源用量截成整数。解析失败、缺字段或未约束的时序保持未知，并提供原报告。

时序摘要与网页图表来自报告实际字段；某个 run 完成不证明当前打开的 design 就是该 run，
也不证明时序收敛。当前设计阶段以该次时序报告的 `Design State` 明确值为依据，缺失或
未识别时保持 `unknown`，不借其他 run 状态补全。现场应把报告头、原生 GUI 显示和 `.rpt` 原文一起核对，记录样本所属
版本/器件/阶段。合成回放与测试桩只能验证处理逻辑，不标成任何版本的 EDA PASS。

AMD Ross 与 Otter 并列且独立可用。[Ross getting-started（固定提交 2cdc9eef）](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/docs/getting-started/README.md)
说明其测试版本为 2026.1；这不是上述四版本的实测证据。其具体 Skill 也有独立版本要求，
例如该提交的 timing methodology Skill 要求 2026.1+，不能为采用该 Skill 自动升级用户工程。

## 定向现场验收

先做 2018.3 与 2024.2，再在 2020.2/2022.2 重复受影响路径；每版使用独立样例目录和
该版实际已安装的器件，不占用 Coding 正在做的本机功能测试。

1. 记录 OS 构建号、Python、安装路径与 `version -short`；验证 GUI/Tcl 启动，以及显式
   attach 命中预期版本。观察另一版已有 GUI 时不得误接管或终止它。
2. 用 [原生样例](HUMAN_GUI_WORKFLOW.md) 创建工程，核对源/约束导入、part/top 和 `.xpr`；
   完成后在同版 GUI 重开，确认不依赖 Otter 仓库中的消费源。
3. 运行综合/实现并对照 Design Runs 与面板；查询、刷新、关闭面板均不重复 launch/reset。
   对中间 step、失败或采样断开核对原始状态，缺百分比保持未知。
4. 保留 timing/utilization 原始 `.rpt`，核对资源名称、小数用量、表头列位置与时序摘要。
   只记录实测所用器件和阶段，不外推全系列兼容。
5. 需要仿真/IP 时再做对应 XSim 启动、日志位置、IP 参数读回和必要生成验证；不因基础
   工程通过就宣布全部仿真、IP 或板卡流程已通过。

每项记录命令、实际结果与产物路径，未执行项写明前提和下一步；不以猜测补齐版本矩阵。
