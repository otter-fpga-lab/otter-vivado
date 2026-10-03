# 为消费者生成本地实验流程

本插件提供运行器和做法，最终页面、动作语义、声音与适配层由智能体生成在消费者工程。
先读 [硬件调试](hardware-debug.md)、[可视化职责](visualization.md)；完整 schema 和
状态语义在源码根 `docs/DEBUG_EXPERIMENT.md`。不要扩建库内固定实验面板。

## 从实验目标生成计划

1. 读取消费者工程和用户目标，定义准备条件、动作、观察窗口、触发与停止条件。区分
   主机“保持静止三秒”与 FPGA 的有限采集深度；长动作通常取关键帧/事件，不能承诺 ILA
   连续记录完整几秒。精确对齐使用设计内事件或帧编号，不靠聊天、网页或 JTAG 延迟。
2. 复用已有 DebugService，核对目标、核 UUID、探针、control、触发配置和 ILA IDLE。
   计划第一步 ready；根据任务安排 countdown、cue、有限 debug 动作和 wait_capture。
   立即采集可能在倒计时结束前完成，按真实事件设置触发，不为套用模板随意 arm。
3. 在消费者目录保存计划，`create_debug_experiment` 指定新目录和最新调试 revision。
   创建尚未执行。每次改计划新建实验；redo 仅重复已固定计划，不覆盖旧轮数据。

## 让本地交互实际接入

- AI 用 MCP start/pause/resume/abort/mark/redo，并读取 `get_debug_experiment`。start 后
  本地运行器计时和执行，不能靠每秒问模型来倒计时。人工确认只能由本地按钮提交，模型
  不能把“我开始了”当成实验者已在现场就绪。
- 消费者适配和 MCP 必须持有同一个 DebugService，别另起 CLI/MCP 进程。Python 适配
  在所有者 asyncio 循环中读取 `service.experiment` 并调用 snapshot()/command()。
  HTTP 工作线程用 loop.call_soon_threadsafe 或 run_coroutine_threadsafe 投递，等待有界
  回执；超时只核对状态，不自动重发。线程不能直接改实验状态。
- 一种集成方式是在消费者 Python 入口中导入 `vivado_mcp.server.mcp`，注册消费者自己的
  本地页面绑定工具，通过该工具 Context 的 lifespan_context.debug_services.get(session)
  获取实际服务，再启动该消费者适配器；MCP 和页面使用同一进程、实例及生命周期。
  这是消费者需实现的接入方式，不是现成页面加载器。无法接入同一实例时先说明限制，
  不交付只有 HTML 却声称可确认/控制的页面。不要在活动构建中替换已有 MCP 进程。
- 接口应只暴露当前实验 id、固定动作和受约束参数；采用回环地址、同源写入、会话随机
  token、请求大小限制和错误回执。确认提交携带实验 id、step_id 和当前实验 revision，
  然后调用 `command('confirm_ready', {'step_id': ...}, revision, source='manual')`。
  适配器自行核对 id，防止旧页面把动作送给新实验；不要信任客户端自报 source 或输出路径。
- 控制权不随人工按钮自动变化。MCP 创建的实验 owner=ai；本地纯人工集成可用
  `DebugExperiment(...,owner='manual')`。control 交接会中止编排，不能继续旧计划写硬件。
  所有者服务关闭时同时关闭消费者 HTTP/声音资源，不能让旧页操作同名重连会话。

## 页面和提示的准确含义

按消费者任务生成当前步骤、剩余时间、就绪/暂停/恢复/中止/重做控件及动作标记。展示实验
status、pause_requested/abort_requested、硬件 operation 回执与明确错误，不把暂停按钮
的点击即时显示成“FPGA 已停止”。已接受短命令会先完成；运行器不会取消上传或回滚 VIO。

关闭浏览器不等于 abort；交付时说明本地运行器仍可能执行后续已声明步骤。
倒计时显示服务 remaining_ms；浏览器可插值显示，但不能自己推进硬件步骤。失去连接时
停止本地插值并显示最后更新时间，不编造继续倒数的真实状态。前后台切换后重新读快照。

cue 按 (experiment_id, seq) 去重。先用用户点击启用音频，消费者可用 Web Audio 发短音，
播放失败提供视觉提示。刷新页面不重放历史声音；网络恢复后过期提示不能补播成“现在做”。
事件 sound_played=false 仅表示运行器未确认播放，不能据此宣称人已经听到并动作。
可用 mark 记录本地实际按钮/动作/播放反馈；frame_id 只作声明，不能冒充与 FPGA 已对齐。

## 暂停、中止、重做与证据

- 暂停保留本地倒计时，已完成步骤不重放；等待 FPGA 的 timeout 按真实经过时间计，
  暂停不会延长采集窗口。没有单独 ILA stop，暂停/中止后采集可能仍等待触发。
- 中止后核对硬件与 operation_id，保留原始记录。unknown 不自动重试/redo，不把新一轮
  成功覆盖成上一轮成功；核对现场后才能新建实验。
- redo 要求上轮明确终止、之后显式 refresh 且相关 ILA 明确 IDLE，并传最新调试版本。
  它创建新 attempt 回到 created，需要 start 和新一轮人工就绪；不得绕过确认自动重跑。
- 保存 experiment.json、逐事件 JSON、每轮 VCD/manifest；引用 source、轮次、输入与计划
  指纹、实际步骤及失败位置。进程崩溃后只审阅已有记录，不自动续跑未完成设备动作。
- 采集完成后按 [分析流程](analysis.md) 比较假设；实验 completed 只是步骤结束，不能
  等同有效采样或通过验证。计划修改应解释哪条证据促成改变。

验证消费者页面的完整流程、音频失败、重复点击、过期 revision、暂停后恢复、硬件回执
未返回时中止、网络断开、刷新后不补播、旧页/新实验隔离、键盘和窄屏。合成数据标 demo；
实际 FPGA/声音/浏览器测试证据分别记录，交付文件路径和启动/接入方法。
