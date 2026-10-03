# 从实现检查点导出调试交付包

`export_debug_bundle` 将明确的实现 DCP 复制到一个新的消费者目录，在专用空 Vivado
会话里打开该副本，通过**同一次 Tcl 执行**生成 `.bit/.ltx` 和三份报告，最后保存
文件 SHA256 与导出来源。CLI/MCP/Python 共用实现，不需要任何页面。

本功能补充已有综合/实现能力。先按 [工程准备](DEBUG_DESIGN.md) 接好 RTL/约束，
用现有构建工具完成实现，确认检查点属于目标 run 且没有过期；不要用导出来绕过失败的
实现。导出期间也不允许其他 GUI/脚本操作这一个专用会话。

## MCP 使用顺序

1. 保留原工程会话，通过当前工程/报告和实现 run 目录确认目标 DCP、完整 part、
   实际 Vivado 版本和源码提交。多份候选时明确选择，不猜最新文件。
2. 用同一个 Vivado 安装创建独立空会话，例如 `start_session` 的 Tcl 模式，或
   `gui` 模式并显式 `port=0`。具体参数以已加载的 MCP schema 为准。
3. 调用：

```text
export_debug_bundle(
  checkpoint_path="/project/project.runs/impl_1/top_routed.dcp",
  output_dir="/project/debug/delivery-001",
  expected_part="xc7k325tffg900-2",
  source_revision="实际消费者源码提交",
  session_id="debug-export"
)
```

上面是调用示意，替换成已核实的路径和对象。`output_dir` 的父目录须存在，目标目录
必须不存在，已有目录/文件/链接全部拒绝。MCP、Python 和 Vivado 必须共享本机文件系统。

导出会拒绝已打开工程或设计的会话，不会执行 close_project、close_design、reset_runs
或关闭原 GUI。独立会话在完成后保留打开的检查点，方便检查；确认空闲后按正常会话
生命周期清理。该保护不是跨 GUI/其他进程的全局锁。

## 独立 CLI

已有专用空 GUI 及其协议端口时：

```bash
python -m vivado_mcp debug-export --checkpoint /project/top_routed.dcp --output-dir /project/debug/delivery-001 --part xc7k325tffg900-2 --port 10003 --source-revision 实际源码提交
```

只 attach，不自动启动/替换 Vivado。CLI 结束只断开本次连接；不会关闭 GUI、取消导出
或删除文件。`--timeout` 默认 1800 秒，范围 1~7200，超时只表示等候结束。
**超时后 Vivado 仍可能正在执行**；不要直接重放，不要为了重试杀掉原进程。
`exported` 退出 0，其余退出 1。

## 目录与证据

| 文件 | 含义 |
| --- | --- |
| `attempt.json` | 执行前持久化的尝试 ID、路径、参数和待确认状态 |
| `source.dcp` | 导出实际打开的检查点副本，复制时核对稳定性与 SHA256 |
| `design.bit` / `design.ltx` | 同一会话、同一打开设计连续生成的产物 |
| `timing.rpt` / `utilization.rpt` / `drc.rpt` | 该设计导出的报告原文；不自动给签核结论 |
| `manifest.json` | 仅完整成功回执及离线核对后生成的来源记录与全包 SHA256 |
| `result.json` | 最终已知结果，含失败阶段或未知原因；写入失败会返回 receipt_error |

导出依次调用 `open_checkpoint`、`write_bitstream`、`write_debug_probes`、
`report_timing_summary -file`、`report_utilization -file`、`report_drc -file`。
不使用 `-force`；每个文件须非空，并核对实际 part 和当前设计。bit 长度、LTX 结构和
器件离线检查失败时不生成完整清单。没有提供预期核/探针清单的初次检查仍为 incomplete；
随后使用消费者独立要求进一步核对。

```bash
python -m vivado_mcp debug-artifacts --bit /project/debug/delivery-001/design.bit --ltx /project/debug/delivery-001/design.ltx --expected /project/debug/expected.json --manifest /project/debug/delivery-001/manifest.json
```

`bundle.status=record_matches` 表示全包文件与本地记录匹配。允许整体搬迁交付目录，
不根据清单里的任意路径访问文件；缺失或被修改的检查点/报告也会阻断核对。

`provenance=recorded_same_checkpoint_export` 表示工具记录了从一个检查点连续导出的过程。
清单可编辑，不是不可伪造的认证；`source_revision` 是调用方声明，工具没有证明源码与
DCP 对应。`pairing=unverified`、`hardware_verified=false`、`timing_signoff=unverified`
保持独立，需审阅报告并按 [硬件流程](HARDWARE_DEBUG.md) 补实机证据。

## 失败与兼容性

- `blocked`：专用会话非空、输出冲突等导出前置失败。目录中仍可能已有检查点副本。
- `partial`：已开始打开检查点/导出，或本地核对/记录失败。保留已有文件，不声称回滚。
- `unknown`：超时、取消、Tcl 会话错误或回执损坏。查看原会话与输出目录，不自动重试。

XML LTX、未识别格式、特定器件只使用 PDI 等情况需要针对实际版本适配，本入口当前
只处理 `.bit/.ltx`。相关官方命令参考为 AMD UG835 的
[open_checkpoint](https://docs.amd.com/r/2022.2-English/ug835-vivado-tcl-commands/open_checkpoint)、
[write_bitstream](https://docs.amd.com/r/2022.2-English/ug835-vivado-tcl-commands/write_bitstream)、
[write_debug_probes](https://docs.amd.com/r/2022.2-English/ug835-vivado-tcl-commands/write_debug_probes)。
实际执行前以本机 `help <command>` 核对受支持选项。本地验证为真实 Tcl + Vivado API
测试桩，不能作为商业 Vivado/Windows/板卡 PASS。
