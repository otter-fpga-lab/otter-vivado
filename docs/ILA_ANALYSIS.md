# 基于采样证据的分析

`analyze_ila_capture` MCP 与 `ila-analyze` CLI 共用离线实现：对数字 VCD 做统计，并检查
消费者明确声明的规则。智能体用这些证据解释假设、选择下一轮采样条件，按已有授权通过
共享调试服务执行。分析器本身只读文件，不连板、不写 VIO、不设置触发，不生成固定页面。

这是有限规则检查和智能体工作流，不是全协议解码器或自动根因识别器。VCD/状态机规则
不能证明 bit/ltx 配对、实际采样完整性、物理毛刺、时序签核或全部协议正确。没有真实
Vivado/Windows/板卡验收记录；测试输入为合成 VCD 和已有导出命令替身。

## 输入约定

先用 [read_ila_waveform](ILA_WAVEFORM.md) 取得真实信号目录，选 VCD 标识符代码，
结合消费者 RTL/设计文档确定编码、复位及握手语义。下面只是格式示例；c/v/r/d/s/rst
必须替换成该文件的标识符，示例状态编码不是对用户设计的假设：

```json
{
  "signals": ["c", "v", "r", "d", "s", "rst"],
  "sample_clock": {"id": "c", "edge": "rising", "values": "after_tick"},
  "reset": {"id": "rst", "active": "1"},
  "checks": [
    {"name": "known_states", "kind": "allowed_values", "signal": "s",
     "values": ["00", "01", "10"]},
    {"name": "state_path", "kind": "state_transitions", "signal": "s",
     "allowed_pairs": [["00", "00"], ["00", "01"], ["01", "01"], ["01", "10"], ["10", "00"]]},
    {"name": "payload_hold", "kind": "stable_while_stalled",
     "valid": "v", "ready": "r", "data": ["d"]}
  ]
}
```

`signals` 必填，其它顶层字段可省略。只要有 checks 或 reset 就必须有 sample_clock；
没有 checks 时只给统计，status=observed。所有引用的 id 必须列在 signals 中，别名按
同一个 id 处理。未知字段、重复名称、不匹配位宽、未知值通配符都会阻断。
CLI 对 JSON 重复字段也阻断；MCP 入口接收已经解码的 JSON 对象。

- `edge` 为 rising/falling；`values` 必须明确 before_tick/after_tick，分别取该时间戳
  所有选中信号更新之前/之后的值。不采用文件行顺序推测 HDL 调度或采样相位。
- 同一 tick 的时钟多次变化，或时钟经过 x/z，均记为歧义并清除相邻样本历史；
  初始已知时钟赋值不是边沿。窗口内时钟缺失的正时长也会使未发现违规的结论降级。
- reset 必须一位，active 是字符串 0/1。按同样取值时机观察到有效复位时跳过检查、
  清除前样本；复位未知记为证据不足。这里是声明的同步检查门控，不推断异步复位语义。
- ILA 导出未必包含能观察到边沿的时钟探针。时钟恒定/缺失时不要把 VCD 时间戳当成
  时钟边沿。可先做统计，或按消费者实际采样记录写专用分析；当前工具不隐式合成时钟。

## 规则的准确含义

| kind | 检查内容 |
| --- | --- |
| allowed_values | 每个有效采样点的 signal 必须属于声明的完整二进制值集合 |
| state_transitions | 窗口内相邻有效采样点的前态/后态必须在 allowed_pairs 中；自环也需显式列出 |
| stable_while_stalled | 前点 valid=1 且 ready=0，则下一点 valid 必须保持 1，且 data 列表中的值不变；下一点 ready=1 也不能撤销这个义务 |

状态值必须与信号位宽完全相同，只接受 0/1，不把 x 当通配符。首个采样点没有前态，
不能检查转移；复位/时钟歧义后也不跨越间隙构造转移。未知状态不会被转成零。
握手规则仅检查已观察到的相邻点稳定性，不保证最终完成，不解码 AXI 包、ID、burst 等。
结尾仍在等待时返回 pending_at_end=true 和补充上下文建议，不能宣称无死锁。

调用：

```bash
python -m vivado_mcp ila-analyze --file /consumer/captures/capture-001/capture.vcd \
  --spec /consumer/analysis/spec.json --start-tick 0 --end-tick 1000
```

对应 MCP 参数为 file_path、spec、start_tick（默认字符串 "0"）、可选 end_tick 和
expected_sha256。窗口包含两端，所有 tick 均用字符串。已有导出清单时传入其中
waveform.sha256，使用 [导出流程](ILA_WAVEFORM.md) 的原始 VCD，不用网页抽点数据分析。

## 输出与证据

schema=`otter.ila-analysis.v1`，主要字段：

- `file`：路径、大小、SHA256；`spec/spec_sha256`：实际规则及排序紧凑 JSON 的指纹。
- `window`：请求窗口、实际文件覆盖的终点、是否覆盖请求、所选事件数。尾部不会外推。
- `statistics`：逐信号赋值数、变化数、0→1/1→0 边沿数、首末变化 tick、未知赋值次数、
  已知/未知保持时长、初始值/终值、已知值的无符号最小最大值。时长是 tick 字符串，
  最大最小值为十进制字符串；包括零时长赋值，不是均值、Hz 或采样次数。
- `sampling`：观察到的有效选定边沿数、时钟歧义、时钟未知时长、有效及未知复位次数。
- `checks`：各规则 evaluated、violations、unknown、no_predecessor、inactive、status；
  evidence 包含违规 tick、原始二进制值及相邻点证据。只保存有限例子，违规总数仍完整。
- `next_capture_suggestions`：关联规则、VCD id、证据 tick 和取证意图；executable=false。
  它不是 Vivado 命令，不将 VCD id 当成硬件 probe 名。具体触发由智能体核对映射后决定。

| status | 结论范围 | CLI 退出码 |
| --- | --- | --- |
| observed | 仅描述数据，没有提供规则 | 0 |
| consistent | 窗口内适用的规则检查一致，存在有效检查且无未解决证据缺口 | 0 |
| inconclusive | 无有效检查，或存在未知值/时钟歧义/未知复位/请求范围超出文件等缺口 | 2 |
| violated | 至少一个确定违反声明规则的实例；其它部分仍可能未知 | 3 |
| blocked | 文件、描述或格式错误，或超出资源限制；不返回半份通过结论 | 1 |

先看每条规则再解释总状态。首个转移的 no_predecessor、握手不适用的 inactive 和有效
复位跳过不自动构成违规；若整条规则没有任何适用检查则 inconclusive，避免空检验通过。
consistent 也只限声明和观察到的相邻点；pending_at_end 必须单独说明。源文件的
capture_completeness/hardware_pairing 始终保留 unverified。

文件只读取并验证一次，复用波形解析器，不拼接分页或用首屏推断全部。保留 32 MiB
文件上限；最多 64 个信号、合计 16384 位、16 条规则、每条最多 256 个值/状态对、
64 KiB 描述、窗口内 200000 个所选事件。每条最多 8 个违规例子，全局证据约 256 KiB，
evidence_truncated 明示例子截断；统计和违规计数不会截断。超预算请缩小窗口或信号。
分析范围外的损坏 VCD 也会阻断，无法由合法 VCD 的末尾证明实际捕获完整。

## 智能体如何闭环

按照 [分析 Skill 参考](../skills/otter-vivado/references/analysis.md) 保存假设、输入映射、
规则、结果和下一轮取证计划。在消费者目录输出报告及需要的可视化；用既有 MCP 共享
服务做已授权操作。每次采集用新目录，变更的触发条件及规则要可追溯。实际业务诊断由
智能体结合工程语义完成，工具提供可核对的统计和反例。
