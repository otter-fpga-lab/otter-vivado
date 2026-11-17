"""XPR 离线解析器测试。

覆盖：核心解析（对合成 fixture 断言字段）、降级路径（缺 Part/无 IP）、
边界（文件不存在 / 空 / 截断 / 过大 / 格式错 / 非 Project 根 / 错扩展名）。
跨平台：全部用 pathlib + tmp_path，无硬编码绝对路径 / 假盘符 / USERPROFILE。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vivado_mcp.analysis.xpr_parser import (
    XprConfig,
    XprStepHook,
    format_xpr,
    parse_xpr,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_SAMPLE = str(_FIXTURES / "sample_project.xpr")


# ====================================================================== #
#  核心解析
# ====================================================================== #


class TestParseXpr:
    """对合成 fixture 断言关键字段（同真机基准的字段形状）。"""

    def test_basic_fields(self):
        cfg = parse_xpr(_SAMPLE)
        assert cfg.part == "xc7a35tcsg324-1"
        assert cfg.top == "top_module"
        assert cfg.sim_top == "tb_top"
        assert cfg.active_sim_set == "sim_1"
        assert cfg.xsim_radix == "hex"
        assert cfg.default_lib == "xil_defaultlib"
        assert cfg.version == "7"
        assert cfg.minor == "40"
        assert cfg.project_path_attr == "C:/work/demo/demo_proj.xpr"

    def test_design_srcs_grouping(self):
        """DesignSrcs = 4（2 .v + 1 .mem + 1 .xci）。"""
        cfg = parse_xpr(_SAMPLE)
        design = cfg.design_srcs
        assert len(design) == 4
        names = [Path(f.path_raw).name for f in design]
        assert "alu.v" in names
        assert "top_module.v" in names
        assert "lut_init.mem" in names
        assert "fifo_gen_0.xci" in names
        # 全部归属 DesignSrcs 类型
        assert all(f.fileset_type == "DesignSrcs" for f in design)

    def test_constrs_and_sim(self):
        cfg = parse_xpr(_SAMPLE)
        assert len(cfg.constrs) == 1
        assert Path(cfg.constrs[0].path_raw).name == "constraints.xdc"
        assert len(cfg.sim_srcs) == 1
        assert Path(cfg.sim_srcs[0].path_raw).name == "tb_top.v"

    def test_ip_detection(self):
        """.xci 被识别为 IP；.v/.mem 不是。"""
        cfg = parse_xpr(_SAMPLE)
        ips = cfg.ips
        assert len(ips) == 1
        assert Path(ips[0].path_raw).name == "fifo_gen_0.xci"
        assert ips[0].is_ip is True
        for f in cfg.design_srcs:
            if f.path_raw.endswith((".v", ".mem")):
                assert f.is_ip is False

    def test_used_in_attrs(self):
        """UsedIn 属性被采集。"""
        cfg = parse_xpr(_SAMPLE)
        alu = next(f for f in cfg.design_srcs if f.path_raw.endswith("alu.v"))
        assert "synthesis" in alu.used_in
        assert "implementation" in alu.used_in
        assert "simulation" in alu.used_in
        # .mem 只用于 synthesis/simulation
        mem = next(f for f in cfg.design_srcs if f.path_raw.endswith(".mem"))
        assert "implementation" not in mem.used_in

    def test_runs(self):
        """synth_1 + impl_1，含 Strategy 与 SynthRun 关联。"""
        cfg = parse_xpr(_SAMPLE)
        assert len(cfg.runs) == 2
        by_id = {r.run_id: r for r in cfg.runs}

        synth = by_id["synth_1"]
        assert synth.run_type == "Ft3:Synth"
        assert synth.part == "xc7a35tcsg324-1"
        assert synth.state == "current"
        assert synth.strategy == "Vivado Synthesis Defaults"
        assert synth.flow == "Vivado Synthesis 2019"
        assert synth.src_set == "sources_1"
        assert synth.constrs_set == "constrs_1"

        impl = by_id["impl_1"]
        assert impl.run_type == "Ft2:EntireDesign"
        assert impl.synth_run == "synth_1"
        assert impl.strategy == "Vivado Implementation Defaults"

    def test_macros_resolution(self):
        """$PPRDIR 展开为工程目录（fixture 所在 fixtures 目录）。"""
        cfg = parse_xpr(_SAMPLE)
        assert "$PPRDIR" in cfg.macros
        # $PPRDIR 等于工程目录（.xpr 父目录）
        assert cfg.macros["$PPRDIR"] == cfg.project_dir
        # 其余宏按默认布局推断（含工程目录前缀）
        assert cfg.macros["$PSRCDIR"].startswith(cfg.project_dir)

    def test_path_resolved_expands_ppr(self):
        """path_resolved 把 $PPRDIR 展开并规整 .. 段。"""
        cfg = parse_xpr(_SAMPLE)
        alu = next(f for f in cfg.design_srcs if f.path_raw.endswith("alu.v"))
        # 原样保留宏
        assert alu.path_raw.startswith("$PPRDIR")
        # 解析视图不再含 $PPRDIR，且 .. 已规整
        assert "$PPRDIR" not in alu.path_resolved
        assert ".." not in alu.path_resolved
        assert alu.path_resolved.endswith("sources/rtl/alu.v")

    def test_to_dict_structure(self):
        cfg = parse_xpr(_SAMPLE)
        d = cfg.to_dict()
        for key in (
            "part", "top", "sim_top", "design_srcs", "constrs",
            "sim_srcs", "ips", "runs", "macros",
        ):
            assert key in d
        assert isinstance(d["design_srcs"], list)
        assert isinstance(d["runs"], list)
        assert len(d["ips"]) == 1


# ====================================================================== #
#  format_xpr
# ====================================================================== #


class TestFormatXpr:
    """中文摘要渲染。"""

    def test_summary_contains_key_fields(self):
        cfg = parse_xpr(_SAMPLE)
        text = format_xpr(cfg)
        assert "工程信息" in text
        assert "xc7a35tcsg324-1" in text
        assert "top_module" in text
        assert "synth_1" in text
        assert "impl_1" in text
        assert "fifo_gen_0.xci" in text
        assert "[IP]" in text

    def test_summary_no_ip_graceful(self, tmp_path):
        """无 IP 时摘要优雅显示"(无 IP)"。"""
        xpr = tmp_path / "noip.xpr"
        xpr.write_text(
            '<?xml version="1.0"?>'
            '<Project Version="7" Minor="40" Path="x.xpr">'
            '<Configuration><Option Name="Part" Val="xc7a35tcsg324-1"/></Configuration>'
            '<FileSets>'
            '<FileSet Name="sources_1" Type="DesignSrcs">'
            '<File Path="$PPRDIR/a.v"/>'
            '<Config><Option Name="TopModule" Val="a"/></Config>'
            '</FileSet>'
            '</FileSets>'
            '</Project>',
            encoding="utf-8",
        )
        cfg = parse_xpr(str(xpr))
        assert cfg.ips == []
        text = format_xpr(cfg)
        assert "(无 IP)" in text


# ====================================================================== #
#  降级路径
# ====================================================================== #


class TestDegraded:
    """缺字段 / 空 fileset 的优雅降级（不抛异常）。"""

    def test_missing_part(self, tmp_path):
        """无 Part 时 part 为空串，仍可解析。"""
        xpr = tmp_path / "nopart.xpr"
        xpr.write_text(
            '<Project Version="7" Minor="40">'
            '<Configuration/>'
            '<FileSets/>'
            '<Runs/>'
            '</Project>',
            encoding="utf-8",
        )
        cfg = parse_xpr(str(xpr))
        assert cfg.part == ""
        assert cfg.top == ""
        assert cfg.files == []
        assert cfg.runs == []

    def test_run_without_strategy(self, tmp_path):
        """Run 缺 Strategy 时 strategy/flow 为空串。"""
        xpr = tmp_path / "norun.xpr"
        xpr.write_text(
            '<Project Version="7" Minor="40">'
            '<Runs><Run Id="synth_1" Type="Ft3:Synth" Part="xc7a35tcsg324-1"/></Runs>'
            '</Project>',
            encoding="utf-8",
        )
        cfg = parse_xpr(str(xpr))
        assert len(cfg.runs) == 1
        assert cfg.runs[0].strategy == ""
        assert cfg.runs[0].flow == ""

    def test_file_without_path_skipped(self, tmp_path):
        """缺 Path 属性的 File 被跳过，不报错。"""
        xpr = tmp_path / "skip.xpr"
        xpr.write_text(
            '<Project Version="7" Minor="40">'
            '<FileSets><FileSet Name="sources_1" Type="DesignSrcs">'
            '<File/>'
            '<File Path="$PPRDIR/a.v"/>'
            '</FileSet></FileSets>'
            '</Project>',
            encoding="utf-8",
        )
        cfg = parse_xpr(str(xpr))
        assert len(cfg.design_srcs) == 1


# ====================================================================== #
#  边界 / 错误处理
# ====================================================================== #


class TestBoundaries:
    """文件不存在 / 空 / 截断 / 过大 / 格式错 / 非 Project / 错扩展名。"""

    def test_file_not_found(self):
        missing = _FIXTURES / "does_not_exist.xpr"
        with pytest.raises(FileNotFoundError):
            parse_xpr(str(missing))

    def test_wrong_extension(self, tmp_path):
        bad = tmp_path / "demo.xml"
        bad.write_text("<Project/>", encoding="utf-8")
        with pytest.raises(ValueError, match="扩展名"):
            parse_xpr(str(bad))

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.xpr"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="XML 解析失败"):
            parse_xpr(str(empty))

    def test_truncated_xml(self, tmp_path):
        """截断的 XML（标签未闭合）抛 ValueError。"""
        trunc = tmp_path / "trunc.xpr"
        trunc.write_text(
            '<?xml version="1.0"?><Project Version="7"><Configuration>',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="XML 解析失败"):
            parse_xpr(str(trunc))

    def test_invalid_content(self, tmp_path):
        bad = tmp_path / "bad.xpr"
        bad.write_text("this is not xml {{{", encoding="utf-8")
        with pytest.raises(ValueError, match="XML 解析失败"):
            parse_xpr(str(bad))

    def test_non_project_root(self, tmp_path):
        """合法 XML 但根不是 <Project> 抛 ValueError。"""
        wrong = tmp_path / "wrong.xpr"
        wrong.write_text("<NotAProject><foo/></NotAProject>", encoding="utf-8")
        with pytest.raises(ValueError, match="Project"):
            parse_xpr(str(wrong))

    def test_file_too_large(self, tmp_path):
        big = tmp_path / "huge.xpr"
        big.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
        with pytest.raises(ValueError, match="文件过大"):
            parse_xpr(str(big))


# ====================================================================== #
#  数据类完整性
# ====================================================================== #


def test_empty_config_to_dict():
    """空 XprConfig 的 to_dict 不抛异常且结构完整。"""
    cfg = XprConfig()
    d = cfg.to_dict()
    assert d["part"] == ""
    assert d["files"] == []
    assert d["runs"] == []


class TestStepHooks:
    """Run 下 Step 的 PreStepTclHook / PostStepTclHook 解析与三类路径标注。"""

    def test_sample_project_has_empty_step_hooks(self):
        cfg = parse_xpr(_SAMPLE)
        assert all(r.step_hooks == () for r in cfg.runs)
        assert "步骤钩子" not in format_xpr(cfg)

    def test_step_hooks_classified_and_formatted(self, tmp_path):
        proj = tmp_path / "proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        in_pre = scripts / "vivado_route_pre.tcl"
        in_pre.write_text("set_param general.maxThreads 1\n", encoding="utf-8")
        rel_post = scripts / "rel_post.tcl"
        rel_post.write_text("set_param general.maxThreads 8\n", encoding="utf-8")
        utils_pre = proj / "demo.srcs" / "utils_1" / "synth_pre.tcl"
        utils_pre.parent.mkdir(parents=True)
        utils_pre.write_text("# utils\n", encoding="utf-8")

        outside = tmp_path / "temp_like" / "ext_hook.tcl"
        outside.parent.mkdir(parents=True)
        outside.write_text("# external or temp\n", encoding="utf-8")

        xpr = proj / "demo.xpr"
        xpr.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Project Version="7" Minor="40">\n'
            '  <Configuration><Option Name="Part" Val="xczu7ev-ffvc1156-2-e"/></Configuration>\n'
            '  <Runs>\n'
            '    <Run Id="synth_1" Type="Ft3:Synth" Part="xczu7ev-ffvc1156-2-e" State="current">\n'
            '      <Strategy Version="1" Minor="2">\n'
            '        <StratHandle Name="Vivado Synthesis Defaults" Flow="Vivado Synthesis 2024"/>\n'
            '        <Step Id="synth_design" PreStepTclHook="$PSRCDIR/utils_1/synth_pre.tcl" '
            'PostStepTclHook=""/>\n'
            '      </Strategy>\n'
            '    </Run>\n'
            '    <Run Id="impl_1" Type="Ft2:EntireDesign" SynthRun="synth_1" State="current">\n'
            '      <Strategy Version="1" Minor="2">\n'
            '        <StratHandle Name="Vivado Implementation Defaults" '
            'Flow="Vivado Implementation 2024"/>\n'
            '        <Step Id="place_design" PostStepTclHook="scripts/rel_post.tcl"/>\n'
            f'        <Step Id="route_design" '
            f'PreStepTclHook="$PPRDIR/scripts/vivado_route_pre.tcl" '
            f'PostStepTclHook="{outside.as_posix()}"/>\n'
            '        <Step Id="write_bitstream" '
            'PreStepTclHook="$PPRDIR/scripts/missing.tcl" '
            'PostStepTclHook="$PPRDIR/../temp_like/ext_hook.tcl"/>\n'
            '      </Strategy>\n'
            '    </Run>\n'
            '  </Runs>\n'
            '</Project>\n',
            encoding="utf-8",
        )

        cfg = parse_xpr(str(xpr))
        by_id = {r.run_id: r for r in cfg.runs}

        synth_hooks = by_id["synth_1"].step_hooks
        assert synth_hooks == (
            XprStepHook(
                step_id="synth_design",
                hook_type="pre",
                path_raw="$PSRCDIR/utils_1/synth_pre.tcl",
                path_resolved=utils_pre.as_posix(),
                category="工程内",
                exists=True,
                labels=("工程内",),
            ),
        )

        impl_hooks = by_id["impl_1"].step_hooks
        assert len(impl_hooks) == 5
        assert [
            (h.step_id, h.hook_type, h.labels, h.category, h.exists)
            for h in impl_hooks
        ] == [
            ("place_design", "post", ("工程内",), "工程内", True),
            ("route_design", "pre", ("工程内",), "工程内", True),
            ("route_design", "post", ("工程外/TEMP",), "工程外/TEMP", True),
            ("write_bitstream", "pre", ("工程内", "不存在"), "工程内；不存在", False),
            ("write_bitstream", "post", ("工程外/TEMP",), "工程外/TEMP", True),
        ]

        d = cfg.to_dict()
        impl_dict = next(r for r in d["runs"] if r["run_id"] == "impl_1")
        assert len(impl_dict["step_hooks"]) == 5
        assert impl_dict["step_hooks"][1]["category"] == "工程内"
        assert impl_dict["step_hooks"][3]["labels"] == ("工程内", "不存在")

        text = format_xpr(cfg)
        assert "步骤钩子(1 个):" in text
        assert "步骤钩子(5 个):" in text
        assert f"$PSRCDIR/utils_1/synth_pre.tcl -> {utils_pre.as_posix()}" in text
        assert "route_design [PRE] [工程内] $PPRDIR/scripts/vivado_route_pre.tcl" in text
        assert f"route_design [POST] [工程外/TEMP] {outside.as_posix()}" in text
        assert (
            "write_bitstream [PRE] [工程内；不存在] $PPRDIR/scripts/missing.tcl"
            in text
        )

    def test_step_hooks_multi_label_temp_env_and_deleted_external(self, tmp_path):
        proj = tmp_path / "env_proj"
        proj.mkdir()
        deleted_ext = tmp_path / "gone" / "ext.tcl"
        xpr = proj / "env_proj.xpr"
        xpr.write_text(
            '<Project Version="7" Minor="40"><Runs><Run Id="impl_1" Type="Ft2:EntireDesign">'
            '<Strategy>'
            '<Step Id="opt_design" PreStepTclHook="%TEMP%/tmp_hook.tcl" '
            'PostStepTclHook="$env(TEMP)/tcl_tmp.tcl"/>'
            f'<Step Id="route_design" PreStepTclHook="{deleted_ext.as_posix()}" '
            'PostStepTclHook="$CUSTOM_MACRO/unknown.tcl"/>'
            '</Strategy></Run></Runs></Project>',
            encoding="utf-8",
        )
        cfg = parse_xpr(str(xpr))
        hooks = cfg.runs[0].step_hooks
        assert [(h.step_id, h.hook_type, h.labels, h.exists) for h in hooks] == [
            ("opt_design", "pre", ("工程外/TEMP", "不存在"), False),
            ("opt_design", "post", ("工程外/TEMP", "不存在"), False),
            ("route_design", "pre", ("工程外/TEMP", "不存在"), False),
            ("route_design", "post", ("不存在",), False),
        ]
        text = format_xpr(cfg)
        assert "opt_design [PRE] [工程外/TEMP；不存在] %TEMP%/tmp_hook.tcl" in text
        assert "opt_design [POST] [工程外/TEMP；不存在] $env(TEMP)/tcl_tmp.tcl" in text
        assert f"route_design [PRE] [工程外/TEMP；不存在] {deleted_ext.as_posix()}" in text
        assert "route_design [POST] [不存在] $CUSTOM_MACRO/unknown.tcl" in text

    async def test_parse_xpr_mcp_tool_renders_hooks(self, tmp_path):
        from vivado_mcp.tools.introspect_tools import parse_xpr as parse_xpr_tool

        proj = tmp_path / "mcp_proj"
        scripts = proj / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "pre.tcl").write_text("# pre\n", encoding="utf-8")
        xpr = proj / "mcp_proj.xpr"
        xpr.write_text(
            '<Project Version="7" Minor="40"><Runs>'
            '<Run Id="impl_1" Type="Ft2:EntireDesign"><Strategy>'
            '<Step Id="route_design" PreStepTclHook="$PPRDIR/scripts/pre.tcl" '
            'PostStepTclHook="C:/no_such_dir/post.tcl"/>'
            '</Strategy></Run></Runs></Project>',
            encoding="utf-8",
        )
        out = await parse_xpr_tool(str(xpr))
        assert "route_design [PRE] [工程内] $PPRDIR/scripts/pre.tcl" in out
        assert "route_design [POST] [工程外/TEMP；不存在] C:/no_such_dir/post.tcl" in out
