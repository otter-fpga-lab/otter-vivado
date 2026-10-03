# Otter Vivado 运行观察

此 fork 延续 NJ 的 [vivado-mcp](https://github.com/mapleleavessssssss-wq/vivado-mcp)
（Apache-2.0），复用其会话、构建和解析器。新入口只观察明确选择的 run，
不生成报告、不打开其他设计，不引入 Ross 依赖。

## 一次源码接入

先按 [一份源码接入多个客户端](SOURCE_CONNECTION.md) 安装，使用
`connect --client all` 一次配置 Cursor、Claude Code、Codex 与 Antigravity。
该文档集中说明目录链接、已有配置保留、Windows junction、自定义路径与日常更新。
完成源码接入后，再按下列流程选择实际 Vivado 会话。
多版本本机先按 [版本兼容指南](VERSION_COMPATIBILITY.md) 选定安装；2018.3 与 2024.2
为优先维护目标，2020.2/2022.2 一并保留，文档核对不代替现场验证。

GUI/attach 使用上游已有的协议注入：首次需要时显式执行 `vivado-mcp install`
（会备份并修改指定 Vivado 的 init Tcl），按 README 选择实际安装路径。
也可以在已有 MCP 客户端中 `start_session(mode="gui", port=0)` 启动独立实例，
由既有实现用 `-source` 加载协议，无需全局注入。已运行且未经注入的 GUI 无法凭空 attach。

## 从工程到人机共用状态

对已有合法工程，使用现有 `start_session` 和 `run_tcl("open_project ...")`。
确认 `get_project_info`、`run_tcl("version -short")` 的实际工程与版本后：

```text
run_synthesis(run_name="synth_1", session_id="default", wait=False)
open_run_monitor(run_name="synth_1", target_step="synth_design", session_id="default")
get_run_snapshot(run_name="synth_1", target_step="synth_design", session_id="default")
```

在运行 MCP 的本机浏览器打开返回的 `http://127.0.0.1:随机端口/随机路径/`。
采样由程序每 5 秒执行，无需模型持续查询；HTTP 每 2 秒只读缓存。
首次快照可能为 `waiting`。综合真实成功后再显式启动：

```text
run_implementation(run_name="impl_1", session_id="default", wait=False)
open_run_monitor(run_name="impl_1", target_step="route_design", session_id="default")
```

已有 `get_run_progress` 仍可单次查询。面板与 `get_run_snapshot` 共用同一观察器；
默认 JSON 省略报告原文（面板可展开），需要时指定 `include_report_text=True`。
面板也显示实际发现的原生 `.xpr` 路径，按
[GUI 工程交接指南](HUMAN_GUI_WORKFLOW.md) 可由人继续操作；文件存在不代表源依赖完整
或已经通过 GUI 打开测试。元数据不可用时仍保留有效 run 状态，不猜工程路径。
原生 `PROGRESS` 为 0 就显示 0，空值显示未知。它是 Vivado run 原生属性，
不保证代表当前目标或整个 FPGA 流程的完成比例，没有 ETA。
`place_design Complete!` 不等于 route 完成；route 完成也不等于 write_bitstream 完成。
错误/取消不会补写 100%。观察超时不会取消底层 run。

报告使用独立的后台读取，不等待慢磁盘才更新 STATUS。查看 `reports_status` 与
`reports_observed_at`；loading/error/stale 可能保留上次报告，页面会单独显示报告状态与
来源。运行已连接不代表报告刚读完，`ready` 仅表示文件读取完成。每个观察器最多一项
报告读取，运行来源改变时不接受迟到的旧来源结果。

不再需要观察时可显式释放页面、缓存与采样：

```text
close_run_monitor(run_name="impl_1", target_step="route_design", session_id="default")
```

它不关闭 Vivado 或停止 run，其他 run/目标的观察器保留；再次需要时重新调用
`open_run_monitor`。只关浏览器标签会保留观察器；Vivado 失联后停止定时查询，最后快照
仍可查看，直到显式关闭观察器或结束所属 MCP。

## 长时间观察与报告阅读

页面支持跟随系统、浅色与深色外观。报告可按名称、阶段或路径过滤；采样刷新保留选中项、
键盘焦点和原文阅读位置。工程/报告路径可以复制到剪贴板，浏览器不允许时会提示手动选择；
复制不会打开工程或执行命令。时序和资源摘要沿用共享解析器，包含 CLB/Slice 名称和
小数 BRAM Tile 用量；缺失/不合法数据明确降级，不算零或默认为通过。

外观参考 [shadcn/ui Theming](https://ui.shadcn.com/docs/theming) 的语义样式与
[Radix Colors 色阶用途](https://www.radix-ui.com/colors/docs/palette-composition/understanding-the-scale)，
使用 Slate/Teal 与状态色值。运行页面仍来自本仓同一静态资源，不需要 React、CDN、网络字体
或新的前端构建环境；颜色许可见 [第三方说明](../THIRD_PARTY_NOTICES.md)。

## 最小原生工程样例

没有工程时，可在**空会话**中创建仓内最小样例：

```tcl
set __otter_demo_dir {D:/scratch/otter-progress-demo-new}
set __otter_demo_part xc7a35tcpg236-1
source -encoding utf-8 {D:/source/otter-vivado/examples/progress/create_demo.tcl}
```

将路径与 part 替换为本机全路径及实际已安装/有许可的器件。输出目录必须不存在，
已有工程不会关闭或覆盖。随后使用上述综合/实现入口。样例没有板卡引脚约束，
只用于观察综合/布局布线，不用于 bitstream 签核或设备操作。

## 独立 CLI 与无 EDA 回放

已有 GUI 的协议端口为 9999 时：

```powershell
.venv\Scripts\python -m vivado_mcp monitor --port 9999 --run impl_1 --target route_design
.venv\Scripts\python -m vivado_mcp monitor --port 9999 --run impl_1 --target route_design --json
```

CLI 只 attach 这个端口，不启动 Vivado；Ctrl+C 仅关闭观察器连接，保留外部会话
及其 PID 标记。关闭浏览器不会取消构建。MCP 自身仍沿用上游的生命周期约定：
结束拥有 Vivado 子进程的 MCP 会关闭它，因此不要为了更新面板而重启活动 MCP。

没有 Vivado 时可运行固定、明确标注的合成回放：

```bash
python -m vivado_mcp monitor --replay examples/progress/running.txt
python -m vivado_mcp monitor --replay examples/progress/failed.txt --json
python -m vivado_mcp monitor --replay examples/progress/stage-complete.txt --target write_bitstream
```

回放不连接 EDA、不读取样本中的本地报告路径、不按时间生成假进度。

## 数据边界

- busy 时让出主命令通道；失联/采样失败保留最后快照并标明最后成功时间，不自动重连/重跑。
- Tcl 最多读取 `runme.log` 的末尾 64 KiB。窗口不完整时行号是窗口内相对值；
  诊断计数仅覆盖显示的尾部 30 行，0 不表示全程无错误。阶段来自最近日志记录，可能落后。
- 仅扫描该 run 目录顶层前 16 个 `.rpt`（文件名排序），每个显示至多 256 KiB。
  超限内容不解析摘要，不跟随报告 symlink，不生成报告、不混入当前打开的其他 design。
- 阶段是文件名线索；报告早于 `.vivado.begin.rst`、NEEDS_REFRESH 或 Design 不匹配
  都显示原因。时间较新仍不能证明本轮目标/约束匹配，标为未核实候选。
  报告摘要沿用现有 timing/utilization parser；全局时序/资源预算保持未知，不当签核工具。
  选中报告可直接查看 WNS/TNS/WHS/THS 与资源用量图表；数值仅来自该报告的解析字段，
  未识别/缺失的指标显示未知。Windows 报告与会话输出共用 UTF-8/系统 ANSI 解码策略。
- 报告和日志只在本机随机路径页面展示；HTTP 只支持页面/缓存 GET，无 Tcl/文件写入口。
  同机其他用户若获得 URL 仍能查看，勿将工程链接公开分享。

## 官方与 Ross 对照

核对日期 2026-10-03；文档研究不冒充本机验证。

| 路线 | 已有证据 | 本轮选择 |
|---|---|---|
| 本 fork main 60b13cf / 0.3.25 | GUI/Tcl/attach、异步 run、解析器；README 基线 2019.1，2018.3/2022.2 仅局部现场 | 复用并加薄观察层；保留作者/许可/贡献历史 |
| AMD Ross main | [源码 2cdc9eef](https://github.com/Xilinx/ross-ai-assistant/tree/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de)、[工具参考](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/docs/reference/vivado-mcp-tools.md)：状态/日志/历史与长命令监控；发布标签 2026.9.1 指向另一提交 c89c3328 | 并列可选；借鉴按需加载、版本核对和证据验证，详见 [Ross 参考](../skills/otter-vivado/references/ross.md)；未运行其二进制 |
| Ross 插件/扩展 | [插件安装](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/docs/getting-started/install-plugin.md)：skills 插件与 MCP 二进制分开，AI Extension 捆绑；各客户端有缓存/链接差异 | 本产品采用 [Skill 原位目录链接 + 源码 MCP](SOURCE_CONNECTION.md)，不复制外部分发包 |

AMD UG835 2026.1：[get_runs](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/get_runs)、
[get_property](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/get_property)、
[launch_runs](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/launch_runs)
说明属性可空、run 可停止在中间 step。UG893 2026.1
[Understanding Run Status](https://docs.amd.com/r/en-US/ug893-vivado-ide/Understanding-Run-Status)
明确运行完成仍可能 timing fail。这些是保守状态判断的依据。

上游 [PR #5](https://github.com/mapleleavessssssss-wq/vivado-mcp/pull/5) 保持 OPEN，
head b40e62cd、`feat/multi-version-optimization` 不变。本实现从 main 独立开发，
不继承旧 PR 的 PLAN_ONLY、版本发现、磁盘/live design 混源等未解决实现。

## 现场待测清单

云端未运行商业 Vivado、Ross 或板卡。请仅在上面新建的消费样例/用户明确指定工程验证：

1. 记录 OS、Python 与 `version -short`；先验证当前实际安装版本，不扩大全版本矩阵。
2. GUI/Tcl 模式运行综合/实现，确认 STATUS、PROGRESS、STATS.ELAPSED 的原文格式，
   实际采样延迟、中文/空格路径、`.vivado.begin.rst` 位置及报告命名。
3. 长运行中持续刷新/关闭面板；主命令较慢时显示 busy；不出现 reset、重复 launch、响应串台。
4. 在独立样例按 `launch_runs impl_1 -to_step place_design` 结束，再以 route/bitstream 为
   观察目标，确认只标阶段完成；失败样例保留真实百分比与错误。不要写板卡。
5. 断开观察器/关闭外部 GUI后确认失联与旧时间；重新打开时显式选择会话，不自动重跑。
6. 留一份旧报告、不同 Design 和 placed 报告，确认 stale/mismatched/post-place；
   正常报告内容与原文件一致，无报告时保持未知。
7. Windows 验证 Skill 链接权限与客户端实际发现；源码更新仅在空闲加载边界使用。
8. 完成样例运行后按 [GUI 指南](HUMAN_GUI_WORKFLOW.md) 在原会话检查 Sources/Reports，
   关闭原工程后重新打开 `.xpr`，确认已导入源码与约束仍可用；不要第二实例并发写同一工程。

实际云端验证命令、结果与远端接续点见根 [TASK.md](../TASK.md)。
