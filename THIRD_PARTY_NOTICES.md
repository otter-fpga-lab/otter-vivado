# Third-party notices

## Codex 本机插件入口

2026-10-07 新增 source-bound Codex 入口与只读当前源指南。compatibility manifest 与路径引用依据 [OpenAI 官方插件文档](https://developers.openai.com/plugins/build/plugins)；参考已存在 Studio 的小入口壳思路，没有复制其业务实现或通用接入框架。简短引导 Skill 按本机 `skill-creator` 规范创建并执行 `quick_validate`，该工具没有复制为产品依赖。业务规范继续只在本产品原 Skill/docs 维护，宿主缓存只携带入口。

## 远程构建迁入来源

2026-10-07 按用户授权从本机独立 `fpga-remote` 仓的 `e6ff98c2ec5bea07e440d694fa35602aa32eb74f` 迁入 stdlib 核心、bash/Tcl 和离线回归资产，正式位置为 `src/vivado_mcp/remote_build/` 与 `tests/remote_build/`。保留原流程与报告语义，新增产品 CLI 路由、私有配置位置和缺少安全解包能力时的明确拒绝；没有复制私人主机配置进源码或并入旧 Git 历史。

原仓未提供独立 LICENSE；来源保留在本机恢复批次 `Backups/20261007_fpga_remote_merge/`，具体入口见 [远程构建说明](docs/REMOTE_BUILD.md)。本次没有改变 NJ 的 vivado-mcp 作者、Apache-2.0、fork 或上游贡献关系。个人配置、SSH 身份及机器路径只保存在忽略文件或显式本机输入，不作为公共资产或依赖。

## Radix Colors

The monitor stylesheet in `src/vivado_mcp/web/monitor.html` uses selected Slate,
Teal, Amber and Tomato color values from [Radix Colors](https://github.com/radix-ui/colors).
No Radix or shadcn/ui runtime component code is bundled.

Source license: https://github.com/radix-ui/colors/blob/main/LICENSE

MIT License

Copyright (c) 2021-2022 Modulz
Copyright (c) 2022-Present WorkOS

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
