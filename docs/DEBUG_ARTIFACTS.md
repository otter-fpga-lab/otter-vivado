# 构建后核对调试产物

`check_debug_artifacts` 与 `debug-artifacts` 只读本地 `.bit/.ltx`。它们检查文件完整性、
目标器件/封装以及指定的 ILA/VIO 核和探针，返回两份文件各自的 SHA256，供保存和对账。
不需要 Vivado，不连接板卡，也不自动构建或烧录。

## 消费者工程里的预期描述

把下面内容按**实际工程要求**修改后保存为 `debug/expected.json`。这是独立核对要求，
不要从待核对 LTX 原样生成后当作独立验证。`name` 是完整层级实例名或 LTX 探针名，
可能与 `debug-prepare` 使用的 IP 模块名不同。

```json
{
  "part": "xc7k325tffg900-2",
  "cores": [
    {
      "name": "u_top/ila_sample",
      "type": "ILA_V3",
      "probes": [
        {"name": "probe0", "width": 64, "direction": "IN", "port_index": 0}
      ]
    }
  ]
}
```

- `part`、`cores` 均可省略，省略部分返回 `incomplete`，用于初次摸底。
- 每个核需要 `name/type/probes`，类型支持 `ILA_V3/VIO_V2/VIO_V3`；其他类型保留
  观察结果并标为未知。可选 `uuid` 仅核对 LTX 字段，接受 32 位十六进制或标准连字符格式。
- 每个探针需要 `name/width`，可选 `direction`（`IN/OUT/INOUT`）和 `port_index`。
  VIO 输入/输出可能各自有端口 0，因此不能只按端口号合并。
- 预期核/探针是**必须存在的子集**，额外核/探针不视为失败。重复实例或探针名会阻断，
  包括多个 probe set 导致的重名；当前不自动选择一个探针集。
- 数组上限：256 个核，每核 1024 个预期探针；宽度为 1~65536 整数。
  CLI 描述文件上限 64 KiB；LTX 输入上限 10 MiB。

## 独立 CLI 与 MCP

```bash
python -m vivado_mcp debug-artifacts --bit /project/build/design.bit --ltx /project/build/design.ltx --expected /project/debug/expected.json
```

输出 JSON。省略 `--expected` 时只摸底，通常返回 `incomplete`。
MCP 使用相同实现：`check_debug_artifacts(bit_path, ltx_path, expected)`；`expected`
传 JSON 对象，不是文件路径。两种入口都不会写入或覆盖工程文件。

| `status` | CLI 退出码 | 含义 |
| --- | --- | --- |
| `consistent` | 0 | 已提供的离线检查一致；仍须验证硬件配对 |
| `incomplete` | 2 | 缺少预期目标、UUID、位宽等核对证据 |
| `blocked` | 1 | 截断、多余载荷、器件/探针不符、读取失败或结构无法解析 |

输入参数错误也退出 1，并在 stderr 输出原因。旧 XML LTX 尚不支持，返回解析阻断，
不判定其硬件内容有错。JSON LTX 按内容识别，不用 Vivado 版本号作为准入白名单。

每项检查都有 `code/status/detail`。`bit` 包括声明/实际载荷长度、构建头部与 SHA256；
`ltx` 包括核 UUID、方向、位宽、端口索引、net/subnet 名与 SHA256。子网顺序保留原文，
**不解释成 probe bit 0 顺序**，也不验证 RTL 时钟域或信号语义。

`.bit` 的文本头部有界读取，整文件分块散列，支持大于 10 MiB 的正常产物；格式本身的
载荷长度是 uint32。缺少 e 段、长度不符或零载荷都不能通过完整性核对。器件比较保留
封装，处理常见速度/温度后缀和省略的 xc 前缀；不从头部推断速度等级、温度或车规资格。
文件在读取期间发生可检测的更改/替换时阻断，应等构建完成再读取；这不是文件锁。

## 与真实构建、板上调试接续

1. 按 [工程准备指南](DEBUG_DESIGN.md) 完成 IP/约束准备，接入实际 RTL，检查时钟与探针。
2. 使用已有综合、实现和报告入口完成构建，审阅对应 run 的 DRC、资源和时序报告。
3. 从同一已实现设计导出 `.bit` 与 `write_debug_probes` 生成的 `.ltx`，在消费者工程
   保存实际 Vivado 版本、工程/run、源码提交、构建报告和这两份文件；不混用旧 LTX。
4. 运行离线核对，保存输出中的 SHA256 与预期描述。修复明确不匹配，核实未知项。
5. 按 [硬件调试指南](HARDWARE_DEBUG.md)，在本机经授权加载对应产物，核对实际器件、
   核和探针，再进行一次读回或采集；保存实测结果。

报告中的 `pairing` 始终是 `unverified`，`hardware_verified` 始终为 `false`。
LTX UUID 无法单独说明 `.bit` 内部核的身份；本工具没有解析配置包 UUID/CRC。
同名、同目录、相近时间和一次读取两份文件都不构成同一次实现的证明。
`consistent` 不能替代这条构建来源链、时序签核或真实硬件验证。

可在仓库中运行合成样例（不构成 Vivado/板卡 PASS）：

```bash
python -m vivado_mcp debug-artifacts --bit tests/fixtures/sample_header.bit --ltx tests/fixtures/sample_probes.ltx --expected examples/debug/artifacts-expected.json
```


## 核对导出清单

[检查点导出](DEBUG_BUNDLE.md) 生成 `manifest.json`。CLI 增加
`--manifest /project/debug/delivery/manifest.json`，MCP 增加 `manifest_path` 参数，
即可核对清单中全部固定产物（检查点、bit、ltx、三份报告）的 SHA256，并检查当前传入
bit/ltx 是否属于该记录。整个目录搬迁后仍可核对，内部文件名保持不变。

`bundle.status=record_matches` 仅表示文件指纹与本地来源记录一致。记录本身可被编辑，
不会把 `pairing` 升级为已验证；硬件配对与源码来源仍需独立证据。缺少文件、改过报告、
替换检查点或错配 bit/ltx 会返回 blocked；不会根据清单中的任意路径读取外部文件。
