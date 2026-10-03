"""入口模块：支持子命令 + MCP server 启动。

用法：
    python -m vivado_mcp              # 启动 MCP server（stdio，供 AI 工具调用）
    python -m vivado_mcp serve         # 同上，显式
    python -m vivado_mcp install       # 注入 Vivado_init.tcl
    python -m vivado_mcp uninstall     # 从 Vivado_init.tcl 移除
    python -m vivado_mcp doctor        # 只读环境诊断
    python -m vivado_mcp version       # 显示版本
    vivado-mcp install --port 9998     # 使用自定义端口
"""

from __future__ import annotations

import argparse
import sys

from vivado_mcp import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vivado-mcp",
        description="Vivado MCP Server — AI 驱动的 FPGA 开发助手。",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    # serve (默认)
    sub.add_parser(
        "serve",
        help="启动 MCP server（stdio 传输，供 Claude Code 等 AI 工具调用）。",
    )

    # install
    p_install = sub.add_parser(
        "install",
        help="注入 Vivado_init.tcl，让 Vivado GUI 启动时自动开启 TCP server。",
    )
    p_install.add_argument(
        "vivado_path",
        nargs="?",
        help="Vivado 可执行文件路径（可选，留空则自动检测）。",
    )
    p_install.add_argument(
        "--port",
        type=int,
        default=9999,
        help="监听端口（默认 9999；server 只绑定该端口，被占即退出不滑动）。",
    )

    # uninstall
    p_uninstall = sub.add_parser(
        "uninstall",
        help="从 Vivado_init.tcl 移除 vivado-mcp 注入。",
    )
    p_uninstall.add_argument(
        "vivado_path",
        nargs="?",
        help="Vivado 可执行文件路径（可选）。",
    )

    # version
    sub.add_parser("version", help="显示版本号并退出。")
    p_versions = sub.add_parser("versions", help="只读列出本机 Vivado 安装与默认选择，不启动 EDA。")
    p_versions.add_argument("--json", action="store_true", help="输出安装路径和版本推断的 JSON。")

    # doctor
    p_doctor = sub.add_parser(
        "doctor",
        help="诊断 Vivado、init Tcl、协议端口和 MCP 客户端配置。",
    )
    p_doctor.add_argument(
        "vivado_path",
        nargs="?",
        help="Vivado 可执行文件路径（可选，留空则自动检测）。",
    )
    p_doctor.add_argument("--port", type=int, default=9999, help="检查的 TCP 端口（默认 9999）。")
    p_doctor.add_argument("--json", action="store_true", help="输出稳定 JSON 结构。")
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="仅修复安全项：复用 install，并备份后原子写入缺失的客户端配置。",
    )
    p_doctor.add_argument(
        "--client",
        choices=("all", "claude-code", "codex"),
        default="all",
        help="--fix 要配置的 MCP 客户端（默认 all）。",
    )

    p_monitor = sub.add_parser("monitor", help="只读观察已有 GUI run，或打开明确回放面板。")
    p_monitor.add_argument("--port", type=int, default=9999, help="已有 GUI 的协议端口。")
    p_monitor.add_argument("--run", default="impl_1", help="实际 run 名称。")
    p_monitor.add_argument(
        "--target", default="route_design",
        choices=("synth_design", "route_design", "write_bitstream"),
        help="要观察的完成目标，不能将中间步骤 Complete 当作该目标完成。",
    )
    p_monitor.add_argument("--replay", help="读取带 VMCP_RUN 标记的回放文本，不连接 EDA。")
    p_monitor.add_argument("--json", action="store_true", help="输出一次结构化采样并退出。")
    p_debug = sub.add_parser("debug", help="人机共用 ILA/VIO 面板；连接已有 GUI 或显式演示。")
    p_debug.add_argument("--port", type=int, default=9999, help="已有 GUI 的协议端口。")
    p_debug.add_argument("--demo", action="store_true", help="合成演示，不连接 Vivado/板卡。")
    p_debug.add_argument("--panel", help="消费者工程中的 JSON 控件描述。")
    p_debug.add_argument("--target", help="精确的已打开 Hardware Manager target 名称。")
    p_debug.add_argument("--device", help="明确 target 内的完整 device 名称。")
    p_debug.add_argument("--json", action="store_true", help="输出一次调试状态并退出。")
    p_prepare = sub.add_parser("debug-prepare", help="离线生成调试工程计划，或准备已有 GUI 工程。")
    p_prepare.add_argument("--spec", required=True, help="消费者工程中的调试设计 JSON 描述。")
    p_prepare.add_argument("--port", type=int, default=9999, help="已有 GUI 的协议端口。")
    p_prepare_mode = p_prepare.add_mutually_exclusive_group()
    p_prepare_mode.add_argument("--apply", action="store_true", help="检查并准备当前 GUI 工程。")
    p_prepare_mode.add_argument("--output-dir", help="离线保存计划和模板，仅创建新文件。")
    p_artifacts = sub.add_parser("debug-artifacts", help="离线核对 bit/ltx 完整性和预期探针。")
    p_artifacts.add_argument("--bit", required=True, help="已构建的 .bit 文件路径。")
    p_artifacts.add_argument("--ltx", required=True, help="对应 .ltx 文件路径。")
    p_artifacts.add_argument("--expected", help="消费者预期器件/核/探针的 JSON；省略则仅摸底。")
    p_artifacts.add_argument("--manifest", help="导出交付包的 manifest.json，核对全包指纹。")
    p_export = sub.add_parser("debug-export", help="在专用空会话中从实现 DCP 导出调试交付包。")
    p_export.add_argument("--checkpoint", required=True, help="已完成实现的 DCP 路径。")
    p_export.add_argument("--output-dir", required=True, help="父目录已存在的新交付目录。")
    p_export.add_argument("--part", required=True, help="检查点的完整器件名称。")
    p_export.add_argument("--port", type=int, required=True, help="专用空 GUI 会话的协议端口。")
    p_export.add_argument("--source-revision", help="声明的源码提交；不作为已验证的来源。")
    p_export.add_argument("--timeout", type=int, default=1800, help="等待秒数；超时不取消 Vivado。")
    p_wave = sub.add_parser("ila-waveform", help="离线读取 VCD 为消费者波形 JSON。")
    p_wave.add_argument("--file", required=True, help="数字 VCD 文件路径。")
    p_wave.add_argument("--signal", action="append", help="精确 VCD 标识符，可重复指定。")
    p_wave.add_argument("--catalog", action="store_true", help="仅信号目录，不返回事件。")
    p_wave.add_argument("--start-tick", default="0")
    p_wave.add_argument("--end-tick")
    p_wave.add_argument("--offset", type=int, default=0)
    p_wave.add_argument("--limit", type=int, default=1000)
    p_wave.add_argument("--expected-sha256", help="分页时锁定首份文件指纹。")
    p_connect = sub.add_parser("connect", help="一次接入源码 MCP 与原地 Skill 目录引用。")
    p_connect.add_argument(
        "--client", nargs="+", choices=("cursor", "claude-code", "codex", "antigravity", "all"),
        default=["codex"], help="可选择多个客户端，all 一次接入四个客户端。",
    )
    p_connect.add_argument("--check", action="store_true", help="只读核对路径与接入状态。")
    p_connect.add_argument("--skills-only", action="store_true", help="只建立 Skill 链接。")
    p_connect.add_argument("--skills-dir", help="单客户端的实际 Skill 父目录。")
    p_connect.add_argument("--config", help="单客户端的实际 MCP 配置文件。")
    p_connect.add_argument(
        "--link-mode", choices=("auto", "symlink", "junction"), default="auto",
        help="默认符号链接；Windows 缺少链接特权时 auto 使用目录 junction。",
    )

    args = parser.parse_args()

    if args.cmd == "ila-waveform":
        import json

        from vivado_mcp.analysis.ila_waveform import read_ila_waveform

        try:
            result = read_ila_waveform(
                args.file, [] if args.catalog else args.signal,
                args.start_tick, args.end_tick, args.offset, args.limit, args.expected_sha256,
            )
        except (ValueError, OSError) as exc:
            print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
            sys.exit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    if args.cmd == "debug-export":
        import asyncio

        from vivado_mcp.debug_bundle_cli import run_debug_export_cli

        try:
            code = asyncio.run(run_debug_export_cli(args))
        except KeyboardInterrupt:
            code = 1
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            code = 1
        sys.exit(code)

    if args.cmd == "debug-artifacts":
        from vivado_mcp.debug_artifacts_cli import run_debug_artifacts_cli

        try:
            code = run_debug_artifacts_cli(args)
        except (ValueError, OSError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            code = 1
        sys.exit(code)

    if args.cmd == "debug-prepare":
        import asyncio

        from vivado_mcp.debug_prepare_cli import run_debug_prepare_cli

        try:
            asyncio.run(run_debug_prepare_cli(args))
        except KeyboardInterrupt:
            pass
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.cmd == "debug":
        import asyncio

        from vivado_mcp.debug_cli import run_debug_cli

        try:
            asyncio.run(run_debug_cli(args))
        except KeyboardInterrupt:
            pass
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.cmd == "monitor":
        import asyncio

        from vivado_mcp.monitor_cli import run_monitor_cli

        try:
            asyncio.run(run_monitor_cli(args))
        except KeyboardInterrupt:
            pass
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.cmd == "connect":
        import json

        from vivado_mcp.connect import connect_clients

        try:
            result = connect_clients(
                clients=args.client, check=args.check, skills_only=args.skills_only,
                skills_dir=args.skills_dir, config_path=args.config, link_mode=args.link_mode,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # 无参数 或 "serve" → 启动 MCP server
    if args.cmd in (None, "serve"):
        from vivado_mcp.server import mcp

        mcp.run(transport="stdio")
        return

    if args.cmd == "version":
        print(f"vivado-mcp {__version__}")
        return

    if args.cmd == "versions":
        import json

        from vivado_mcp.config import list_vivado_installations

        report = list_vivado_installations()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(report["note"])
            for item in report["installations"]:
                selected = " [默认选择]" if item["path"] == report["selected_path"] else ""
                print(f"{item['version_from_path']:9} {item['path']}{selected}")
            if report["selection_error"]:
                print(f"[未选定] {report['selection_error']}")
        return

    if args.cmd == "doctor":
        from vivado_mcp.doctor import format_report, run_doctor

        try:
            report = run_doctor(
                vivado_path=args.vivado_path,
                port=args.port,
                fix=args.fix,
                client=args.client,
            )
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(2)
        print(format_report(report, as_json=args.json))
        if report.exit_code:
            sys.exit(report.exit_code)
        return

    if args.cmd == "install":
        from vivado_mcp.install import install

        try:
            install(vivado_path=args.vivado_path, port=args.port)
        except (FileNotFoundError, PermissionError, OSError) as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.cmd == "uninstall":
        from vivado_mcp.install import uninstall

        try:
            uninstall(vivado_path=args.vivado_path)
        except (FileNotFoundError, PermissionError, OSError) as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        return


if __name__ == "__main__":
    main()
