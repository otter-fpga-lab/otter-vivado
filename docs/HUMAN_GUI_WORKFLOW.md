# 人和智能体共用原生 Vivado 工程

Otter Vivado 操作 Vivado 的工程、运行和报告。交付给人的是原生 `.xpr` 工程及其依赖，
可在 Vivado GUI 中继续查看、约束和构建；浏览器运行面板提供真实状态与报告索引。
使用 GUI 打开工程不需要 MCP、Skill 或 Ross 正在运行。

## 新工程的可运行入口

在本机 Vivado **空闲且未打开工程**的 GUI Tcl Console 中执行：

```tcl
set __otter_demo_dir {D:/vivado_work/otter-progress-demo-new}
set __otter_demo_part xc7a35tcpg236-1
source -encoding utf-8 {D:/source/otter-vivado/examples/progress/create_demo.tcl}
```

替换为本机实际路径和已安装、有许可的器件型号。输出目录必须尚不存在。
也可在本机已有空闲 MCP 会话通过 `run_tcl` 执行相同 Tcl；变量化路径含特殊字符时
优先用 `safe_tcl` 参数模板。Windows Tcl 路径推荐使用 `/` 和花括号，避免反斜杠转义。

脚本用 `create_project` 创建磁盘工程，以 `import_files` 将样例 `demo.v` 和 `demo.xdc`
导入新工程的 `.srcs`，设置 `top=demo`，输出 `VMCP_DEMO:project_file=...xpr`。
路径取自创建后的真实工程目录；以这条输出为准，GUI 与 Tcl 模式的目录布局可能不同。
`VMCP_DEMO_ERR:` 表示未完成；中途失败保留现场，不自动清除输出后重试。

这是独立消费工程的输入，创建后可在工程内编辑，不再依赖 Otter 工具仓库的位置。
Otter 的 Skill、CLI/MCP 和 UI 仍只有一份源码，不复制到工程内。
样例只有时钟约束，没有板卡引脚配置；创建动作不启动运行、不生成 bitstream、不操作设备。
综合和实现的真实观察入口见 [运行观察](RUN_MONITOR.md)。

## 人类接手与重新打开

优先在拥有该工程的**同一个 Vivado GUI 会话**接手，避免两个可写会话同时管理同一工程。
GUI 中可直接使用 Sources、Design Runs、Open Synthesized Design / Open Implemented Design
和 Reports；面板只观察选定 run，不替代 Vivado 的原理图、器件布局或时序路径视图。

需要重新打开时，先确认原会话及后台 runs 已正常结束。在同版 Vivado GUI 的
**File → Project → Open** 选择上述 `.xpr`；也可在一个空 GUI 会话的 Tcl Console 执行：

```tcl
open_project {D:/vivado_work/otter-progress-demo-new/otter_progress_demo.xpr}
```

示例路径只是说明，实际使用脚本输出或运行面板中已发现的 `.xpr` 路径。
面板确认文件存在不代表已验证工程依赖完整或 GUI 打开成功；未发现 `.xpr` 时会如实显示，
不会为 Non-Project / 内存设计自动创建或转换工程。单个 `.dcp` 可以用于查看设计结果，
但不能代替包含源、约束和运行配置的 `.xpr` 工程交付。

重新打开后，在 Sources 检查源文件和约束是否存在，在 Project Settings 核对 part/top，
在 Design Runs 核对同一 run 的状态，再打开已有报告。工程升级提示、缺失 IP/器件或许可问题
应保留具体版本和消息；不要把面板“已完成”当作跨版本兼容或时序签核证明。

## 已有工程和迁移

已有工程保持原来的源组织方式。`add_files` 引用共享 RTL/IP 是正常用法；工具不会自动
导入、搬动或批量重写用户工程。需要迁移到另一目录或另一台机器时，不要只拷贝 `.xpr`：
它可能仍引用外部 RTL、XDC、include 目录、IP 仓库或 Tcl hooks。

等待 runs 空闲后，在 Vivado 中显式执行 **File → Project → Archive**，或：

```tcl
archive_project {D:/handoff/my-project-new.zip}
```

目标 zip 必须未存在，不加 `-force`。AMD `archive_project` 会收集工程所需源、约束及运行结果；
需要归档的 `tcl.pre` / `tcl.post` 脚本应先作为工程源纳入。在新目录解压后，以目标机同版
Vivado 实际重开并检查依赖；外部许可证、器件安装和第三方 IP 可用性仍需目标机验证。
归档是用户明确需要迁移时产生的交付包，不是日常内容同步机制。

## Windows、版本与现场验收

Windows 是主要使用环境。先在 PowerShell 确认实际安装的 Vivado：

```powershell
& 'C:\AMD\Vivado\2026.1\bin\vivado.bat' -version
```

路径和版本仅为示例，旧版本常安装在 `C:\Xilinx\Vivado\...`。新工程优先使用短、纯 ASCII
路径如 `D:/vivado_work`；上游记录了 2019.x 在部分中文工程/运行目录中的故障，Python 或
Tcl 层能正确传递中文路径不表示具体 Vivado 版本支持全部路径组合。不要自动更改用户工程路径。

本轮在 Linux 使用真实 Tcl 解释器和文件 I/O 验证输入导入、空格/中文/方括号路径的参数边界、
不覆盖已有目录、不关闭已有工程和错误留痕；Vivado API 是测试桩，**没有真实 EDA/GUI PASS**。
Windows 本机仍需按实际 Vivado 版本完成以下定向验收：

1. 创建样例后核对 `.xpr`、`.srcs/sources_1` 和 `.srcs/constrs_1`，记录实际版本/part。
2. 综合运行时打开浏览器面板，核对 GUI Design Runs 的同一 run；关闭面板不影响运行。
3. 运行结束并正常关闭工程后，临时移开工具仓库或将消费工程复制到新目录，再用 GUI 重开；
   检查无缺失源/约束，能打开综合结果。恢复工具仓库后 MCP 接入仍指向原来的唯一源。
4. 对带 IP、BD、include 和 Tcl hooks 的真实工程，显式归档到新文件，在新目录重开并核对依赖。

这些检查不需要板卡，也不占用 Coding 的现场功能测试。

## AMD 与 Ross 依据

2026-10-03 实际读取 AMD UG835 2026.1 的以下正文；运行时仍应以用户安装版本的 `help` 为准：

- [create_project](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/create_project)：
  默认写 `.xpr`；`-in_memory` 不产生磁盘工程；GUI 模式目录可能追加项目名。
- [import_files](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/import_files)：
  与 `add_files` 的引用行为不同，输入导入本地 `.srcs/<fileset>/imports`；路径默认相对优先。
- [current_project](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/current_project)：
  返回工程名而不是项目文件路径。
- [open_project](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/open_project) 与
  [archive_project](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/archive_project)：
  `.xpr` 打开入口与包含依赖的原生迁移方式。

AMD Ross [Vivado 工具参考（2cdc9eef）](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/docs/reference/vivado-mcp-tools.md)
同样区分 Tcl 执行、GUI 和状态/日志通道，但明确 `vivado_display` 的 VNC/X11 功能只支持 Linux。
本产品在 Windows 使用本机 Vivado GUI 和浏览器观察，不把 Ross 的 Linux 显示能力当作 Windows
兼容证据。Ross 与 Otter Vivado 并列可选，互不作为运行依赖。
