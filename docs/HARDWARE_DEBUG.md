# 人和 AI 共用的 ILA/VIO 调试

本库提供调试接口和状态服务。以下网页是可选参考实现；智能体为消费者工程生成页面时，
读取 [可视化 Skill](../skills/otter-vivado/references/visualization.md)，页面保存在消费者工程。

首批能力面向**已经下载带有 ILA/VIO 的设计、已经在 Hardware Manager 打开目标**的工程。
可以明确选择目标和器件、发现调试核及探针、写 VIO 输出、配置一个 ILA 探针的触发值与
触发位置、启动单窗口采集，并在完整采集后上传至 Vivado。通用页面和 MCP 工具调用
同一个调试服务；页面控件直接执行操作，每次调整不需要经过模型。

工程中的 IP 创建、MARK_DEBUG 与网表插核约束生成见 [工程准备指南](DEBUG_DESIGN.md)。
已有 [bit/ltx 离线核对](DEBUG_ARTIFACTS.md)，可检查完整性和预期核/探针。
已有 [实现检查点导出](DEBUG_BUNDLE.md) 和构建接续流程；真实硬件配对仍待验证。
浏览器波形、采样导出、倒计时实验或自动分析闭环仍未提供。
没有 ILA 停止按钮。上传返回 Vivado 的采样数据对象名，波形仍在原生 Vivado 查看。
现场设备验证与不依赖商业 EDA 的自动化测试分别记录；合成演示不能作为板卡验证证据。

## 先运行合成演示

在本仓库根目录安装源码后运行：

```bash
python -m pip install -e ".[dev]"
python -m vivado_mcp debug --demo --panel examples/debug/panel.json
```

打开命令输出的本机 URL。页面明确标为演示数据，目标为 `demo://isp`，器件为
`demo-fpga`。示例滑杆改变本地 `gain`，开关改变本地 `bypass`，不访问 Vivado、JTAG
或真实板卡。立即采集只产生固定合成结果；等待触发不会由计时器模拟完成。
体验上传流程请使用“立即采集”；若演示进入等待触发，重启 demo 可清除本地状态。

一次输出快照并退出，无需浏览器：

```bash
python -m vivado_mcp debug --demo --panel examples/debug/panel.json --json
```

快照的 `source: "demo"` 标识合成输入。它能验证本地控件绑定和操作流程，不能证明实际
Vivado 的命令、设备状态、触发时序或硬件功能正确。

## 连接已有 Vivado

1. 先启动已配置本库 TCP 接入的 Vivado GUI，准备板卡及匹配的 `.bit` / `.ltx`。
2. 在 Hardware Manager 中连接服务器、打开硬件目标，确认 ILA/VIO 探针可见。
3. 运行命令并从页面选择准确的目标与器件：

```bash
python -m vivado_mcp debug --port 9999
```

`--port` 是已有 Vivado 的协议端口。此入口仅 attach，不启动 Vivado、不打开 JTAG
target、不烧录，不自动选择真实设备。先用以下命令获取发现的完整名称：

```bash
python -m vivado_mcp debug --port 9999 --json
```

再将发现的名称原样用于 `--target` 和 `--device`，两者必须同时传入；也可继续在页面
中选择。`Ctrl+C` 仅关闭本调试服务和 attach 连接，不关闭 Vivado、不停止已启动或
正在等待触发的硬件采集。

页面的已写入/读回值来自 VIO 探针，**不等于业务逻辑已经采用该参数**。需要下一帧生效、
原子更新或明确应答的工程，应提供相应 RTL 控制路径和状态反馈。VIO 不提供确定时长的
硬件脉冲，浏览器操作时序也不能作为 FPGA 精确触发时基。

## 让 AI 与人接续操作

已有 MCP Vivado 会话可使用以下工具：

| 工具 | 用途 |
| --- | --- |
| `open_debug_panel(session_id, panel_path?)` | 为现有会话返回本机页面 URL；可加载项目 JSON 面板 |
| `get_debug_snapshot(session_id)` | 只读共享缓存，含控制权、设备状态及操作结果 |
| `debug_action(action, params, expected_revision, session_id)` | 提交一次操作并返回操作 ID |
| `close_debug_panel(session_id)` | 关闭页面服务与轮询，不关闭 Vivado 或停止 ILA |

要让人工页面与 AI 使用同一份状态，打开 **该 MCP 会话返回的 URL**。单独运行的
`debug` CLI 或另一个 MCP 进程拥有独立服务，控制权不会跨服务同步。

新服务默认 `control: "manual"`。AI 先取得最新快照，用其中 `revision` 提交
`control`，参数为 `{"owner": "ai"}`，然后才能选择设备和执行硬件操作。人工通过
页面切换回 `manual` 后，AI 的写入会被拒绝。这个控制权只协调本服务，不能拦截原生
Vivado GUI、`run_tcl` 或其他进程；从那些入口操作时仍需协调。

每次提交都使用最新 `expected_revision`，等待操作完成后再读取快照进行下一步。
忙碌或版本过期的请求被拒绝，不排队。`operation_id` 是受理回执，最终结果须查
`operations` 中对应项：`succeeded`、`failed` 或 `unknown`。`unknown` 表示结果
未能完整确认，可能已部分生效，不能自动重放；应核对当前设备，成功执行 `refresh`
后再决定下一次动作。`observed_at` 表示最后成功观察时间，缓存不等于持续实时测量。

| action | params | 行为 |
| --- | --- | --- |
| `inventory` | `{}` | 发现已连接的目标及器件 |
| `select` | `{"target":"完整目标名称","device":"完整器件名称"}` | 明确选择并读取其 ILA/VIO |
| `refresh` | `{}` | 显式刷新当前选择 |
| `control` | `{"owner":"ai"}` 或 `{"owner":"manual"}` | 切换本服务控制权 |
| `write_vio` | `{"core":"核名称","probe":"探针名称","value":"123"}` | 写非负十进制或 `0x` 十六进制整数 |
| `configure_ila` | `{"core":"核名称","probe":"探针名称","trigger_value":"eq1'b1","trigger_position":0}` | 使用 Vivado 原生触发值语法；位置可省略 |
| `arm_ila` | `{"core":"核名称","immediate":false}` | 等待触发；`true` 为立即采集 |
| `upload_ila` | `{"core":"核名称"}` | 仅上传已经完整结束的单窗口采集 |

配置一个探针不会自动清空其他探针的触发条件。执行前应检查完整触发设置，避免把
页面可编辑的单项误认为当前 ILA 的全部触发条件。ILA 的原始状态、采集完成标记和
采样数量均保留在快照中；条件采集、多窗口与高级触发暂不由此入口配置。

页面使用绑定于 `127.0.0.1` 的临时 HTTP 服务和独立访问令牌。页面相对路径
`snapshot.json` 仅读缓存，`actions` 接收操作请求。它是本机页面内部传输协议，
包含同源与令牌校验；项目面板使用下面的 JSON 绑定方式，不依赖内部 URL 写入硬件。

## AI 为工程生成专用控件

将面板 JSON 存在消费者工程中。库负责控件和探针绑定，工程负责名称、含义、范围及
布局选择。AI 先读取真实探针结构与工程说明，再生成描述，不能仅凭名称猜参数语义。

以下示例来自 [examples/debug/panel.json](../examples/debug/panel.json)：

```json
{
  "title": "ISP 调试示例（合成演示）",
  "controls": [
    {
      "kind": "slider",
      "label": "增益寄存器原始值",
      "core": "demo_vio",
      "probe": "gain",
      "min": 0,
      "max": 4095,
      "step": 1
    },
    {
      "kind": "toggle",
      "label": "算法旁路",
      "core": "demo_vio",
      "probe": "bypass"
    }
  ]
}
```

真实工程须替换成实际发现的核与探针完整名称，并去掉“合成演示”标题。使用
`--panel path/to/panel.json` 或 `open_debug_panel(panel_path=...)` 加载。

首批面板最多 64 KiB、64 个控件，只支持非负整数滑杆和 `0/1` 开关；滑杆范围上限
65535，实际写入还受探针位宽约束。不接受 JavaScript、外部页面、任意 Tcl 或重复
探针绑定。浮点、定点缩放、枚举、单位、曲线和自定义网页布局尚未实现。

## 命令与属性依据

本批按 AMD/Xilinx 官方 [UG835 2022.2 Tcl Command Reference](https://www.xilinx.com/support/documents/sw_manuals/xilinx2022_2/ug835-vivado-tcl-commands.pdf)
与 [UG912 2022.2 Properties Reference](https://www.xilinx.com/support/documents/sw_manuals/xilinx2022_2/ug912-vivado-properties.pdf)
核对 `get_hw_*`、`refresh_hw_vio`、`commit_hw_vio`、`run_hw_ila`、`upload_hw_ila_data`
及相应对象属性。使用精确对象匹配，缺失属性保留未知或拒绝相关操作。单独停止没有使用
猜测的 `stop_hw_ila` 命令，也不把会停止采集的上传操作当作无副作用的状态读取。
真实版本的属性、采样状态新鲜度和 GUI 交互仍需在现场验证；文档核对不等于实机通过。

## 后续逐步推进

1. **接入真实设备验证**：核对 Vivado GUI、Hardware Manager、实际 ILA/VIO 的发现、
   写入读回、触发、立即采集及完整上传；保留 Windows 和实际版本的现场证据。
2. **完成调试构建**：[工程准备](DEBUG_DESIGN.md) 已支持 IP 配置、例化模板、
   `MARK_DEBUG` 和网表插核约束；继续核对实际连接、构建并关联 `.bit` / `.ltx`。
   业务 RTL 新增参数生效机制继续按 RTL 开发验证。
3. **采样证据与分析**：导出采样、浏览器波形、协议和状态机分析、保存采集配置与证据，
   支持 AI 基于真实数据选择下一轮触发条件。
4. **人机配合实验**：本地确认就绪、倒计时、声音/动作提示、暂停或重做、动作标记；
   需要精确对齐时，以 FPGA 触发事件或帧编号作为依据。高速 ILA 的采样窗口很短，
   “静止三秒、移动三秒”需选择关键帧、条件采样或低速统计，不能等同连续录制六秒。
5. **工程专用调试台**：扩展按钮、枚举、定点数、单位、预设、趋势曲线和可读取的板卡
   信息；图像预览接工程实际图像通道，ILA/VIO 继续承担控制和调试。
6. **分步与自动化共存**：每一步可由人或 AI 独立执行、交接，逐步支持静态调试闭环，
   动态实验由本地流程执行，避免依赖聊天往返来控制操作时间。
