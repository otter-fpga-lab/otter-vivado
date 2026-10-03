# 工程控件语义接口

插件提供精确数值换算、枚举、单位、预设预览与受约束 VIO 写入；智能体在消费者工程生成
控件与适配层。没有新增固定页面，也没有改动旧 `panel_path` 的 slider/toggle schema。
本接口独立于旧面板 JSON，不能直接把下述描述传给 `open_debug_panel(panel_path=...)`。

## 工程声明

以下是示例，target/device/core/uuid/probe 必须替换成实际发现值。符号、位数、单位和
缩放来自 RTL/寄存器约定或用户明确声明，不能根据探针名称推断或视作校准证明。

```json
{
  "version": 1,
  "title": "工程参数",
  "target": "cable/serial",
  "device": "xc7_0",
  "controls": [
    {
      "id": "gain", "label": "输出设定", "core": "vio", "uuid": "vio-uuid",
      "probe": "gain", "width": 8, "direction": "out", "kind": "number", "unit": "V",
      "signed": true, "fractional_bits": 4, "scale": "2", "offset": "1",
      "min": "-15", "max": "16.875", "step": "0.125"
    },
    {
      "id": "enable", "label": "使能", "core": "vio", "uuid": "vio-uuid",
      "probe": "enable_n", "width": 1, "direction": "out", "kind": "enum", "unit": "",
      "options": [
        {"id": "on", "label": "使能（有效低）", "raw": "0x0"},
        {"id": "off", "label": "关闭", "raw": "0x1"}
      ]
    }
  ],
  "presets": [
    {"id": "idle", "label": "空闲", "writes": [
      {"control_id": "enable", "value": "off"},
      {"control_id": "gain", "value": "0"}
    ]}
  ]
}
```

字段严格，除 `presets` 外以上各类字段均必需；未知字段拒绝。`direction` 为 `in/out`，
in 仅解读不能写。1~64 个控件，0~32 个预设，每预设 1~64 次写入；描述规范 JSON 最大
64 KiB，名称最多 1024 字符、无 NUL；控件 id、探针绑定、预设 id 不重复。一个 VIO 探针
只对应一个控件。同一预设不能重复写同一控件，避免把两个写入误当作可靠脉冲。

number 的工程公式为：

`工程值 = 原始整数 / 2^fractional_bits × scale + offset`

`width` 为 1~256，`signed` 显式选择补码/无符号，`fractional_bits` 为 0~width。
`scale` 非零（可负），`step` 正值；min ≤ max，max 必须位于从 min 起算的 step 网格。
范围端点、步长及提交值都须能精确表示为硬件整数，超过范围/位宽、非网格值均拒绝；
不自动舍入、截断或饱和。min/max/step/scale/offset/数值写入均为普通十进制**字符串**，
最长 512 字符；不接受 JSON 数字、NaN、指数。内部使用有理数，解码输出也为字符串。
例如以上设定 `"0"` → 补码整数 -8 → `0xF8`，读回 `F8` → `"0" V`。

enum 的 value 为 option id，raw 为显式 `0x` 十六进制字符串，按无符号位模式解释；
1~256 个选项，id/raw 都不重复。两个选项可生成开关，但有效低必须按声明编码。
不包含自动清零、脉冲、循环扫描或寄存器读改写能力。

## 离线预览与快照解释

`resolve_debug_controls(spec, writes?, preset?, hardware?)` 不需要 Vivado 或会话。

- writes 为 `[{control_id,value}]`，与 preset 二选一；均省略时仅验证声明/解释快照。
- 返回规范 JSON 的 `profile_sha256`、声明、每控件绑定/读回以及有序 `writes` 计划。
  键顺序不影响指纹，列表顺序影响指纹。`executed=false`、`atomic=false`。
- hardware 可传 `get_debug_snapshot().hardware`；精确匹配目标、VIO 名称/UUID、探针名、
  位宽、方向。缺少或冲突字段标 `blocked`；无快照标 `unverified`；匹配仅表示
  `matched_supplied_snapshot`，不是实时查板证据。预览写入值即使存在 blocked 仍可输出，
  消费者不可据此绕过受约束写入口。
- VIO `value` 是十六进制读回（`"10"` 是 16）；不把 `staged_value` 当已提交值。解码
  状态含 known、unknown、invalid、unmapped、outside_policy，保留 raw；未知不补零，
  未映射枚举不挑默认选项，超业务范围仍显示真实数值。outside_policy 不阻止用户通过
  有效写入修正输出，但消费者必须先检查连接和控制权。

## 单次受约束写入

`write_debug_control(spec, control_id, value, expected_profile_sha256, expected_revision,
session_id='default')` 使用当前会话的同一个 DebugService。先显式选择目标、切 control
为 ai，传预览的指纹及最新调试 revision；提交值使用工程单位字符串/枚举 id。

指纹防止预览声明与执行声明意外变化，不是服务器保存的“当前工程版本”。消费者负责保存
当前声明与指纹、拒绝旧页面提交；服务不会自动注册 profile 或推断哪个工程声明最新。
每次写入固定复制声明，提交时及真正执行前核对硬件绑定；复用 control、busy、revision、
活动实验独占和现有 `write_vio` 单探针提交，不创建另一个会话或发任意 Tcl。

返回 operation_id 后读取快照对应 operations：

- `semantic_request` 保留指纹、控件 id、请求工程值与 wire_hex；提交已接受不代表写完。
- `result` 保留原始 VIO 回执，`result.semantic` 补充实际读回解码、指纹与
  `readback_verified`。核对 commit、core/probe/width 和原始值，不只相信一个成功字段。
- 写前失配为 failed、未发写入；写入已开始后的错误、读回失配、后续刷新失败为 unknown，
  保留已有回执，不自动重试。即便 semantic.readback_verified=true，只要 operation
  仍是 unknown 就不能把整个操作显示成功；显式核对现场后决定后续动作。
- `functional_effect_verified=false`：VIO 锁存读回不证明业务逻辑采纳、物理输出或校准。
  若设计提供 applied/ack/帧编号，应另外采集证据。

预设只展开计划，不是服务器多写事务。消费者按列表顺序逐次调用语义写入口；每次等待
succeeded 与已核对读回，再取最新 revision。途中失败/未知即停止，记录已完成项和未执行项，
没有回滚；后续点击不能自动重跑整份预设。多参数同时生效必须由硬件影子寄存器与提交握手
提供，不能靠网页逐写假装原子。旧实验 debug/write_vio 步骤仍是原始值，不会自动应用语义。

## Python 消费者适配

本地页面与 MCP 共用服务，消费者可在服务所属 asyncio 循环调用：

```python
from vivado_mcp.debug_controls import resolve_controls

preview = resolve_controls(spec, writes=[{"control_id": "gain", "value": "0"}])
accepted = service.submit_control(
    spec, "gain", "0", preview["profile_sha256"],
    service.snapshot()["revision"], source="manual",
)
# control 必须属于 manual；接着按 operation_id 查询共享快照。
```

HTTP 工作线程须投递到该循环；不要从其它循环或直接在 HTTP 线程写入。实际消费者适配
应携带按钮点击时的 revision（示例只演示 Python 入口），服务端固定 source、会话身份和
声明，不相信浏览器自报 source。库未新增 HTTP 路由/任意 HTML 加载器；接入方式见
[实验适配约定](DEBUG_EXPERIMENT.md) 和 [控件生成参考](../skills/otter-vivado/references/controls.md)。
验证使用合成数据/真实 Tcl 解释器替身；真实 Vivado、Windows、板卡与消费者浏览器仍需现场验收。
