# Otter Vivado：远程构建合入与既有能力维护

Status: CLOSED_WAITING_FOR_REAL_USE（文档入口已收拢；CodeBuddy 客户端探测无问题为用户报告）
Target: otter-fpga-lab/otter-vivado
Basis: 2026-10-08 用户批准保留 Claude 修复并优化工具便利性；Claude 已继续 HDMI 工程，本轮只维护工具仓
Next: 按实际任务使用当前同源入口，具体工具或现场问题带源码提交与已有回执反馈本仓
Result: Antigravity/WorkBuddy 原生安装与缓存 SDK、CodeBuddy 原生登记与源壳 SDK 通过，50 工具；既有诊断边界与 Codex 四文本结果保留

## User Library整理收口（2026-10-08）

本轮User Library归位、同源接入与安装/使用说明整理已收口。后续按实际工程中的工具反馈维护；已有厂商版本、GUI、SSH或板卡未测项按原范围保留，不为目录收口启动补测。

用户已授权本批Git提交与推送；此前本地提交随本次收口同步，最终远端提交和跨产品接续见[Hub议题](https://github.com/lingshuncangqiong/otter-agent-hub/blob/main/topics/20261002_otter-series-rollout/TASK.md)。本次只回填记录，复用既有有效验证。

## 文档入口与实际反馈（2026-10-08）

README 收敛为用途、首次安装/使用、日常更新、使用导航和反馈；详细流程保留在
[使用指南](docs/USAGE.md)，六客户端的接入方法与证据分开写入
[客户端接入](docs/SOURCE_CONNECTION.md)。AGENTS 指向同一接入页，CLAUDE 只导入 AGENTS。
普通源码更新等活动任务完成后正常重启；依赖变化重跑原 editable 安装，路径/入口/元数据
变化再修正源引用或更新薄壳。旧 README 主要锚点保留为新内容导航。

- 用户反馈：CodeBuddy 客户端探测无问题，未给逐工具明细；按用户报告记录，不扩展为所有 EDA/GUI/SSH/设备通过。
- 既有授权：系列常规维护已有推送授权，发布与设备动作另按任务授权；本批按主控安排只做本地提交，推送由主控收口。
- **PASS（文档）**：7 个 Markdown 的 106 个本地链接、19 个锚点和 3 个 JSON 样例；PowerShell 样例仅语法解析；AST 静态核对 50 个 MCP 定义与工具表一致，相关 CLI/安装参数、CLAUDE 入口及 `git diff --check` 通过。未执行样例或重跑产品测试。
- `NOT_RUN`：本轮客户端安装/配置、厂商 EDA、GUI、SSH、业务工程与设备；本轮只维护 Markdown。
- 下一步：按实际任务使用当前入口，具体工具问题带源码提交与已有调用/日志反馈本仓。

## 首次安装闭环与 CodeBuddy 入口（2026-10-08）

README 路由到[既有接入页](docs/SOURCE_CONNECTION.md#first-setup)，补齐环境准备、生成壳、
本地市场 JSON 与原生登记安装；已有市场只合并本产品条目。CodeBuddy IDE 复用
`--client workbuddy` 格式，但安装子进程的两个配置根变量均明确选 `~/.codebuddy`；
WorkBuddy 随包 CLI 只是本机复用示例，CodeBuddy 可用自己的 CLI，不要求安装 WorkBuddy。

- **PASS（文档）**：市场 JSON/版本、PowerShell 语法、相关本地链接与 `git diff --check`；纯文档未改代码或配置，未重跑产品测试。
- **PASS（主控本机）**：`otter-vivado@otter-local` 在 CodeBuddy 用户范围原生登记/启用；本机 IDE 源码确认从 `~/.codebuddy` 已登记目录市场读源壳。按该源壳 command/args/env 执行 SDK，50 工具及 `vivado_guide` 内容/源 hash 与正式源一致；这不是 IDE 当前会话运行证据。
- **证据**：工作区 `Backups/20261008_codebuddy_studio/codebuddy.native.plugins.json` 与 `codebuddy-otter-vivado-check.json`；恢复材料不作为产品入口或依赖。
- **NOT_RUN**：当前 CodeBuddy IDE 重启后加载/模型调用、厂商、GUI、SSH、业务工程和设备。本机旧 Vivado 环境参数已保留在新壳，不改变设备或执行边界。

普通源码/指南 `git pull` 后在空闲边界正常重启；依赖声明变化重跑原 editable 安装命令，
入口/路径/元数据变化维护薄壳并按宿主原生方式更新。本批分仓本地提交，推送由主控按授权处理。

## WorkBuddy 同源薄插件（2026-10-08）

从 Antigravity 提交 `255fdfe` 的干净 main 接续，按用户批准增加 `plugin --client workbuddy`；
方法见[接入说明](docs/SOURCE_CONNECTION.md#workbuddy-native-plugin)。四文本为
`.codebuddy-plugin/plugin.json`、`.mcp.json`、共用 bootstrap 和 README，manifest 只含
`name/version/description/skills/mcpServers`。无 Codex interface、业务副本、嵌套链接或新安装平台。

- **PASS**：WorkBuddy 新分支生成/CLI与现有输出、路径、私有解释器、editable 保护定向
  **5 PASS**；修改文件 Ruff lint、无新增格式差异、文档链接与 diff 检查通过。
  Codex/Antigravity 各四文本的修改前后 SHA256 全部相同，复用既有宿主与业务证据。
- **PASS（主控本机）**：native validate/install/list 成功，`otter-vivado@otter-local` 启用；
  实际缓存 `~/.workbuddy/plugins/cache/otter-local/otter-vivado/0.3.25` 只有四文本。
  按缓存 command/args/cwd 运行 SDK，只读 `vivado_guide`，50 工具，源根/内容/hash 与正式源一致。
  只新增本机市场/启用项；旧记录的版本、路径、启停保留，原生管理器只刷新 lastUpdated。
- **证据**：工作区 `Backups/20261008_antigravity_workbuddy/workbuddy.native.plugins.json`
  与 `workbuddy-otter-vivado-check.json`；恢复材料不是产品入口或运行依赖。
- **NOT_RUN**：WorkBuddy 当前会话刷新/模型采用，以及 EDA、GUI、SSH、业务工程与设备。

普通指南/MCP 更新在既有空闲边界重启生效，不刷新壳；只有入口/元数据变化时升版本并用原生
`plugin update` 接入，避免同版本缓存不覆盖。此批单独本地提交，由主控按授权推送。

## Antigravity 同源薄插件（2026-10-08）

基于干净 main `10e8cb2`，按用户批准增加 `plugin --client antigravity`，复用当前私有 editable
环境、MCP 启动参数与 bootstrap。原生格式见[接入说明](docs/SOURCE_CONNECTION.md#antigravity-native-plugin)。
只生成 `plugin.json`、`mcp_config.json`、短 Skill 与 README；manifest 只有 `name/description`，
不复制业务或长指南，不建嵌套链接，生成器不登记宿主。既有 guide、会话与执行权限模型未改。

- **PASS**：Windows 私有 Python 的生成/CLI/覆盖、相对路径、非 editable 与错误解释器保护
  定向 **10 PASS**；修改文件 Ruff lint 通过，新格式无新增差异，HEAD 已有 CLI 格式不重排。
  重新生成的 Codex 四文本与修改前 SHA256 全部相同，没有重装 Codex 或扩展其回归矩阵。
- **PASS（主控本机）**：Antigravity native validate/install 成功，`agy plugin list` 登记本插件
  的 skills/mcpServers；实际 `~/.gemini/config/plugins/otter-vivado` 只有四文本。
  从该缓存配置运行 SDK，只读 `vivado_guide` 成功，50 工具；source_root/content/hash 等于正式源。
  全局 `config.json/mcp_config.json/skills.json` 未变。
- **证据**：工作区 `Backups/20261008_antigravity_workbuddy/antigravity.native.plugins.json`
  与 `antigravity-otter-vivado-check.json`；恢复材料不是运行依赖。
- **NOT_RUN**：已有 IDE 会话刷新、模型实际调用、EDA、GUI、SSH、业务工程与设备。
  WorkBuddy 后续适配与实际结果见上文，不以安装成功替代厂商或模型采用证据。

生成壳与源工具按原生命周期更新。此批本地提交，不 push；既有 Claude 修复及下方验证继续按原范围保留。

## 接入诊断改进（2026-10-08）

从干净 main `7627650d6687ad6de50dda252eb149390e83293f` 接续，保留 Claude 的修复：
`doctor` 识别已启用 Codex 原生插件，`connect` 共用判定并拒绝新增第二份普通入口。
本次仅完善配置诊断，没有修改真实客户端配置、重启活动服务或接触 HDMI 工程。

- 客户端检查只表示登记/启用，成功结果显式返回 `runtime_verified=false`，文案说明实际
  MCP 运行与工具调用未验证；既有 Vivado TCP 协议检查单独保留，没有增加在线健康门禁。
- 原生插件与可验证直接登记重复、或多个可验证直接登记时只告警，让使用者核对是否有意
  保留；`--fix` 保持客户端配置字节原样且不创建备份。Codex 按 `enabled=false` 排除停用
  条目，不自动启用；Claude 仅核对用户级登记并注明项目开关未采样，不将 entry 的
  `enabled/disabled` 当作实际停用。损坏 TOML、错误 `mcp_servers` 类型及常用键名
  `vivado/vivado-mcp` 的无效启动条目不会被原生插件或另一个有效条目遮住。
- 启动项核对接受 `-B -m vivado_mcp` 及可选 `serve`，继续拒绝 `-c`、`doctor`、
  `uninstall` 等路径；没有放宽任意参数或更改会话、执行、审批模型。

产品私有 Python / Windows 定向验证：

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/test_doctor.py tests/test_connect.py::test_codex_native_plugin_refuses_duplicate_source_entry_before_writes -q
.\.venv\Scripts\ruff.exe check src/vivado_mcp/doctor.py tests/test_doctor.py
.\.venv\Scripts\ruff.exe format --check src/vivado_mcp/doctor.py tests/test_doctor.py
git diff --check
```

**42 passed**；修改文件 Ruff/check/format 与 diff 检查 PASS。夹具覆盖上述真实失效模式，
客户端配置与 Vivado/TCP 均为隔离替身；本轮实际宿主 MCP 调用、EDA/远端/板卡 **NOT_RUN**。
Claude 此前的全量测试含既有失败、WinError 1314 链接权限和 LTX 读取检测波动，本轮不重跑
全量或修复这些无关问题，既有全量结果不作为本次定向回归通过的证据。

Claude 本机核对已有 `coding-tools/vivado/otter-vitis` 三项登记、Skill 入口及
`claude mcp` 健康检查证据。用户重启后，同一 Claude 会话于 10:35/10:36 报告三组 MCP
已出现在当前会话并继续 HDMI 任务；主控尚未看到这三组工具的只读调用回执。因此记录为
“重启后发现已报告，实际调用证据尚缺”，不要求暂停业务补诊断或增加每任务必跑检查。
本节随本次实现提交保存，推送由主控统一处理。

## 接入文档入口（2026-10-08）

README 与根 AGENTS 路由到[既有接入文档](docs/SOURCE_CONNECTION.md)，CLAUDE 通过 `@AGENTS.md` 复用维护入口；新增五客户端速查，默认示例明确选择单客户端，保留 `all` 的重复入口边界。纯文档静态参数、相对链接与差异检查 PASS；本轮安装、宿主、EDA/板卡 NOT_RUN。后续接入从该页选择实际宿主，不因进入仓库自动安装。

## Codex 源绑定插件（2026-10-07）

从干净 main `b527672614fc870cdc89da18b47edc98dedcd673` 接续，保留三个本机 ahead 提交及全部既有实现。`vivado-mcp plugin --client codex --output <新绝对目录>` 只生成 manifest、MCP 配置、简短 bootstrap Skill 与 README，不覆盖已有目标、不安装宿主或写全局配置。MCP 绑定本仓 `.venv` 的 editable 源；普通业务/长指南不进缓存。格式与来源见 [接入说明](docs/SOURCE_CONNECTION.md#codex-native-plugin)。

新增 `vivado_guide` 的 `overview/remote/remote-tcl/reference` 明确路由，按次读当前源、返回真实源根/同源 CLI/内容指纹；reference 只接受白名单文件名并拒绝重定向/越界，不开放任意文件、不读 hosts 或凭据。Python 在正常重载边界更新，没有文件监听或热更新框架。已有本地与远程实现、workspace 和 GUI/TCP 职责保持；没有新远程执行 MCP 工具。

Codex 已启用本插件时，旧 `connect` 的 Codex 源码接入（含 check/skills-only）在预检拒绝双重入口并说明去掉 codex/all；不改配置、不称 ready、不禁用已有插件。其它客户端源码方式保留。WorkBuddy 仅记录本机历史 `mcpServers`/Skill junction 事实及同源引用去向，本轮不改其配置、不新增未经验证的原生格式。

| 本轮证据 | 实际范围 |
|---|---|
| 生成器、指南与真实 SDK stdio | 与现有远程 CLI 入口的定向集合 **20 PASS / 1 SKIP**：生成四文件；从隔离缓存 cwd 用原绑定 command/args 启动源 MCP，guide/list_sessions 通过；同一会话更新隔离源指南后取得新内容/指纹。生产指南没有临时探针 |
| 路径与配置边界 | 任意 reference/路径拒绝，现有目录保留，非 editable 不创建目标；Codex 已启用插件的临时配置两种模式 **2 PASS**，拒绝普通入口并保持字节与目录不变；没有读取/修改真实其他客户端配置 |
| Skill | 使用本机 skill-creator，简短入口只路由真实任务；`quick_validate` PASS，使用已有 PyYAML 解释器，未新增产品依赖 |
| 主控原生宿主观察 | `codex plugin add otter-vivado@personal --json` 安装成功；实际缓存 `~/.codex/plugins/cache/personal/otter-vivado/0.3.25`。缓存四文件与生成壳一致，无 src/长 references；app-server 的 skills/list 发现 `otter-vivado:otter-vivado`，指定 MCP 从缓存配置启动，实际 50 工具 |
| 主控真实安装缓存协议 | 以已装缓存 `.mcp.json` 为启动输入、cwd 为实际 cache，SDK initialize/list_tools=50、overview/remote/list_sessions **PASS**；overview 文本/指纹逐字匹配源，源根为当前 E 仓，session 为空。记录在工作区 `Backups/20261007_vivado_codex_plugin/installed-mcp-check.json`，无 EDA/SSH |

主控核对实际配置只新增 `plugins.'otter-vivado@personal'.enabled=true`，personal marketplace 只增一项；没有普通 Vivado Skill 链接或独立 MCP 项，没有改变其他客户端。原生发现与 SDK 调用不等于模型稳定采用或 EDA/硬件验收；当前聊天工具快照未热注入，本回合没有通过模型工具目录调用该插件，不以隔离/原生协议证据冒充模型任务。

本轮不启动 Vivado、SSH/Linux、共享盘或板卡；这些仍 **NOT_RUN**。Windows 文件 symlink 的独立重定向用例因 WinError 1314 缺权限 **SKIP**，未当成通过；其余路径白名单与 source-bound 证据按实际测试记录。既有远程流程与其历史验证继续复用，不重跑厂商矩阵。

## 远程构建合入（2026-10-07）

起点为干净 main `105e5f8a2952f4593c6dce6eeb75a66be6bcea47`，保留此前本机 ahead 提交与恢复分支；来源是本机 `fpga-remote@e6ff98c2ec5bea07e440d694fa35602aa32eb74f`。按最新授权只做产品合并、验证和旧源码收口，没有运行 `connect/install` 或注册、启用任何客户端 Skill/MCP/Plugin。

正式实现是 `src/vivado_mcp/remote_build/`：stdlib 核心与同包 bash/Tcl；现有 `vivado-mcp remote` 或 `python -m vivado_mcp.remote_build` 调用它。保留工程/原生 Tcl、workspace、主机队列、状态、报告、共享/SCP 交付和清理语义；不用本地 GUI/TCP session，也不增通用调度框架。使用说明见 [远程构建](docs/REMOTE_BUILD.md)与[原生 Tcl](docs/REMOTE_TCL.md)，Skill 仅增加实际 CLI 路由。

原主机配置原样复制到忽略文件 `config/remote-hosts.local.json`，未显示或提交内容。默认同仓读取，`OTTER_VIVADO_REMOTE_CONFIG` 与显式 `--config` 可覆盖；公共 example 不含私人 IP/用户名/机器路径，wheel 只包含 example。缺配置明确说明；离线 `inspect/report --results` 不加载它、不调用 SSH/SCP。旧源码与全 Git 历史由工作区保留，恢复索引是 `Backups/20261007_fpga_remote_merge/`；不将私人旧历史合入本产品，也不保留旧路径桥接或固定 Python 版本的 PS1。

两个必要边界修正：离线 main 不再无条件读取主机配置；包解压发现缺少 `tarfile.data_filter` 时在下载前明确拒绝，不采用无过滤回退。Windows Tcl 的空 env 赋值使原替身首轮失败，仅将测试夹具改为 `unset`，真实 Tcl driver 不变。

| 本轮实际验证 | 结果与范围 |
|---|---|
| Windows / Python 3.13，`pytest tests/remote_build -q` | **44 PASS + 11 subtests PASS**：迁入原 37 项，新增 CLI 真实子进程、配置选择、离线输入/报告强制拒绝网络入口、资产定位/LF及解包缺能力回归；全部使用隔离夹具 |
| 定向 Ruff / format、`pip check` | **PASS**；原 shell/报告三引号字面格式保留，长行例外只标注在这些字面结尾，没有改写生成脚本文本 |
| 现有 Git Bash `runner_smoke.sh` | **PASS**：归档/checksum、共享发布、发布失败不伪报成功；真实本机 shell 与 fake Vivado，不调用商业工具 |
| 现有 tclsh 五个 driver 替身 | `synth/route/bitstream/reference/synth_failure` **5 PASS**，覆盖阶段控制、reference、产物与失败传播；不代表 Vivado Tcl 或 EDA 通过 |
| `pip wheel . --no-deps` 与仓外解包 | **PASS**：core/模块入口/bash/Tcl/example 入包，资产保持 LF，无私人配置；从解包目录读取模块/资产，根 CLI、直接模块 `--help` 与无配置离线 report 通过 |

产品私有 `.venv` 已 editable 安装 `.[dev]`，CLI 及开发依赖就绪；这是开发/测试环境，不代表客户端接入。**NOT_RUN：真实 SSH 主机、共享盘、Vivado/EDA、生产工程、设备/板卡及客户端。** 没有新版本发布或 push，没有全量厂商/产品矩阵；下方历史证据继续按原范围保留。

旧源码收口已完成：原完整仓与Git保留于工作区 `99_垃圾堆/20261007_fpga_remote_merged/fpga-remote/`，源HEAD仍为 `e6ff98c2ec5bea07e440d694fa35602aa32eb74f`；原主机配置与新忽略文件字节一致，根导航已改为本产品远程入口。旧位置移走后，已再次从产品私有入口执行 `vivado-mcp remote --help` 成功，工作区未留下旧路径兼容入口。本次运行实现提交为 `6310b17e26acc4fcde1edb4af9e1205d1e8a1687`；此后收口仅更新文档，不重跑已有效的回归。

## 本机整理恢复点（2026-10-07）

本机正式源已归位；main 接续 origin/main 002793d。原贡献分支和 upstream 保留。retained/local-pre-reorganization@753ea17 保存原 ae41c64 及此前三处未提交工程保护修复；retained/main-before-reorganization 保存原本机 main。恢复分支仅为原始成果，不代表已验证或已合入当前产品；如后续需要相应功能，先与现行实现对照，不能整分支盲合。

该整理轮次只处理源码和旧入口，当时产品私有 venv 退出活动使用，依赖信息和原环境在本机恢复/暂存批次。本次远程合入的新开发环境与验证以上节为准；客户端仍由用户后续安排。配置/资料位置与总进度见 [Hub 整理 TASK](https://github.com/lingshuncangqiong/otter-agent-hub/blob/main/topics/20261002_otter-series-rollout/TASK.md)。

## 本轮收口：第 6 项独立 ILA 停止核对（2026-10-03）

从干净 main `d8bc13deff054a960f9f405f9bc5b81000c80585` 接续。用户明确要求完成本轮后
结束，真实 Vivado、Windows、板卡、消费者页面/声音以后实际调用时顺手验证，不再挂为
阻塞项。本节随收口实现提交保存；没有发布包或操作真实设备。

| 项目 | 本轮结果 | 实现记录 |
|---|---|---|
| 1. 调试构建交付 | 已交付专用会话 DCP 导出、bit/ltx/报告清单与生成指引 | `234a4dd` |
| 2. 采样导出与波形数据 | 已交付 VCD 导出与离线读取/分页 | `91e932d` |
| 3. 采样分析与取证 | 已交付统计、声明规则与反例证据 | `fbc9845` |
| 4. 本地实验编排 | 已交付就绪、倒计时、提示、标记、暂停/恢复/中止/重做 | `cd55063` |
| 5. 工程控件语义 | 已交付枚举/定点/单位/预设预览与受约束写入 | `d8bc13d` |
| 6. 独立 ILA 停止 | 已核对并明确当前插件不支持；提供原因、适用范围与处理路径 | 本次收口提交 |

- 实际下载并读取 UG835 v2022.2 PDF：run_hw_ila 负责启动；reset_hw_ila 重置配置；
  upload_hw_ila_data 会停止并上传、覆盖同核数据对象；wait_on_hw_ila 超时只结束等待。
  未发现独立 stop_hw_ila 条目，不编造停止命令、不隐式上传或 reset 来替代。证据页码与
  官方链接见 [ILA 停止核对](docs/ILA_STOP.md)。结论仅适用于当前插件/API 和已核对文档，
  不声称所有版本均无其它能力，也没有动态探测实际 Vivado。
- 快照保持 capabilities.stop_ila=false，新增 stop_ila_details；后端占位错误明确
  上传/reset 的影响。MCP 工具数不变，未新增硬件动作、Tcl 命令、传输路径或固定页面。
- Skill 指导消费者解释不可用原因；暂停/中止/超时/关页不等于硬件停止。实际有需要时
  协调原生 Hardware Manager 处理，再显式 refresh 核对身份和状态，不自动重 arm/重做。

验证环境 Linux / Python 3.12.14 / MCP SDK 2.3.0，Tcl 与硬件替身边界沿用前五项。

```bash
source .venv/bin/activate
pytest tests/test_debug_capabilities.py tests/test_debug_backend.py \
  tests/test_debug_service.py tests/test_debug_experiment.py tests/test_debug_experiment_tools.py \
  tests/test_debug_controls.py tests/test_debug_controls_tools.py tests/test_debug_http.py \
  tests/test_readme_hooks.py -q -rs -k 'not browser'
ruff check src/ tests/
git diff --check
```

定向回归 **216 passed / 3 deselected**；3 项为未改网页的浏览器测试，不记为通过。
验证能力读取不查询/改写硬件、unsupported 请求不分发，保持后端不发停止/上传命令，
并回归共享服务、实验、工程控件和 HTTP 边界。Ruff、diff、文档链接检查通过。

本轮结束。真实设备/Windows/页面/声音尚未验收，按用户要求仅保留这个事实；不作为
未完成开发项。以后调用时若出现具体问题，再带实际版本、命令/回执、文件与最小复现
处理。以下章节为历史工作记录，其中“下一项/待现场验证”反映当时状态，以本节为准。

## 当前工作：第 5 项工程控件语义（2026-10-03）

从干净 main `cd55063f52a9379676b159dff60dbf8e82fa1aaf` 接续（本地实验编排提交）。
用户要求开始下一项，继续以插件接口、数据与智能体生成指引交付；本节随实现提交保存。

- 新增独立 version=1 工程控件描述，严格绑定 target/device、核名称/UUID、探针位宽/方向。
  number 显式声明补码/无符号、定点、scale/offset、单位及 min/max/step；enum 显式选择
  option id/位模式。1~256 位、十进制字符串/有理数精确计算，不经浮点、不舍入/饱和。
- `resolve_debug_controls` 离线生成指纹、读回解释与有序预设计划。快照匹配不冒充实时
  证据；未知/坏值/未映射枚举/超策略范围保留原始值，不把 GUI staged 值当已提交。
- `write_debug_control` 与 Python submit_control 复用同一共享服务，保持控制权、版本、
  busy 与实验独占。执行前复查实时绑定；保存语义请求指纹、原始回执与解码结果。
  写前拒绝与写后 unknown 分开，读回匹配不证明业务生效，失败后不自动重放。
- 预设只展开有序计划，消费者逐项等待成功与最新 revision。明确部分完成、非原子、无
  回滚、不支持主机精确脉冲；当前实验原始 write_vio 步骤不会隐式应用控件语义。
- 新增 [接口与示例](docs/DEBUG_CONTROLS.md) 和
  [智能体控件生成参考](skills/otter-vivado/references/controls.md)，补齐 Skill 路由与
  现有说明。控件 JSON、页面、适配与当前配置版本由消费者持有；没有增加固定 UI/HTTP
  路由，不改变旧 panel_path schema、Tcl 或会话传输。

验证：Linux / Python 3.12.14 / MCP SDK 2.3.0；asyncio 与 Tcl 解释器真实，硬件为明确替身。

```bash
source .venv/bin/activate
pytest tests/test_debug_controls.py tests/test_debug_controls_tools.py \
  tests/test_debug_experiment.py tests/test_debug_experiment_tools.py \
  tests/test_debug_service.py tests/test_debug_backend.py tests/test_debug_http.py \
  tests/test_debug_cli.py tests/test_ila_capture.py tests/test_ila_analysis_cli.py \
  tests/test_debug_bundle.py tests/test_debug_artifacts_cli.py tests/test_debug_design_tools.py \
  tests/test_debug_prepare_cli.py tests/test_prompts.py tests/test_readme_hooks.py \
  tests/test_version.py -q -rs -k 'not browser'
ruff check src/ tests/
git diff --check
python -m pip wheel --no-deps . -w /workspace/scratch/otter-vivado-controls-wheels
```

定向回归 **320 passed / 3 deselected**，3 项为未改网页的浏览器测试，不记为通过。
首轮控件/共享服务/实验子集 **98 passed** 已包含于回归，不相加。覆盖补码边界、定点、
负 scale、80 位整数、有效低枚举、错误声明、未知读回、实际 MCP→服务→Tcl 替身链路、
单探针提交不影响其它 GUI 暂存值、旧指纹/版本、控制权、身份变化、实验独占、读回失配
及写后断连保留回执。Ruff、diff、文档链接/示例与 wheel 检查通过。
没有真实 Vivado、Windows、板卡、消费者浏览器验证；未改 web/、未发布包或操作设备。

## 当前工作：第 4 项本地实验编排（2026-10-03）

从干净 main `fbc9845fb8ae616ffcaad0c3295b7e7e1d89e0b1` 接续（上一轮采样分析提交）。
用户明确要求开始下一项，继续按插件能力边界实现；本节随实现提交保存，不发布包。

- 新增 `DebugExperiment` 和创建/快照/控制三个 MCP 入口，复用现有 DebugService。
  消费者声明 ready/countdown/cue/debug/wait_capture 步骤，创建后尚未执行；首步必须
  来自消费者本地人工确认，MCP 不代替实际就绪。计划固定，计时不依赖聊天往返。
- 单调时钟倒计时支持暂停/恢复，提示与动作标记含序号、UTC 和单调相对时间。声音由
  消费者页面按事件播放，运行器不声称已发声、人已动作或已与 FPGA 对齐。
- 有限硬件步骤复用 revision、control 与短操作回执；固定目标/核 UUID/探针结构，
  包括进入硬件执行前的再次核对。活动实验拒绝其它写入/选目标，控制权交接中止编排。
  暂停/中止先等待已接受短操作，不停止 FPGA、不回滚 VIO，不重放已完成步骤或未知结果。
- 新消费者目录保存计划/指纹/初始硬件快照，每轮独立目录保存逐事件 JSON 和 VCD 导出。
  redo 要求上轮明确终止、之后显式 refresh、相关 ILA 明确 IDLE；用操作身份判断刷新先后，
  不受系统时间回拨影响。旧轮保留，unknown 不可自动 redo，崩溃后不自动续跑。
- 新增 [实验接口与边界](docs/DEBUG_EXPERIMENT.md) 和
  [消费者实验生成参考](skills/otter-vivado/references/experiment.md)，更新 README、Skill
  入口及 ILA Prompt。说明同进程/同服务适配、人工按钮、声音去重、旧页隔离与现场验证；
  没有新增固定实验 UI 或任意页面加载服务器，不能只交 HTML 就宣称已接入。

验证环境 Linux / Python 3.12.14 / MCP SDK 2.3.0，真实 asyncio，Tcl 解释器使用 EDA 替身。

```bash
source .venv/bin/activate
pytest tests/test_debug_experiment.py tests/test_debug_experiment_tools.py \
  tests/test_debug_service.py tests/test_debug_backend.py tests/test_debug_http.py \
  tests/test_debug_cli.py tests/test_ila_capture.py tests/test_ila_analysis_cli.py \
  tests/test_debug_bundle.py tests/test_debug_artifacts_cli.py tests/test_debug_design_tools.py \
  tests/test_debug_prepare_cli.py tests/test_prompts.py tests/test_readme_hooks.py \
  tests/test_version.py -q -rs -k 'not browser'
ruff check src/ tests/
git diff --check
python -m pip wheel --no-deps . -w /workspace/scratch/otter-vivado-experiment-wheels
```

定向回归 **266 passed / 3 deselected**；3 项是未改网页对应的浏览器测试，不记为通过。
随后补系统时间回拨的刷新边界并重跑实验/MCP/共享服务子集 **45 passed**；更新 Prompt
后其子集 **6 passed**，与上述覆盖重叠，不相加。覆盖本地人工确认、暂停剩余时间、
完成步骤不重放、接受硬件操作后中止/日志故障仍保留共享回执、身份变化与选目标保护、
失联/关闭、等待采集晚到/超时、重做新目录/ILA 未停止拒绝、实际 MCP 导出链路。
Ruff、diff、文档/Skill 链接与 wheel 模块检查通过；未改 Tcl、会话传输或 web/。
没有真实板卡、Vivado、Windows、消费者浏览器/声音设备验证。completed 只是计划步骤
结束，不代表有效采样、物理动作完成或硬件 PASS；下一项接续工程控件语义。

## 当前工作：第 3 项采样分析与智能体取证（2026-10-03）

从干净 main `91e932d9b919cb3bbfce426e0b34e38530c1f2a7` 接续（上一轮采样导出提交）。
用户要求继续，沿用插件能力边界。本节随本次实现提交保存，不发布包、不操作真实设备。

- 新增 `analyze_ila_capture` MCP 与 `ila-analyze` CLI，纯离线分析；共享 VCD 解析器
  在一次扫描中统计所选信号，不拼首屏分页或重新实现另一套解析规则。
- 统计赋值/变化/边沿数、已知/未知 tick 时长、宽整数无符号范围；按消费者显式规则检查
  allowed_values、state_transitions、stable_while_stalled。全部标识符和语义由工程声明，
  不猜协议、状态编码、时钟相位或复位。ILA 未导出有效时钟边沿时，规则检查不能伪造通过。
- 输入与规则指纹、窗口、原始二进制前后值及反例 tick 可追溯；observed、consistent、
  inconclusive、violated、blocked 分开。同刻时钟歧义、未知值、复位不跨接历史；尾部不
  外推，不把零有效检查或等待未完成当成通过。证据例子可截断，计数完整；资源超限阻断。
- 新增 [分析接口说明](docs/ILA_ANALYSIS.md) 与
  [分析 Skill 参考](skills/otter-vivado/references/analysis.md)，串联假设、映射、规则、
  证据、下一轮取证意图和既有共享服务内的已授权操作；保留旧记录，不覆盖规则制造 PASS。
  更新既有 ILA Prompt，使采集优先走共享 revision/控制权与回执，加入实际分析入口。
- 建议是不可执行的取证意图；智能体仍须核对 VCD id 到真实 hw_probe 的映射和触发能力。
  没有增加自动设备写入、固定分析网页或完整 AXI/UART/SPI 解码器。根因解释依赖消费者
  工程语义，规则一致仅指观察范围，不代替真实硬件配对、完整采集或时序签核。

验证环境 Linux / Python 3.12.14 / MCP SDK 2.3.0，VCD 为合成样本，EDA 命令为明确替身。

```bash
source .venv/bin/activate
pytest tests/analysis/test_ila_analysis.py tests/analysis/test_ila_waveform.py \
  tests/test_ila_analysis_cli.py tests/test_ila_capture.py tests/test_debug_backend.py \
  tests/test_debug_service.py tests/test_debug_bundle.py tests/test_debug_artifacts_cli.py \
  tests/test_debug_design_tools.py tests/test_debug_prepare_cli.py tests/test_prompts.py \
  tests/test_readme_hooks.py tests/test_version.py -q -rs
ruff check src/ tests/
git diff --check
python -m pip wheel --no-deps . -w /workspace/scratch/otter-vivado-analysis-wheels
```

定向回归 **244 passed**，无跳过；随后更新 ILA Prompt 与对应断言，重跑该子集 **6 passed**
（包含已有 Prompt 测试，不相加为独立覆盖数）。分析/波形/MCP/CLI 首批与边界子集
**77 passed** 已包含在上述回归中。覆盖宽值与大 tick、前/后取值、同刻更新行序、复位、
未知/丢失时钟、状态历史间隙、背压释放时的保持义务、无适用规则、尾部 pending、
坏文件/坏描述/指纹变化、有限反例与超事件预算，以及现有导出至读取和共享服务入口。
Ruff、diff、文档/Skill 链接、CLI help、wheel 模块检查通过；未改 Tcl/会话传输或 web/。
没有真实 Vivado VCD、商业 EDA、Windows 或板卡验证，复杂协议和实际诊断仍按使用反馈
在消费者工程处理。下一项继续本地实验编排，不扩大成本去铺全版本验证矩阵。

## 当前工作：第 2 项采样导出与波形数据（2026-10-03）

用户要求继续下一项，沿用已明确的插件边界：接口、数据与生成方法放库内，页面由智能体
在消费者工程生成。从干净 main `234a4ddbe8f0ae55f24b69ffb2627fa58beea502` 接续；
这是上一轮检查点交付的实现提交。本节随本次实现提交保存，不发布包、不操作真实设备。

- `debug_action(export_ila)` 复用同一 DebugService 控制权、revision、busy、目标和
  UUID 检查。完整单窗口上传与 VCD 导出在同次 Tcl execute 中连续执行，使用该次上传
  返回对象，不自动 arm/停止/烧录，不以旧对象冒充本次采集。
- 消费者新目录保存 attempt、原始 VCD、成功 manifest 和 result；包含版本、核身份、
  配置触发位置、采样数、文件 SHA256。部分失败/超时/取消保留 unknown，不覆盖/重放。
  30 秒等待超时后仍需核对 Vivado；本地记录不证明 bit/ltx 配对或实际物理采样周期。
- `read_ila_waveform` MCP 与 `ila-waveform` CLI 纯离线读取有限数字 VCD，提供别名信号
  目录、窗口前保持值、含边界窗口和事件分页；宽总线、x/z、大 tick 都用字符串。
  同刻多次变化保留顺序，expected_sha256 防止不同文件分页拼接，不抽点或假造初值。
  文件/目录/位宽/响应有界；不支持的实数、字符串、dump 开关等明确阻断。
- 新增 [波形接口说明](docs/ILA_WAVEFORM.md) 和
  [波形生成 Skill 参考](skills/otter-vivado/references/waveform.md)，更新入口与 README。
  既有参考页 HTTP 仍不接受任意导出路径；消费者自定义实时适配需共享同一服务。
  本批没有修改 web/、绘制演示页面或冒充用户板卡数据。

验证为 Linux / Python 3.12.14 / MCP SDK 2.3.0，真实 Tcl 解释器，EDA 命令为明确替身。

```bash
source .venv/bin/activate
pytest tests/analysis/test_ila_waveform.py tests/test_ila_capture.py \
  tests/test_debug_backend.py tests/test_debug_service.py tests/test_debug_http.py \
  tests/test_debug_bundle.py tests/test_debug_artifacts_cli.py tests/test_debug_design_tools.py \
  tests/test_debug_prepare_cli.py tests/test_protocol_regression.py tests/test_session.py \
  tests/test_probe_then_attach.py tests/test_tcl_utils.py tests/test_prompts.py \
  tests/test_readme_hooks.py tests/test_version.py -q -rs -k 'not browser'
ruff check src/ tests/
git diff --check
python -m pip wheel --no-deps . -w /workspace/scratch/otter-vivado-waveform-wheels
```

定向回归 **372 passed / 6 skipped / 3 deselected**；6 项是已有 Windows-only 测试，
3 项是未改页面对应的浏览器检查。随后补输出大小防护、散列移出事件循环，重跑读取器、
导出后端和真实 MCP 的受影响子集 **86 passed**。不是相加的独立覆盖数。
覆盖实际 MCP 导出至离线读取链路、共享控制权、原生 Tcl 引号、未完成/UUID 变更拒绝、
部分导出、取消/超时不重放、分页/指纹变化和坏数据范围外也拒绝。
Ruff、diff、本地文档链接与 wheel 模块检查通过；没有实际 EDA、Windows、板卡或
真实 Vivado VCD 样本，因此仅软件流程完成，现场命令/属性/输出格式需按使用反馈核对。
自动分析、协议/状态机推断、下一轮触发策略属于下一项，没有在本批宣称实现。

## 当前工作：第 1 项调试构建交付与插件边界（2026-10-03）

用户要求按剩余项目顺序推进，并明确：本仓作为插件，提供“告诉智能体怎么做可视化”
的能力，最终页面由智能体为消费者工程生成。该偏好优先于之前把内置面板当成产品主界面
的表述。库提供操作接口、共享状态、数据与 Skill 指引；项目页面、布局、主题和业务
控件语义留在消费者目录。已有网页保留为可选参考，不破坏现有使用者；本批未改 web/。

从 main `6b5c2c615868cd6a37ffc63820a45ae4a07505e1` 接续，工作树初始干净。
第 1 项按插件/智能体工作流完成软件层接续，不重写现有综合实现后端：

- Skill 的 `debug-delivery.md` 串联已有工程准备、run、报告和导出入口；明确确认目标
  routed DCP、实际 part/version/run 及源码来源，不取“目录最新文件”代替核对。
- 新增 `export_debug_bundle` MCP、`debug-export` CLI 与共享 Python 实现。检查点复制到
  消费者新目录后，在没有打开工程/设计的专用会话中，单次 execute 连续生成 bit、ltx
  和 timing/utilization/DRC 报告；不关闭/切换原 GUI，不启动综合或实现，不烧板。
- 保存 attempt、result、仅成功生成的 manifest；记录实际 Vivado 返回版本/设计/part、
  检查点和全部产物 SHA256。源码提交作为 declared_source_revision，不冒充已验证来源。
  部分失败保留文件；超时/取消/协议损坏为 unknown，不重放，不自动结束 Vivado。
- 离线核对新增 manifest_path / --manifest，检查固定全包文件和传入 bit/ltx 的指纹。
  可整体搬迁目录；缺失/改过检查点或报告同样阻断。record_matches 是本地记录一致，
  pairing/hardware_verified/timing_signoff 仍保留未验证，不使用本地清单作不可伪造认证。
- 新增 `visualization.md`，说明页面生成、字段来源、静态/实时集成、单一共享服务、
  revision/控制权/操作回执、宽整数和未知数据展示。当前内置服务器不能直接加载任意
  HTML，自定义实时页面需明确适配层；没有把文档指引说成已交付任意页面加载器。

验证环境 Linux / Python 3.12.14 / MCP SDK 2.3.0，真实 Tcl（Vivado 命令为测试桩）。

```bash
source .venv/bin/activate
pytest tests/test_debug_bundle.py tests/analysis/test_debug_artifacts.py \
  tests/analysis/test_bit_header_parser.py tests/analysis/test_ltx_parser.py \
  tests/test_debug_artifacts_cli.py tests/test_debug_design_tools.py \
  tests/test_debug_prepare_cli.py tests/test_debug_design.py tests/test_debug_ip.py \
  tests/test_debug_service.py tests/test_prompts.py tests/test_protocol_regression.py \
  tests/test_session.py tests/test_probe_then_attach.py tests/test_tcl_utils.py \
  tests/test_version.py -q -rs
ruff check src/ tests/
git diff --check
python -m pip wheel --no-deps . -w /workspace/scratch/otter-vivado-export-wheels
```

结果 **493 passed / 6 skipped**。跳过为已有 Windows-only 会话测试；未记为通过。
覆盖空会话保护、错误器件、现有文件、中文/空格/$/[] 路径、同次 Tcl 调用顺序、
stdio sentinel 包装、部分导出、未知回执/取消不重放、清单篡改/搬迁、真实 MCP 注册调用
（42 工具）、CLI attach-only/断开和离线核对。Ruff、diff、Skill/文档链接通过；
实际 wheel 含导出模块与 Tcl 模板，CLI help 正常。不需要浏览器测试，本批未改页面。

官方 UG835 链接留在指南；本轮 HTTP 请求仅取得门户 HTML，未取得命令正文，执行前
需用实际 Vivado 的 help 核对选项。没有商业 Vivado、真实 routed DCP、Windows 或板卡。
因此第 1 项是软件实现/流程已接通，实机验收待补；不能宣称真实构建或板卡配对 PASS。
后续按顺序推进采样导出与波形数据能力，再做分析、实验流程、控件语义与 ILA 停止。

## 当前工作：调试产物离线核对（2026-10-03）

用户报告原云环境可能过期，并要求继续优化。新环境从正式 origin/main `df1da99`
恢复；本对话没有读取旧聊天正文，以仓库任务记录为接续依据。当前环境不含商业 Vivado
和板卡，先完成可独立验证的产物核对，不把它宣称为自动构建或硬件配对闭环。

本批完成：

- 新增 `check_debug_artifacts` MCP 与 `debug-artifacts` CLI，共用纯 Python 核对实现。
  核对 bit 声明/实际载荷长度、目标器件/封装、ILA/VIO 精确实例名、探针位宽/方向/端口，
  可选核对 LTX UUID；返回两份文件 SHA256，供消费者保存证据。
- `consistent/incomplete/blocked` 区分离线一致、证据不足、明确不匹配。`pairing`
  始终 `unverified`，不从相同名称、目录、时间或 LTX UUID 推断 bit 内核身份。
- `.bit` 从全量读入/10 MiB 上限改为有界头部和分块散列，新增载荷完整性字段，
  保留缺段解析能力；检查重复/未知头部字段和读取期间可检测的文件变化。
- LTX 保留 UUID、VIO 分类、端口与子网信息；支持 BOM、两种下标方向，缺位宽返回未知。
  拒绝损坏 UTF-8/数组结构/重复 JSON 字段，不再静默替换名称或默认为一位。
- CLI 退出码 0/2/1 对应上述三个状态；MCP 通过后台线程读取文件。README、示例和
  [产物指南](docs/DEBUG_ARTIFACTS.md) 已接到工程准备与硬件调试流程。包版本未变、未发布。

验证：Linux / Python 3.12.14 / MCP SDK 2.3.0，合成 bit/ltx，无 EDA API 调用。

```bash
source .venv/bin/activate
pytest tests/analysis/test_bit_header_parser.py tests/analysis/test_ltx_parser.py \
  tests/analysis/test_debug_artifacts.py tests/test_debug_artifacts_cli.py \
  tests/test_debug_design_tools.py tests/test_debug_prepare_cli.py tests/test_debug_cli.py \
  tests/test_prompts.py tests/test_version.py tests/test_readme_hooks.py -q -rs
ruff check src/ tests/
git diff --check
python -m pip wheel --no-deps . -w /workspace/scratch/otter-vivado-wheels
```

最终 **158 passed**，无跳过；包含 12 MiB 合成 bit 分块读取、载荷截断/多余字节、
封装不符、重复 UUID/名称、探针缺失/方向/位宽、读取时替换文件、CLI 退出码与真实 MCP
注册调用（41 工具）。首次 README hook 用例因子进程未使用 venv 失败，激活 venv 后
同一完整定向集合通过；未修改 hook 行为。Ruff、diff、相关文档链接通过，实际 wheel
含新增模块；文档 CLI 样例返回 consistent/unverified/false，与证据边界一致。

下一步保持真实 IP/RTL/约束和构建来源核对优先。XML LTX 仍未支持，多个探针集重名
保守阻断；不自动选集。没有 bit 内部 UUID/CRC 解析、自动同次构建导出或板卡配对认证。
没有 Windows/Vivado/硬件实测，不要求扩成全版本矩阵；有实际工程反馈后定向修复。
本批按现有贡献约定保存到 main；后续接续读取 origin/main 最新 HEAD。

## 当前工作：调试工程准备（2026-10-03）

从 main `acbe7ffcac4e89eed6111acb8d61d6e3b91fe874` 接续。用户确认继续逐步实施，并询问
专用界面的视觉职责：配色、布局和风格交给以后生成页面的智能体与消费者项目；本库
提供可靠接口、控件绑定语义、状态反馈和基本可用性。当前 JSON 仍只支持滑杆/开关，
不能把未来任意布局、曲线与实验功能说成已经可用。

- 新增 `plan_debug_design / inspect_debug_design / prepare_debug_design` 三个 MCP 工具，
  加上原有入口共 40 个；`debug-prepare --spec` 提供独立 CLI。默认离线生成，
  `--output-dir` 只创建消费者新文件；`--apply` 连接已有 GUI，退出不关闭 Vivado。
- `ila_ip/vio_ip` 生成结构化配置与 RTL 例化模板，按实际已安装 IP 定义创建；创建后
  检查 CONFIG 属性并读回，再生成 IP 输出产物。预检不承诺 part/配置已兼容，created
  不表示 RTL 已集成。VIO 宽初始值以十六进制字符串保持精度。
- `ila_netlist` 生成实现阶段的 unmanaged Tcl 插核约束；所有 net 与同名核在首次
  变更前检查，每个 probe 从 bit 0 明确连接。`mark_debug` 单独生成综合保留约束，
  不能恢复已经优化掉的信号。ASCII 文件用 UTF-8 十六进制还原特殊 net 名称。
- 在线约束准备仅在当前工程 `otter_debug` 创建并注册专用文件，按目标 run 的
  CONSTRSET 设置 USED_IN 与 LATE，返回所有共用该约束集的 run。不会执行生成约束、
  打开其它 design 或启动构建；constraints_added 不表示插核或连接已验证。
- 应用重新核对预检返回的 name/directory/part，拒绝工程切换、同名文件/IP、活动 run
  与符号链接目标。部分失败保留现场；传输或回执未知时不自动重放、不报告回滚。
- Skill 与 [工程准备指南](docs/DEBUG_DESIGN.md) 说明分步操作、消费者文件归属、RTL
  职责和后续构建验证。没有改其它 Otter 产品或原生运行观察器。

依据：官方 UG835/UG912/UG903 2022.2 与 PG159 VIO；特别使用约束集中的 unmanaged
`.tcl` 承载检查，不把完整 Tcl 控制流写成普通 XDC。未取得 PG172 正文，不声称已依据它
认证所有 ILA 参数；本地描述范围仍由实际 IP 属性配置和真实 run 最终验证。独立评审
发现并修复了规范化路径后才检查链接导致的目录逃逸，并加入已有/悬空链接回归。

验证环境 Linux / Python 3.12.14；真实 Tcl 解释器内的 Vivado 对象为测试桩。执行：

```bash
.venv/bin/pytest tests/test_debug_design.py tests/test_debug_ip.py \
  tests/test_debug_prepare_cli.py tests/test_debug_backend.py tests/test_debug_service.py \
  tests/test_debug_http.py tests/test_debug_cli.py tests/test_prompts.py \
  tests/test_tcl_utils.py tests/test_session.py tests/test_session_encoding.py \
  tests/test_probe_then_attach.py -q -rs
.venv/bin/pytest tests/test_debug_design_tools.py -q
.venv/bin/ruff check src/ tests/
git diff --check
uv build --wheel --out-dir /tmp/otter-vivado-design-wheel
```

主批 **459 passed / 8 skipped**；独立真实 MCP 注册/调用 **12 passed**，核对 40 工具。
跳过均需 Windows 或 CP936；未将其记为通过。覆盖特殊名称/位序、缺失/歧义 net 在
变更前失败、工程切换、文件冲突、共享约束集、部分应用、协议失败和既有调试/会话回归。
CLI 实际导出三份产物并拒绝重复覆盖；Ruff、diff、本地文档链接、wheel 新模块通过。
实现已进入 main：[09d155f](https://github.com/otter-fpga-lab/otter-vivado/commit/09d155f6861e288ef00c0d13088d5234c9e5cd70)。
18 个 blob 和完整 tree 与本地验证版本一致；本交接记录随后更新，接续取最新 HEAD。
没有商业 Vivado、Windows 或板卡实测。

接下来优先闭合真实连接、构建、bit/ltx 配对；再做采样导出/波形与分析、本地倒计时/
动作提示，以及更多项目控件。业务参数协议、CDC 和 ISP 算法继续由消费者 RTL 设计验证。

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

首批实现已进入 `main`：[`328185d`](https://github.com/otter-fpga-lab/otter-vivado/commit/328185dfdf4e61fc7280e586b14ba3f2423c6206)。
通过现有 GitHub 连接写入，20 个 blob 和完整 tree 与本地验证版本逐项一致；后续取最新 main。

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
