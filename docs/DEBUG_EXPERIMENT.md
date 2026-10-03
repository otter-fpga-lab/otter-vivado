# 本地实验编排

本库提供 `DebugExperiment` 运行器和 3 个 MCP 入口；智能体在消费者工程生成控制页面、
适配层和实验计划。没有新增固定实验网页或独立占用 Vivado 的进程。就绪、倒计时、提示、
动作标记、暂停/恢复/中止和重做在本机运行，后续步骤不等待模型逐条回复。

计时依据是主机单调时钟，不是 FPGA 时钟。操作系统调度、EDA 查询、浏览器渲染和音频
都存在延迟。cue 只是提示事件；声音由消费者播放，运行器从不声称声音已播放或人已行动。
毫秒倒计时不代表可以用 ILA 连续记录几秒钟，高速动态对齐仍需硬件触发/帧编号。

## 建立实验

先复用已有会话/DebugService，明确 control、target/device，成功刷新。创建时记录设备、
核 UUID 和探针结构，相关 ILA 必须为 IDLE；运行前及硬件步骤执行时均核对身份。
`create_debug_experiment(spec,output_dir,expected_revision,session_id)` 使用 control=ai。
Python 消费者可用 `DebugExperiment(service,...,owner="manual")` 复用人工控制的服务。
expected_revision 来自 `get_debug_snapshot`；output_dir 父目录须存在，目录必须不存在。

以下是格式示例；核名称必须替换成实际发现值，时长和时序由消费者任务决定：

```json
{
  "title": "动作响应观察",
  "steps": [
    {"id": "ready", "kind": "ready", "label": "确认装置和观察目标已就绪"},
    {"id": "arm", "kind": "debug", "action": "arm_ila", "params": {"core": "实际ILA名称"}},
    {"id": "count", "kind": "countdown", "label": "保持静止", "duration_ms": 3000},
    {"id": "move", "kind": "cue", "label": "开始移动目标", "tone": "beep"},
    {"id": "wait", "kind": "wait_capture", "core": "实际ILA名称", "timeout_ms": 30000},
    {"id": "save", "kind": "debug", "action": "export_ila", "params": {"core": "实际ILA名称"}}
  ]
}
```

此例只说明编排顺序：需先配置真实事件触发。立即采集可能在三秒倒计时结束前早已完成，
不能用这种计划宣称记录了动作。计划本身不含任意 Tcl、Python、烧录或设备选择指令。

| kind | 除 id/kind 外的字段与行为 |
| --- | --- |
| ready | label；等待消费者本地人工确认，首步必须为 ready，可在后续再次确认 |
| countdown | label、duration_ms；单调计时，暂停保留剩余时间 |
| cue | label、tone（none/beep）；发布一次事件，由消费者显示/播放 |
| debug | action、params；仅 configure_ila、write_vio、arm_ila、export_ila |
| wait_capture | core、timeout_ms；轮询完整采集状态，不调用停止或重试 arm |

步骤 id 唯一，最多 64 步；每段计时 1~600000ms，计划最多 64 KiB。debug 参数沿用
[共享调试入口](HARDWARE_DEBUG.md)，但 export_ila 不接收 output_dir；运行器自动选择
本轮 `capture-NNN` 新目录。计划创建后不可修改；改计划须新建实验，redo 保留同一计划指纹。

## 控制与回执

`get_debug_experiment` 返回当前实验 id、revision、status、attempt、step、remaining_ms、
请求状态和最近 100 个事件；读取不查询 EDA，倒计时刷新不改变 revision。
`debug_experiment_action(experiment_id,action,params,expected_revision,session_id)` 中的
revision 是**实验版本**，与硬件快照版本分开。id 不符、旧版本或重复启动直接拒绝。

| action | params | 含义 |
| --- | --- | --- |
| start | {} | 启动本地任务，进入首个 ready；返回后本地继续运行 |
| pause | {} | 请求暂停；已接受硬件短操作先完成回执，再进入 paused |
| resume | {} | 仅 paused 可恢复，不重放完成步骤，也不重启 FPGA 采集 |
| abort | {} | 中止后续编排，等待已接受短操作；不回滚 VIO、不停止 FPGA |
| mark | {label,frame_id?} | 保存主机动作标记；frame_id 是人工声明，frame_alignment=unverified |
| redo | {expected_debug_revision} | 上轮明确终止、之后显式 refresh 且相关 ILA 为 IDLE 时，建立新轮 |

人工确认由同一事件循环中的 Python 入口完成：

```python
experiment.command("confirm_ready", {"step_id": current_step_id},
                   current_experiment_revision, source="manual")
```

MCP 不提供代替人工确认的动作；start 不代表人已准备好。消费者的本地按钮才提交该事件。
实验 owner 是硬件步骤采用的共享控制来源，本地人工按下就绪/暂停按钮不会偷偷改变它。
有活动实验（包括 created/paused）时，普通 debug_action 的写入和 select 被拒绝；
inventory/refresh 仍可用。显式变更 control 会中止编排，不能交回人工后继续设备写入。
服务锁不覆盖原生 Vivado GUI、run_tcl 或其它进程，仍需协调这些入口。

状态包括 created、waiting_ready、running、paused，以及 completed/aborted/failed/unknown。
failed 表示本地流程不能继续，不表示此前硬件动作未发生。unknown 保存不确定结果，不能
自动重做或重试；先看共享操作回执、设备与已有文件，核对后再新建计划。completed 只表示
声明步骤结束，不等于采集有效、声音已播放、实验假设成立或硬件测试通过。

wait_capture 使用实际经过的主机时间作为期限，暂停不会延长 FPGA 的采样窗口；恢复时若
超时即失败，返回过晚的查询也不进入下一步。暂停/中止无法取消正在执行的硬件短命令。
关闭调试服务会唤醒本地等待并中止后续步骤，保留已接受命令的回执，不关闭 Vivado。
仅关闭浏览器不会中止本地运行器；需要停止后续步骤时应显式 abort。

## 保存与重做

消费者目录包含 experiment.json（计划、指纹、owner、来源、设备初始快照），以及每轮
独立的 attempt-0001 等目录。逐事件 `event-NNNNNN.json` 独占创建，记录 seq、实验版本、
轮次、步骤、UTC、单调时钟相对 elapsed_ns、动作来源、硬件意图/operation_id/最终结果。
采样导出也在本轮目录。主机时间与 VCD tick 不自动对齐；声音事件 sound_played=false。

快照只保留最近 100 个事件，完整事件从文件读；最多 32 轮/4096 事件，超限停止后续动作。
日志写入失败会进入 unknown；如果设备动作已经接受，仍等待共享回执，不重复执行。
进程崩溃/断电可能留下不完整文件或没有 finished 事件；不提供崩溃后自动续跑或设备重放。
读取旧记录用于审阅，不能凭一个倒计时值恢复实验。

redo 必须在本轮结束后通过普通 debug_action(refresh) 明确核对设备，传最新调试版本。
旧 attempt 永远保留；相关 ILA 仍等待触发时拒绝重做，交回原生工具处理后再刷新。
unknown 不支持 redo；不能用“重做”掩盖一次结果不明的写入。

## 消费者集成与验证

见 [实验生成 Skill 参考](../skills/otter-vivado/references/experiment.md)。本库没有提供
任意网页加载或独立实验 HTTP 服务。消费者适配必须绑定同一个 DebugService 实例，所有
实验 command/snapshot 在所属 asyncio 循环执行；不要额外启动 MCP/CLI 去争抢会话。

已验证 Linux 上的 asyncio 状态机、共享服务和 MCP 入口，真实 Tcl 使用 EDA 命令替身。
尚无真实设备、Windows、声音设备或消费者浏览器验收。消费者页面交付时还需核对提示去重、
人工确认、键盘/窄屏、音频失败、断连和日志目录；不要把测试样本当作现场实验。
