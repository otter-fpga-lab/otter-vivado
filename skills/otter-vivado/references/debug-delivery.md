# 从调试工程到可核对交付包

复用现有构建工具，导出不依赖固定网页。完整参数和回执说明见源码根
`docs/DEBUG_BUNDLE.md`。未知信息先从工程与当前会话发现，不选“最新文件”代替来源核对。

1. 按工程准备参考完成 IP/约束和业务 RTL 连接。读取实际工程、part、run、Vivado
   版本和消费者源码提交；创建 IP 或注册约束不表示连接通过。
2. 使用已有 `run_synthesis`、`run_implementation` 和进度/报告入口完成目标阶段。
   复用原项目会话；已有 run 活动时继续观察，不重新 reset/launch。失败先看原始日志。
3. 从对应实现 run 的目录确认唯一且明确的 routed DCP。核对 run 是否过期、源/约束
   是否变化；记录消费者源码提交和原构建报告。不能自动取目录中时间最新的检查点。
4. 使用相同实际 Vivado 安装建立专用空会话（独立 GUI 用 `port=0`，或独立 Tcl
   session），保留原 GUI。不要把原工程的 session 传给导出以图自动关闭工程。
5. 调用 `export_debug_bundle(checkpoint_path, output_dir, expected_part, ...)`。
   `output_dir` 必须是父目录已存在的新目录，`expected_part` 是完整 part。
   `source_revision` 是调用方声明，不是工具已验证的源码证明。
6. `exported` 之后，用 `check_debug_artifacts(..., expected, manifest_path)` 核对
   消费者独立指定的核/探针要求和全包指纹；审阅导出的时序、资源和 DRC 报告。
   `record_matches` 仅确认本地来源记录一致；硬件配对仍为 unverified。
7. 保存交付包和构建来源到消费者工程，再按已授权的设备流程验证实际板卡、探针、
   读回和采集。需要可视化时读 [visualization.md](visualization.md)，由智能体生成页面。

`blocked` 表示导出前置不满足；`partial` 表示可能已有检查点/产物；`unknown` 表示命令
回执无法确认，Vivado 可能仍在执行。保留目录与 attempt/result 记录，检查原专用会话，
不要清目录重跑或结束仍在执行的会话。完整回执确认后需要再导出时使用新目录。
导出不会自动结束专用会话；后续正常清理由调用方在确认空闲后执行。
