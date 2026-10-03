---
name: otter-vivado
description: 使用 Otter Vivado 的真实会话、综合/实现进度、日志与报告；人和智能体共用本机运行面板。适用于 AMD Vivado 工程，不负责其他厂商工具或自动操作板卡。
---

# Otter Vivado

本 Skill 与 MCP 来自同一份仓库源码。接入入口为 `python -m vivado_mcp connect --client codex`（Claude Code 使用 `--client claude-code`）；先在这份源码目录使用同一 Python 执行 `python -m pip install -e .`。接入只建立 Skill 目录引用并配置 MCP，不复制业务库，不注入 Vivado 安装，不启动或重启 Vivado。

## 按任务选工具

- 先用 `list_sessions` 查看会话。需要工作时使用 `start_session`；先读其参数，核对工程路径、Vivado 版本与现有会话。已有可用会话直接复用。
- 发起已授权的综合：`run_synthesis(wait=False)`；实现：`run_implementation(wait=False)`。沿用工程实际 run 名称及 session ID，不因查询而重新 launch/reset。
- 查看运行阶段、Vivado 实报百分比与日志：`get_run_progress`。没有可靠百分比时说明未知；不按耗时推算，不把失败或仅启动成功写成完成。
- 用 `open_run_monitor` 打开/取得本机会话的面板 URL；人通过浏览器读取真实状态、日志和可用报告。它不隐式启动综合或实现。
- 用 `get_run_snapshot` 取得与面板共用的缓存 JSON。核对采样时间、状态和查询错误；缓存陈旧或会话不可用时如实说明，不包装成实时成功。
- 需要完整诊断时按 MCP 当前工具列表选择时序、资源或警告报告工具；报告对应实际工程/run/产物。没有 Vivado 或报告文件时说明实际缺口，保留可执行的现场命令与验证边界。

所有工具以当前 MCP 返回的 schema 为准。不要猜造工程状态、工具结果或新增参数。Skill 不附带第二份后端，CLI/MCP/UI 共用本仓实现。

## 持续运行与边界

普通源码和 Skill 修改直接维护本仓，不复制快照，不要求运行同步器。新的调用在实际加载边界使用新版；已加载的 Python 模块和模型上下文不承诺热更新。长任务期间只查询；不要为更新、刷新面板或读取状态停止、重启或替换其会话。重新加载 MCP 留到工作完成的空闲边界。

连接既有 GUI 所需的 TCP 服务按 README 单独配置；不得把一次源码接入当作修改 Vivado 安装的授权。写入板卡、烧录器或生产设备需要用户明确授权，不由查看进度自动触发。

Ross 与本产品并列可选，按当前任务价值选择，不要求互相调用或替换。本仓保留原作者、许可证、fork 和上游贡献关系。RTL Library 只读，Coding 的用户/哈基米现场测试不由此 Skill 接管。
