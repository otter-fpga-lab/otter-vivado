"""xdc_linter 单元测试。"""

from pathlib import Path

import pytest

from vivado_mcp.analysis.xdc_linter import (
    format_lint_report,
    lint_xdc_file,
    lint_xdc_files,
)


@pytest.fixture
def tmp_xdc(tmp_path: Path):
    """工厂:把内容写入临时 XDC 文件。"""
    def _make(name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p
    return _make


class TestMissingIostandard:
    def test_detects_missing_iostandard(self, tmp_xdc):
        p = tmp_xdc("bad.xdc", "set_property PACKAGE_PIN V14 [get_ports {led[7]}]\n")
        issues = lint_xdc_file(p)
        rules = {i.rule for i in issues}
        assert "MISSING_IOSTANDARD" in rules

    def test_iostandard_via_dict_is_ok(self, tmp_xdc):
        p = tmp_xdc("good.xdc",
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
        )
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_iostandard_via_separate_statement_is_ok(self, tmp_xdc):
        p = tmp_xdc("sep.xdc", (
            "set_property PACKAGE_PIN W5 [get_ports clk]\n"
            "set_property IOSTANDARD LVCMOS33 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)


class TestPinConflict:
    def test_same_pin_two_ports_error(self, tmp_xdc):
        p = tmp_xdc("conflict.xdc", (
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports other]\n"
        ))
        issues = lint_xdc_file(p)
        errors = [i for i in issues if i.severity == "error" and i.rule == "PIN_CONFLICT"]
        assert len(errors) == 1
        assert "W5" in errors[0].message.upper()

    def test_same_pin_same_port_no_error(self, tmp_xdc):
        p = tmp_xdc("dup.xdc", (
            "set_property PACKAGE_PIN W5 [get_ports clk]\n"
            "set_property IOSTANDARD LVCMOS33 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "PIN_CONFLICT" for i in issues)


class TestDuplicatePort:
    def test_port_assigned_two_pins_warning(self, tmp_xdc):
        p = tmp_xdc("duppin.xdc", (
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "set_property -dict { PACKAGE_PIN W6 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        dup = [i for i in issues if i.rule == "DUPLICATE_PORT"]
        assert len(dup) == 1


class TestCreateClock:
    def test_create_clock_no_period_error(self, tmp_xdc):
        p = tmp_xdc("clk.xdc", "create_clock -name sys_clk [get_ports clk]\n")
        issues = lint_xdc_file(p)
        errors = [i for i in issues if i.rule == "CLOCK_NO_PERIOD"]
        assert len(errors) == 1

    def test_create_clock_with_period_ok(self, tmp_xdc):
        p = tmp_xdc("clk.xdc", (
            "create_clock -name sys_clk -period 10.000 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "CLOCK_NO_PERIOD" for i in issues)


class TestWildcardIostandard:
    """审计 P0:通配符 / 纯 -dict / 多端口列表形式的 IOSTANDARD 必须被认出来。"""

    def test_wildcard_iostd_covers_indexed_port(self, tmp_xdc):
        """led[*] 的 IOSTANDARD 覆盖 led[0],不应误报 MISSING_IOSTANDARD。"""
        p = tmp_xdc("wild.xdc", (
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n"
            "set_property PACKAGE_PIN W5 [get_ports {led[0]}]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_wildcard_iostd_does_not_cover_other_port(self, tmp_xdc):
        """通配符只覆盖匹配到的端口,不匹配的端口仍要报。"""
        p = tmp_xdc("wild2.xdc", (
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n"
            "set_property PACKAGE_PIN T17 [get_ports btn]\n"
        ))
        issues = lint_xdc_file(p)
        assert any(
            i.rule == "MISSING_IOSTANDARD" and i.port == "btn" for i in issues
        )

    def test_question_mark_wildcard(self, tmp_xdc):
        """? 单字符通配符也按 Tcl glob 语义匹配。"""
        p = tmp_xdc("wild3.xdc", (
            "set_property IOSTANDARD LVCMOS33 [get_ports {sw?}]\n"
            "set_property PACKAGE_PIN V17 [get_ports sw0]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_dict_only_iostd_with_separate_pin(self, tmp_xdc):
        """纯 -dict IOSTANDARD(无 PACKAGE_PIN)+ 独立 PACKAGE_PIN 行,不应误报。"""
        p = tmp_xdc("dictonly.xdc", (
            "set_property -dict { IOSTANDARD LVCMOS18 SLEW FAST } [get_ports btn]\n"
            "set_property PACKAGE_PIN T17 [get_ports btn]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_multiport_list_iostd(self, tmp_xdc):
        """[get_ports {a b c}] 多端口列表要拆开逐个入账。"""
        p = tmp_xdc("multi.xdc", (
            "set_property IOSTANDARD LVCMOS33 [get_ports {a b c}]\n"
            "set_property PACKAGE_PIN U1 [get_ports a]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_dict_inline_iostd_covers_redundant_bare_pin_line(self, tmp_xdc):
        """审计 P3:-dict 内联 IOSTANDARD 入账后,同 port 的重复裸 PACKAGE_PIN 行不误报。

        Vivado 属性按 port 累积,第一行已设置 clk 的 IOSTANDARD。
        """
        p = tmp_xdc("inline.xdc", (
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "set_property PACKAGE_PIN W5 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)


class TestCrossFileIostandard:
    """审计 P1:IOSTANDARD 与 PACKAGE_PIN 分属不同 XDC 文件时,规则 2 必须看全局聚合。

    否则误报 MISSING_IOSTANDARD,fixer 进而向引脚 XDC 写入覆盖真实值的约束。
    """

    def test_exact_iostd_in_other_file_no_false_positive(self, tmp_xdc):
        f_iostd = tmp_xdc("iostd.xdc",
            "set_property IOSTANDARD LVCMOS18 [get_ports clk]\n")
        f_pins = tmp_xdc("pins.xdc",
            "set_property PACKAGE_PIN W5 [get_ports clk]\n")
        report = lint_xdc_files([f_iostd, f_pins])
        assert all(i.rule != "MISSING_IOSTANDARD" for i in report.issues)

    def test_wildcard_iostd_in_other_file_no_false_positive(self, tmp_xdc):
        """评审复现场景:公共 XDC 放通配 IOSTANDARD,引脚 XDC 放 PACKAGE_PIN。"""
        f_iostd = tmp_xdc("iostd.xdc",
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n")
        f_pins = tmp_xdc("pins.xdc",
            "set_property PACKAGE_PIN U16 [get_ports {led[0]}]\n")
        report = lint_xdc_files([f_iostd, f_pins])
        assert all(i.rule != "MISSING_IOSTANDARD" for i in report.issues)

    def test_uncovered_port_still_reported_across_files(self, tmp_xdc):
        """跨文件聚合不放过真正没配 IOSTANDARD 的端口。"""
        f_iostd = tmp_xdc("iostd.xdc",
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n")
        f_pins = tmp_xdc("pins.xdc", (
            "set_property PACKAGE_PIN U16 [get_ports {led[0]}]\n"
            "set_property PACKAGE_PIN T17 [get_ports btn]\n"
        ))
        report = lint_xdc_files([f_iostd, f_pins])
        missing = [i for i in report.issues if i.rule == "MISSING_IOSTANDARD"]
        assert len(missing) == 1
        assert missing[0].port == "btn"

    def test_single_file_api_semantics_unchanged(self, tmp_xdc):
        """lint_xdc_file 单文件直接调用的语义不变:本文件内没配仍要报。"""
        p = tmp_xdc("alone.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
        issues = lint_xdc_file(p)
        assert any(i.rule == "MISSING_IOSTANDARD" for i in issues)


class TestEncodingDegraded:
    """审计 P2:编码降级必须在用户可见报告里露出,不只进 logger。"""

    def _write_undecodable(self, tmp_path):
        p = tmp_path / "weird.xdc"
        # \xff\xff 既非合法 UTF-8 也非合法 GBK
        p.write_bytes(b"set_property PACKAGE_PIN U16 [get_ports foo]\n# \xff\xff\n")
        return p

    def test_undecodable_file_emits_warning_issue(self, tmp_path):
        p = self._write_undecodable(tmp_path)
        issues = lint_xdc_file(p)
        deg = [i for i in issues if i.rule == "ENCODING_DEGRADED"]
        assert len(deg) == 1
        assert deg[0].severity == "warning"
        # 消息含具体解码情况:哪两种编码失败、用了什么降级方式
        assert "UTF-8" in deg[0].message
        assert "GBK" in deg[0].message
        assert "替换符" in deg[0].message

    def test_degraded_visible_in_format_output(self, tmp_path):
        """输出断言(不只 caplog):format_lint_report 里能看到降级。"""
        p = self._write_undecodable(tmp_path)
        report = lint_xdc_files([p])
        text = format_lint_report(report)
        assert "ENCODING_DEGRADED" in text
        assert "替换符" in text

    def test_normal_files_no_degraded_issue(self, tmp_path):
        """UTF-8 / GBK 正常解码的文件不得出现 ENCODING_DEGRADED。"""
        utf8 = tmp_path / "ok.xdc"
        utf8.write_text(
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n",
            encoding="utf-8",
        )
        gbk = tmp_path / "gbk.xdc"
        gbk.write_bytes("# 时钟约束\n".encode("gbk"))
        for p in (utf8, gbk):
            issues = lint_xdc_file(p)
            assert all(i.rule != "ENCODING_DEGRADED" for i in issues)


class TestContinuationCreateClock:
    """审计 P1:create_clock 反斜杠续行不应误报 CLOCK_NO_PERIOD。"""

    def test_continued_create_clock_with_period_ok(self, tmp_xdc):
        """-period 在续行上的合法 create_clock 不应报错。"""
        p = tmp_xdc("cont.xdc", (
            "create_clock -name sys_clk \\\n"
            "    -period 10.000 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "CLOCK_NO_PERIOD" for i in issues)

    def test_continued_create_clock_missing_period_still_reported(self, tmp_xdc):
        """折叠后真缺 -period 的续行 create_clock 仍要报,行号取首行。"""
        p = tmp_xdc("cont2.xdc", (
            "create_clock -name sys_clk \\\n"
            "    [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        errors = [i for i in issues if i.rule == "CLOCK_NO_PERIOD"]
        assert len(errors) == 1
        assert errors[0].line == 1


class TestCrossFileConflict:
    def test_cross_file_pin_conflict(self, tmp_xdc):
        f1 = tmp_xdc("a.xdc",
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n")
        f2 = tmp_xdc("b.xdc",
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports led]\n")
        report = lint_xdc_files([f1, f2])
        cross = [i for i in report.issues if i.rule == "PIN_CONFLICT_CROSS_FILE"]
        assert len(cross) == 1


class TestFormat:
    def test_pass_report(self, tmp_xdc):
        p = tmp_xdc("good.xdc",
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "create_clock -name sys_clk -period 10.000 [get_ports clk]\n"
        )
        report = lint_xdc_files([p])
        text = format_lint_report(report)
        assert "PASS" in text

    def test_fail_report(self, tmp_xdc):
        p = tmp_xdc("bad.xdc", (
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports led]\n"
            "create_clock -name sys_clk [get_ports clk]\n"
        ))
        report = lint_xdc_files([p])
        text = format_lint_report(report)
        assert "FAIL" in text
        assert "PIN_CONFLICT" in text
        assert "CLOCK_NO_PERIOD" in text


class TestTclControlFlow:
    """XDC 中禁止 Tcl 控制流和 proc（依据 UG903）。"""

    @pytest.mark.parametrize(
        ("kw", "snippet"),
        [
            (
                "if",
                'if {$rev == "B"} {\n    set_property PACKAGE_PIN W5 [get_ports clk]\n}\n',
            ),
            (
                "for",
                "for {set i 0} {$i < 2} {incr i} {\n"
                "    set_property SLEW FAST [get_ports clk]\n}\n",
            ),
            (
                "foreach",
                "foreach p {led[0] led[1]} {\n"
                "    set_property IOSTANDARD LVCMOS18 [get_ports $p]\n}\n",
            ),
            ("while", "while {$i < 2} {\n    incr i\n}\n"),
            (
                "proc",
                "proc my_pin {p pin} {\n"
                "    set_property PACKAGE_PIN $pin [get_ports $p]\n}\n",
            ),
            ("switch", "switch $mode {\n    a { set_property SLEW FAST [get_ports clk] }\n}\n"),
        ],
    )
    def test_detects_all_control_flow_and_proc_keywords(self, tmp_xdc, kw, snippet):
        p = tmp_xdc(f"{kw}.xdc", snippet)
        issues = lint_xdc_file(p)
        cf_issues = [i for i in issues if i.rule == "TCL_CONTROL_FLOW"]
        assert len(cf_issues) == 1
        issue = cf_issues[0]
        assert issue.severity == "error"
        assert issue.line == 1
        assert kw in issue.message
        assert "UG903" in issue.message
        assert "unmanaged" in issue.message
        assert "独立 XDC" in issue.message
        assert "USED_IN" in issue.message
        assert "约束集" in issue.message

    def test_real_case_sublvds_if_and_report_output(self, tmp_xdc):
        """复现真实案例：XCZU7EV_SUBLVDS.xdc 写了 if 导致引脚约束不生效。"""
        p = tmp_xdc(
            "XCZU7EV_SUBLVDS.xdc",
            (
                "# SUB_LVDS bank constraints\n"
                "set_property -dict { PACKAGE_PIN A1 IOSTANDARD LVDS } [get_ports clk_p]\n"
                'if {$SUBLVDS_MODE eq "ON"} {\n'
                "    set_property -dict { PACKAGE_PIN B1 IOSTANDARD SUB_LVDS } [get_ports data_p]\n"
                "}\n"
            ),
        )
        report = lint_xdc_files([p])
        cf_issues = [i for i in report.issues if i.rule == "TCL_CONTROL_FLOW"]
        assert len(cf_issues) == 1
        assert cf_issues[0].line == 3
        text = format_lint_report(report)
        assert "FAIL" in text
        assert "[TCL_CONTROL_FLOW] XCZU7EV_SUBLVDS.xdc:3" in text
        assert "UG903" in text
        assert "unmanaged Tcl" in text
        assert "USED_IN" in text

    def test_detects_semicolon_continuation_namespace_and_command_subst(self, tmp_xdc):
        """支持分号分隔、续行、::if、引号/花括号命令名、[...] 替换及嵌套块。"""
        p = tmp_xdc(
            "variants.xdc",
            (
                "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]; "
                "if {$a} { set_property SLEW FAST [get_ports clk] }\n"
                "if \\\n"
                "    {$b} {\n"
                "    foreach x {clk} {\n"
                "        set_property PULLUP true [get_ports $x]\n"
                "    }\n"
                "}\n"
                '::if {$c} { set_property SLEW SLOW [get_ports clk] }\n'
                '"if" {$d} { set_property SLEW SLOW [get_ports clk] }\n'
                "{while} {$e} { incr e }\n"
                'set val [if {$f} {expr 1} else {expr 2}]\n'
                'set msg "v=[for {set i 0} {$i < 1} {incr i} {expr $i}]"\n'
                "catch { switch $m { a { } } }\n"
            ),
        )
        issues = [i for i in lint_xdc_file(p) if i.rule == "TCL_CONTROL_FLOW"]
        lines_found = [(i.line, i.message.split("`")[1]) for i in issues]
        assert lines_found == [
            (1, "if"),
            (2, "if"),
            (4, "foreach"),
            (8, "if"),
            (9, "if"),
            (10, "while"),
            (11, "if"),
            (12, "for"),
            (13, "switch"),
        ]

    def test_ignores_comments_including_continued_and_inline_comments(self, tmp_xdc):
        """整行 #、行尾 ;# 及反斜杠续行注释中的关键字不得误报；双反斜杠续行终止后仍检测。"""
        p = tmp_xdc(
            "comments.xdc",
            (
                "# if for foreach while proc switch\n"
                "    # if {$a} { set_property PACKAGE_PIN W5 [get_ports clk] }\n"
                "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk] "
                ";# switch if for while proc foreach\n"
                "# comment with continuation \\\n"
                "if {$a} { set_property PACKAGE_PIN W6 [get_ports led] }\n"
                "set_property SLEW FAST [get_ports clk] ;# inline cont \\\n"
                "foreach x {a b} { puts $x }\n"
                "# comment ending with literal double backslash \\\\\n"
                "if {$real} { set_property PULLUP true [get_ports clk] }\n"
            ),
        )
        issues = [i for i in lint_xdc_file(p) if i.rule == "TCL_CONTROL_FLOW"]
        assert len(issues) == 1
        assert issues[0].line == 9

    def test_ignores_strings_braces_and_identifiers(self, tmp_xdc):
        """引号字符串、花括号字符串、多行文本及含关键字的端口/属性名不得误报。"""
        p = tmp_xdc(
            "strings_ids.xdc",
            (
                'set_property DESCRIPTION "if for foreach while proc switch" [current_design]\n'
                'set_property NOTE "say \\"if\\" and \\[if {1} {expr 1}\\]" [current_design]\n'
                'set multiline "\n'
                "if rev B is selected\n"
                "switch voltage to 1.8V\n"
                "for details see schematic\n"
                '"\n'
                "set_property NOTE {if {$a} {switch} foreach while for proc [if {1}]} "
                "[current_design]\n"
                "set_property MULTILINE_NOTE {\n"
                "    if you change this pin\n"
                "    switch bank voltage\n"
                "} [current_design]\n"
                "set_property -dict { PACKAGE_PIN A1 IOSTANDARD LVCMOS33 } "
                "[get_ports if_clk]\n"
                "set_property -dict { PACKAGE_PIN A2 IOSTANDARD LVCMOS33 } "
                "[get_ports {switch_en}]\n"
                "set_property -dict { PACKAGE_PIN A3 IOSTANDARD LVCMOS33 } "
                "[get_ports for_test]\n"
                "set_property -dict { PACKAGE_PIN A4 IOSTANDARD LVCMOS33 } "
                "[get_ports while_ok]\n"
                "set_property -dict { PACKAGE_PIN A5 IOSTANDARD LVCMOS33 } "
                "[get_ports proc_rst]\n"
                "set_property -dict { PACKAGE_PIN A6 IOSTANDARD LVCMOS33 } "
                "[get_ports foreach_cnt]\n"
                "set_property -dict { PACKAGE_PIN A7 IOSTANDARD LVCMOS33 } [get_ports if]\n"
                "set_property -dict { PACKAGE_PIN A8 IOSTANDARD LVCMOS33 } [get_ports {switch}]\n"
                'set_property -dict { PACKAGE_PIN A9 IOSTANDARD LVCMOS33 } [get_ports "for"]\n'
                "set_property MODE if [get_ports if_clk]\n"
                'set_property STR_HASH "a # b\n'
                'if still inside quote" [current_design]\n'
                "set_property MID_QUOTE foo\"if\"bar [get_ports if_clk]\n"
                "set_property MID_BRACE foo{switch}bar [get_ports if_clk]\n"
            ),
        )
        issues = [i for i in lint_xdc_file(p) if i.rule == "TCL_CONTROL_FLOW"]
        assert issues == []

    def test_crlf_line_endings_and_line_numbers(self, tmp_path):
        """Windows CRLF 文件中续行注释不误报，后续真实控制流行号准确。"""
        p = tmp_path / "crlf_cf.xdc"
        raw = (
            "# crlf comment \\\r\n"
            "if {$ignored} { }\r\n"
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\r\n"
            "switch $mode {\r\n"
            "    a { }\r\n"
            "}\r\n"
        )
        p.write_bytes(raw.encode("utf-8"))
        issues = [i for i in lint_xdc_file(p) if i.rule == "TCL_CONTROL_FLOW"]
        assert len(issues) == 1
        assert issues[0].line == 4

    def test_gbk_xdc_with_if_detected(self, tmp_path):
        """GBK 编码 XDC 文件中的中文注释不误报，真实 if 正常检出。"""
        p = tmp_path / "gbk_if.xdc"
        content = (
            "# 如果使用子卡则启用该引脚\n"
            "if {$use_subcard} {\n"
            "    set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "}\n"
        )
        p.write_bytes(content.encode("gbk"))
        issues = [i for i in lint_xdc_file(p) if i.rule == "TCL_CONTROL_FLOW"]
        assert len(issues) == 1
        assert issues[0].line == 2

    def test_nested_blocks_bracket_comment_depth_and_autofix_skip(self, tmp_xdc):
        """elseif/else/then、namespace eval、[...] 内注释含 ]、深嵌套不死循环及 auto_fix 跳过。"""
        from vivado_mcp.analysis.xdc_auto_fixer import plan_fixes

        p = tmp_xdc(
            "nested.xdc",
            (
                "if {$a} then {\n"
                "    set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
                "} elseif {$b} {\n"
                "    for {set i 0} {$i < 1} {incr i} {\n"
                "        set x [# comment with ]\n"
                "expr 1]\n"
                "    }\n"
                "} else {\n"
                "    namespace eval myns {\n"
                "        proc helper {} {}\n"
                "    }\n"
                "}\n"
                "set safe_data {if {$a} {switch} foreach while for proc}\n"
            ),
        )
        issues = [i for i in lint_xdc_file(p) if i.rule == "TCL_CONTROL_FLOW"]
        assert [(i.line, i.message.split("`")[1]) for i in issues] == [
            (1, "if"),
            (4, "for"),
            (10, "proc"),
        ]
        # 深度超限时安全退出，不崩溃不死循环
        deep = tmp_xdc("deep.xdc", ("[" * 50) + "if {1} {}" + ("]" * 50) + "\n")
        assert isinstance(lint_xdc_file(deep), list)
        # xdc_auto_fix 不自动修改 TCL_CONTROL_FLOW，归入 skipped
        rep = plan_fixes([p])
        assert rep.patches == []
        assert any(i.rule == "TCL_CONTROL_FLOW" for i in rep.skipped)
