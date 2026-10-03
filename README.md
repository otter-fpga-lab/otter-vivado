# Otter Vivado — Vivado 工具与人机共用工作流

Otter Vivado 专注 **AMD Vivado 工具能力**：工程管理、Tcl 执行、综合/实现、仿真、IP、
真实运行状态与报告，以及原生 GUI 操作交接。RTL 设计与编码由 `otter-rtl-coding` 等上层
产品负责，本产品为它们和人类提供可独立使用的执行与验证入口。

以 **Windows 本机使用**为主要场景，兼顾 Linux。默认从 `main` 接续；
**[一次目录链接接入 Cursor、Claude Code、Codex 与 Antigravity](docs/SOURCE_CONNECTION.md)**，
**[原生 Vivado GUI 工程与交付](docs/HUMAN_GUI_WORKFLOW.md)**，以及
**[真实进度面板、报告浏览与可运行样例](docs/RUN_MONITOR.md)** 各有具体步骤。
Skill、CLI/MCP 和 UI 共用一份实现。AMD Ross 与 Otter 并列可选，两者均可独立使用；
参考依据见 [Ross 对照](skills/otter-vivado/references/ross.md)。开发接续见 [TASK.md](TASK.md)。

本仓是 NJ 的 [vivado-mcp](https://github.com/mapleleavessssssss-wq/vivado-mcp) 的保留历史
fork，沿用原作者、Apache-2.0 和上游贡献关系。Python 包名与 CLI 保持 `vivado-mcp`；
PyPI 上的同名包是上游发行版，Otter 功能按本仓源码入口安装。

[![Upstream PyPI](https://img.shields.io/pypi/v/vivado-mcp?label=upstream%20PyPI)](https://pypi.org/project/vivado-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/vivado-mcp)](https://pypi.org/project/vivado-mcp/)
[![License](https://img.shields.io/github/license/otter-fpga-lab/otter-vivado)](LICENSE)
[![CI](https://github.com/otter-fpga-lab/otter-vivado/actions/workflows/ci.yml/badge.svg)](https://github.com/otter-fpga-lab/otter-vivado/actions/workflows/ci.yml)

**人和智能体操作同一份 Vivado 工程，观察同一次运行。**

49 个 MCP 工具覆盖会话、综合、实现、真实进度、时序、XDC、IP、波形与烧录；其余 Vivado Tcl 能力由 `run_tcl`/`safe_tcl` 承载。原生 Vivado GUI 用于工程、Block Design、原理图与波形操作，本机网页提供只读运行观察，以及独立的 [ILA/VIO 调试面板](docs/HARDWARE_DEBUG.md)。调试支持已有硬件目标，并提供 [ILA/VIO 工程准备](docs/DEBUG_DESIGN.md)；并支持 [构建产物离线核对](docs/DEBUG_ARTIFACTS.md)。现支持 [ILA 采样导出与离线波形数据](docs/ILA_WAVEFORM.md)，智能体据此在消费者工程生成页面；现提供 [本地实验运行器与生成指引](docs/DEBUG_EXPERIMENT.md)，以及 [工程控件语义与预设](docs/DEBUG_CONTROLS.md)。

| 49 个 MCP 工具 | 8 个工作流 Prompt | 2 个会话 Resources | GUI / Tcl / attach 三种会话 |
|---:|---:|---:|---:|

> 本项目控制的是**你本机安装的 Vivado**，不是云端综合服务。命令在当前用户权限下执行；工具说明和诊断建议以中文为主。
>
> **English:** Otter Vivado provides local AMD Vivado project, execution, simulation, IP, status, and report tools for humans and agents. It includes 49 MCP tools, 8 workflow prompts, native GUI/headless/attach sessions, and a shared read-only run monitor. RTL authoring belongs to the separate coding product.

**导航**：[快速开始](#快速开始) · [原生 GUI 与交付](docs/HUMAN_GUI_WORKFLOW.md) · [工具职责](#工具职责) · [工作流 Prompts](#工作流-prompts) · [工具列表](#工具列表) · [会话模式](#会话模式) · [架构](#架构) · [CLI](#cli-参考) · [反馈](#反馈与-bug-提交)

## 环境要求

- **Python ≥ 3.10**，Windows / Linux
- **Xilinx Vivado**：必须安装在运行 vivado-mcp 的本机
- **MCP Python SDK 2.x**：`pip` 自动安装；Python 3.10 另需自动安装的 `tomli`

Windows 是使用与兼容维护重点；当前云端自动化通过不等于商业 Vivado、Windows GUI 或板卡现场已通过。实际验证范围与待测步骤见 [TASK.md](TASK.md) 和 [GUI 指南](docs/HUMAN_GUI_WORKFLOW.md)。

下表是维护重点与已有证据，不是可用版本白名单。其他版本也可使用相同入口；是否可用取决于
该次任务需要的 Tcl 命令、器件/IP 与报告格式。遇到具体差异再定向处理，无需先跑完整版本矩阵。

| Vivado 版本 | 维护重点 | 已有证据 | 相关现场检查 |
|---|---|---|---|
| **2018.3** | **优先维护，常用版本** | 本轮核对该版官方 Tcl/安装文档；历史 [上游 PR #1](https://github.com/mapleleavessssssss-wq/vivado-mcp/pull/1) 仅验证 IPDEF-only IP 元数据 | Windows GUI/Tcl/attach、构建、报告、原生工程重开 |
| **2024.2** | **优先维护，常用版本** | 本轮核对该版官方 Tcl/安装/分析文档，未运行商业 EDA | 同上，兼顾不同器件的资源报告名称与数值 |
| 2020.2 | 持续兼容 | 本轮核对该版官方核心 Tcl 命令与选项，未运行商业 EDA | 按同一最小工程定向复测 |
| 2022.2 | 持续兼容 | 本轮核对官方 Tcl；历史 [上游 Issue #2](https://github.com/mapleleavessssssss-wq/vivado-mcp/issues/2) 为 Windows 10 GUI/XSim 局部现场记录 | 按同一最小工程定向复测，历史记录不代表完整通过 |
| 2019.1 | 保留上游历史基线 | 原作者长期使用 GUI/Tcl/attach 与 FPGA 工具流程 | 保留回归，不用历史结果替代上述四版本验收 |
| 其他版本 | 同一套入口，不设版本白名单 | 以实际使用结果为准 | 遇到差异时核对本机帮助和受影响能力 |

支持目标与实际通过记录分开维护；选版本、官方依据与验收范围见
[版本兼容说明](docs/VERSION_COMPATIBILITY.md)。使用旧版工程不要求升级到新版 Vivado。

## 快速开始

### 1. 安装

按 [源码接入指南](docs/SOURCE_CONNECTION.md) 从本仓 `main` 安装一次，并为需要的客户端建立 Skill 目录链接及源码 MCP 配置。使用同一个 Python 环境执行下文命令；无需安装 PyPI 上游包覆盖源码接入。

### 2. 先运行环境诊断

```bash
vivado-mcp doctor
```

`doctor` 默认完全只读，检查 Vivado 路径、init Tcl 注入、9999 端口协议、Claude Code/Codex 配置，并给出精确的修复计划。CI 或 Agent 可使用结构化输出：

```bash
vivado-mcp doctor --json
```

### 3. 选择原生 GUI 的连接方式

`start_session(mode="gui", port=0)` 可启动独立 GUI，并通过 `-source` 加载协议。
如需 attach 到以后手动打开的 GUI，才按 [GUI 指南](docs/HUMAN_GUI_WORKFLOW.md) 显式配置指定安装的 init Tcl：

```bash
vivado-mcp install
```

这会修改你 Vivado 的 `Vivado_init.tcl`，让以后启动 GUI 时自动开启 TCP server（绑定 install 指定的单一端口，默认 9999；被占即退出，不会滑动到其他端口）。**原文件会备份**，`vivado-mcp uninstall` 可恢复。

如果 Vivado 装在受保护目录（如 `C:\Program Files\`），用管理员身份运行命令即可。

也可以让 doctor 执行安全修复：

```bash
vivado-mcp doctor --fix --client all
```

`--fix` 才会写文件：复用幂等的 Vivado 注入，并在备份后原子更新选定客户端配置；不会删除第三方注入、终止占用端口的进程或自动升级软件。

### 4. MCP 手动配置（可选）

已运行 `connect` 时可跳过本节。保留手工维护配置时，Claude Code 使用 `~/.claude.json`，Cursor 使用项目级 `.cursor/mcp.json` 或用户级 `~/.cursor/mcp.json`；两者都在 `mcpServers` 中加入，并将 `command` 改为源码环境解释器的绝对路径：

```json
"vivado": {
  "command": "python",
  "args": ["-m", "vivado_mcp"],
  "env": {
    "VIVADO_PATH": "D:/Xilinx/Vivado/2019.1/bin/vivado.bat"
  },
  "type": "stdio"
}
```

Codex 使用 `~/.codex/config.toml`：

```toml
[mcp_servers.vivado]
command = "python"
args = ["-m", "vivado_mcp"]

[mcp_servers.vivado.env]
VIVADO_PATH = "D:/Xilinx/Vivado/2019.1/bin/vivado.bat"
```

> 将 `VIVADO_PATH` 替换为你的 Vivado 实际路径：
> - **Windows**: `"D:/Xilinx/Vivado/2019.1/bin/vivado.bat"`
> - **Linux**: `"/opt/Xilinx/Vivado/<版本>/bin/vivado"`
> - 也可以不设置 `VIVADO_PATH`，将 Vivado `bin` 目录加入系统 `PATH`。
>
> `VIVADO_PATH` 负责让 MCP server 找到 Vivado 可执行文件；上一步的 `vivado-mcp install` 负责给 GUI/attach 模式注入 TCP server。其他支持 stdio MCP 的客户端使用相同的 `command`、`args` 和 `env`，配置文件位置以客户端文档为准。

### 5. 在空闲边界加载客户端配置

首次配置后按客户端要求加载 MCP，可发现 49 个工具、8 个工作流 Prompt 和 2 个会话状态 Resource。已有活动构建时保留其 MCP 会话，完成后再重载；结束拥有 Vivado 子进程的 MCP 会关闭该进程。

### 6. 冒烟验证

在客户端中发送：

```text
启动一个 GUI 会话，然后执行 Tcl: version -short
```

AI 应依次调用 `start_session(mode="gui")` 和 `run_tcl("version -short")`。成功时 Vivado GUI 会启动（已有注入服务则直接 attach），并返回版本号。失败时直接运行 `vivado-mcp doctor`，无需逐项猜配置。

<details>
<summary>向原上游贡献时的源码入口</summary>

```bash
git clone https://github.com/mapleleavessssssss-wq/vivado-mcp.git
cd vivado-mcp
pip install -e ".[dev]"
```
</details>

各版本的完整变更和迁移说明见 [CHANGELOG](CHANGELOG.md)。

## 工具职责

本产品提供 Vivado 执行、查询和可视化能力。专用工具集中处理需要解析或跨命令协调的工作：

1. **结构化解析**：IO / 时序报告 → JSON / 中文摘要（比原始表格省 token）
2. **本地知识库**：CRITICAL WARNING 按 ID 分类 + 中文修复建议（Tcl 里写这个太难）
3. **跨命令协议**：sentinel、会话管理、超时、比特流前置安全检查
4. **跨会话工具**：`compare_xci` 纯 Python 对比两个 XCI 文件，不需要 Vivado
5. **共享运行观察**：人和智能体读取同一缓存中的真实状态、日志与候选报告。

BD、仿真、硬件调试和 IP 配置可通过 `run_tcl`/`safe_tcl` 使用当前版本支持的 Vivado Tcl。
工具参数以 MCP schema 为准，版本差异先核对本机官方帮助。已有 RTL/XDC 检查器保留为验证能力；
涉及 RTL 设计或功能修改时将诊断证据交给 Coding 工作流，避免在 Vivado 工具层复制编码产品。
可交付工程应能由人用原生 GUI 重新打开，交付内容和验证步骤见 [GUI 指南](docs/HUMAN_GUI_WORKFLOW.md)。

## 特性

- **三种会话模式**：GUI 可视化、Tcl 无头运行，以及只连接现有 GUI 的 attach
- **49 个 MCP 工具** — 覆盖 Vivado 执行、运行观察、诊断、离线解析和外部检查工具联动
- **8 个证据驱动工作流** — 每个流程都要求新鲜基线、最小安全修改、复测门禁与明确停止条件
- **一条命令自检** — `doctor` 只读定位环境问题，`doctor --fix` 才执行受限、可备份的修复
- **可靠的长任务协议** — 综合/实现支持 `wait=False` 立即返回 job id，再由 `get_run_progress` 查询
- **超时响应不串台** — 每个 session 保留在途响应所有权；旧响应完成前拒绝下一命令，不会把 FIRST 的结果交给 SECOND
- **智能诊断** — 综合/实现后自动提取 CRITICAL WARNING / ERROR 分类 + 中文修复建议（含 18+ 种已知 ID）
- **IO 验证** — XDC 约束（**支持 -dict 和传统两种语法**）对比实际引脚分配，GT 端口不匹配标记为 CRITICAL
- **IP 调试** — 查询 IP 所有 CONFIG.* 参数（含 GUI 隐藏参数）、纯 Python 对比两个 XCI 文件
- **Bitstream 安全检查** — 生成比特流前自动检测 CRITICAL WARNING 并阻止（可 force 跳过）
- **结构化报告** — IO 和时序报告解析为 JSON，便于 AI 精确提取数值（**不再有"假 PASS"陷阱**）
- **安全转义** — `safe_tcl` 自动对路径/标识符做 Tcl list 转义，Windows 含空格/中文/$ 的路径也能用
- **多会话支持** — 默认复用端口 9999 的单个 GUI（不同 session_id 也 attach 同一台）；传 `port=0` 自动分配空闲端口启动独立实例；server 只绑单一端口，被占即退出不滑动

## 工作流 Prompts

Prompts 解决的是“按什么顺序做、什么证据才算完成”，不会增加工具数量。正文只在选择该 Prompt 时加载，不会全部常驻上下文。

| Prompt | 用途 | 核心门禁 |
|---|---|---|
| `fpga_workflow` | RTL 到 bitstream 的完整流程 | 上游失败不进入下游，post-route signoff 后才写 bitstream |
| `debug_timing` | setup/hold 时序收敛 | baseline → 分类 → 最小修复 → 同指标复测，禁止假 false path |
| `debug_gt_mapping` | GT 引脚与 Lane 映射 | 原理图/XDC/实际布局三方证据一致后再改约束 |
| `debug_ip_config` | IP 参数与 XCI 漂移 | golden 来源可信、修改后 regenerate + synthesis 验证 |
| `debug_pcie` | PCIe 分层排查 | 物理 → 时钟复位 → 时序 → 协议，上一层未过不下钻 |
| `simulation_bringup` | XSim 编译、运行与失败分类 | compile 不等于 pass；必须有非零测试和新鲜运行结果 |
| `cdc_audit` | CDC crossing 审计 | 不用 waiver 隐藏真实 crossing，约束必须有结构证据 |
| `ila_hardware_debug` | ILA 插入、烧录与采波 | bit/ltx 配对、明确 JTAG target、有限等待，禁止全机 kill XSim |

## 会话模式

`start_session` 工具支持三种模式：

| mode | 效果 | 适合 |
|---|---|---|
| `"gui"` (默认) | **先 probe 端口**(0.3.19+):已有 vmcp server 直接 attach,没有才 spawn `vivado -mode gui` | 交互开发、实时观察波形/原理图;**支持复用你手动开的 GUI**(只要装过 `vivado-mcp install`) |
| `"tcl"` | `vivado -mode tcl` 无头子进程 | CI、批处理、不需要 GUI |
| `"attach"` | 只 attach,不 spawn(端口无 server 时直接报错) | 严格保证不会启新 GUI 进程的场景 |

```
用户: 启动 GUI 会话
AI: [调用 start_session(mode="gui")]
    → 端口空 → spawn 新 Vivado;端口已有 → attach 到现有 GUI(0.3.19+)
    
用户: 我刚自己手动开了 Vivado GUI,直接接管
AI: [调用 list_sessions]   → 看到 <external@9999>(你手动开的)
    [调用 start_session(mode="gui")]   → 自动 attach,不会再开第二个 GUI

用户: 批处理跑 10 个项目
AI: [调用 start_session(mode="tcl")] → 无 GUI,跑得更快
```

长时间综合/实现可以启动后立即返回，不占住一次 MCP 调用：

```text
run_synthesis(run_name="synth_1", session_id="default", wait=False)
→ 综合已异步启动。job_id: default:synth_1

get_run_progress(run_name="synth_1", session_id="default")
→ STATUS / PROGRESS / 当前 phase / log tail / 最后更新时间
```

`job_id` 是由 `session_id:run_name` 组成的任务回执；查询时将两部分分别传给 `get_run_progress`。默认 `wait=True` 保持原有“等待完成并自动诊断”的兼容行为。每个 session 的命令严格串行，不同 session 可并行。

### Resources

- `vivado://sessions`：当前所有会话的结构化状态
- `vivado://session/{session_id}/status`：指定会话的状态、模式、端口与存活信息

## 工具列表

### 会话管理
| 工具 | 说明 |
|------|------|
| `start_session` | 启动 Vivado 会话（gui/tcl/attach 三种模式） |
| `stop_session` | 关闭指定会话(B13 修复:taskkill /T 递归杀进程树 + 清 vivado_pid*.str) |
| `list_sessions` | 列出所有活跃会话 |

### Tcl 执行（核心）
| 工具 | 说明 |
|------|------|
| `run_tcl` | 执行任意 Vivado Tcl 命令——**AI 拼命令的主力** |
| `safe_tcl` | 带参数模板，自动 Tcl 转义，路径含空格/中文/$ 时使用 |

### 设计流程
| 工具 | 说明 |
|------|------|
| `run_synthesis` | 运行综合，Python 轮询不阻塞，完成后自动 open_run + 诊断 |
| `run_implementation` | 运行实现（布局布线） |
| `get_run_progress` | 查询指定 run 的原生状态、百分比、最近观测阶段与日志尾部；日志更新时间不能单独证明运行卡住 |
| `generate_bitstream` | 生成比特流（默认前置 CRITICAL WARNING 安全检查） |
| `program_device` | 编程 FPGA 设备（封装 open_hw_manager → connect → program） |

### 人机共用运行观察

| 工具 | 说明 |
|------|------|
| `open_run_monitor` | 返回本机只读面板 URL，观察指定 run/目标阶段，不启动构建 |
| `get_run_snapshot` | 与面板共用状态缓存，运行与报告独立标注采样状态、时间和来源 |
| `close_run_monitor` | 释放选定观察器、页面与缓存，保留 Vivado 会话和正在执行的 run |

### 为消费者工程生成可视化

插件提供操作接口、状态/报告数据和 [可视化生成 Skill](skills/otter-vivado/references/visualization.md)。
智能体按具体工程和用户偏好生成页面，保存到消费者工程中；布局、样式与项目语义不写死在库里。
已有运行/调试网页是可选参考实现，使用 CLI、MCP 或 Python 能力不要求采用这些页面。
当前固定页面服务器尚不能直接加载任意消费者 HTML，生成的实时页面需有明确的本机适配层。

### ILA/VIO 板上调试
| 工具 | 说明 |
|------|------|
| `open_debug_panel` | 返回现有会话的本机调试页面，可加载消费者工程的滑杆/开关描述 |
| `get_debug_snapshot` | 读取共享设备、探针、控制权与操作结果缓存 |
| `debug_action` | 明确选择目标后配置 ILA、启动/上传/导出采集或写 VIO；短操作返回可查询回执 |
| `resolve_debug_controls` | 离线验证工程控件、精确换算、读回解释与有序预设预览 |
| `write_debug_control` | 按声明与指纹写单个 VIO，共享版本/控制权并核对读回 |
| `create_debug_experiment` | 创建消费者本地实验计划，固定设备身份与新记录目录 |
| `get_debug_experiment` | 读取本地步骤、倒计时和事件，不查询 EDA |
| `debug_experiment_action` | 启动、暂停、恢复、中止、标记和显式重做，不代替本地人工就绪 |
| `close_debug_panel` | 中止本地实验后续步骤并释放服务/轮询，不停止 ILA 或关闭 Vivado |

首批面向原生 Hardware Manager 已打开、已加载匹配探针的设备。人工与 AI 共用调试状态和操作协议；现有页面为可选参考，CLI/MCP 也可独立使用。用法、合成演示、边界和后续能力见 [硬件调试指南](docs/HARDWARE_DEBUG.md)。

### ILA/VIO 工程准备

| 工具 | 说明 |
|------|------|
| `plan_debug_design` | 离线生成 IP 创建、RTL 例化片段或精确网表约束 |
| `inspect_debug_design` | 只读核对工程身份、run/约束集或已安装 IP 定义 |
| `prepare_debug_design` | 创建并配置 IP，或注册专用调试约束；保留部分失败现场 |
| `check_debug_artifacts` | 离线核对 bit/ltx 长度、器件、ILA/VIO 核和探针；不把元数据一致当作硬件配对通过 |
| `analyze_ila_capture` | 离线统计采样并检查显式状态/握手规则，返回证据和下一轮取证意图 |
| `read_ila_waveform` | 离线读取数字 VCD，返回精确信号值、时间窗口和分页事件，供消费者生成波形页面 |
| `export_debug_bundle` | 从专用空会话中的实现检查点导出 bit/ltx、报告和 SHA256 来源清单 |

人工入口 `python -m vivado_mcp debug-prepare --spec design.json`；默认只生成计划。
`--output-dir` 保存消费者文件，`--apply` 准备已有 GUI 工程。创建产物之后仍需接入 RTL、
构建并核对 bit/ltx，见 [工程准备指南](docs/DEBUG_DESIGN.md)。
采样分析与下一轮取证使用 [分析能力与边界](docs/ILA_ANALYSIS.md)，按消费者声明解释数据。

同一实现 DCP 的交付使用 [调试导出流程](docs/DEBUG_BUNDLE.md)，不必加载任何网页。

### 新手引导 & 工程摸底
| 工具 | 说明 |
|------|------|
| `get_next_suggestion` | **0.3.2** 11 档决策表:没项目 → open/create,没顶层 → set_property TOP,综合完成 → run_implementation...每档附可执行命令 |
| `get_project_info` | **0.3.0** 一次拿齐项目摸底:名称/part/顶层/源文件/XDC/IP/runs 状态 |
| `get_pre_commit_summary` | **0.3.4** 生成 markdown 工程摘要直接贴 commit body:项目/时序 WNS+WHS/资源/CW/READY-WARN-BLOCK 门禁 |

### 诊断(独家差异化)
| 工具 | 说明 |
|------|------|
| `get_critical_warnings` | 提取并按 ID 分类 CRITICAL WARNING + ERROR,含 18+ 种已知 ID 的中文修复建议。**0.3.9** 加 `compare_with_last=True` 差分。**0.3.14** errors=0+cw=0 但 STATUS=ERROR 时 tail runme.log 扫非标关键词(TclStackFree/segfault/中文路径 cmd 报错)。**0.3.15/16** `run_name='sim_*'` 时:先 glob xsim/*.log;全空就自动 `launch_simulation -scripts_only` + Vivado session 内 `exec` 跑 compile/elaborate.bat 抓真错 |
| `check_bitstream_readiness` | **0.3.0** 烧板前一键 READY/WARN/BLOCK 综合判定 |
| `verify_io_placement_tool` | 对比 XDC 约束（-dict/传统两种语法）与实际 IO 布局，GT 不匹配标为 CRITICAL |
| `xdc_lint` | **0.3.0** 纯 Python 静态 XDC 检查(PIN_CONFLICT / 漏 IOSTANDARD / DUPLICATE_PORT / CLOCK_NO_PERIOD / 跨文件冲突),不需 Vivado |
| `xdc_auto_fix` | **0.3.3** 自动补 IOSTANDARD + create_clock -period,dry_run 预览 + 板卡 profile(basys3/nexys-a7/arty-a7/zybo/kc705),不碰 PIN_CONFLICT |
| `verilog_compile_check` | **0.3.4** 用 iverilog / verilator 做语法 + 连接性检查,通常远快于完整 Vivado 综合。未装返回 SKIP + 安装指引,支持 Windows+scoop 路径自动发现 |

### IP 调试
| 工具 | 说明 |
|------|------|
| `inspect_ip_params` | 查询 IP 实例所有 CONFIG.* 参数（含 GUI 隐藏项），支持关键词过滤 |
| `compare_xci` | 纯 Python 对比两个 XCI 文件的参数差异（无需 Vivado 会话） |
| `get_ip_status` | **0.3.4** 检查哪些 IP 需要升级 / 被锁定 / 已最新,附 upgrade_ip 批量建议 |

### 离线摸底（无需 Vivado 会话，纯 Python）
| 工具 | 说明 |
|------|------|
| `parse_xpr` | **0.3.23** 离线解析 .xpr 工程文件——不启 Vivado 秒级拿 part/顶层/源文件(按 fileset 分组,含 .v/.mem/.xci IP)/XDC/runs。对照 `get_project_info` 需先 open_project(中文路径会 TclStackFree 崩) |
| `parse_bit_header` | **0.3.23** 离线解析 .bit 头部——设计名/part(原始 `7k325tffg900` + 规整 `xc7k325tffg900`)/构建日期时间/SHA256。烧前防错板 + 交付对账,Vivado 无 Tcl 命令读离线 .bit |
| `parse_ltx` | **0.3.23** 离线解析 .ltx ILA 探针清单——probe 名/位宽/映射 net。连板抓波前先拿清单,`get_hw_probes` 需活 hw session |

### 结构化报告
| 工具 | 说明 |
|------|------|
| `get_io_report` | IO 引脚报告（JSON），自动判定 GT/GPIO 类型 |
| `get_timing_report` | 时序报告,含 PASS/FAIL 判定、**数据来源标注**(post-synth 估算 vs post-route 最终)、关键路径详情。**0.3.9** 违例时自动附 Top N 违例路径 + 5 种模式分类(CDC/HIGH_FANOUT/LONG_COMBO/IO_UNREGISTERED/UNKNOWN)+ 具体 Tcl 修复命令 |
| `get_utilization_report` | **0.3.0** 结构化资源占用(LUT/FF/BRAM/DSP/IOB),> 90% 标 CRITICAL,70-90% 标 WARN |

> 通用报告（power / drc / clock / methodology / cdc 等）请直接用
> `run_tcl("report_power -return_string")`，无需包装。

### 波形显示(XSim)
| 工具 | 说明 |
|------|------|
| `set_wave_zoom` | **0.3.22** 设置波形时间缩放窗:改 .wcfg XML → close -force → open 重载(Vivado 2019.1 无 Tcl zoom 命令,跨命令协议封装) |
| `set_wave_analog` | **0.3.22** 把信号设为 Analog 模拟显示:自动补 STYLE_ 前缀 + 全路径/显示名寻址 + 空对象判空(三个实测静默坑一次封掉)。注意先 zoom 后 analog(重载会冲掉 analog 设置) |

## 可选:Claude Code Hook 配置示例

> **注意**:`.claude/` 目录不随仓库 / PyPI 包分发,下面是一份**可选**的 hook 配置示例,
> 复制到你自己项目的 `.claude/settings.json` 即可启用。hook 里 import 的
> `vivado_mcp.analysis` 模块随 `pip install vivado-mcp` 一起装好,无需额外脚本。
> 所有 hook 命令均为**单行** `python -c`(分号串联)——Windows cmd 与 bash 下都能直接执行,
> 多行 `python -c` 在 cmd 下会 SyntaxError 静默失效,自行改写时请保持单行。

配好后 AI 不只会"被动应答",还能**主动守门**:

| Hook | 触发事件 | 作用 |
|---|---|---|
| `bitstream-guard` | AI 调 `generate_bitstream` 前 | 弹确认框(permissionDecision: ask):提醒先跑 `check_bitstream_readiness`,由你决定放行或拒绝,不会硬阻断流程 |
| `xdc-lint` | 保存任意 `.xdc` 文件后 | 纯 Python 静态检查:PIN_CONFLICT / 漏 IOSTANDARD / create_clock 缺 -period 等,无需等综合 |
| `verilog-lint` | 保存任意 `.v` / `.sv` 文件后 | 零依赖预检:module 名匹配文件名 / endmodule 存在 / 括号配对 |
| `iverilog-check` | 保存任意 `.v` / `.sv` 文件后 | **0.3.4** iverilog 或 verilator 语法+连接性检查,未装静默跳过,有 error 时阻断 |
| `session-guard` | Claude 停下时 | 扫 `vivado_pid*.str` 文件,提醒清理未关闭的 Vivado session |

<details>
<summary>可复制的 settings.json 片段(点击展开)</summary>

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__vivado__generate_bitstream",
        "hooks": [
          {
            "type": "command",
            "statusMessage": "bitstream-guard",
            "command": "python -c \"import json; print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'ask', 'permissionDecisionReason': '烧板前确认已跑 check_bitstream_readiness 且结论为 READY(时序违例/未布线状态下生成的是无效比特流)'}}))\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "statusMessage": "xdc-lint",
            "command": "python -c \"import json,sys; sys.stderr.reconfigure(encoding='utf-8'); d=json.load(sys.stdin); fp=d.get('tool_input',{}).get('file_path') or d.get('tool_response',{}).get('filePath') or ''; fp.lower().endswith('.xdc') or sys.exit(0); from vivado_mcp.analysis.xdc_linter import lint_xdc_files, format_lint_report; r=lint_xdc_files([fp]); r.issues and (sys.stderr.write('[xdc-lint hook] '+format_lint_report(r)+chr(10)), sys.exit(2))\""
          },
          {
            "type": "command",
            "statusMessage": "verilog-lint",
            "command": "python -c \"import json,sys; sys.stderr.reconfigure(encoding='utf-8'); d=json.load(sys.stdin); fp=d.get('tool_input',{}).get('file_path') or d.get('tool_response',{}).get('filePath') or ''; fp.lower().endswith(('.v','.sv')) or sys.exit(0); from vivado_mcp.analysis.verilog_quick_check import quick_check_verilog, format_report; t=format_report(quick_check_verilog(fp)); t and (sys.stderr.write('[verilog-lint hook] '+t+chr(10)), sys.exit(2))\""
          },
          {
            "type": "command",
            "statusMessage": "iverilog-check",
            "command": "python -c \"import json,sys; sys.stderr.reconfigure(encoding='utf-8'); d=json.load(sys.stdin); fp=d.get('tool_input',{}).get('file_path') or d.get('tool_response',{}).get('filePath') or ''; fp.lower().endswith(('.v','.sv')) or sys.exit(0); from vivado_mcp.analysis.verilog_compile_check import compile_check, format_compile_report; r=compile_check([fp], tool='auto', timeout=15.0); r.tool_available or sys.exit(0); r.errors and (sys.stderr.write('[iverilog-check hook] '+format_compile_report(r)+chr(10)), sys.exit(2))\""
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "statusMessage": "session-guard",
            "command": "python -c \"import sys,glob; sys.stderr.reconfigure(encoding='utf-8'); pids=glob.glob('vivado_pid*.str'); pids and (sys.stderr.write('[session-guard] 发现未清理的 Vivado session 痕迹: '+', '.join(pids)+'。建议用 mcp__vivado__stop_session 清理,或手动删除 .str 文件。'+chr(10)), sys.exit(2))\""
          }
        ]
      }
    ]
  }
}
```

</details>

把片段写入你项目的 `.claude/settings.json` 后,Claude Code 打开该项目时会弹框:*"检测到项目配置了 hook,是否信任?"* — 选 **Yes** 即启用。

要禁用单个或全部:在 `.claude/settings.local.json`(个人本地文件,不进 git)写入 `{"hooks": {}}` 覆盖即可。

## 使用示例 — 一轮完整的调试闭环

下面是 Vivado 2019.1 的两组实机片段：Basys 3 工程用于演示 IO/DRC 修复，独立的 Kintex-7 PCIe XDMA 工程用于演示时序诊断。两组结果串起 `get_critical_warnings` → XDC 修复 → `compare_with_last` 验证 → 时序自动诊断 → 烧板的完整闭环。

### 起点:打开项目,立刻看 ERROR / CW 详情

```
用户: 打开 basys3_uart 工程看看为什么 impl 挂了
AI:  [start_session(mode="gui")]
     [run_tcl("open_project .../basys3_uart.xpr")]
     [get_critical_warnings(run_name="impl_1")]

     !! 发现 3 条 ERROR !!
     诊断概览: errors=3, critical_warnings=0, warnings=0

     --- [ERROR][DRC BIVC-1] IO_STANDARD_MISMATCH (1 条) ---
       首次出现: 第 165 行
       示例消息: Bank IO standard Vcc: Conflicting Vcc voltages in bank 14...
       建议: Bank 内 IOSTANDARD 不一致(同一 Bank 的端口用了不同电压,如 LVCMOS18 和 LVCMOS33)。
       修复: 在 XDC 给所有端口显式指定 IOSTANDARD,同 Bank 保持电平一致。

     --- [ERROR][Vivado_Tcl 4-23] DRC_FAILED (1 条) ---
       建议: DRC 失败导致 Placer 未运行。
       修复: 查看同一日志里前面的 [DRC xxx-N] 定位根因,常见是 BIVC-1/NSTD-1/UCIO-1。

     --- [ERROR][Common 17-39] STAGE_ABORT (1 条) ---
       建议: 前置阶段失败导致后续阶段未能启动。
```

诊断第一步就锁定根因:`BIVC-1` 在 `Bank 14` 有电压冲突,后两条 ERROR 都是连锁反应。

### 修 XDC + 再综合

```
用户: 按建议在 XDC 里给所有端口显式补 IOSTANDARD 再综合
AI:  [xdc_auto_fix(board="basys3", dry_run=false)]   # 0.3.3 一键补 IOSTANDARD
     [run_synthesis]
     [run_implementation]
```

### 验证真的修到了点 — `compare_with_last` 差分

```
用户: 确认一下修好了
AI:  [get_critical_warnings(run_name="impl_1", compare_with_last=True)]

     诊断概览: errors=0, critical_warnings=0, warnings=3

     === CW 差分报告(对比上次快照)===
     修复效果: 已消除 3 条 / 新出现 0 条 / 仍存在 0 条

     [-] 已消除(3):
       • [DRC BIVC-1] IO_STANDARD_MISMATCH
       • [Vivado_Tcl 4-23] DRC_FAILED
       • [Common 17-39] STAGE_ABORT

     结论: 修复生效,所有上次存在的问题均已消除。
```

不需要人工对比两份日志 —— 差分直接给结论:3 条 ERROR 都消了,**修复生效**。

### 时序违例?自动给 Tcl 修复命令(0.3.9 亮点)

换到一个 WNS 违例的大工程(PCIe XDMA):

```
用户: 时序没过,告诉我怎么改
AI:  [get_timing_report]

     === 时序摘要 === 状态: FAIL (时序违例)
       Setup  WNS = -9.247 ns  TNS = -495735.094 ns
              失败端点: 247261 / 247354
       Hold   WHS = +0.015 ns  THS = 0.000 ns

     --- 违例路径 Top 10 ---
       [1] SETUP slack -9.247 ns  [HIGH_FANOUT]
           起点: .../xdma_0/.../tready_thrtl_reg/C
           终点: .../pcie_7x_i/pcie_block_i/CFGPMTURNOFFOKN
           延迟分解: logic 0.309 ns  route 2.094 ns  skew -6.845 ns  levels=2
           建议: 布线延迟(2.094 ns)远大于逻辑延迟(0.309 ns),通常是高扇出或跨芯片走线。
                 先跑 `report_high_fanout_nets -fanout_greater_than 1000` 定位,
                 然后对主干寄存器加 `set_property MAX_FANOUT 50 [get_cells <...>]`,
                 或在综合阶段加 `-fanout_limit 50` 让工具自动复制寄存器。

       [2] SETUP slack -9.219 ns  [HIGH_FANOUT]
           ...
```

不再有"时序挂了,咋办"的空转 —— 工具直接按模式(CDC / HIGH_FANOUT / LONG_COMBO / IO_UNREGISTERED / UNKNOWN)分类,给出具体 Tcl 命令。

### 烧板前安全门 + 编程设备

```
用户: 修好后准备烧板
AI:  [check_bitstream_readiness]
     结论: READY — route_design Complete / WNS +0.026 ns / CW=8 (GT_LOC 都是 IP 内部,可忽略)

     [generate_bitstream]          # 自动拦截:有 CW 会阻止,可 force=True 跳过
     [program_device(bitstream_path="impl_1/top.bit")]
```

### 常用旁路工具

其他单次查询/对比用例:

```
# 对比两个 XCI 找出配置漂移
compare_xci(file_a="golden.xci", file_b="suspect.xci")
# → PF0_DEVICE_ID: A=9024 | B=9038
# → LINK_SPEED:    A=5.0_GT/s | B=8.0_GT/s

# 验证 GT 引脚实际布局是否和 XDC 一致(支持 -dict 语法)
verify_io_placement_tool
# → !!! CRITICAL 不匹配 !!!  端口: pcie_7x_mgt_rtl_0_rxp[0]
#    XDC: AA4 | 实际: M6

# 任意 Tcl — AI 拼命令的主力
run_tcl("foreach p [get_ports] { puts \"$p: [get_property PACKAGE_PIN $p]\" }")
safe_tcl("set_property PACKAGE_PIN {0} [get_ports {1}]", args=["W5", "clk"])
```

## 架构

```mermaid
flowchart LR
    Agent["Cursor / Claude Code / Codex / Antigravity"] -->|"stdio MCP"| MCP["vivado-mcp"]
    MCP --> Tools["49 Tools"]
    MCP --> Prompts["8 Workflow Prompts"]
    MCP --> Resources["2 Session Resources"]
    Tools --> Tcl["SubprocessSession\nmode=tcl"]
    Tools --> Gui["GuiSession\nmode=gui"]
    Tools --> Attach["GuiSession\nmode=attach"]
    Tcl -->|"stdio + UUID sentinel"| VivadoTcl["vivado -mode tcl"]
    Gui -->|"TCP length-prefix"| VivadoGui["local Vivado GUI"]
    Attach -->|"TCP length-prefix"| VivadoGui
    Tools --> Monitor["共享运行状态缓存"]
    Human["使用者"] --> VivadoGui
    Human --> View["本机只读面板"]
    View --> Monitor
```

**核心协议**：
- **subprocess 模式**：`catch + UUID sentinel`（stdio 分帧，修复了 0.1.0 的行顺序 bug）
- **GUI/attach 模式**：TCP length-prefix framing（4 字节 BE + UTF-8 payload）
- 命令通过十六进制编码传输，避免 Tcl 注入，并覆盖含空格、中文和特殊字符的路径
- 每个 session 同时只拥有一个在途响应；调用超时不会释放协议所有权，避免迟到响应污染下一条命令

## CLI 参考

| 命令 | 说明 |
|---|---|
| `python -m vivado_mcp` | 启动 MCP server（供 AI 工具调用） |
| `vivado-mcp serve` | 同上 |
| `vivado-mcp install [path] [--port 9999]` | 注入 Vivado_init.tcl |
| `vivado-mcp uninstall [path]` | 从 Vivado_init.tcl 移除 |
| `vivado-mcp doctor [path] [--port 9999] [--json]` | 只读检查环境与连接 |
| `vivado-mcp doctor --fix [--client all\|claude-code\|codex]` | 备份后修复可安全自动处理的配置 |
| `vivado-mcp connect --client all [--check]` | 一次接入四客户端的源码 MCP 与 Skill 目录链接；`--check` 仅核对 |
| `vivado-mcp debug --port 9999` | 连接已有 GUI，打开 ILA/VIO 调试面板 |
| `vivado-mcp debug --demo --panel examples/debug/panel.json` | 可交互的合成演示，不连接 EDA 或板卡 |
| `vivado-mcp monitor --port 9999 --run impl_1 --target route_design` | 只观察指定已有 GUI 的 run；详见 [运行观察](docs/RUN_MONITOR.md) |
| `vivado-mcp version` | 显示 Otter Python 包版本 |
| `vivado-mcp versions [--json]` | 只读列出本机 Vivado 安装候选与默认选择，不启动 EDA；详见 [版本兼容](docs/VERSION_COMPATIBILITY.md) |

## 开发

```bash
git clone -b main https://github.com/otter-fpga-lab/otter-vivado.git
cd otter-vivado
python -m pip install -e ".[dev]"

# 运行测试（不需要 Vivado）
pytest

# 代码检查
ruff check src/ tests/
```

## 反馈与 Bug 提交

### 通过 Code Agent 整理反馈

遇到问题时，可让客户端整理复现信息，再用于产品讨论。当前 fork 未开启 Issues，反馈入口为
[产品 PR #1](https://github.com/otter-fpga-lab/otter-vivado/pull/1) 的讨论，或 [TASK](TASK.md) 对应的 Hub 议题。

<details>
<summary>点击展开</summary>

````
我在使用 Otter Vivado (https://github.com/otter-fpga-lab/otter-vivado，Python 包名 vivado-mcp) 时遇到了问题。

请帮我整理一份可复现的反馈,按以下步骤操作:

1. 收集我的环境信息:
   - 操作系统: 运行 `[System.Environment]::OSVersion.VersionString`(PowerShell)或 `systeminfo | findstr /B /C:"OS"`(cmd)
   - Python 版本: 运行 `python --version`
   - vivado-mcp 版本: 运行 `vivado-mcp version`(或 `pip show vivado-mcp`)
   - Vivado 版本: 优先读取已有空闲会话的 `version -short`；忙时使用已有日志头，无法确认就记未知；`.xpr` 的 Project Version 是文件格式信息，不能当作实际运行版本
   - 当前 Vivado 进程: PowerShell 跑 `Get-Process | Where-Object { $_.ProcessName -like "*vivado*" }`
   - MCP 客户端类型(Claude Code / Cursor / Codex 等)及版本
   - 我使用的 Vivado 模式(`gui` / `tcl` / `attach`)

2. 询问我:
   - 期望的行为是什么
   - 实际发生了什么
   - 复现步骤(从 `start_session` 开始的完整工具调用序列)
   - 相关的工具输出 / 错误日志(优先已有 `get_run_snapshot`、原始错误或日志片段；收集反馈不重跑构建或仿真)

3. 输出用于产品 PR #1 讨论的 Markdown:
   - 标题: 简洁的问题概述,前缀可用 `[bug]` / `[feature]` / `[docs]`
   - 正文包含以下部分: **环境信息**、**问题描述**、**复现步骤**、**期望行为 vs 实际行为**、**相关日志**
   - 带上当前源码提交;涉及特定工具(如 `get_critical_warnings`)时写明工具名

仓库: otter-fpga-lab/otter-vivado
讨论: https://github.com/otter-fpga-lab/otter-vivado/pull/1
````

</details>

### 直接反馈

在 [产品 PR #1](https://github.com/otter-fpga-lab/otter-vivado/pull/1) 讨论中记录源码提交、
`vivado-mcp version`、Vivado 版本和复现步骤。Otter 产品反馈留在本产品/Hub 议题；
适合回馈原项目的通用修复，再按 [贡献指南](CONTRIBUTING.md) 整理为独立上游投稿。

## 文档

- [一次源码接入](docs/SOURCE_CONNECTION.md) — 四客户端共用源码与 Skill 目录链接
- [原生 GUI 工程与交付](docs/HUMAN_GUI_WORKFLOW.md) — Windows 操作入口与工程交接
- [运行观察](docs/RUN_MONITOR.md) — 真实进度、日志、报告与最小样例
- [CHANGELOG](CHANGELOG.md) — 版本变更历史
- [迁移指南 0.1 → 0.2](docs/MIGRATION_0.1_to_0.2.md) — 每个被删工具的 run_tcl/safe_tcl 替代
- [审计报告](docs/AUDIT_REPORT.md) — 0.1.0 的 7 个 bug 根因分析
- [IP 调试实践手册](docs/IP_DEBUG_GUIDE.md) — PCIe GT 映射调试、XCI 配置对比等实战
- [PITFALLS](PITFALLS.md) — MCP 物理上无法替你做的事(需手动操作)

## 许可证

[Apache License 2.0](LICENSE)
