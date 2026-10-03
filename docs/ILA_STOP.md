# 独立 ILA 停止：本轮核对结论

本插件不提供独立 `stop_ila` 操作。此项已完成能力核对与边界交付，不等待现场验证才收口。
这不是“所有 Vivado 版本都不能停止”的断言，也不代表自动探测过用户当前安装版本。

## 官方依据

2026-10-03 下载并读取 AMD/Xilinx
[UG835 v2022.2，2022-10-19](https://www.xilinx.com/support/documents/sw_manuals/xilinx2022_2/ug835-vivado-tcl-commands.pdf)。
以下页码为文档印刷页码；PDF 全文未找到 `stop_hw_ila` 命令条目。

| 命令 | 文档页码 | 已核对的含义 | 插件处理 |
|---|---|---|---|
| `run_hw_ila` | 1580 | “Arm hardware ILAs.”，`-trigger_now` 为立即触发采集；所列语法无 stop 参数 | 只作启动，不用立即触发冒充停止 |
| `reset_hw_ila` | 1515–1516 | 重置触发/采集控制属性及默认比较值；`-reset_compare_values false` 仍重置核控制属性 | 不用 reset 代替停止或保留配置 |
| `upload_hw_ila_data` | 1826–1827 | “Stop capturing. Upload any captured hardware ILA data.”，可中断采集并上传；同核已有 hw_ila_data 对象会被覆盖 | 当前 upload/export 仍要求完整单窗口，不开放“停止但不上传”假接口 |
| `wait_on_hw_ila -timeout` | 1843 | 超时终止的是等待命令 | 不声称超时已停止硬件 |

上传后的部分窗口、旧数据对象被覆盖和配置重置都是实际行为变化，不能隐藏在一个名为
“独立停止”的按钮后。以后若工程需要“停止并上传部分采样”，应按这个完整语义设计，
并基于实际版本/设备验证，而不是绕过现有完整采集检查。

## 机器可读边界与消费者指引

`get_debug_snapshot().capabilities.stop_ila` 继续为 false，兼容现有消费者；新增
`stop_ila_details` 提供 status、scope、reason_code、reason、reference、
active_version_probed、hardware_verified 和 fallback。范围为当前插件 API；
active_version_probed/hardware_verified 均为 false。读取能力不发送 Tcl、不启动会话。

MCP `debug_action`、共享服务及本地实验都不接受 stop_ila；后端占位方法明确报不支持，
不调用上传、重置或立即触发。暂停/中止实验只停止本地后续编排，关闭页面/服务也不停止
ILA，等待超时不能显示成“已停止”。消费者生成页面时说明不可用原因，不生成可点击的
虚假硬件停止按钮；实际停止状态必须另行从设备确认。

用户实际需要停止时，在已授权范围内：

1. 中止当前本地编排，等已接受短操作结束并记录回执；不要通过取消通信假装取消硬件命令。
2. 协调控制权交回人工，在原生 Hardware Manager 对明确的目标/核处理；按实际版本界面
   和帮助确认影响。插件不承诺原生操作保留采样、配置或没有其它副作用。
3. 回到共享服务显式 refresh，核对同一 target/device/核 UUID、ILA 状态与数据情况。
   不把点击按钮或 GUI 操作本身作为 IDLE 证据，不自动重 arm、重做或覆盖原有记录。

确有版本差异时，后续在实际使用中读取本机 `help` 和命令日志再扩展。发现同名 Tcl 命令
只证明名称存在，不证明停止语义或兼容性；不要自动试运行停止/上传/reset 来探测能力。
本轮没有真实 Vivado/板卡验证，也不以此阻塞完成。
