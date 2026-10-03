# Otter Vivado：真实运行进度与报告查看

Status: OPEN
Target: otter-fpga-lab/otter-vivado
Basis: Hub main 0e9c375e；topics/20261002_vivado-progress-and-ross/TASK.md
Next: 按四客户端原地接入入口核对本机链接发现与空闲重载；继续在 Draft PR #1 接续，不触发活动 Vivado 重启
Result: 首轮真实观察流程已交付；本轮增加四客户端同源链接接入与 Ross 参考任务路由，接入定向 57 passed

## 接手与边界

2026-10-03 核对正式 origin 为 https://github.com/otter-fpga-lab/otter-vivado.git；
工作树初始干净，main=60b13cf03397e6d58c7e16852277b678ab8a4d9d，无本仓已有 PR。
旧贡献分支 feat/multi-version-optimization=b40e62cd33f8c37e9627f0961926577edb1b2f9d
保留不动，fork parent 为 mapleleavessssssss-wq/vivado-mcp。原作者、Apache-2.0
许可和上游贡献关系保持不变；本轮源码沿用 main，不复制旧 PR 尚未复核功能。

已读 README、CONTRIBUTING、pyproject、flow/report tools、run_progress_parser、
Tcl 查询和会话管理/传输实现。main 无 AGENTS/TASK 或 project_tools.py；新增本 TASK。
main 已有 wait=False、get_run_progress、报告解析器；缺本机持续展示，且失败强制
100% 与任意 Complete 提前终止是本轮需修的真实缺口。

只改本产品和对应 Hub 议题。Ross 与本产品并列可选；不改旧贡献分支，不触碰
Coding/Library，不操作板卡、不自动合并/发布/改变可见性。

## 已完成工作块（2026-10-03）

- 复用现有会话和 QUERY_RUN_PROGRESS，5 秒后台采样；新增 open_run_monitor /
  get_run_snapshot 和 monitor CLI，浏览器只读同一缓存。忙/超时/失联保留最后时间，
  不自动重连、reset、cancel、relaunch；关闭面板不关闭 Vivado。
- 按 synth_design / route_design / write_bitstream 目标判断完成；不再合成初始 0%
  或失败 100%。取消、阶段完成、未知值分开；慢观察查询不会中断原构建等待。
- runme.log 读取限末尾 64 KiB；已有报告限该目录前 16 份、每份前 256 KiB。
  新鲜度、Design 不匹配和阶段线索可追溯，复用 timing/utilization parser；不混入
  当前其他 design，未知来源不升级全局 PASS，不把运行完成等同签核。
- 修复只读 attach 退出误删用户 PID 标记。展示 HTTP 只绑定 loopback、随机路径，
  Host/Origin 受限，无执行/写入接口，页面文本不执行日志/报告中的 HTML。
- connect 一次登记源码 MCP + Skill 目录链接，复用已有配置备份实现，不复制业务库，
  不注入/重启 Vivado；已有冲突入口拒绝覆盖。保持原作者/包 metadata/许可不变。
- README 提供入口；docs/RUN_MONITOR.md 提供可运行 workflow、官方/Ross 对照和现场步骤；
  examples/progress 有独立消费工程源码及明确标注的固定合成回放。

## 后端决定

采用现有 main 会话/解析器 + 薄显示层。Ross 2026.9.1 / 2cdc9eef 文档提供
status/log/history 与独立监控通道，实测声明为 2026.1；其 skills 插件与 MCP 二进制
独立分发、客户端可能缓存，不代表旧版 Vivado/Windows display 已实测。本产品与 Ross
并列可选，不建立依赖、不宣布替代，不分发其二进制。官方 UG835/UG893 2026.1
确认中间 step 可完成、run 完成仍可能时序失败，具体链接留在使用文档。

上游 PR #5 读取时仍 OPEN、head b40e62cd。此次只读借鉴其 review 的阶段/报告来源
约束，没有修改旧分支/PR 或联系维护者。

## 验证与下一动作

实际环境：Linux / Python 3.12.14 / MCP SDK 2.3.0；安装 editable 开发依赖，
有 tclsh 和 Chromium，没有运行 Vivado、Ross 或板卡。

定向命令：

```bash
PYTHONPATH=.venv/lib/python3.12/site-packages:src python -m pytest \
  tests/test_run_monitor.py tests/test_monitor_http.py tests/test_flow_progress.py \
  tests/analysis/test_run_progress_parser.py tests/test_report_tools.py \
  tests/test_diagnostic_tools.py tests/test_protocol_regression.py tests/test_session.py \
  tests/test_probe_then_attach.py tests/test_connect.py tests/test_doctor.py tests/test_prompts.py -q
.venv/bin/ruff check src/ tests/
git diff --check
.venv/bin/python -m pip wheel --no-deps . -w <temporary-wheel-directory>
```

- 定向 pytest：315 passed / 6 skipped；保留平台条件跳过，无全产品/厂商矩阵扩测。
  覆盖运行/成功/失败/取消/阶段未达目标、空进度、Tcl错误、busy/失联、迟到响应不串台、
  日志读取边界、陈旧/不匹配/未知报告、接入冲突和同源幂等。
- 同批包含真实 Chromium 桌面 1440 / 手机 390 宽渲染、XSS 文本、展示失联保留快照；
  无页面/控制台错误。Python 标准库 HTTP 边界和只读行为通过。模拟 EDA 输入不算 EDA PASS。
- ruff 全部源码/测试通过；diff 无空白错误。wheel 构建成功并确认包含 HTML、CLI、
  MCP monitor 模块及原 Tcl server 资源；未发布包。
- CLI 固定回放实际可运行：route Complete + write_bitstream 目标显示 stage_complete，
  原生 100% 保留但不冒充目标完成；失败样本保持 37.5%。

未测/限制：实际 Vivado 2019.1 或用户当前版本的 GUI/Tcl/attach，Windows 文件编码、
状态属性/日志/报告命名与性能，Windows symlink/junction 权限及客户端真实 Skill 发现，
Ross 二进制、完整签核/bitstream/硬件。精确现场流程见 docs/RUN_MONITOR.md 最后一节，
不要求抢占 Coding 本机测试。展示质量卡当前保持 unknown，报告仅带来源供人工核查。

## GitHub 恢复点

分支：`feat/progress-view-study`，base `main` @ 60b13cf。
已保存远端领取提交：`ced7425d770c7ff6647dbee84938d527ebcb8b5c`。
完整实现已推送：`14ce0d9c8b5fe5cad42b15bd5865b94f7b2fe8f8`。
Draft PR：[otter-vivado #1](https://github.com/otter-fpga-lab/otter-vivado/pull/1)，
base `main`，head `feat/progress-view-study`；未合并/发布。接续时 fetch 此分支最新 HEAD，
不要回退到上述实现锚点。对应 Hub 仅回填 Vivado 议题，不改系列总表。

Git HTTPS push 在本环境返回 401；
使用现有授权的 GitHub Git Data API 上传同一 blob/tree/commit 并逐项比对 SHA、
仅 fast-forward 更新本分支，不改全局凭据或旧贡献分支。

## 四客户端原地接入优化（2026-10-03，按用户后续偏好）

用户确认产品作为完整插件能力使用，偏好 Cursor / Claude Code / Codex / Antigravity
通过符号链接读取同一源码。本批继续 `feat/progress-view-study` / Draft PR #1，
开始时远端和工作树均在 `1eb8d62` 且干净，不另建产品或重写运行后端。

已完成：

- `connect --client all` 或选择多个客户端，一次预检/接入；`--check` 完全只读。
  Cursor 默认与 Codex 共用 `.agents/skills/otter-vivado`，保留已有 Cursor 原生入口；
  Claude 用原生 Skill 目录，Antigravity 使用官方当前配置目录并识别既有旧布局。
- `--skills-only`、单客户端 `--skills-dir/--config` 覆盖支持用户既有布局；Windows
  auto 首选 symlink，仅缺少 symlink 特权时回退 junction，不复制 Skill/参考文件。
- 已有配置 symlink 保留，多个客户端链接同一 JSON 时只写一次真实目标；其他服务器、
  现有停用标记和搜索路径覆盖保留。冲突先报明，不覆盖目录、异源入口或同期配置变化。
  `needs_attention` 区分已停用/导入路径覆盖，不能据此声称客户端已经可调用。
- Ross main `2cdc9eef` 的按任务发现输入、按需读 references、用版本/消息签名查官方资料、
  参数读回与证据复测已落实为本仓 Skill 路由。Ross release `2026.9.1` 实际指向
  `c89c3328`，与 main 区分。未复制其二进制或引入运行依赖。
- 普通用户级 Skill 链接 + editable MCP 是本轮正式接入方式。Ross 文档所述原生插件
  缓存/链接限制使“四客户端插件缓存原地热更新”不可承诺；源内容只改一处，活动会话
  不自动重启，MCP 模块在正常空闲加载边界更新。

验证：`.venv/bin/pytest tests/test_connect.py tests/test_doctor.py -q` 为 **57 passed**；
覆盖实际临时目录链接、一次修改 Skill 与 references 从所有入口读到新内容、四客户端
CLI 预检/写入/幂等复查、共享 JSON 链接、已有路径、配置冲突、同期修改、停用状态，
以及 Windows junction 参数/权限分支模拟。修改文件 Ruff 与 diff 检查通过；Skill
引用的工具名与真实源码核对，Markdown 本地引用校验通过。没有重跑 EDA/全库矩阵。

未测：Windows 真实 junction/中文空格路径、四宿主实际 Skill 发现和缓存加载、Cursor
跨原生/兼容目录同名入口显示、Antigravity 旧 MCP 路径在用户实际版本上的加载。
这些保留为现场定向检查；没有本机安装/改动用户客户端，没有商业 EDA/Ross 执行。
