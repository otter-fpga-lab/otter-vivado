# 原生 Tcl 任务

`run-tcl` 直接执行输入闭包中的 Tcl，不生成 `.xpr`、不改写 XDC、不插入综合或实现步骤，也不要求 `.bit/.dcp`。适用于已有 Tcl／EDAM／OOC 流程。

共享目录、任务工作区、主机队列、日志、结果发布与清理和 `run` 共用。当前原生入口要求主机配置了共享目录。Codex 默认 workspace 为当前任务 ID；普通 PowerShell 需显式加 `--workspace <名称>`。

## 调用

```powershell
vivado-mcp remote run-tcl `
  --host example --workspace task-a --manifest "/path/to/prepared-ooc/job.json"
```

默认保留中间文件。支持 `--jobs/--output/--poll-seconds/--timeout/--remote-cleanup`；不接受 `--target/--reference`，原生流程的阶段、参数、增量策略由自己的 Tcl 决定。

## 输入清单

JSON 清单所在目录就是输入根目录。文件与目录使用相对路径，保留它们在根目录下的结构：

```json
{
  "schema_version": 1,
  "name": "scaler-ooc",
  "vivado_version": "2024.2",
  "entry": "tools/vivado_ooc.tcl",
  "arguments": ["case_config.tcl"],
  "inputs": ["case_config.tcl", "rtl", "constraints"],
  "required_outputs": ["r/result.txt", "r/ps_t.rpt"],
  "reports": {
    "timing": "r/ps_t.rpt",
    "check_timing": "r/ps_c.rpt"
  },
  "source_identity": {
    "repository": "otter-rtl-library",
    "git_commit": "<实际提交>",
    "case": "<实际 case ID>"
  }
}
```

- 必填：`schema_version/name/vivado_version/entry`。入口文件自动纳入输入闭包。
- `inputs`：额外文件或目录；目录递归复制，只包含明确列出的输入，不扫描或上传整个仓库。
- `arguments`：直接传给 Tcl 的 `argv`，支持空参数、空格、中文及 `$[]` 等字面文本，不做模板展开。
- `required_outputs`：相对结果目录的文件路径。进程返回零但缺少声明的输出时，交付失败并保留中间文件。可为空，例如只需要日志的命令检查。
- `reports`：可选的报告名称映射，支持 `timing/check_timing/drc/methodology/route/cdc/utilization`；保留仓库原有文件名。映射不会自动增加验收门槛，必须存在的报告应同时列入 `required_outputs`。
- `source_identity`：调用方提供的 Git、case 等身份信息，原样存入摘要；工具不为原生任务另算逐文件哈希，也不将这些声明冒充独立验证。

路径使用 `/`，不接受绝对路径、`..` 或链接输入。清单与源码放在工具管理的 `fpga-remote` 目录之外，避免随工作区清理。输入闭包应由仓库原有 EDAM／依赖解析生成；不要另外维护一份容易失真的手写全仓库列表。

## Tcl 执行环境

Vivado 从独立快照根目录启动，并提供：

| 环境变量 | 含义 |
|---|---|
| `FPGA_REMOTE_WORK_DIR` | 本次快照根目录 |
| `FPGA_REMOTE_ARTIFACT_DIR` | 本次需要保留的输出目录 |
| `FPGA_REMOTE_LOG_DIR` | 本次工具日志目录 |
| `FPGA_REMOTE_JOBS` | 调用方指定的 jobs 值，原生脚本自行消费 |
| `FPGA_REMOTE_FLOW` | `tcl` |

工具不会替脚本选择 `general.maxThreads`，不会注入 part/top、重置 run、重建 BD/IP 或插入 DCP。Tcl 的工作目录、文件读取和输出语义照常生效；脚本若主动 `cd`，后续相对路径由脚本负责。

生成可搬迁的配置时，可以这样设置路径：

```tcl
set rtl_repo_root $::env(FPGA_REMOTE_WORK_DIR)
set rtl_output_dir [file join $::env(FPGA_REMOTE_ARTIFACT_DIR) r]
set rtl_all_verilog_files [list [file join $rtl_repo_root rtl top.sv]]
set rtl_xdc_files [list [file join $rtl_repo_root constraints ooc.xdc]]
# part、top、parameter、clock、stage 等继续由原仓库 case 生成。
```

工具不会解析 Tcl 来猜依赖，也不会替换脚本中的 Windows 绝对路径。此类路径应在仓库生成配置时改为快照内相对路径或上述环境变量；这属于输入闭包搬迁，不涉及把 OOC 流程转换成 project mode。

## 结果与成功语义

结果保存在本工作区的 `results/<name>/<build-id>/`，包含原生 `artifacts/`、工具 `logs/`、输入清单 `native-job.json` 和 `remote-build-summary.json`。

- `flow=tcl`、`RUN_TARGET=script` 表示原生脚本执行。
- `RUN_RESULT=SUCCESS` 只表示进程返回零、声明的输出已交付且工具步骤完成。
- `RUN_QUALITY=NOT_EVALUATED`，`artifacts.stages` 为空；工具不推断综合、routed timing 或板级签核结论。
- 非零退出或缺少声明输出均返回失败，禁止自动清理。
- `--remote-cleanup purge` 只按上述执行与交付条件删除中间目录，保留报告和日志；它不表示仓库的质量门槛已运行。

调用方已有的功能、资源和时序验收继续按项目实际要求执行。需要先复核报告时保留默认 `keep`，完成相应工作后再选择清理；不要仅凭远端退出码改写项目的验证结论。

需要查看多个阶段或指定 DUT 的资源时，可直接使用 `report <handle> --instance <实例>` 只读复核已发布的 `.rpt`。它按每份报告的实际命令与 Design State 保留阶段边界，原生任务与项目任务均可使用；详见 [远程构建说明](REMOTE_BUILD.md)。

## 约束可见性

项目模式和原生模式均可在摘要的 `artifacts.constraints` 中看到 `no_clock`、内部未约束端点、缺少输入／输出 delay 的数量，以及报告是否明确说没有用户时序约束。

- `ISSUES_REPORTED`：报告明确列出缺失项，CLI 同时输出 `CONSTRAINT_WARNING`。
- `NO_ISSUES_REPORTED`：识别到的检查项未报告缺失，不等于所有约束已签核。
- `NOT_CHECKED`：没有可识别的报告证据。

因此“综合完成”和“约束缺失”可以同时报告；不会把所有无时钟设计自动判失败，也不会把缺失约束隐藏在 `SYNTH_PASS` 后面。

## 与 RTL 仓库现有入口的衔接

仓库准备阶段只需导出本 case 的源文件闭包和可搬迁配置，再声明原来的 `tools/vivado_ooc.tcl` 及参数。原 OOC 脚本、XDC 执行顺序、diagnostic 查询和资源判定继续由仓库维护。无需本地启动 Vivado 创建临时 `.xpr`，也无需为传输生成 `post_synth` hook、`write_xdc` 或替换 checkpoint。

工程按需要调用同一 `vivado-mcp remote run-tcl` 入口；产品不修改生产 RTL 或替工程建立第二套远端适配。
