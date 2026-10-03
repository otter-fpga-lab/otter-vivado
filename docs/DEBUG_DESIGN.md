# 准备 ILA/VIO 调试工程

本库提供结构化调试描述、离线生成、现有工程预检和显式应用。人可以保存并检查生成的
Tcl/RTL 片段；AI 可以使用同一份描述，通过 MCP 工具分步准备工程。生成文件和项目
专用描述保存在消费者工程中。

这一阶段负责准备调试源和约束。创建 IP 或添加约束成功之后，仍须完成需要的 RTL
连接，运行综合、实现，检查报告，并生成配套 `.bit` / `.ltx`。这些准备操作不会自动
综合、实现、下载板卡，也不会修改当前内存中的综合网表。

## 选择准备方式

| `kind` | 用途 | 应用后的结果 |
| --- | --- | --- |
| `ila_ip` | 在 RTL 中例化 ILA | 创建配置 IP 并生成输出产物；返回须接入 RTL 的例化片段 |
| `vio_ip` | 在 RTL 中例化 VIO | 创建配置 IP 并生成输出产物；返回须接入 RTL 的例化片段 |
| `mark_debug` | 在综合阶段保留指定信号 | 将生成的标记约束加入指定综合 run 的约束集 |
| `ila_netlist` | 在实现阶段给综合网表插入 ILA | 将生成的插核约束加入指定实现 run 的约束集 |

VIO 输出必须实际连到设计中的控制路径，才会改变硬件行为。参数下一帧生效、跨时钟域
传递、成组参数原子更新等行为，仍需在业务 RTL 中设计和验证。生成描述不能替代这些连接。

`MARK_DEBUG` 能参与保留综合阶段的信号，但不能恢复已经被优化掉的 net。已完成综合
而找不到目标信号时，应先补充保留措施并重新综合，再确认实际综合网表名称。

## 先离线生成

在已安装本库的环境中运行以下命令；默认只向标准输出返回 JSON，不连接 Vivado，
不写工程文件：

```bash
python -m vivado_mcp debug-prepare --spec examples/debug/design-ila.json
```

显式指定消费者工程目录，可保存 `plan.json` 以及计划中的全部 `artifacts`：

```bash
python -m vivado_mcp debug-prepare --spec examples/debug/design-ila.json --output-dir /path/to/consumer/debug/ila
```

`--spec` 是最多 64 KiB 的 UTF-8 JSON 文件。`--output-dir` 的任一目标文件已经存在，
此次保存就会拒绝，已有文件保持原样。要保留多次方案，请为每次方案选择新的目录。
计划中的 `remaining_steps` 记录后续工作；离线生成成功不表示该 Vivado 版本、器件、
时钟或资源已经通过检查。

## 在已有 Vivado 工程中应用

先在已有 Vivado GUI 中打开目标工程，并启用本库的 TCP 接入：

```bash
python -m vivado_mcp debug-prepare --spec /path/to/consumer/debug/design.json --apply --port 9999
```

CLI 只连接指定端口的已有 GUI。它先预检，取得 `name`、`directory`、`part` 工程身份，
再把同一身份传给应用步骤；应用时重新核对，避免中途切换工程后写入另一个项目。
预检未通过时输出原因并退出，不继续应用。退出只断开本次连接，不关闭 Vivado。

`--apply` 与 `--output-dir` 互斥：前者修改明确的 Vivado 工程，后者只把离线产物保存到
指定目录。应用遇到错误时，应先核对返回的工程状态和已有文件；部分完成或结果未知
不能当作已回滚，也不能直接重放。

AI 可用以下 MCP 工具完成相同过程：

| 工具 | 行为 |
| --- | --- |
| `plan_debug_design(spec)` | 离线校验描述并生成计划、Tcl、例化片段或约束 |
| `inspect_debug_design(spec, session_id)` | 只读检查当前工程、IP 目录或指定 run/约束集 |
| `prepare_debug_design(spec, expected_project, session_id)` | 核对预检返回的工程身份后创建 IP 或注册约束 |

`expected_project` 使用预检返回的完整 `project` 对象，不能从工程文件名推测。
输入是具有固定字段的调试描述，不接受任意 Tcl 代码。`inspect` 返回 `ready` 才能进入
应用；IP 的版本相关配置仍需要在创建对象后检验，预检不等于配置已被 Vivado 接受。

## RTL 例化方式

### ILA

[design-ila.json](../examples/debug/design-ila.json) 创建两个探针：

```json
{
  "kind": "ila_ip",
  "name": "ila_isp",
  "clock": "pixel_clk",
  "depth": 4096,
  "probes": [
    {"name": "frame_valid", "width": 1},
    {"name": "pixel_data", "width": 24}
  ]
}
```

`probes` 顺序就是 `probe0`、`probe1` 的端口顺序。`depth` 省略时为 1024，支持范围为
1024 至 131072 之间的 2 的幂，实际可用配置由 Vivado 和器件确认。每个探针给出 RTL
信号名和完整位宽；本入口不推断总线宽度、位选、拼接或时钟域。

### VIO

[design-vio.json](../examples/debug/design-vio.json) 观察帧计数，并提供增益和旁路控制：

```json
{
  "kind": "vio_ip",
  "name": "vio_isp",
  "clock": "pixel_clk",
  "inputs": [{"name": "frame_count", "width": 16}],
  "outputs": [
    {"name": "gain", "width": 12, "initial": 256},
    {"name": "bypass", "width": 1, "initial": 0}
  ]
}
```

`inputs` 是从业务设计进入 VIO 的观察值，`outputs` 是从 VIO 驱动业务设计的控制值。
两者可以省略为空列表，但总计至少有一个探针。输出的 `initial` 是硬件初始化原始值，
省略时为 0；可填写非负整数、十进制字符串或 `0x` 十六进制字符串，数值必须能放入
相应位宽。宽值建议用 `0x` 字符串输入；规范计划回传十六进制字符串，避免 JSON 客户端
丢失大整数精度。它不负责单位换算、定点缩放或业务生效应答。

这两种描述的 `name`、`clock` 和探针信号名采用非关键字的简单 Verilog 标识符，
不接受层次路径、位选或表达式；未知字段会被拒绝。生成产物为 `<name>_create.tcl`
和 `<name>_instance.vh`。例化片段需要放到正确模块中，并核对已有信号的方向、位宽、
驱动来源及采样时钟。工具不会自行改写业务 RTL，也不会把返回的片段自动加入源码。

在线应用创建和配置 IP，读取该版本实际公开的配置属性、验证读回结果并生成输出产物。返回
`status: "created"` 表示这一准备步骤完成，不表示例化已经接入、IP 已完成综合，
或设计已生成 bitstream。

## 网表插核与保留约束

`ila_netlist` 使用综合网表中真实存在的完整 net 名称。每组 `nets` 从 `bit 0` 开始
排列；以下例子把 `pixel[0]` 连到 `probe1[0]`，把 `pixel[1]` 连到 `probe1[1]`：

```json
{
  "kind": "ila_netlist",
  "name": "ila_camera",
  "run": "impl_1",
  "clock": "u_isp/pixel_clk",
  "depth": 4096,
  "probes": [
    {"nets": ["u_isp/frame_valid"]},
    {"nets": ["u_isp/pixel[0]", "u_isp/pixel[1]"]}
  ]
}
```

`run` 省略时为 `impl_1`。生成脚本按完整名称精确匹配时钟和探针，检查名称唯一性、
目标是否存在、是否已有同名调试核；这些检查在创建调试核之前执行。通配符不会被当作
选择一组信号的请求。脚本不猜测时钟域；自由运行时钟、跨域采样和资源需求必须由工程
负责人或智能体结合设计证据确认。

独立的 `mark_debug` 描述用于保留信号：

```json
{
  "kind": "mark_debug",
  "name": "camera_observe",
  "run": "synth_1",
  "nets": ["u_isp/frame_valid", "u_isp/pixel[0]", "u_isp/pixel[1]"]
}
```

`run` 省略时为 `synth_1`。这里的名称须在综合处理约束的阶段可解析；仅在后续综合网表
中出现的名称未必能用于更早的保留约束。没有匹配对象时脚本会报错，不会静默跳过。

约束通过专用 unmanaged Tcl 文件保存，以支持完整的检查和插核流程。在线应用将文件
以仅创建方式写入工程目录的 `otter_debug`，然后加入指定 run 的 `CONSTRSET`：

| 类型 | `USED_IN_SYNTHESIS` | `USED_IN_IMPLEMENTATION` | `PROCESSING_ORDER` |
| --- | --- | --- | --- |
| `mark_debug` | `true` | `false` | `LATE` |
| `ila_netlist` | `false` | `true` | `LATE` |

应用只注册文件，不执行生成脚本，不修改当前内存设计。返回 `constraints_added`
表示约束已加入，`built: false`、`connections_verified: false` 表示仍需真实构建验证。
若多个 run 共用此约束集，返回的 `affected_runs` 列出受影响的 run，应据此安排构建。
生成文件已存在或已注册时拒绝覆盖；更新方案应使用新的明确名称，并检查旧约束的取舍。

后续复用现有 `run_synthesis`、`run_implementation` 等构建入口。完成后核对实际调试核、
探针连接、资源占用与时序结果，再进入[板上调试](HARDWARE_DEBUG.md)。本阶段尚未实现
`.bit/.ltx` 自动配对与下载、采样导出、浏览器波形以及倒计时的人机配合实验。

## 智能体生成界面的职责

工程专用页面的视觉风格、配色和布局由生成页面的智能体根据用户需求决定。本库负责
接口、控件绑定语义、状态与错误反馈，以及默认控件的基本可用性和可访问交互。这些
约定让不同界面都能可靠操作同一套调试能力。

信号含义、原始值和业务单位的关系、合理范围与实验步骤保存在消费者工程中。智能体
先读取实际探针结构和这些工程说明，再生成控件描述。当前已有非负整数滑杆和开关；
任意自定义网页布局、定点数、曲线和多步骤实验仍需后续扩展。准备 ILA/VIO 工程描述
不代表这些显示能力已经实现。

## 命令依据与验证范围

工程约束、网表对象和调试核命令参照 AMD/Xilinx 官方文档：

- [UG835 2022.2 Tcl 命令参考](https://www.xilinx.com/support/documents/sw_manuals/xilinx2022_2/ug835-vivado-tcl-commands.pdf)
- [UG903 2022.2 约束使用指南](https://www.xilinx.com/support/documents/sw_manuals/xilinx2022_2/ug903-vivado-using-constraints.pdf)
- [UG912 2022.2 属性参考](https://www.xilinx.com/support/documents/sw_manuals/xilinx2022_2/ug912-vivado-properties.pdf)
- [PG159 VIO 产品指南](https://www.xilinx.com/support/documentation/ip_documentation/vio/v3_0/pg159-vio.pdf)

自动化测试覆盖描述校验、脚本生成、文件保护、模拟会话预检及应用分支；这些测试不
连接商业 EDA。具体 Vivado 版本的 IP 属性、约束加载顺序、综合网表连接和 Windows
现场行为仍需真实 Vivado 工程验证，板上采集还需实际硬件验证。


## 构建后核对产物

完成 RTL 连接和实际构建后，用 [调试产物核对](DEBUG_ARTIFACTS.md) 读取 `.bit/.ltx`，
对照消费者工程要求检查器件、核实例名和探针，并保存 SHA256。MCP 入口为
`check_debug_artifacts`，独立 CLI 为 `debug-artifacts`。这一步不自动启动构建，
也不能仅凭两份文件的名称、时间或 LTX UUID 认证配对；仍需同一次实现的来源和板上验证。
