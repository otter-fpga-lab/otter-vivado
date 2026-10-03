# 证据与官方资料

## 先确认本机版本，再查适用文档

优先使用实际安装版本附带的 Tcl `help`/`-help`、本地官方 UG/PG 文档和用户已配置的本地知识库。先从会话、安装路径或工程说明发现位置，限定到相关安装/文档目录搜索；无需全盘扫描或复制一份文档快照到本 Skill。

本地缺项时使用 [AMD 文档门户](https://docs.amd.com/) 与 [AMD Adaptive Support](https://adaptivesupport.amd.com/) 的官方资料。按当前任务查：

| 问题 | 官方资料 |
|---|---|
| Tcl 命令、run 属性与参数 | UG835 Vivado Tcl Command Reference |
| IDE、run 状态与工程操作 | UG893 Vivado Design Suite User Guide: Using the Vivado IDE |
| 时序、约束与实现分析 | UG906 Design Analysis and Closure Techniques；UG903 Using Constraints |
| 综合行为 | UG901 Synthesis |
| 仿真与主机/许可兼容 | UG900 Logic Simulation；UG973 Release Notes, Installation, and Licensing |
| IP 配置和接口行为 | 对应 IP 的 Product Guide（PG）及安装版本中的实际 IP 元数据 |

链接可能默认跳转到最新文档；引用时记录实际文档版本/章节，不把最新参数直接套进旧版 Vivado。若用户已有 `amd-doc-search` 可将它作为一个检索入口；本产品不要求安装它，也不提供同名工具。

查询工具异常时带上原始消息 ID、关键文本、阶段和 Vivado 版本。Answer Record 中的影响版本、修复版本与 workaround 分开记录。文档建议属于候选方案；同条件重跑并比对原始故障后才能写成“已验证”。涉及约束语义、接口或预期行为的改变先说明需求影响。

## 报告与验证留什么

按实际任务简洁记录，不为每次状态查询生成大报告：

- **对象**：工程路径、source revision（可用时）、Vivado 版本、part/top、session/run 和目标阶段。
- **运行证据**：原始 STATUS/PROGRESS、最后成功采样时间、实际日志或报告路径；观察失败保留错误原因。
- **报告来源**：当前 design 或磁盘文件、post-synth/post-place/post-route 阶段、生成时间及与本轮输入的对应证据。不确定则明确未核实。
- **结论**：目标阶段是否完成、时序/资源/约束结果分别是什么；截断、格式不识别、无约束或缺报告均保留为未知或验证缺口。
- **修改与复测**：最小变更、执行命令、实际结果、尚未执行项和下一步通过条件。文档研究、离线解析、固定回放与真实 EDA 运行分别标明。

报告解析沿用本仓工具输出，查看真实原文以核对异常值、计数和源码位置。尾部日志只支持该窗口的结论；命令返回成功也需要核对请求的值、阶段或产物。静态检查通过、Vivado run 完成、时序收敛与功能正确是分别需要证据的结论。
