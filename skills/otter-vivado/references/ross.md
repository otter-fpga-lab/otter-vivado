# AMD Ross 参考与选择

2026-10-03 核对 [Xilinx/ross-ai-assistant](https://github.com/Xilinx/ross-ai-assistant) 的 main 为 [`2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de`](https://github.com/Xilinx/ross-ai-assistant/tree/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de)；最新发布标签 [2026.9.1](https://github.com/Xilinx/ross-ai-assistant/releases/tag/2026.9.1) 指向 `c89c3328dc0f03520a36ea9a7af7f8c9597ba92f`，与 main 不同。下面按 main 做源码/官方说明研究，不是本机运行 Ross 的验证结论；复查后续版本时重新核对相关入口。

## 借鉴的是工作方法

| Ross 实际材料 | 本产品如何采用 |
|---|---|
| [vivado-simulate-rtl](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/skills/vivado-simulate-rtl/SKILL.md) 按请求读取局部 references，先发现工程和环境，明确终止条件 | Skill 首页只做任务路由；会话、构建、诊断与证据说明按需读本仓 references。 |
| [known-issue-research](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/skills/vivado-simulate-rtl/references/known-issue-research.md) 用消息签名、版本与构造定位文档，再复测 workaround | 本地版本匹配的官方资料优先，再查 AMD 官方站点；结论标明文档依据与实际复测，保留未验证项。 |
| [Vivado MCP 工具参考](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/docs/reference/vivado-mcp-tools.md) 分出执行、状态、日志、历史；同会话执行串行，长命令另行监控 | 复用本仓执行通道和观察缓存；人和模型读取同一状态，繁忙时让出执行通道。Ross 的工具名及能力不能直接当作本仓工具调用。 |
| [vivado-rtl-lint](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/skills/vivado-rtl-lint/SKILL.md) 与 [elaboration analysis](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/skills/vivado-rtl-elaboration-analysis/SKILL.md) 将报告计数、消息、源码位置和验证步骤对应 | 沿用已有解析器，报告必须能回到原始证据；缺项不合成为 0 或 PASS。 |
| [vivado-ip-configurator](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/skills/vivado-ip-configurator/SKILL.md) 检查参数读回以及是否实现原始意图 | 在本仓已有 `inspect_ip_params`/`compare_xci` 基础上核对实际配置；不因 Tcl 无错误就声称参数或功能生效。 |

## 共享源码的安装取舍

Ross 的 [README](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/README.md) 区分 skills 插件、Vivado MCP 分发包及 AI Extension；其 [插件安装说明](https://github.com/Xilinx/ross-ai-assistant/blob/2cdc9eef1b6b5b17fa45f5e4d468e5483a1c92de/docs/getting-started/install-plugin.md) 还明确了客户端差异：Claude Code/Codex 的 marketplace 安装会复制到缓存，Cursor 本地插件不接受指向其 local 目录外的链接，Antigravity 使用自己的 manifest schema。

因此，“作为插件使用”并不保证客户端原生插件安装器会原地读取源码。本产品采用独立 Skill 目录链接与 editable Python 源码 MCP，满足多个脚手架共用一份内容的要求。Skill 链接和原生插件目录是不同的发现入口；不要把后者的规则或缓存更新承诺套给前者。同名入口保持指向同一源码，不另装一份缓存版本；客户端跨兼容目录的去重、发现、重载和 Windows 链接权限按实际版本现场核对。

## 并列使用的边界

用户可选择 Otter 的现有会话/可视化，也可选择 Ross 的专门方法和工具。两者没有强制依赖或替代关系。使用 Ross 专门 Skill 时先核对其依赖、工具前缀与 Vivado 版本；例如这一版本的 timing methodology Skill 明确要求 Vivado 2026.1+，不代表旧版本自动兼容。

Ross 的 `vivado_doc_search` 来自独立的 `amd-doc-search` MCP，并非其 Vivado MCP 自带能力。本仓同样不声称提供 Ross 的 history、display、LSF/SSH 或相应完整分析工具。若两套工具同时可用，明确当前哪套工具拥有会话；复用报告/日志证据，不由另一个服务重置、接管或关闭正在工作的会话。
