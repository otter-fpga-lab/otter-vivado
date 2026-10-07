---
name: otter-vivado
description: 操作 AMD Vivado 工程、本地或远程构建、调试和已有报告时，读取当前产品指南并选择工具。RTL 编码由相应编码能力处理。
---

# Otter Vivado 入口

先调用本插件 MCP 的 `vivado_guide(section="overview")`，从返回的 `source_root` 与指南接续当前任务；工具前缀使用宿主实际发现的名称。

远程构建按需调用 `vivado_guide(section="remote")` 或 `remote-tcl`，使用返回的同源 `cli` 加 `remote` 子命令。需要详细参考时，用 `section="reference"` 和返回列表中的文件名读取；不要从插件缓存猜业务源码或读取私人配置。

本文件只负责发现入口，不复制产品规范。指南服务未加载时如实说明；工具、宿主发现与 EDA/设备结果分别报告。
