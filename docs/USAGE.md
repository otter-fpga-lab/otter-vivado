# Vivado 使用指南

首次安装与更新见 [客户端接入](SOURCE_CONNECTION.md)；日常入口见 [README](../README.md)。
本页保留会话、工具参数用途和历史示例，参数以当前 MCP schema 与实际安装的 Vivado 帮助为准。
客户端安装、Python/模拟测试、真实 EDA 和板卡结果分别记录在 [TASK](../TASK.md)。

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
可交付工程应能由人用原生 GUI 重新打开，交付内容和验证步骤见 [GUI 指南](HUMAN_GUI_WORKFLOW.md)。

## 特性

- **三种会话模式**：GUI 可视化、Tcl 无头运行，以及只连接现有 GUI 的 attach
- **50 个 MCP 工具** — 覆盖 Vivado 执行、运行观察、诊断、离线解析和外部检查工具联动
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

### 源指南

| 工具 | 说明 |
|---|---|
| `vivado_guide` | 只读取得当前正式源 Skill、源根与同源 CLI；按需读取 remote、remote-tcl 和白名单 reference |

### 会话管理
| 工具 | 说明 |
|------|------|
| `start_session` | 启动 Vivado 会话（gui/tcl/attach 三种模式） |
| `stop_session` | 结束指定会话；会清理其拥有的 Vivado 子进程，先完成该会话的活动运行 |
| `list_sessions` | 列出所有活跃会话 |

### Tcl 执行（核心）
| 工具 | 说明 |
|------|------|
| `run_tcl` | 执行当前会话的 Vivado Tcl；按任务明确工程和动作范围 |
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

插件提供操作接口、状态/报告数据和 [可视化生成 Skill](../skills/otter-vivado/references/visualization.md)。
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

首批面向原生 Hardware Manager 已打开、已加载匹配探针的设备。人工与 AI 共用调试状态和操作协议；现有页面为可选参考，CLI/MCP 也可独立使用。用法、合成演示、边界和后续能力见 [硬件调试指南](HARDWARE_DEBUG.md)。

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
构建并核对 bit/ltx，见 [工程准备指南](DEBUG_DESIGN.md)。
采样分析与下一轮取证使用 [分析能力与边界](ILA_ANALYSIS.md)，按消费者声明解释数据。

同一实现 DCP 的交付使用 [调试导出流程](DEBUG_BUNDLE.md)，不必加载任何网页。

### 新手引导 & 工程摸底
| 工具 | 说明 |
|------|------|
| `get_next_suggestion` | **0.3.2** 11 档决策表:没项目 → open/create,没顶层 → set_property TOP,综合完成 → run_implementation...每档附可执行命令 |
| `get_project_info` | **0.3.0** 一次拿齐项目摸底:名称/part/顶层/源文件/XDC/IP/runs 状态 |
| `get_pre_commit_summary` | **0.3.4** 生成 markdown 工程摘要直接贴 commit body:项目/时序 WNS+WHS/资源/CW/READY-WARN-BLOCK 门禁 |

### 诊断
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

这是一份可选的 Claude Code 项目配置，不随仓库或 Python 包分发。保留现有
`.claude/settings.json`，只合并需要的 hooks；不是使用本产品的前置步骤。
下面的 `command` 使用单行 `python -c`，其中 `python` 应指向本仓 editable 环境的
解释器；需要时改为该解释器的绝对路径，并按实际宿主的命令环境核对引用。
`bitstream-guard` 是使用者主动选择的确认提示，其余检查范围如下：

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
            "command": "python -c \"import sys,glob; sys.stderr.reconfigure(encoding='utf-8'); pids=glob.glob('vivado_pid*.str'); pids and (sys.stderr.write('[session-guard] 发现未清理的 Vivado session 痕迹: '+', '.join(pids)+'。请结合 list_sessions 和进程归属核对；痕迹文件本身不证明会话仍存活，不据此停止活动会话或删锁。'+chr(10)), sys.exit(2))\""
          }
        ]
      }
    ]
  }
}
```

</details>

合并后按当前 Claude Code 的项目配置/信任机制加载；本页不把示例当作已安装配置。
禁用时移除自己添加的 hook，保留其他已有配置，按当前宿主规则重新加载。

## 使用示例 — 一轮完整的调试闭环

以下保留上游 Vivado 2019.1 的历史使用片段：Basys 3 演示 IO/DRC 修复，另一个 Kintex-7 PCIe XDMA 工程演示时序诊断。它们不是本轮 EDA/板卡验证，也不是可直接套用的工程参数；修改约束和设备操作应使用当前任务的实际目标。

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

该次差分显示三条旧 ERROR 已消除；整体是否通过仍核对本次运行、DRC 和时序结果。

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

工具分类和 Tcl 建议是诊断线索。上述路径的 skew 也占主要延迟，应核对实际时钟与约束，再决定是否采用高扇出处理；不能按分类自动修改工程。

### 烧板前安全门 + 编程设备

`check_bitstream_readiness` 与 `generate_bitstream` 的 CW 检查是不同结果。历史片段的
`READY` 不自动允许 `force=True`；先解释实际 CW，再按当前目标与动作授权生成和配置设备。

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

## CLI 参考

| 命令 | 说明 |
|---|---|
| `python -m vivado_mcp` | 启动 MCP server（供 AI 工具调用） |
| `vivado-mcp serve` | 同上 |
| `vivado-mcp install [path] [--port 9999]` | 注入 Vivado_init.tcl |
| `vivado-mcp uninstall [path]` | 从 Vivado_init.tcl 移除 |
| `vivado-mcp doctor [path] [--port 9999] [--json]` | 只读检查环境与连接 |
| `vivado-mcp doctor --fix [--client all\|claude-code\|codex]` | 备份后修复可安全自动处理的配置 |
| `vivado-mcp connect --client <目标客户端> [--check]` | 接入所选客户端的源 MCP 与 Skill 链接；`--check` 仅核对。`all` 含四端，Codex 原生插件已启用时不能同时选 Codex |
| `vivado-mcp debug --port 9999` | 连接已有 GUI，打开 ILA/VIO 调试面板 |
| `vivado-mcp debug --demo --panel examples/debug/panel.json` | 可交互的合成演示，不连接 EDA 或板卡 |
| `vivado-mcp monitor --port 9999 --run impl_1 --target route_design` | 只观察指定已有 GUI 的 run；详见 [运行观察](RUN_MONITOR.md) |
| `vivado-mcp version` | 显示 Otter Python 包版本 |
| `vivado-mcp versions [--json]` | 只读列出本机 Vivado 安装候选与默认选择，不启动 EDA；详见 [版本兼容](VERSION_COMPATIBILITY.md) |
| `vivado-mcp plugin --client <codex、antigravity 或 workbuddy> --output <新绝对目录>` | 只生成所选宿主的同源插件壳；安装由宿主另行完成 |
| `vivado-mcp remote <命令>` | 远程工程/原生 Tcl 构建、队列、状态、交付与离线报告；详见 [远程构建](REMOTE_BUILD.md) |
