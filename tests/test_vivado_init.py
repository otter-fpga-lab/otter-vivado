"""vivado_init.py 单元测试：重点覆盖 Tcl 注释反斜杠续行解析。"""

from __future__ import annotations

import pytest

from vivado_mcp import doctor
from vivado_mcp.run_feedback import parse_vivado_init_max_threads
from vivado_mcp.vivado_init import inspect_init_tcl_file, parse_general_max_threads


class TestTclCommentLineContinuation:
    """Tcl `#` 注释行尾反斜杠续行不得把下一行当成生效命令。"""

    def test_continued_comment_does_not_false_positive(self):
        """注释行以反斜杠结尾时，下一行 set_param 仍是注释，不得误报。"""
        text = (
            "# 临时注释掉高线程配置 \\\n"
            "set_param general.maxThreads 32\n"
        )
        assert parse_general_max_threads(text) == []
        assert parse_vivado_init_max_threads(text) == ("", 0)

    def test_continued_comment_does_not_mask_prior_real_risk(self):
        """前面已有 32，后续续行注释里的 8 不得覆盖前面的真实风险。"""
        text = (
            "set_param general.maxThreads 32\n"
            "# 下面这行本想注释掉，但上行尾有反斜杠 \\\n"
            "set_param general.maxThreads 8\n"
        )
        matches = parse_general_max_threads(text)
        assert matches == [{"line": 1, "raw": "32", "value": 32}]
        assert parse_vivado_init_max_threads(text) == ("32", 1)

    def test_inline_semicolon_comment_continuation(self):
        """`;# ... \\` 内联注释续行同样吞掉下一行，不影响更后面的真实设置。"""
        text = (
            "set_param general.maxThreads 16 ;# 保留上行 \\\n"
            "set_param general.maxThreads 4\n"
            "set_param general.maxThreads 24\n"
        )
        matches = parse_general_max_threads(text)
        assert matches == [
            {"line": 1, "raw": "16", "value": 16},
            {"line": 3, "raw": "24", "value": 24},
        ]

    def test_chained_comment_continuations_and_line_numbers(self):
        """多行连续反斜杠续行与空行终止续行，后续行号保持准确。"""
        text = (
            "# line 1 \\\n"
            "# line 2 \\\n"
            "set_param general.maxThreads 64 \\\n"
            "set_param general.maxThreads 99\n"
            "set_param general.maxThreads 8\n"
        )
        assert parse_general_max_threads(text) == [{"line": 5, "raw": "8", "value": 8}]

    def test_blank_line_terminates_comment_continuation(self):
        """续行到纯空白行（无尾随反斜杠）后结束注释，再下一行恢复解析。"""
        text = (
            "# comment \\\n"
            "   \n"
            "set_param general.maxThreads 16\n"
        )
        assert parse_general_max_threads(text) == [{"line": 3, "raw": "16", "value": 16}]

    def test_even_vs_odd_trailing_backslashes_in_comment(self):
        """偶数个反斜杠是字面反斜杠（终止注释），奇数个才续行。"""
        even_text = (
            "# path ends with escaped backslash \\\\\n"
            "set_param general.maxThreads 32\n"
        )
        assert parse_general_max_threads(even_text) == [
            {"line": 2, "raw": "32", "value": 32}
        ]

        odd_text = (
            "# path ends with three backslashes \\\\\\\n"
            "set_param general.maxThreads 32\n"
        )
        assert parse_general_max_threads(odd_text) == []

    def test_trailing_space_after_backslash_does_not_continue(self):
        """反斜杠后有空格不是 Tcl 续行，下一行仍应生效。"""
        text = (
            "# trailing backslash then space \\ \n"
            "set_param general.maxThreads 32\n"
        )
        assert parse_general_max_threads(text) == [
            {"line": 2, "raw": "32", "value": 32}
        ]

    def test_crlf_comment_continuation(self):
        """Windows CRLF 换行下的反斜杠续行（``\\`` + CRLF）也能正确吞掉下一行。"""
        text = (
            "# windows comment \\\r\n"
            "set_param general.maxThreads 32\r\n"
            "set_param general.maxThreads 8\r\n"
        )
        assert parse_general_max_threads(text) == [{"line": 3, "raw": "8", "value": 8}]

    def test_comment_continuation_inside_braced_block_and_after_escaped_newline(self):
        """花括号块内及 `; \\` 续行后的 `#` 续行注释均按 Tcl 规则处理。"""
        text = (
            "if {[catch {\n"
            "    set_param general.maxThreads 32\n"
            "    # disabled override \\\n"
            "    set_param general.maxThreads 4\n"
            "}]} {}\n"
            "puts ok; \\\n"
            "# continued comment after semicolon-backslash \\\n"
            "set_param general.maxThreads 2\n"
        )
        assert parse_general_max_threads(text) == [{"line": 2, "raw": "32", "value": 32}]

    def test_unclosed_quote_in_continued_comment_does_not_break_next_line(self):
        """被续行注释吞掉的行若含未闭合引号/花括号，不得影响下一行真实命令。"""
        text = (
            "# comment \\\n"
            '"unclosed quote and { brace\n'
            "set_param general.maxThreads 16\n"
        )
        assert parse_general_max_threads(text) == [
            {"line": 3, "raw": "16", "value": 16}
        ]

    def test_inspect_init_tcl_file_and_doctor_verdicts(self, tmp_path, monkeypatch):
        """端到端校验 inspect_init_tcl_file 与 doctor 在两类场景下不再误报/漏报。"""
        monkeypatch.setattr(doctor.sys, "platform", "win32")
        home = tmp_path / "home"
        user_init = home / "AppData" / "Roaming" / "Xilinx" / "Vivado" / "Vivado_init.tcl"
        user_init.parent.mkdir(parents=True)

        # 场景 1：注释续行吞掉 32 → 不误报
        user_init.write_text(
            "# disabled \\\nset_param general.maxThreads 32\n",
            encoding="utf-8",
        )
        info_ok = inspect_init_tcl_file(user_init)
        assert info_ok["max_threads"] is None
        check_ok = doctor._check_user_vivado_init_tcl(home)
        assert check_ok.status == "ok"
        assert check_ok.evidence["max_threads"] is None

        # 场景 2：真实 32 在前，续行注释里的 8 在后 → 不漏报高线程风险
        user_init.write_text(
            "set_param general.maxThreads 32\n"
            "# disabled override \\\n"
            "set_param general.maxThreads 8\n",
            encoding="utf-8",
        )
        info_warn = inspect_init_tcl_file(user_init)
        assert info_warn["max_threads"] == 32
        check_warn = doctor._check_user_vivado_init_tcl(home)
        assert check_warn.status == "warn"
        assert check_warn.evidence["max_threads"] == 32


@pytest.mark.parametrize(
    ("tcl_snippet", "expected"),
    [
        ("# c \\\nset_param general.maxThreads 32\n", []),
        ("# c \\\\\nset_param general.maxThreads 32\n", [32]),
        (
            "set_param general.maxThreads 32\n# c \\\nset_param general.maxThreads 4\n",
            [32],
        ),
    ],
)
def test_matches_real_tcl_interpreter_semantics(tcl_snippet, expected):
    """与标准 Tcl 解释器语义一致。"""
    matches = parse_general_max_threads(tcl_snippet)
    assert [m["value"] for m in matches] == expected
