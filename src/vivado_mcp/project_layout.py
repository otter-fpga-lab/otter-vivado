"""工程目录约定：只解析 Debug/Download/脚本/笔记/Vivado 运行目录/回收站位置。

工程根 = ``.xpr`` 所在目录。默认约定：

- ``Debug/``：可见的过程文件目录，按 ``Debug/YYYYMMDD_<主题>/`` 分子目录；
  ``Debug/NOTES.md`` 是唯一的关键点记录；``Debug/scripts/`` 放智能体生成的脚本。
- ``Download/``：交付固件与说明（本模块只给出路径）。
- Vivado 运行目录：与 Windows 开始菜单快捷方式一致，``%APPDATA%\\Xilinx\\Vivado``。
- 回收站：向上查找最近的 ``99_垃圾堆``；找不到返回 ``None``，绝不自动创建。

工程根下可选 ``otter-vivado.toml`` 的 ``[layout]`` 覆盖上述各项。``resolve_layout``
只读解析、不创建任何目录；需要新建调试子目录时显式调用 ``new_debug_subdir``。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 依赖 pyproject 中的条件依赖 tomli
    import tomli as tomllib

CONFIG_NAME = "otter-vivado.toml"
TRASH_NAME = "99_垃圾堆"
LAYOUT_KEYS = (
    "debug_dir", "download_dir", "scripts_dir", "notes_file", "vivado_cwd", "trash_dir",
)
_DEFAULTS = {
    "debug_dir": "Debug",
    "download_dir": "Download",
    "scripts_dir": "Debug/scripts",
    "notes_file": "Debug/NOTES.md",
}
_CACHE_ENV = "OTTER_VIVADO_CACHE_DIR"
_XPR_SEARCH_DEPTH = 4
DEFAULT_DEBUG_SIZE_LIMIT_BYTES = 1024 * 1024 * 1024  # 1 GiB
DEFAULT_HYGIENE_MAX_FILES = 10_000
DEFAULT_HYGIENE_TIMEOUT_SECONDS = 2.0
_LEFTOVER_HIDDEN_NAMES = (".vmcp", ".work")


class LayoutError(ValueError):
    """工程目录约定无法解析（路径不存在、多个 .xpr 或配置非法）。"""


@dataclass(frozen=True)
class ProjectLayout:
    """解析后的工程目录约定；路径均为绝对路径，trash_dir/xpr/config_path 可为 None。"""

    project_root: Path
    xpr: Path | None
    debug_dir: Path
    notes_file: Path
    scripts_dir: Path
    download_dir: Path
    vivado_cwd: Path
    trash_dir: Path | None
    config_path: Path | None
    rules: tuple[str, ...]

    def to_dict(self) -> dict:
        def text(value: Path | None) -> str | None:
            return None if value is None else str(value)

        return {
            "project_root": str(self.project_root),
            "xpr": text(self.xpr),
            "debug_dir": str(self.debug_dir),
            "notes_file": str(self.notes_file),
            "scripts_dir": str(self.scripts_dir),
            "download_dir": str(self.download_dir),
            "vivado_cwd": str(self.vivado_cwd),
            "trash_dir": text(self.trash_dir),
            "config_path": text(self.config_path),
            "rules": list(self.rules),
        }


def _absolute(path: Path) -> Path:
    return Path(os.path.normpath(os.path.abspath(path)))


def default_vivado_cwd() -> Path:
    """Vivado 运行目录默认值（.Xil/.jou/.log 的落点），不创建目录。

    Windows 与开始菜单 GUI 快捷方式的“起始位置”一致：``%APPDATA%\\Xilinx\\Vivado``。
    Linux 桌面入口没有统一的起始目录（通常落在 ``~`` 并散落日志），因此取 Vivado
    在 Linux 上的每用户数据目录 ``~/.Xilinx/Vivado``，与 Windows 语义对应。
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return _absolute(base / "Xilinx" / "Vivado")
    return _absolute(Path.home() / ".Xilinx" / "Vivado")


def user_cache_root() -> Path:
    """本插件的用户缓存根目录，不创建目录。

    ``OTTER_VIVADO_CACHE_DIR`` 可显式覆盖（测试与受限环境使用）；Windows 为
    ``%LOCALAPPDATA%\\otter-vivado``，其它平台为 ``$XDG_CACHE_HOME/otter-vivado``
    （缺省 ``~/.cache/otter-vivado``）。
    """
    override = os.environ.get(_CACHE_ENV)
    if override:
        return _absolute(Path(override).expanduser())
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".cache"
    return _absolute(base / "otter-vivado")


def project_cache_key(project_dir: str | os.PathLike) -> str:
    """规范化工程路径的短哈希 + 工程目录名，用作用户缓存子目录名。"""
    absolute = os.path.normpath(os.path.abspath(project_dir))
    normalized = os.path.normcase(absolute)
    digest = hashlib.sha256(normalized.replace("\\", "/").encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^\w.-]+", "_", Path(absolute).name).strip("._") or "project"
    return f"{digest}_{name[:40]}"


def _find_trash(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / TRASH_NAME
        if candidate.is_dir():
            return candidate
    return None


def _single_xpr(directory: Path) -> Path | None:
    """目录内唯一 .xpr；没有返回 None，多个时报错要求显式指定。"""
    try:
        candidates = sorted(
            p for p in directory.iterdir() if p.suffix.lower() == ".xpr" and p.is_file()
        )
    except OSError as exc:
        raise LayoutError(f"无法读取工程目录 {directory}：{exc}") from exc
    if len(candidates) > 1:
        names = "、".join(p.name for p in candidates)
        raise LayoutError(f"目录 {directory} 含多个 .xpr（{names}），请传入具体的 .xpr 路径。")
    return candidates[0] if candidates else None


def _load_config(path: Path) -> dict[str, str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise LayoutError(f"无法读取配置 {path}：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise LayoutError(f"配置 {path} 不是 UTF-8 文本：{exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise LayoutError(f"配置 {path} 不是合法 TOML：{exc}") from exc
    unknown = set(data) - {"layout"}
    if unknown:
        raise LayoutError(
            f"配置 {path} 含未知表/键：{', '.join(sorted(unknown))}；只支持 [layout]。"
        )
    layout = data.get("layout", {})
    if not isinstance(layout, dict):
        raise LayoutError(f"配置 {path} 的 layout 必须是 [layout] 表。")
    unknown = set(layout) - set(LAYOUT_KEYS)
    if unknown:
        raise LayoutError(
            f"配置 {path} 的 [layout] 含未知键：{', '.join(sorted(unknown))}；"
            f"可用键：{', '.join(LAYOUT_KEYS)}。"
        )
    for key, value in layout.items():
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise LayoutError(f"配置 {path} 的 [layout].{key} 必须是非空路径字符串。")
    return {key: value.strip() for key, value in layout.items()}


def _configured(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return _absolute(path if path.is_absolute() else root / path)


def _display(path: Path | None, root: Path) -> str:
    if path is None:
        return "null"
    try:
        return path.relative_to(root).as_posix() + ("/" if path.suffix == "" else "")
    except ValueError:
        return str(path)


def _rules(root: Path, values: dict[str, Path | None]) -> tuple[str, ...]:
    def show(key: str) -> str:
        return _display(values[key], root)

    trash = (
        f"需要丢弃的文件移到 {show('trash_dir')}，由用户最终删除。"
        if values["trash_dir"] is not None
        else "未找到 99_垃圾堆：不要自动创建回收站，需要丢弃文件时先问用户。"
    )
    return (
        "不要在工程内创建 .work、.vmcp 等隐藏目录或自造工作树。",
        f"过程文件（导出、实验、临时报告）放 {show('debug_dir')}<YYYYMMDD_主题>/ 子目录。",
        f"关键结论只追加到 {show('notes_file')}，不另建零散笔记。",
        f"智能体生成的临时 Tcl/Python 脚本放 {show('scripts_dir')}，"
        "不要挂到 run 的 TCL.PRE/TCL.POST。",
        "构建使用 Vivado 原生 .runs/.gen 目录，重新编译直接覆盖，不另建工作树。",
        f"交付固件与说明放 {show('download_dir')}。",
        f"Vivado 运行目录（.Xil/.jou/.log）默认 {values['vivado_cwd']}，不要落在工程根。",
        trash,
    )


def resolve_layout(project: str | os.PathLike) -> ProjectLayout:
    """解析工程目录约定；接受 .xpr 路径或工程目录。只读，不创建任何目录。"""
    if project is None or not str(project).strip():
        raise LayoutError("需要工程 .xpr 路径或工程目录。")
    if "\x00" in str(project):
        raise LayoutError("工程路径不能包含 NUL 字符。")
    path = _absolute(Path(str(project).strip()).expanduser())
    if path.suffix.lower() == ".xpr":
        if not path.is_file():
            raise LayoutError(f"工程文件不存在：{path}")
        root, xpr = path.parent, path
    elif path.is_dir():
        root, xpr = path, _single_xpr(path)
    else:
        raise LayoutError(f"工程路径不存在或不是 .xpr/目录：{path}")

    config_path = root / CONFIG_NAME
    config = _load_config(config_path) if config_path.is_file() else {}
    values: dict[str, Path | None] = {
        key: _configured(root, config.get(key, default)) for key, default in _DEFAULTS.items()
    }
    values["vivado_cwd"] = (
        _configured(root, config["vivado_cwd"]) if "vivado_cwd" in config
        else default_vivado_cwd()
    )
    values["trash_dir"] = (
        _configured(root, config["trash_dir"]) if "trash_dir" in config else _find_trash(root)
    )
    return ProjectLayout(
        project_root=root,
        xpr=xpr,
        debug_dir=values["debug_dir"],
        notes_file=values["notes_file"],
        scripts_dir=values["scripts_dir"],
        download_dir=values["download_dir"],
        vivado_cwd=values["vivado_cwd"],
        trash_dir=values["trash_dir"],
        config_path=config_path if config_path.is_file() else None,
        rules=_rules(root, values),
    )


def _norm_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _format_bytes(num_bytes: int) -> str:
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / (1024 ** 3):.2f} GiB"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / (1024 ** 2):.2f} MiB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.2f} KiB"
    return f"{num_bytes} B"


def _trash_suggestion(trash_dir: Path | None, target_desc: str) -> str:
    if trash_dir is not None:
        return (
            f"请核对不再需要后，手动将{target_desc}移入 {trash_dir} 并写清单（清单.md/清单.json），"
            "不要直接删除；本工具保持只读，不自动移动。"
        )
    return (
        f"请先向用户确认目标回收站目录（未找到 {TRASH_NAME}，不要自动创建），"
        f"确认后手动将{target_desc}移入 trash_dir 并写清单（清单.md/清单.json）；"
        "本工具保持只读，不自动移动。"
    )


@dataclass(frozen=True)
class _DirScanStats:
    total_bytes: int
    scanned_files: int
    truncated: bool
    timed_out: bool


def _scan_dir_size(
    root: Path,
    *,
    max_files: int = DEFAULT_HYGIENE_MAX_FILES,
    timeout_seconds: float = DEFAULT_HYGIENE_TIMEOUT_SECONDS,
) -> _DirScanStats:
    """有界扫描目录总大小（受文件数上限和超时保护，不跟随目录符号链接）。"""
    if not root.is_dir() or root.is_symlink():
        return _DirScanStats(0, 0, False, False)
    if max_files <= 0:
        return _DirScanStats(0, 0, True, False)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    stack: list[Path] = [root]
    total_bytes = 0
    scanned_files = 0
    scanned_dirs = 0
    max_dirs = max(1, max_files)
    while stack:
        if time.monotonic() >= deadline:
            return _DirScanStats(total_bytes, scanned_files, True, True)
        if scanned_dirs >= max_dirs:
            return _DirScanStats(total_bytes, scanned_files, True, False)
        current = stack.pop()
        scanned_dirs += 1
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        return _DirScanStats(total_bytes, scanned_files, True, True)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if scanned_files >= max_files:
                                return _DirScanStats(total_bytes, scanned_files, True, False)
                            scanned_files += 1
                            try:
                                total_bytes += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
                    except OSError:
                        continue
        except OSError:
            continue
    return _DirScanStats(total_bytes, scanned_files, False, False)


def inspect_layout_hygiene(
    layout: ProjectLayout,
    *,
    debug_size_limit_bytes: int = DEFAULT_DEBUG_SIZE_LIMIT_BYTES,
    max_files: int = DEFAULT_HYGIENE_MAX_FILES,
    timeout_seconds: float = DEFAULT_HYGIENE_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """只读检查工程目录卫生状况：残留隐藏目录、单数 script/ 与超限 debug_dir。"""
    items: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    search_roots: list[tuple[str, Path]] = [("project_root", layout.project_root)]
    if _norm_key(layout.debug_dir) != _norm_key(layout.project_root) and layout.debug_dir.is_dir():
        search_roots.append(("debug_dir", layout.debug_dir))

    for location, base in search_roots:
        loc_label = "工程根" if location == "project_root" else "Debug 目录"
        for name in _LEFTOVER_HIDDEN_NAMES:
            candidate = base / name
            key = _norm_key(candidate)
            if key in seen_paths or not os.path.lexists(candidate):
                continue
            seen_paths.add(key)
            items.append({
                "kind": "leftover_hidden_dir",
                "name": name,
                "location": location,
                "path": str(candidate),
                "message": (
                    f"在{loc_label}发现残留 {name}：{candidate}"
                    "（应改用用户缓存或 Debug/<YYYYMMDD_主题>/）。"
                ),
                "suggestion": _trash_suggestion(layout.trash_dir, f"残留的 {name}"),
            })

    singular_script = layout.project_root / "script"
    if singular_script.is_dir() and _norm_key(singular_script) != _norm_key(layout.scripts_dir):
        items.append({
            "kind": "singular_script_dir",
            "path": str(singular_script),
            "expected_scripts_dir": str(layout.scripts_dir),
            "message": (
                f"工程根存在误建的单数目录 script/：{singular_script}，"
                f"而约定的 scripts_dir 为 {layout.scripts_dir}。"
            ),
            "suggestion": _trash_suggestion(
                layout.trash_dir,
                f"多余的 script/（先确认脚本已迁入 {layout.scripts_dir}）",
            ),
        })

    if layout.debug_dir.is_dir():
        scan = _scan_dir_size(
            layout.debug_dir,
            max_files=max_files,
            timeout_seconds=timeout_seconds,
        )
        if scan.total_bytes > debug_size_limit_bytes:
            prefix = "至少 " if scan.truncated else ""
            suffix = "（扫描受文件数上限或超时保护提前结束）" if scan.truncated else ""
            items.append({
                "kind": "oversized_debug_dir",
                "path": str(layout.debug_dir),
                "total_bytes": scan.total_bytes,
                "threshold_bytes": debug_size_limit_bytes,
                "scanned_files": scan.scanned_files,
                "truncated": scan.truncated,
                "timed_out": scan.timed_out,
                "message": (
                    f"debug_dir 总大小已达 {prefix}{_format_bytes(scan.total_bytes)}"
                    f"（超过阈值 {_format_bytes(debug_size_limit_bytes)}，"
                    f"已扫 {scan.scanned_files} 个文件）{suffix}：{layout.debug_dir}。"
                ),
                "suggestion": _trash_suggestion(layout.trash_dir, "旧 Debug 子目录"),
            })

    return items


def _debug_dir_of(layout: ProjectLayout | dict) -> Path:
    if isinstance(layout, ProjectLayout):
        return layout.debug_dir
    if isinstance(layout, dict) and layout.get("debug_dir"):
        return Path(layout["debug_dir"])
    raise LayoutError("layout 需要 debug_dir。")


def _topic_slug(topic: str) -> str:
    slug = re.sub(r"[^\w-]+", "_", str(topic or "")).strip("_")
    return slug[:48] or "debug"


def _candidate_names(topic: str, now: datetime | None):
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    base = f"{stamp}_{_topic_slug(topic)}"
    yield base
    for index in range(2, 1000):
        yield f"{base}_{index}"


def reserve_debug_subdir(
    layout: ProjectLayout | dict, topic: str, *, now: datetime | None = None,
) -> Path:
    """确保 debug_dir 存在，返回尚不存在的 ``YYYYMMDD_HHMMSS_<topic>`` 路径（不创建它）。

    供要求“父目录存在的新目录”的严格导出入口使用：子目录由调用方以排他语义创建。
    """
    debug_dir = _debug_dir_of(layout)
    debug_dir.mkdir(parents=True, exist_ok=True)
    for name in _candidate_names(topic, now):
        candidate = debug_dir / name
        if not os.path.lexists(candidate):
            return candidate
    raise LayoutError(f"{debug_dir} 下同名调试目录过多，请清理或显式指定 output_dir。")


def new_debug_subdir(
    layout: ProjectLayout | dict, topic: str, *, now: datetime | None = None,
) -> Path:
    """创建 ``<debug_dir>/YYYYMMDD_HHMMSS_<topic>``；同名时追加 ``_2``、``_3`` 后缀。"""
    debug_dir = _debug_dir_of(layout)
    debug_dir.mkdir(parents=True, exist_ok=True)
    for name in _candidate_names(topic, now):
        candidate = debug_dir / name
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise LayoutError(f"{debug_dir} 下同名调试目录过多，请清理后重试。")


def find_project_from_path(path: str | os.PathLike) -> Path | None:
    """从工程内文件（如 .runs/impl_1/*.dcp）向上查找唯一 .xpr；找不到返回 None。"""
    start = _absolute(Path(path).expanduser())
    directories = [start] if start.is_dir() else []
    directories.extend(start.parents)
    for directory in directories[:_XPR_SEARCH_DEPTH + 1]:
        try:
            found = _single_xpr(directory)
        except LayoutError:
            return None
        if found is not None:
            return found
    return None


_QUERY_SESSION_XPR = (
    "if {[catch {current_project} __vmcp_layout_p] || $__vmcp_layout_p eq {}} {\n"
    "    puts \"VMCP_LAYOUT_XPR:NONE\"\n"
    "} else {\n"
    "    set __vmcp_layout_d [get_property DIRECTORY [current_project]]\n"
    "    set __vmcp_layout_n [get_property NAME [current_project]]\n"
    "    puts \"VMCP_LAYOUT_XPR:[file normalize "
    "[file join $__vmcp_layout_d $__vmcp_layout_n.xpr]]\"\n"
    "}"
)


async def query_session_xpr(session, timeout: float = 10.0) -> str | None:
    """读取会话当前工程的 .xpr 路径；未打开工程或查询失败返回 None（只读查询）。"""
    try:
        result = await session.execute(_QUERY_SESSION_XPR, timeout=timeout)
    except Exception:
        return None
    if getattr(result, "is_error", False):
        return None
    for line in str(getattr(result, "output", "")).splitlines():
        line = line.strip()
        if line.startswith("VMCP_LAYOUT_XPR:"):
            value = line[len("VMCP_LAYOUT_XPR:"):].strip()
            return value if value and value != "NONE" else None
    return None


NO_PROJECT_FOR_OUTPUT = (
    "未传 output_dir，且无法从当前会话推出工程（未打开工程或查询失败）；"
    "请先打开工程，或显式传入 output_dir（父目录已存在的新目录）。"
)


async def default_output_dir(session, topic: str, *, hint_path: str | None = None) -> str:
    """按工程约定预留 ``Debug/YYYYMMDD_HHMMSS_<topic>``（不创建子目录本身）。

    ``hint_path`` 是工程内文件（如 DCP），优先从它向上查找 .xpr；否则取会话当前工程。
    推不出工程时抛出带中文说明的 LayoutError。
    """
    xpr: str | Path | None = find_project_from_path(hint_path) if hint_path else None
    if xpr is None and session is not None:
        xpr = await query_session_xpr(session)
    if not xpr:
        raise LayoutError(NO_PROJECT_FOR_OUTPUT)
    return str(reserve_debug_subdir(resolve_layout(xpr), topic))
