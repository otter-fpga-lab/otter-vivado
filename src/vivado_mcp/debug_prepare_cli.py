"""调试工程准备入口：默认离线规划，显式 apply 才连接现有 GUI。"""

from __future__ import annotations

import json
from pathlib import Path

from vivado_mcp.debug_design import DebugDesignPreparation, plan_debug_design
from vivado_mcp.vivado.gui_session import GuiSession

MAX_SPEC_BYTES = 64 * 1024


def _load_spec(filename: str) -> dict:
    with Path(filename).open("rb") as source:
        raw = source.read(MAX_SPEC_BYTES + 1)
    if len(raw) > MAX_SPEC_BYTES:
        raise ValueError("调试工程描述不能超过 64 KiB")
    try:
        spec = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("调试工程描述必须是 UTF-8 JSON") from exc
    if not isinstance(spec, dict):
        raise ValueError("调试工程描述必须是 JSON 对象")
    return spec


def _save_plan(plan: dict, directory: str) -> list[str]:
    """预检全部目标，使用独占创建；任何已有目标均保持原样。"""
    destination = Path(directory).expanduser().resolve()
    contents = {"plan.json": json.dumps(plan, ensure_ascii=False, indent=2) + "\n"}
    for artifact in plan.get("artifacts", []):
        name, content = artifact["filename"], artifact["content"]
        if (
            not isinstance(name, str) or not name or name in {".", ".."}
            or "/" in name or "\\" in name or ":" in name
            or name in contents or not isinstance(content, str)
        ):
            raise ValueError("生成产物必须使用互不重复的普通文件名，不能包含目录")
        contents[name] = content
    targets = [(destination / name, content) for name, content in contents.items()]
    for target, _ in targets:
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"目标文件已存在，未写入任何产物：{target}")
    destination.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for target, content in targets:
            with target.open("x", encoding="utf-8", newline="\n") as output:
                created.append(target)
                output.write(content)
    except Exception:
        for target in created:
            target.unlink(missing_ok=True)
        raise
    return [str(target) for target in created]


def _print_result(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def run_debug_prepare_cli(args) -> None:
    """只准备调试源或约束；不综合、实现、烧录，也不关闭已有 GUI。"""
    if args.apply and args.output_dir:
        raise ValueError("--apply 与 --output-dir 不能同时使用")
    if args.apply and not 1 <= args.port <= 65535:
        raise ValueError("--port 必须是已运行 GUI 的实际端口 (1~65535)")
    spec = _load_spec(args.spec)
    plan = plan_debug_design(spec)
    if not args.apply:
        if args.output_dir:
            plan = {**plan, "saved_files": _save_plan(plan, args.output_dir)}
        _print_result(plan)
        return

    session = GuiSession("", session_id="debug-prepare", port=args.port, attach_only=True)
    try:
        await session.start(timeout=5)
        preparation = DebugDesignPreparation(session)
        inspected = await preparation.inspect(spec)
        if inspected.get("status") != "ready":
            _print_result(inspected)
            raise RuntimeError("调试工程预检未通过；请按结果修正后重试")
        project = inspected.get("project")
        if not isinstance(project, dict) or not all(
            isinstance(project.get(key), str) and project[key]
            for key in ("name", "directory", "part")
        ):
            _print_result(inspected)
            raise RuntimeError("预检未返回完整工程身份，未执行调试工程修改")
        result = await preparation.apply(spec, expected_project=project)
        _print_result(result)
        if result.get("status") not in {"created", "constraints_added"}:
            raise RuntimeError("调试工程准备未完整完成；请先核对返回结果，不要直接重放")
    finally:
        await session.stop()
