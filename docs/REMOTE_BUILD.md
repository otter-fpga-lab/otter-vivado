# 远程 Vivado 构建

`vivado-mcp remote` 从本机发起 Linux Vivado 批处理，沿用原 `fpga-remote` 的工程快照、原生 Tcl 任务、主机排队、状态、报告和结果交付。它使用 `vivado_mcp.remote_build`，不启动本地 GUI/TCP 会话。当前没有远程构建 MCP 工具；智能体有命令执行能力时使用同一 CLI，不把 `run_tcl` 当作远程入口。

## 本机入口与配置

在产品私有 Python 环境安装本仓后，使用 `vivado-mcp remote --help`，或 `python -m vivado_mcp.remote_build --help`。Windows 未激活环境时可直接使用 `.venv\Scripts\vivado-mcp.exe`；不依赖固定 Python 版本的 PowerShell 包装器。客户端接入另行安排，运行 CLI 不要求先注册 Skill/MCP/Plugin。

源码安装默认读取产品根 `config/remote-hosts.local.json`，该文件被 Git 忽略。`--config <文件>` 优先于环境变量 `OTTER_VIVADO_REMOTE_CONFIG`，后者优先于默认路径。参考 [无私人信息的样例](../config/remote-hosts.example.json)，填入已确认的主机、工具及共享映射；样例不是可用主机配置。wheel 在另一位置使用时，显式指定自己的配置。主机配置不进入 wheel、源码提交或客户端受管缓存。

`inspect` 和 `report --results` 仅操作明确的本地输入，不需要主机配置、不调用 SSH/SCP。`hosts` 只读配置；`probe` 才查询远端。远程执行仍使用已有系统 `ssh/scp` 和调用方配置的 SSH alias，不读写私钥，不自动安装系统工具或修改 SSH 配置。

```text
vivado-mcp remote inspect --project /path/to/project --json
vivado-mcp remote report --results /path/to/results --instance top/dut
vivado-mcp remote --config /path/to/private-hosts.json run --host example --workspace task-a --project /path/to/project --target synth
vivado-mcp remote --config /path/to/private-hosts.json run-tcl --host example --workspace task-a --manifest /path/to/job.json
```

## 已有流程与交付

| 入口 | 实际作用 |
|---|---|
| `run` / `build` | `.xpr + .srcs` 独立快照；显式 `synth/route/bitstream`，可指定匹配的已交付 routed DCP 作为 reference |
| `run-tcl` | 按清单复制输入闭包并执行原生 Tcl，不转换为 project mode；见 [原生任务](REMOTE_TCL.md) |
| `status/logs/list` | 查询同一 handle 的排队、进程状态和日志；没有原生百分比就不推算总进度 |
| `fetch` / `report` | 共享结果或 SCP 包交付；只读报告按实际命令、Design State 和实例范围解析 |
| `clean/purge/prune/workspace-clean` | 既有范围检查、活动任务保护与清理语义；`workspace-clean` 默认预览，`--yes` 才删除 |

构建任务沿用 `--workspace`；未指定时可读现有 `CODEX_THREAD_ID`，其它环境显式给名称。主机队列一次运行一个构建，`--jobs` 是该构建的并行度。CLI 退出不取消已经脱离的 runner；同一任务的日志和交付仍用实际 handle 接续。

工程模式只接收可搬迁的顶层 `.xpr` 与匹配 `.srcs`，拒绝未纳入的外部源码引用。共享映射存在时在 Linux 本地快照中构建，再发布到独立结果区；无共享映射的工程流程使用 SCP 与包 SHA256。原生 Tcl 当前需要共享映射。配置和原工程保持权威，工具管理的 `fpga-remote` 生成区域不能作为原工程提交。

默认 `--remote-cleanup keep`。`run` 保存摘要并核对执行与交付后，才处理该次明确选择的 `trash/purge`；失败或缺少声明产物保留中间文件。清理不隐含在查询中，也不能凭合并授权执行任何主机动作。

归档解包要求解释器具备 `tarfile.data_filter`（Python 3.12+ 或带安全回补的版本）；缺少能力会在下载前明确拒绝，不回退到无过滤解包。共享文件交付与离线报告无需此解包能力。

## 结论与来源

进程成功、交付成功、时序/资源报告和板卡验证分别解释。原生 Tcl 的 `RUN_QUALITY=NOT_EVALUATED`，不从返回零推断综合/实现或签核。保存的报告可独立复核，不覆盖历史摘要；`--output` 只创建新 JSON 文件。

本次从本机 `fpga-remote@e6ff98c2ec5bea07e440d694fa35602aa32eb74f` 迁入，保留原核心、bash/Tcl 与回归资产，未并入带私人配置的旧 Git 历史。原完整历史和本机配置的恢复索引是工作区 `Backups/20261007_fpga_remote_merge/`，旧仓退出由工作区收口处理；它们不是运行依赖。产品 [TASK](../TASK.md)记录实际验证，主机/EDA/共享盘/板卡没有运行就是 **NOT_RUN**。
