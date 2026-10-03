# ILA/VIO 与工程调试面板

板上调试只操作已有 Hardware Manager 打开的明确 target/device，不会打开 JTAG target
或烧录。工程准备另有结构化入口。使用实际 MCP schema；完整用法与 JSON 样例在源码根
`docs/HARDWARE_DEBUG.md`、`docs/DEBUG_DESIGN.md`、`examples/debug/`。

1. 复用现有 Vivado 会话，通过 `get_debug_snapshot`/`debug_action` 使用共享服务。
   需要参考页时可用 `open_debug_panel`；生成消费者页面先读 [可视化参考](visualization.md)。
   另一个 CLI/MCP 进程的控制权不会自动同步。独立参考入口为
   `python -m vivado_mcp debug --port 9999`。
2. `get_debug_snapshot` 只读缓存。通过 `debug_action` 的 `inventory` 获取完整名称，
   `control {owner: ai}` 明确接手，再用 `select {target, device}` 选择目标。
   每次使用最新 `revision`，等待对应操作回执结束后再继续；不默认选列表第一项。
3. 已授权范围内，`write_vio` 写一个实际输出；`configure_ila` 设置一个探针比较值与
   可选触发位置；`arm_ila` 开始等待触发或立即采集；`upload_ila` 只上传完整单窗口。
   首批没有独立停止接口、浏览器波形或数据导出；在原生 Vivado 查看上传数据。
4. `unknown` 表示结果可能已经部分生效，不自动重试。先检查设备并显式 `refresh`；
   VIO 读回匹配也不证明业务逻辑已经采用参数，仍需设计提供帧标记或应答。
5. 人接手后 AI 停止写入。此控制权只协调本服务；原生 GUI、`run_tcl`、其它进程
   不受此锁约束，使用这些入口时明确协调，不承诺全局设备锁。

需要复用现有参考页时，可读取实际探针及项目语义，生成仅含 `title` 与 `controls` 的 JSON。
这只是参考页的配置格式，不限制消费者自己生成的页面布局。
首批控件为非负整数 `slider` 或 0/1 `toggle`，绑定精确 `core`、`probe`。
滑杆需 `min/max/step`，范围上限 65535，实际写入同时受硬件宽度限制；未知宽度、输入
探针、无效绑定不可写。不猜定点格式、单位或参数生效时刻。描述放消费者工程，工具库
只维护通用控件与操作实现。`open_debug_panel(panel_path=...)` 可加载该描述。

工程准备先用 `plan_debug_design` 选择 ila_ip/vio_ip/ila_netlist/mark_debug；描述、
例化模板与生成约束留在消费者工程。`inspect_debug_design` 返回 ready 后，使用其完整
project 身份调用 `prepare_debug_design`。只创建 IP 或注册约束，不会改业务 RTL或启动
构建；constraints_added 不代表已插核，created 不代表已接入 RTL。缺失/重名 net 会使
加载约束的实际 run 失败；partial 或未知时先核对现场，不重放。再用已有构建、报告入口
验证连接、时序、资源与配套 bit/ltx，不能用准备成功代替这些证据。
检查点导出接续读 [debug-delivery.md](debug-delivery.md)。人工可用
`debug-prepare --spec ...` 离线规划，`--output-dir` 保存新文件，`--apply` 准备已有 GUI。

专用界面的布局与风格由生成智能体按用户偏好决定；库负责可用控件、绑定、操作状态和
基本可用性。当前 JSON 只支持已有 slider/toggle，不能承诺任意自定义 HTML 都可直接加载。

需要采样导出分析、倒计时或声音提示时，应明确这些仍是后续工作，不把现有
ILA workflow prompt 或合成演示当成已实现的功能。精确动态配合最终需要硬件触发/帧标记；
网页与 JTAG 不提供周期精确的时序。业务 RTL 改造沿用 Coding，处理器软件调试沿用 Vitis。


构建后可用 `check_debug_artifacts(bit_path, ltx_path, expected)`，或
`python -m vivado_mcp debug-artifacts --bit ... --ltx ... --expected ...`。
预期描述使用实际层级实例名与探针名，不从 IP 模块名猜实例；格式见
`docs/DEBUG_ARTIFACTS.md`。`blocked` 表示明确不一致/解析失败，`incomplete` 表示缺少
离线核对证据，`consistent` 仅表示所提供的离线检查一致。`pairing` 始终为 `unverified`；
不得把 LTX UUID、同名/同目录文件或 SHA256 当作 .bit 内核身份或板卡 PASS。
