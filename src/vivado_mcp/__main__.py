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
