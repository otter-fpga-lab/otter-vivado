"""XDC 静态检查器 (pure Python,不依赖 Vivado)。

在综合/实现之前就能发现常见约束错误,避免等 30s 综合后才看到
NSTD-1/UCIO-1 之类的问题。检查维度:

1. PACKAGE_PIN 冲突:同一物理引脚被多个 port 引用
2. 漏 IOSTANDARD:有 PACKAGE_PIN 但没配 IOSTANDARD(触发 NSTD-1)
3. 重复约束:同一 port 被多次赋同属性(后者覆盖前者)
4. create_clock 缺少 -period
5. set_property 语法明显错误(空引脚名等)
6. 编码降级提示:文件既非 UTF-8 也非 GBK 时按替换符降级解码,检查结果可能不完整
7. Tcl 控制流与过程:XDC 中禁止使用 if/for/foreach/while/proc/switch(依据 UG903)

所有检查基于原始 XDC 文件文本,不依赖 Vivado Tcl 解析器。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vivado_mcp.analysis.xdc_parser import (
    _DICT_RE,
    _TRADITIONAL_RE,
    _clean_port,
    _fold_continuations,
    _read_xdc_text,
    _strip_comment,
)


@dataclass(frozen=True)
class LintIssue:
    """一条 lint 问题。"""
    severity: str       # "error" / "warning" / "info"
    rule: str           # 规则名(如 "PIN_CONFLICT")
    message: str        # 中文说明
    file: str           # XDC 文件路径
    line: int           # 行号
    port: str = ""      # 涉及端口(如适用)


@dataclass
class LintReport:
    """lint 总报告。"""
    issues: list[LintIssue] = field(default_factory=list)
    files_checked: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "files_checked": self.files_checked,
            "issue_count": len(self.issues),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [asdict(i) for i in self.issues],
        }


# -dict 内部 IOSTANDARD 值
_DICT_IOSTD_RE = re.compile(r"IOSTANDARD\s+(\S+)", re.IGNORECASE)
# 传统语法的 IOSTANDARD 设置
_TRAD_IOSTD_RE = re.compile(
    r"set_property\s+IOSTANDARD\s+(\S+)\s+\[\s*get_ports\s+(\{[^}]+\}|\S+?)\s*\]",
    re.IGNORECASE,
)
# create_clock 检测(含 -period 则合法)
_CREATE_CLOCK_RE = re.compile(r"create_clock\b(.*)", re.IGNORECASE)

_MAX_TCL_DEPTH = 32
_TCL_CONTROL_FLOW_KEYWORDS = frozenset(
    {"if", "for", "foreach", "while", "proc", "switch"}
)


def _is_line_continuation(text: str, i: int) -> int:
    """若 ``text[i:]`` 为 Tcl 反斜杠续行，返回跳过的字符数（2 或 3），否则 0。"""
    if text[i] != "\\":
        return 0
    if i + 1 < len(text) and text[i + 1] == "\n":
        return 2
    if i + 2 < len(text) and text[i + 1 : i + 3] == "\r\n":
        return 3
    return 0


def _skip_tcl_comment(text: str, i: int, line: int) -> tuple[int, int]:
    """跳过从 ``#`` 开始的 Tcl 注释（奇数个行尾反斜杠延续到下一行）。"""
    n = len(text)
    while i < n:
        cont = _is_line_continuation(text, i)
        if cont:
            i += cont
            line += 1
            continue
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if text[i] == "\n":
            return i + 1, line + 1
        i += 1
    return n, line


def _read_tcl_braced(text: str, i: int, line: int) -> tuple[str, int, int]:
    """读取 ``{...}``，返回 ``(inner, next_i, next_line)``；``text[i]`` 须为 ``{``。"""
    n = len(text)
    i += 1
    start = i
    depth = 1
    while i < n and depth > 0:
        cont = _is_line_continuation(text, i)
        if cont:
            line += 1
            i += cont
            continue
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "\n":
            line += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1, line
        i += 1
    return text[start:n], n, line


def _read_tcl_quoted(
    text: str,
    i: int,
    line: int,
    *,
    depth: int,
    out: list[tuple[int, str]],
) -> tuple[str, bool, int, int]:
    """读取 ``"..."``，扫描其中的 ``[...]`` 命令替换而不把字面文本当成命令。"""
    n = len(text)
    i += 1
    start = i
    has_subst = False
    while i < n:
        cont = _is_line_continuation(text, i)
        if cont:
            has_subst = True
            line += 1
            i += cont
            continue
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            has_subst = True
            i += 2
            continue
        if ch == "[":
            has_subst = True
            i, line = _scan_tcl_script(
                text,
                i + 1,
                line,
                stop_at_bracket=True,
                depth=depth + 1,
                out=out,
            )
            continue
        if ch == "\n":
            line += 1
        elif ch == '"':
            return text[start:i], has_subst, i + 1, line
        i += 1
    return text[start:n], has_subst, n, line


def _read_tcl_bare(
    text: str,
    i: int,
    line: int,
    *,
    stop_at_bracket: bool,
    depth: int,
    out: list[tuple[int, str]],
) -> tuple[str, bool, int, int]:
    """读取裸词，扫描其中嵌入的 ``[...]`` 命令替换。"""
    n = len(text)
    start = i
    has_subst = False
    while i < n:
        ch = text[i]
        if ch in (" ", "\t", "\r", "\n", ";"):
            break
        if stop_at_bracket and ch == "]":
            break
        if _is_line_continuation(text, i):
            break
        if ch == "\\" and i + 1 < n:
            has_subst = True
            i += 2
            continue
        if ch == "[":
            has_subst = True
            i, line = _scan_tcl_script(
                text,
                i + 1,
                line,
                stop_at_bracket=True,
                depth=depth + 1,
                out=out,
            )
            continue
        i += 1
    return text[start:i], has_subst, i, line


def _iter_nested_script_blocks(
    cmd_name: str,
    words: list[tuple[str, str, int]],
) -> list[tuple[str, int]]:
    """提取明显作为 Tcl 脚本体传递的 ``{...}`` 参数块（不扫描数据花括号）。"""
    blocks: list[tuple[str, int]] = []
    if cmd_name == "proc":
        if len(words) >= 4 and words[3][0] == "brace":
            blocks.append((words[3][1], words[3][2]))
    elif cmd_name == "while":
        if len(words) >= 3 and words[2][0] == "brace":
            blocks.append((words[2][1], words[2][2]))
    elif cmd_name == "for":
        for idx in (1, 3, 4):
            if idx < len(words) and words[idx][0] == "brace":
                blocks.append((words[idx][1], words[idx][2]))
    elif cmd_name == "foreach":
        if len(words) >= 4 and words[-1][0] == "brace":
            blocks.append((words[-1][1], words[-1][2]))
    elif cmd_name == "if":
        idx = 1
        while idx < len(words):
            idx += 1  # 跳过条件表达式
            if idx < len(words) and words[idx][1] == "then":
                idx += 1
            if idx < len(words):
                if words[idx][0] == "brace":
                    blocks.append((words[idx][1], words[idx][2]))
                idx += 1
            if idx >= len(words):
                break
            tag = words[idx][1]
            if tag == "elseif":
                idx += 1
                continue
            if tag == "else":
                idx += 1
            if idx < len(words) and words[idx][0] == "brace":
                blocks.append((words[idx][1], words[idx][2]))
            break
    elif cmd_name == "catch":
        for kind, val, wline in words[1:]:
            if kind == "bare" and val.startswith("-"):
                continue
            if kind == "brace":
                blocks.append((val, wline))
            break
    elif cmd_name == "eval":
        if len(words) == 2 and words[1][0] == "brace":
            blocks.append((words[1][1], words[1][2]))
    elif cmd_name == "uplevel":
        if len(words) == 2 and words[1][0] == "brace":
            blocks.append((words[1][1], words[1][2]))
        elif len(words) == 3 and words[2][0] == "brace":
            blocks.append((words[2][1], words[2][2]))
    elif cmd_name == "try":
        if len(words) >= 2 and words[1][0] == "brace":
            blocks.append((words[1][1], words[1][2]))
    elif cmd_name == "namespace":
        if len(words) >= 4 and words[1][1] == "eval" and words[3][0] == "brace":
            blocks.append((words[3][1], words[3][2]))
    return blocks


def _scan_tcl_script(
    text: str,
    i: int,
    line: int,
    *,
    stop_at_bracket: bool,
    depth: int,
    out: list[tuple[int, str]],
) -> tuple[int, int]:
    """按 Tcl 词法拆分命令，在命令首词位置检测控制流与 ``proc``。"""
    n = len(text)
    if depth > _MAX_TCL_DEPTH:
        return n, line

    while i < n:
        # 跳过命令间的空白、空分号与续行
        while i < n:
            cont = _is_line_continuation(text, i)
            if cont:
                i += cont
                line += 1
            elif text[i] in (" ", "\t", "\r", "\n", ";"):
                if text[i] == "\n":
                    line += 1
                i += 1
            else:
                break
        if i >= n:
            break
        if stop_at_bracket and text[i] == "]":
            return i + 1, line
        if text[i] == "#":
            i, line = _skip_tcl_comment(text, i, line)
            continue

        cmd_line = line
        words: list[tuple[str, str, int]] = []
        closed_bracket = False

        while i < n:
            while i < n:
                cont = _is_line_continuation(text, i)
                if cont:
                    i += cont
                    line += 1
                elif text[i] in (" ", "\t", "\r"):
                    i += 1
                else:
                    break
            if i >= n:
                break
            ch = text[i]
            if stop_at_bracket and ch == "]":
                closed_bracket = True
                break
            if ch == ";":
                i += 1
                break
            if ch == "\n":
                line += 1
                i += 1
                break

            word_line = line
            if ch == "{":
                inner, i, line = _read_tcl_braced(text, i, line)
                words.append(("brace", inner, word_line))
            elif ch == '"':
                inner, has_subst, i, line = _read_tcl_quoted(
                    text, i, line, depth=depth, out=out
                )
                words.append(("quote", "" if has_subst else inner, word_line))
            else:
                bare, has_subst, i, line = _read_tcl_bare(
                    text,
                    i,
                    line,
                    stop_at_bracket=stop_at_bracket,
                    depth=depth,
                    out=out,
                )
                words.append(("bare", "" if has_subst else bare, word_line))

        if words:
            cmd_raw = words[0][1]
            cmd_name = cmd_raw[2:] if cmd_raw.startswith("::") else cmd_raw
            if cmd_name in _TCL_CONTROL_FLOW_KEYWORDS:
                out.append((cmd_line, cmd_name))
            for block_text, block_line in _iter_nested_script_blocks(cmd_name, words):
                _scan_tcl_script(
                    block_text,
                    0,
                    block_line,
                    stop_at_bracket=False,
                    depth=depth + 1,
                    out=out,
                )

        if closed_bracket:
            return i + 1, line

    return n, line


def _find_tcl_control_flow(text: str) -> list[tuple[int, str]]:
    """扫描 XDC 文本中处于命令位置的 Tcl 控制流与 ``proc``，返回 ``[(line, keyword)]``。"""
    hits: list[tuple[int, str]] = []
    _scan_tcl_script(text, 0, 1, stop_at_bracket=False, depth=0, out=hits)
    return hits


def _split_ports(port: str) -> list[str]:
    """把 get_ports 的多端口列表(如 "a b c")拆成单个端口名。"""
    return port.split()


def _glob_match(pattern: str, name: str) -> bool:
    """Tcl get_ports 风格的 glob 匹配:* 任意串、? 任意单字符,[ ] 按字面匹配。

    不能用 fnmatch:fnmatch 把 [*] 当字符类,而 XDC 总线写法 led[*] 里的
    方括号是端口名的字面字符,led[*] 应当匹配 led[0]、led[7]。
    """
    regex = "".join(
        ".*" if ch == "*" else "." if ch == "?" else re.escape(ch)
        for ch in pattern
    )
    return re.fullmatch(regex, name) is not None


def _parse_constraints_from_text(text: str) -> tuple[
    list[tuple[int, str, str, str | None]],   # (line, port, pin, iostandard|None)  -dict 和传统
    list[tuple[int, str, str]],                # (line, port, iostandard) 独立 IOSTANDARD 语句
    list[tuple[int, str]],                     # (line, raw) create_clock 行
]:
    """从已解码的 XDC 文本扫描三类约束结果。"""
    pin_constraints: list[tuple[int, str, str, str | None]] = []
    iostd_constraints: list[tuple[int, str, str]] = []
    create_clock_lines: list[tuple[int, str]] = []

    # 先折叠反斜杠续行,否则续行 create_clock / 跨行 -dict 全部不可见
    for lineno, raw_line in _fold_continuations(text):
        line = _strip_comment(raw_line)
        if not line.strip():
            continue

        # -dict 语法
        for m in _DICT_RE.finditer(line):
            inner = m.group(1)
            port = _clean_port(m.group(2))
            pin_match = re.search(r"PACKAGE_PIN\s+(\S+)", inner, re.IGNORECASE)
            iostd_match = _DICT_IOSTD_RE.search(inner)
            pin = pin_match.group(1) if pin_match else ""
            iostd = iostd_match.group(1) if iostd_match else None
            if pin:
                pin_constraints.append((lineno, port, pin, iostd))
                # -dict 内联的 IOSTANDARD 也要入账:Vivado 属性按 port 累积,
                # 同 port 的其它裸 PACKAGE_PIN 行不应再报 MISSING_IOSTANDARD
                if iostd:
                    for p in _split_ports(port):
                        iostd_constraints.append((lineno, p, iostd))
            elif iostd:
                # 纯 -dict IOSTANDARD 行(无 PACKAGE_PIN)也要入账,
                # 否则规则 2 看不见它,误报 MISSING_IOSTANDARD
                for p in _split_ports(port):
                    iostd_constraints.append((lineno, p, iostd))

        # 传统语法(PACKAGE_PIN)
        for m in _TRADITIONAL_RE.finditer(line):
            before = line[:m.start()]
            if "-dict" in before and "{" in before and "}" not in before:
                continue
            port = _clean_port(m.group(2))
            pin_constraints.append((lineno, port, m.group(1), None))

        # 独立 IOSTANDARD(多端口列表 {a b c} 拆成单端口)
        for m in _TRAD_IOSTD_RE.finditer(line):
            port = _clean_port(m.group(2))
            for p in _split_ports(port):
                iostd_constraints.append((lineno, p, m.group(1)))

        # create_clock
        m_clk = _CREATE_CLOCK_RE.search(line)
        if m_clk:
            create_clock_lines.append((lineno, line.strip()))

    return pin_constraints, iostd_constraints, create_clock_lines


def _parse_constraints_with_iostd(path: Path) -> tuple[
    list[tuple[int, str, str, str | None]],   # (line, port, pin, iostandard|None)  -dict 和传统
    list[tuple[int, str, str]],                # (line, port, iostandard) 独立 IOSTANDARD 语句
    list[tuple[int, str]],                     # (line, raw) create_clock 行
    str | None,                                # 探测到的编码,None = 降级解码(替换符)
]:
    """扫描 XDC 文件,返回三类结构化结果 + 编码探测结果。

    - 每个 PACKAGE_PIN 约束(含 -dict 内联的 IOSTANDARD)
    - 独立的 set_property IOSTANDARD 语句
    - create_clock 语句
    - 编码名(None 表示走了替换符降级解码,文本可能不完整)
    """
    text, encoding = _read_xdc_text(path)
    pin_constraints, iostd_constraints, create_clock_lines = _parse_constraints_from_text(text)
    return pin_constraints, iostd_constraints, create_clock_lines, encoding


def lint_xdc_file(
    path: str | Path,
    known_iostd_ports: set[str] | None = None,
) -> list[LintIssue]:
    """对单个 XDC 文件做静态检查,返回问题列表。

    Args:
        path: XDC 文件路径。
        known_iostd_ports: 其它 XDC 文件里已配置 IOSTANDARD 的端口集合
            (可含通配符模式),供 lint_xdc_files 跨文件聚合后传入;
            单文件直接调用时留空即可。
    """
    path_obj = Path(path)
    if not path_obj.exists():
        return [LintIssue(
            severity="error",
            rule="FILE_NOT_FOUND",
            message=f"XDC 文件不存在: {path}",
            file=str(path),
            line=0,
        )]

    issues: list[LintIssue] = []
    source_file = str(path_obj)

    text, encoding = _read_xdc_text(path_obj)
    pin_cs, iostd_cs, create_clock_lines = _parse_constraints_from_text(text)

    # 规则 0: 编码降级要在用户可见报告里露出(不只进日志)——
    # 替换符可能已破坏部分约束文本,下游检查结果可能不完整,fixer 也会拒写该文件
    if encoding is None:
        issues.append(LintIssue(
            severity="warning",
            rule="ENCODING_DEGRADED",
            message=(
                "文件既不是合法 UTF-8 也不是合法 GBK,已按 UTF-8 替换符降级解码(只读):"
                "部分约束文本可能损坏导致漏检,自动修复将跳过该文件,请先转存为 UTF-8"
            ),
            file=source_file,
            line=0,
        ))

    # 规则 1: PACKAGE_PIN 冲突(同一 pin 被不同 port 占用)
    pin_to_ports: dict[str, list[tuple[int, str]]] = {}
    for (lineno, port, pin, _iostd) in pin_cs:
        pin_to_ports.setdefault(pin.upper(), []).append((lineno, port))
    for pin, occurrences in pin_to_ports.items():
        if len({p for _, p in occurrences}) > 1:
            ports_str = ", ".join(f"{p}(行{ln})" for ln, p in occurrences)
            issues.append(LintIssue(
                severity="error",
                rule="PIN_CONFLICT",
                message=f"引脚 {pin} 被多个 port 占用: {ports_str}",
                file=source_file,
                line=occurrences[0][0],
            ))

    # 规则 2: 漏 IOSTANDARD(同 port 没有在 -dict 里带 IOSTANDARD,也没有独立语句)
    # 通配符写法 set_property IOSTANDARD ... [get_ports {led[*]}] 覆盖 led[0] 等,
    # 必须按 Tcl glob 语义匹配,不能字符串精确相等。
    # 多文件 lint 时并上跨文件聚合的端口集合,IOSTANDARD 与 PACKAGE_PIN
    # 分属不同 XDC 是常见组织方式,只看本文件会误报
    iostd_ports = {port for (_, port, _) in iostd_cs}
    if known_iostd_ports:
        iostd_ports |= known_iostd_ports
    iostd_patterns = [p for p in iostd_ports if "*" in p or "?" in p]
    for (lineno, port, pin, iostd) in pin_cs:
        if iostd is not None or port in iostd_ports:
            continue
        if any(_glob_match(pat, port) for pat in iostd_patterns):
            continue
        issues.append(LintIssue(
            severity="warning",
            rule="MISSING_IOSTANDARD",
            message=(
                f"端口 {port} 只有 PACKAGE_PIN={pin} 没配 IOSTANDARD,"
                "将触发 NSTD-1 警告,严重时会导致 Bank 电压冲突(BIVC-1 ERROR)"
            ),
            file=source_file,
            line=lineno,
            port=port,
        ))

    # 规则 3: 同 port 多次被约束 PACKAGE_PIN(后者覆盖前者)
    port_to_pins: dict[str, list[tuple[int, str]]] = {}
    for (lineno, port, pin, _) in pin_cs:
        port_to_pins.setdefault(port, []).append((lineno, pin))
    for port, occurrences in port_to_pins.items():
        if len(occurrences) > 1 and len({p for _, p in occurrences}) > 1:
            pins_str = ", ".join(f"{p}(行{ln})" for ln, p in occurrences)
            issues.append(LintIssue(
                severity="warning",
                rule="DUPLICATE_PORT",
                message=(
                    f"端口 {port} 被多次约束不同引脚: {pins_str} —— "
                    "Vivado 会采用最后一个,前面的约束无效"
                ),
                file=source_file,
                line=occurrences[0][0],
                port=port,
            ))

    # 规则 4: create_clock 缺 -period
    for (lineno, raw) in create_clock_lines:
        if "-period" not in raw.lower():
            issues.append(LintIssue(
                severity="error",
                rule="CLOCK_NO_PERIOD",
                message=f"create_clock 缺少 -period 参数: {raw}",
                file=source_file,
                line=lineno,
            ))

    # 规则 5: XDC 中出现 Tcl 控制流或过程 (if/for/foreach/while/proc/switch)
    for lineno, kw in _find_tcl_control_flow(text):
        issues.append(LintIssue(
            severity="error",
            rule="TCL_CONTROL_FLOW",
            message=(
                f"XDC 中不允许使用 Tcl 控制流/过程 `{kw}`"
                "（依据 AMD Vivado UG903 Using Constraints: 标准 XDC 仅支持约束命令及"
                "有限内置 Tcl 命令 set/list/expr，不支持控制流与 proc，可能导致引脚等约束不生效）。"
                "修复建议: 拆成独立 XDC 文件，或改用 USED_IN 属性"
                "（USED_IN_SYNTHESIS / USED_IN_IMPLEMENTATION）/ 按 run 选择约束集（CONSTRSET）；"
                "若必须使用 Tcl 控制流，请迁入 unmanaged Tcl 约束文件"
                "（read_xdc -unmanaged 或在约束集中将 file_type 设为 Tcl）。"
            ),
            file=source_file,
            line=lineno,
        ))

    return issues


def lint_xdc_files(paths: list[str | Path]) -> LintReport:
    """批量 lint 多个 XDC 文件,并做跨文件检查(全局 pin 冲突、全局 IOSTANDARD)。"""
    report = LintReport()
    all_pin_map: dict[str, list[tuple[str, int, str]]] = {}  # pin -> [(file, line, port)]

    # 第一遍:跨文件聚合(与 PIN_CONFLICT 聚合同模式)。
    # IOSTANDARD 端口集合必须先聚合成全局再跑规则 2:
    # "公共 XDC 放通配 IOSTANDARD + 引脚 XDC 放 PACKAGE_PIN" 是真实组织方式,
    # 逐文件检查会误报 MISSING_IOSTANDARD,fixer 进而写入覆盖真实值的约束
    global_iostd_ports: set[str] = set()
    for p in paths:
        path_obj = Path(p)
        if path_obj.exists():
            pin_cs, iostd_cs, _, _ = _parse_constraints_with_iostd(path_obj)
            global_iostd_ports.update(port for (_, port, _) in iostd_cs)
            # 收集 pin 供跨文件冲突检查
            for (lineno, port, pin, _iostd) in pin_cs:
                all_pin_map.setdefault(pin.upper(), []).append(
                    (str(path_obj), lineno, port)
                )

    # 第二遍:逐文件 lint,规则 2 带上全局 IOSTANDARD 端口集合
    for p in paths:
        path_obj = Path(p)
        report.files_checked.append(str(path_obj))
        report.issues.extend(
            lint_xdc_file(path_obj, known_iostd_ports=global_iostd_ports)
        )

    # 跨文件的 PIN_CONFLICT(同一 pin 在不同文件里指向不同 port)
    for pin, occurrences in all_pin_map.items():
        files_with_pin = {f for f, _, _ in occurrences}
        ports_for_pin = {p for _, _, p in occurrences}
        if len(files_with_pin) > 1 and len(ports_for_pin) > 1:
            # 只在"跨文件冲突"时报告,避免单文件已经报过的重复
            # 但要判断是否和单文件内冲突重复 —— 按文件数 > 1 判断即可
            loc_str = ", ".join(
                f"{Path(f).name}:{ln}:{p}" for f, ln, p in occurrences
            )
            report.issues.append(LintIssue(
                severity="error",
                rule="PIN_CONFLICT_CROSS_FILE",
                message=f"引脚 {pin} 在多个 XDC 文件里被不同 port 占用: {loc_str}",
                file=occurrences[0][0],
                line=occurrences[0][1],
            ))

    return report


def format_lint_report(report: LintReport) -> str:
    """格式化 lint 报告为中文文本。"""
    lines: list[str] = []

    if not report.issues:
        lines.append("=== XDC 静态检查: PASS ===")
        lines.append(f"检查了 {len(report.files_checked)} 个文件,无问题。")
        return "\n".join(lines)

    n_err = len(report.errors)
    n_warn = len(report.warnings)
    header = "=== XDC 静态检查: "
    if n_err > 0:
        header += f"FAIL ({n_err} errors, {n_warn} warnings) ==="
    else:
        header += f"WARN ({n_warn} warnings) ==="
    lines.append(header)
    lines.append(f"扫描文件: {len(report.files_checked)}")
    lines.append("")

    # 按严重级分组展示
    for sev, label in [("error", "ERROR"), ("warning", "WARNING"), ("info", "INFO")]:
        subset = [i for i in report.issues if i.severity == sev]
        if not subset:
            continue
        lines.append(f"--- {label} ({len(subset)} 条) ---")
        for issue in subset:
            file_name = Path(issue.file).name
            lines.append(f"  [{issue.rule}] {file_name}:{issue.line}")
            lines.append(f"    {issue.message}")
        lines.append("")

    return "\n".join(lines).rstrip()
