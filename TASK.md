# Otter Vivado：人机共用 ILA/VIO 调试

Status: OPEN
Target: otter-fpga-lab/otter-vivado
Basis: 用户于 2026-10-03 确认板上调试、可视化和产品边界，并授权逐步实施；从 main 3750590 接续
Next: 在本机核对首批 Hardware Manager 路径；继续自动插核/工程准备，再推进采样证据与本地人机配合实验
Result: 首批共享调试服务、MCP/CLI、本地面板和消费者控件描述已实现；商业 Vivado/Windows/板卡未测

## 当前工作：ILA/VIO 首批（2026-10-03）

用户确认完整方向：自动规划/插入 ILA 与 VIO、配置并生成调试产物、板上控制与采集、
AI 分析和人工分步接手；本地页面组织倒计时/动作提示，避免动态实验依赖聊天往返；
AI 可以利用库内控件与操作接口，为消费者工程生成专用调试面板。

边界：本仓维护 Vivado 工具操作、通用调试控件和流程。项目的参数含义、范围、面板配置
与实验方案留在消费者工程；业务 RTL 修改沿用 Coding，处理器软件调试归 Vitis，
组件选取/导出归 Studio，项目组织和领域方法归 Engineering Guide。共用可视化不代表
产品合并。本轮没有改其他产品源码、上游或旧贡献分支，也没有操作真实设备。

### 首批已完成

- `HardwareDebugBackend` 精确枚举已连接/已打开 target/device、ILA/VIO 与探针，
  复用同一个 BaseSession.execute。没有隐式连接、选择第一台设备、烧录或修改 current_*。
- VIO 单个输出写入、宽度/范围核对、实际读回，并保存/恢复其他 GUI 暂存值。读回
  不匹配记为 unknown；读回正确不等于业务参数已经生效。
- ILA 基本触发比较值/位置设置、单窗口 arm/立即采样、完整采集上传至 Vivado。
  首批没有独立停止、数据导出或浏览器波形；不以 upload 的停止副作用冒充停止操作。
- `DebugService` 共用 revision、人工/AI 控制权、短操作回执、缓存及后台轮询。
  busy/陈旧请求不排队，结果未知不自动重试；关闭/重开生命周期互斥，旧操作先收回执。
  本服务控制权不拦截原生 GUI、run_tcl 或其他 CLI/MCP 进程，使用时须协调。
- 四个 MCP 工具 `open_debug_panel/get_debug_snapshot/debug_action/close_debug_panel`；
  `vivado-mcp debug` 独立 attach CLI。MCP 工具总数为 37，原运行观察器继续只读。
- 本地页面提供精确设备选择、VIO 数值/滑杆/开关、ILA 配置与采集、状态和操作记录；
  回环监听、同源写入、JSON 限制，无任意 Tcl 或任意路径执行入口。
- 消费者可用有限 JSON 描述 slider/toggle，例子见 `examples/debug/panel.json`。
  `debug --demo` 提供明确合成演示，不接触 EDA/硬件；等待触发不靠计时器伪造完成。

唯一使用入口：[硬件调试指南](docs/HARDWARE_DEBUG.md)。Skill 路由已增加按需参考。
Tcl 接口依据是官方 UG835/UG912 2022.2；具体链接在模板注释与使用指南。目标版本的实际
属性、动态状态刷新、VIO 读回与 GUI 暂存交互仍需现场确认，文档依据不是板卡 PASS。

### 本批验证

环境：Linux、Python 3.12.14、MCP SDK 2.3.0、真实 Tcl 解释器与 Chromium。未安装或
运行商业 Vivado、Windows、Ross 或板卡。执行：

```bash
.venv/bin/pytest tests/test_debug_service.py tests/test_debug_backend.py \
  tests/test_debug_http.py tests/test_debug_cli.py tests/test_prompts.py -q
.venv/bin/ruff check src/ tests/
git diff --check
uv build --wheel --out-dir /tmp/otter-vivado-debug-wheel
```

- 首批最终定向 **133 passed**：包含真实 Tcl 解释器中的硬件 API 测试桩、共享服务并发
  与结果未知处理、HTTP 边界、CLI/Demo、MCP Prompt/注册及真实 Chromium 交互。
- 较早同批扩大到原 monitor/lifecycle、Tcl 协议、GUI/stdio 会话与编码回归为
  **266 passed / 8 skipped**；跳过均需真实 Windows 或 CP936。末轮只改动服务/页面后
  已重新跑上面的 133 项，没有把跳过项或合成输入记为硬件 PASS。
- Chromium 完整 Demo：选择合成设备、项目滑杆、开关、触发配置、立即采样、上传，共
  7 次操作均收到了 succeeded 回执；桌面与手机无页面异常，截图留在临时输出而未入库。
- 全仓 Ruff、diff、相关本地文档链接通过；实际 wheel 含新增模块与 debug.html；
  真实 MCP 列出 37 工具。未发布软件包、未改本机客户端配置、未操作真实硬件。

### 后续接续

1. 本机用已有带 ILA/VIO 的非生产测试设计，核对发现、实际输出读回、触发、立即采集、
   上传及原生 GUI 接续；先补实际使用版本，不扩成全版本/全板卡矩阵。
2. 在同一实现继续调试工程准备：IP 配置、MARK_DEBUG/综合后插核、必要连接、构建及
   bit/ltx 配对；不把业务 RTL 功能改造混入工具层。
3. 采样导出、浏览器波形与基于数据的分析；再加本地就绪确认、倒计时/提示、动作标记、
   暂停重做。精确对齐使用 FPGA 触发/帧标记，高速 ILA 不直接承诺连续六秒录制。
4. 按项目需求扩展枚举、定点数、单位、预设、趋势和实际可读的板卡信息。

以下为前轮运行观察与接入的历史记录，早先 CLOSED 仅指前轮交付收口。

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
Coding/Library，不操作板卡、不发布或改变可见性。本轮用户后续明确授权适合就合入，
之后默认 main 开发；这覆盖前轮“保留 Draft、不合并”的默认安排，其他边界保持。

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

## 首轮 GitHub 恢复点（历史，当前入口见末节）

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

本轮实现已推送：`e42ab542dd7d3ec8ab48a3048fd9a524fd4038fa`，继续同一个
[产品 Draft PR #1](https://github.com/otter-fpga-lab/otter-vivado/pull/1)。
正式接入和更新说明集中于 [SOURCE_CONNECTION.md](docs/SOURCE_CONNECTION.md)，
已有环境只需用该环境解释器执行 `python -m vivado_mcp connect --client all`；
日常改源无需再次运行接入命令。重大接入取舍续记到
[Hub Draft PR #5](https://github.com/lingshuncangqiong/otter-agent-hub/pull/5) 的 Vivado 议题。

## Windows、原生 GUI 与工具定位（2026-10-03，用户授权合入）

用户明确 Windows 为主要使用环境，要求工程能交给人用 Vivado GUI 打开、加强可视化、
定位为 Vivado 工具能力，并允许合入本轮 PR；以后本产品默认 main 日常开发，真实并行或
隔离/上游投稿需要时才另开分支。此次接续已有 PR #1，从远端 `ef86d16` 继续，无 reset。

本批完成：

- GUI 和 install 共用 ASCII/UTF-8 Tcl 引导命令，避免中文、空格、`$`、`[]` 路径展开；
  install/uninstall 仅编辑注入块，保留已有 init 的 ANSI/UTF-8、BOM、CRLF 与其他内容。
  报告读取支持 Windows 系统 ANSI，并避免 UTF-8 截断汉字触发错误回退。
- 样例使用原生磁盘 `create_project` + `import_files`，源和约束存入消费工程；根据真实
  当前工程目录输出存在的 `.xpr`。不覆盖/关闭已有工程、不启动 run，中途失败保留现场。
  [GUI 指南](docs/HUMAN_GUI_WORKFLOW.md) 提供同一 GUI 操作、重开、依赖核对和原生归档。
- 面板新增已发现 `.xpr` 路径、WNS/TNS/WHS/THS 和资源 Used/Available/Util% 图表；
  全部取共享查询/现有解析器。缺失为未知，不给内存工程或回放伪造 GUI 就绪结论，
  全局质量仍 unknown。可选工程元数据失败不影响有效 run 状态。
- README、包说明、Skill 和贡献指南明确工程/执行/仿真/IP/状态/报告职责；RTL 编码归
  Coding，原生 GUI 和网页各自承担已有功能。默认安装 main，保留 NJ、Apache-2.0、
  包名/版本、fork 与旧贡献分支。Ross 仍并列可选，无二进制依赖。
- 实际读取 AMD UG835 2026.1 的 create_project/import_files/current_project/open_project/
  archive_project 正文，结合 Ross `2cdc9eef` 工具参考；版本和证据链接在 GUI 指南，
  不将 Ross Linux-only display 说成 Windows 已支持。

本轮定向验证（Linux / Python 3.12.14 / 真实 Tcl、Chromium；EDA API 为明确测试桩）：

```bash
PYTHONPATH=.venv/lib/python3.12/site-packages:src python -m pytest \
  tests/test_install_encoding.py tests/test_gui_bootstrap.py tests/test_tcl_utils.py \
  tests/test_session_encoding.py tests/test_doctor.py tests/test_connect.py \
  tests/test_windows_runtime.py tests/test_project_observation.py tests/test_run_monitor.py \
  tests/test_flow_progress.py tests/test_probe_then_attach.py -q -rs
PYTHONPATH=.venv/lib/python3.12/site-packages:src python -m pytest \
  tests/test_demo_project.py tests/test_monitor_http.py -q -rs
.venv/bin/ruff check src/ tests/
git diff --check
```

分别 **192 passed / 7 skipped** 和 **39 passed**。跳过项为 5 个需要真实 Windows 的
junction/.bat/ANSI 运行测试和 2 个需中文 Windows CP936 的编码测试；没有 Windows PASS。
真实 Chromium 桌面/390 手机验证数值、未知降级、Windows 路径文本、XSS、失联保留快照，
无页面/控制台错误。Tcl 测试验证消费源导入、迁移后输入仍在、预检保护、可选元数据和
编码边界；它们不能证明真正 Vivado 可重开。Ruff、diff、文档链接与 32 工具清单核对通过。

未测/阻塞：真实 Windows/Vivado GUI/Tcl/attach、客户端链接发现、完整 `.xpr` 重开和报告
格式/性能仍按 GUI/RUN_MONITOR 指南现场验证，不占用 Coding 测试。GitHub Actions API
返回 0 workflows / 0 runs；读取启用设置返回凭据权限 403，未获得云端 Windows runner。
修改 GitHub About 简介同样返回 `Resource not accessible by integration` (403)；
README/包说明定位已更新，About 待有仓库设置权限的入口改为相同工具定位。

本批实现远端提交：`9d8b3a4ac96cf6357c32b33c5c73350eca056a7d`。
[产品 PR #1](https://github.com/otter-fpga-lab/otter-vivado/pull/1) 已按用户授权合入，
merge commit：`cf768a67f9e7982f21bbea2c946861c1adea9e8c`，保留全部原提交与作者。
本地已切回并 fast-forward 到 `main`，后续使用 `origin/main` 最新 HEAD；本条交接也直接
提交到 main。没有删除旧分支、发布包、改可见性或操作设备。旧贡献分支仍为 `b40e62cd`。
对应 Hub 只更新现有 Vivado 议题与 [PR #5](https://github.com/lingshuncangqiong/otter-agent-hub/pull/5)。

## 当前用户偏好与哈基米交接（2026-10-03）

- Windows 为主。常用 Vivado **2018.3、2020.2、2022.2、2024.2**，其中 **2018.3 和
  2024.2 优先**。这是产品维护/现场验收目标，不是已经跑通的兼容认证；保留上游2019.1
  历史验证来源。显式选择版本，已有工程不自动升级，运行中的会话不为更新而重启。
- 界面美观参考 shadcn/ui 与 Radix Colors，沿用本机共享状态薄视图；优先易读、键盘操作、
  长时间观察和报告查找，不为外观引入第二套业务实现。普通开发继续在 main 小块提交。
- **待哈基米协助：GitHub About 简介。** 用户已确认由哈基米处理，本云端修改
  `otter-fpga-lab/otter-vivado` 仓库 About 的 API 权限返回403，不重复尝试或改凭据。
  入口：[产品仓库](https://github.com/otter-fpga-lab/otter-vivado) 右侧 About 设置。
  仅修改 description，建议直接使用下列文案；无需改仓名、可见性、许可、fork或旧分支：

  > Otter Vivado — 本机 AMD Vivado 工程、执行、仿真、IP、真实进度与报告工具；人和智能体共用原生 GUI 工程，Skill / CLI / MCP / UI 共用实现。保留 vivado-mcp 上游来源与许可。

  完成后在本节记录实际修改结果即可。README 与包说明已采用相同工具定位；About 待办
  不阻塞代码工作，也无需占用 Coding 的现场测试。

## 日常体验与四版本兼容加固（2026-10-03，main）

接续远端 main `b9a19ba0d16a4b7fcedc16bb656bf2f58c3cc157`，该提交已保存上述版本偏好
和哈基米 About 交接。实现已提交并推送到 main：
[`53cfa8ee2318ae17c1c5f10571554997b9eff9d4`](https://github.com/otter-fpga-lab/otter-vivado/commit/53cfa8ee2318ae17c1c5f10571554997b9eff9d4)。
以后取 `origin/main` 最新 HEAD，不回退到锚点；原 PR #1 已合入，不另建重复 PR。

已完成：

- 面板参考 shadcn/ui 语义样式与 Radix Slate/Teal 配色，支持系统/浅色/深色、窄屏布局、
  报告搜索和路径复制。刷新保留报告选择、键盘焦点和阅读位置；复制受限时可选择原路径。
  仍为同源状态的本机只读页面，没有引入第二业务库或外部 CDN。所用配色的 MIT 许可保留在
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，随 wheel 一并安装。
- `python -m vivado_mcp versions --json` 只读列出已知目录、PATH 与配置的候选安装。
  路径推断版本与实际 `version -short` 证据明确区分；显式路径或 `VIVADO_PATH` 无效时
  报错，不悄悄切版本。同一 session ID 不替换另一显式路径；自动接续固定端口 GUI 时，
  显式版本不匹配/无法核实只关闭本次探测连接，保留原 GUI，提示独立实例或显式 attach。
- STATUS 与报告文件读取分开发布。慢报告不拖住状态采样或 CLI 退出；独立显示
  `reports_status`、读取时间、来源和错误，迟到的旧 run 报告丢弃。新增 `close_run_monitor`
  释放观察器/HTTP，不停止 Vivado/run；失联停止轮询，仍保留最后快照供查看。
- 复用资源解析器补充 CLB 名称与分数 BRAM，非法数值明确降级。时序阶段只取该份报告的
  `Design State`，未知不借其他 run 状态补全；两个 READY 汇总也核对本报告阶段，
  综合后/布局后/未知阶段保留数字并降级。它们仍是当前打开设计的有限检查，不代替签核。
- 运行轮询允许可选 elapsed 属性缺失；重置命令使用官方完整 `reset_runs`。实际读取
  四版本 UG835 与 2018.3/2024.2 UG973，固定版本依据、Windows 边界、工程不自动升级、
  原生 GUI 交接和现场步骤集中在 [VERSION_COMPATIBILITY.md](docs/VERSION_COMPATIBILITY.md)。
  Ross 保持并列可选，其文档中的 2026.1 测试声明不能外推为用户四版本已支持。

验证环境为 Linux / Python 3.12.14；定向命令：

```bash
PYTHONPATH=.venv/lib/python3.12/site-packages:src python -m pytest \
  tests/test_config.py tests/test_session_selection.py tests/test_session.py \
  tests/test_probe_then_attach.py tests/test_doctor.py tests/test_run_monitor.py \
  tests/test_monitor_lifecycle.py tests/test_monitor_http.py tests/test_project_observation.py \
  tests/test_poll_compatibility.py tests/test_flow_progress.py \
  tests/analysis/test_run_progress_parser.py tests/analysis/test_timing_parser.py \
  tests/analysis/test_util_parser.py tests/test_report_tools.py -q -rs
.venv/bin/ruff check src/ tests/
git diff --check
```

结果 **383 passed / 6 skipped**。6 项跳过均为 `test_session.py` 中需要真实 Windows 的
既有测试；没有将这些跳过记为通过。包含 3 个真实 Chromium 浅/深色及手机浏览器检查、
Tcl 解释器测试、TCP 协议与慢磁盘子进程退出测试，EDA API/报告均为明确测试桩或样本。
Ruff、diff、本地文档链接、README 与实际 33 个 MCP 工具一致性检查通过；实际构建 wheel
并核对 UI、Tcl 与第三方许可文件存在。未扩大到全仓/全版本测试。

未测/下一动作：没有商业 Vivado、Windows、Ross 或板卡运行结果。先在用户空闲时按兼容
指南对 **2018.3 与 2024.2** 分别记录 OS/实际版本、GUI/Tcl/attach、原生 `.xpr` 重开、
独立样例的 run 状态与 timing/utilization 原文，再在 2020.2/2022.2 验证受影响路径。
既有 Windows 中文/空格路径、客户端链接发现和实际磁盘性能仍需现场核对；不自动升级 IP、
不动生产工程、不占用 Coding 的本机测试。About 继续由哈基米处理，不重复请求仓库设置权限。

## 本轮收口，转实际使用反馈（2026-10-03）

用户确认不同 Vivado 版本通常仍可共用工具，要求最后优化一轮后收口，后续遇到实际问题
再处理。本节是当前接续状态；CLOSED 表示本轮交付结束，不代表全部版本/设备经过认证。
从 main `a0b2c129571e54dd2d569c837f019aed016153f5` 继续，保留此前交付与旧贡献分支。

- 核对代码没有四版本白名单；2018.3/2024.2 仍是常用维护重点。README、版本指南与
  Skill 明确按实际命令/选项/报告差异处理，不因版本号不同拒绝使用或要求升级，也不把
  四版本测试作为使用门槛。以前记录的现场清单改为按需选用，不继续主动铺开验证矩阵。
- 修复自定义安装目录不含版本号时直接拒绝复用 GUI 的限制：先通过已有协议查询
  `version -short`，成功取得实际版本后复用并显示来源。明确选择的已知版本不符、
  查询错误/无效值/超时仍保护原 GUI，不启动替代实例、不停止活动运行。
- 修复面板随机端口变化导致主题偏好丢失：仅将 system/light/dark 外观枚举保存在
  本机 host-only Cookie 中，兼容已有 localStorage；存储不可用时仍可正常观察。
  没有在 Cookie 中保存工程/路径/token，没有更改只读 HTTP 或 Vivado 控制能力。
- 反馈模板改为实际会话/日志版本，不把 `.xpr` 的 Project Version 格式字段当工具版本；
  优先已有快照和原始错误，不为整理反馈重跑构建/仿真。后续记录源码提交、实际环境、
  最小复现及受影响能力即可，不要求用户先做一轮全面测试。

About 文案继续交哈基米，入口和范围见上文。没有增加商业 EDA、Windows 或板卡实测记录；
已有真实 GUI/工程重开、客户端链接等未测项保留为使用时按需核对，不阻塞本轮收口。

末轮实现：main
[`c8c7953166fe86f61913c20ba325711f72eaf7e2`](https://github.com/otter-fpga-lab/otter-vivado/commit/c8c7953166fe86f61913c20ba325711f72eaf7e2)。
本节随后的交接提交一同推送 main；后续始终 fetch 最新 HEAD。

定向验证：

```bash
.venv/bin/pytest tests/test_session_selection.py tests/test_probe_then_attach.py -q
PYTHONPATH=.venv/lib/python3.12/site-packages:src python -m pytest \
  tests/test_monitor_http.py -q -k browser
.venv/bin/ruff check src/vivado_mcp/tools/session_tools.py \
  src/vivado_mcp/vivado/gui_session.py tests/test_session_selection.py tests/test_monitor_http.py
git diff --check
```

会话/连接 **54 passed**（最后仅调整临时安装路径夹具后，selection 子集 **16 passed**）；
Chromium **4 passed / 29 deselected**。验证自定义路径与非四目标版本的协议连接、无效返回/
错误/超时保留原 GUI，以及两个随机端口的主题继承、刷新与存储失败回退。协议服务为明确
测试桩，不是 Vivado；未重跑前轮 383 项或其他 EDA 矩阵。修改文件 Ruff、diff、本地文档
链接检查通过。用户确认的默认 main 开发、原作者/许可/fork 和旧贡献分支保持不变。
