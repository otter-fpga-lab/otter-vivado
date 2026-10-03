# ILA 采样导出与波形数据

本库提供数据接口与生成方法；智能体在消费者工程创建波形页面。本功能没有新增固定网页。
当前提供数字 VCD 导出/读取，可接续 [离线规则分析与智能体取证](ILA_ANALYSIS.md)。
CSV、WDB、实数/字符串 VCD 和完整协议解码不在本能力范围内。没有 Vivado、Windows 或板卡实测记录；命令链经过真实 Tcl 解释器和硬件
命令替身验证，真实工具输出格式仍需实际使用时核对。

## 从共享服务导出

沿用 [硬件调试](HARDWARE_DEBUG.md) 的 inventory、明确 control=ai、select、refresh。
每步等待 operation_id 的最终回执；导出前读取最新快照 revision：

```json
{
  "action": "export_ila",
  "params": {"core": "实际发现的 ILA 核名称", "output_dir": "/consumer/captures/capture-001"},
  "expected_revision": 12,
  "session_id": "board"
}
```

这是 `debug_action` 的新动作，不是新的独立会话。父目录须存在，输出目录必须不存在，
不覆盖旧文件。服务检查控制权、revision、busy、目标和核 UUID；Tcl 再精确选择已经打开的
目标与设备，只接受 IDLE、采样数等于数据深度、配置及状态均为单窗口的完整采集。
上传与导出在一次 execute 中连续完成，用本次 `upload_hw_ila_data` 返回对象调用
`write_hw_ila_data -vcd_file`，不读取之前缓存的对象，不自动 arm、停止或烧录。
即使之前已 upload，本动作也会重新上传当前完整采集，不导出任意旧对象。

目录内容：

- `attempt.json`：操作开始前保存请求、时间和目标。
- `capture.vcd`：Vivado 原始导出，可能在失败后只留下部分内容。
- `manifest.json`：完整成功回执后才创建，包含数据对象、核 UUID、采样数、配置触发位置、
  Vivado 版本、VCD SHA256/大小；数值元数据保留原始字符串。
- `result.json`：最终结果；失败或取消保留 unknown 和错误，不自动重试。写回执失败时
  返回 receipt_error；不能把回执文件缺失当成操作没执行。

`capture_complete=true` 仅表示这次导出通过了上述状态条件。配置触发位置不是已经换算
好的 VCD 时间。`hardware_pairing=unverified`，不能用清单证明加载了正确的 bit/ltx。
原生 GUI、run_tcl 和其它进程不受本服务控制权约束，操作期间仍需协调。默认后端等待
30 秒；大采样导出若超时，核对已有文件和 Vivado 状态，不自动重复。

导出需要 MCP 或同一 `DebugService.submit()`。既有参考页的 HTTP 动作白名单不接受
export_ila 或任意输出路径；消费者实时页面需自己的明确适配层，共享同一服务。

## 离线读取

`read_ila_waveform` MCP 和 `ila-waveform` CLI 复用纯 Python 读取器，无需 Vivado：

```bash
python -m vivado_mcp ila-waveform --file /consumer/captures/capture-001/capture.vcd --catalog
python -m vivado_mcp ila-waveform --file /consumer/captures/capture-001/capture.vcd \
  --signal '!' --start-tick 0 --end-tick 100 --limit 1000
```

MCP 参数：`file_path` 必填；`signal_ids=null` 选全部，`[]` 仅目录，其它列表按 VCD
标识符代码精确选择，不以可能重复的显示名选择。`start_tick` 默认 `"0"`，`end_tick`
可空，二者均为十进制字符串，窗口包含两端。`offset=0`、`limit=1000` 控制事件分页。
下一页保留同一过滤条件，使用 `next_offset`，并传 `expected_sha256=file.sha256`。
首读导出物时也可传入 `manifest.waveform.sha256` 核对来源；manifest 本身是本地记录，
不具备防伪认证。CLI 读取成功返回 0，坏数据/文件或不支持格式返回 1 和 blocked JSON。

返回 schema 为 `otter.waveform.v1`：

| 字段 | 含义 |
| --- | --- |
| `file` | 路径、大小、SHA256；读取期间可检测的文件变化会阻断 |
| `signals` | 声明顺序的 id/scope/name/range/width/type；别名共享 id 和事件 |
| `timescale` | 文件标注的 magnitude/unit，缺失为 null；不代表真实采样周期 |
| `initial_values_before_start` | 开始时刻之前的最后值；null 表示没有证据 |
| `events` | 文件顺序的 `{tick,id,value}`；tick 和全位宽二进制 value 均为字符串 |
| `offset/next_offset/truncated` | 过滤后事件分页位置；同一 tick 的变化不会因分页丢失 |
| `matching_events/last_tick` | 全文件验证后，窗口内匹配事件数和最后时间标记 |
| `trigger_tick/sample_period_seconds` | 均为 null，不能从 VCD 推断 |
| `capture_completeness` | unverified；合法 VCD 可能只是截短但语法完整的文件 |

上限：32 MiB 文件、4096 个声明、4096 位宽、64 层 scope；展开信号目录最多 256 KiB，
所选信号总位宽最多 262144（超限先取目录，再缩小选择）。limit 1~10000，单页
事件的值/标识符约 256 KiB，达到任一上限分页，不抽样。每次查询重新验证整个文件，
适合有限离线采样，不是大波形索引服务器。范围外或页后存在坏数据也会阻断。
支持常见数字声明、scope、timescale、dumpvars/dumpall、标量/向量和 x/z；
实数、字符串、dumpon/dumpoff 或未知指令明确阻断，不静默忽略失去观察的区间。
VCD 无结束校验码，无法证明物理采集完整；更大文件可保留原件在 Vivado 中查看。

## 页面交付

智能体按 [波形生成参考](../skills/otter-vivado/references/waveform.md) 在消费者目录
生成页面、数据和说明。使用合成数据开发页面时显式标 demo；真实数据必须保留原始 VCD、
清单、指纹和读数条件。波形能显示、格式解析通过、实际板卡通过是三个不同结论。
