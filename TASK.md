# Otter Vivado：真实运行进度与报告查看

Status: OPEN
Target: otter-fpga-lab/otter-vivado
Basis: Hub main 0e9c375e；topics/20261002_vivado-progress-and-ross/TASK.md
Next: 实现共享状态观察器、本机只读面板与定向验证，提交 Draft PR
Result: 已领取；开发分支 feat/progress-view-study，从实际 origin/main 60b13cf 开始

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

## 当前工作块

- 复用现有会话和 Tcl 协议，后台定时只读采样，浏览器和 MCP 共用事实。
- 独立显示目标完成、阶段完成、失败、未知进度、忙碌和失联。
- 读取已有日志和报告，显示源文件、阶段与新鲜度；不通过查询生成报告或切换设计。
- CLI、MCP、Skill 共用源码；普通更新原地引用，活动会话在正常边界加载新版。

## 验证与下一动作

当前尚未完成验证；云端未发现 Vivado/Ross/GUI/板卡。后续在此记录实际命令、
结果、远端 commit/PR，以及准确现场待测步骤。模拟输入不作为真实 EDA PASS。
