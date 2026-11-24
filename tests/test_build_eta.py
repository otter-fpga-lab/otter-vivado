"""Vivado 分阶段日志解析、历史耗时基准与诚实 ETA 估算测试（全模拟与 tmp_path）。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from vivado_mcp.build_eta import (
    BuildHistoryStore,
    compute_build_eta,
    find_best_log_for_target,
    make_history_key,
    parse_runme_step_timings,
)


def _write_completed_impl_log(
    path: Path,
    *,
    init_s: int = 15,
    opt_s: int = 35,
    place_s: int = 120,
    phys_s: int = 30,
    route_s: int = 200,
    bit_s: int | None = None,
    power_opt_s: int | None = None,
    with_error: bool = False,
) -> None:
    lines = [
        "# Start of session at: Mon Nov 24 10:00:00 2026",
        "Vivado v2024.2",
        "Command: init_design",
        f"init_design: Time (s): cpu = 00:00:10 ; elapsed = 00:00:{init_s:02d} .",
        "init_design completed successfully",
        "Command: opt_design",
        f"opt_design: Time (s): cpu = 00:00:25 ; elapsed = 00:00:{opt_s:02d} .",
        "opt_design completed successfully",
    ]
    if power_opt_s is not None:
        lines.extend(
            [
                "Command: power_opt_design",
                f"power_opt_design: Time (s): cpu = 00:00:10 ; elapsed = {power_opt_s} .",
                "power_opt_design completed successfully",
            ]
        )
    place_hms = f"00:{place_s // 60:02d}:{place_s % 60:02d}"
    route_hms = f"00:{route_s // 60:02d}:{route_s % 60:02d}"
    lines.extend(
        [
            "Command: place_design",
            "Phase 4.1 Global Placement",
            f"place_design: Time (s): cpu = 00:02:00 ; elapsed = {place_hms} .",
            "place_design completed successfully",
            "Command: phys_opt_design",
            f"phys_opt_design: Time (s): cpu = 00:00:20 ; elapsed = 00:00:{phys_s:02d} .",
            "phys_opt_design completed successfully",
            "Command: route_design",
            "Phase 4.1 Global Routing",
            f"route_design: Time (s): cpu = 00:03:00 ; elapsed = {route_hms} .",
            "route_design completed successfully",
        ]
    )
    if bit_s is not None:
        lines.extend(
            [
                "Command: write_bitstream -force top.bit",
                f"write_bitstream: Time (s): cpu = 00:00:20 ; elapsed = 00:00:{bit_s:02d} .",
                "write_bitstream completed successfully",
            ]
        )
    if with_error:
        lines.append("ERROR: [Route 35-1] Routing failed.")
    lines.append("INFO: [Common 17-206] Exiting Vivado...")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_completed_synth_log(
    path: Path,
    *,
    cum_elab: int = 10,
    cum_opt: int = 35,
    cum_map: int = 65,
    cum_io: int = 75,
    cum_rep: int = 80,
    synth_total: int = 84,
) -> None:
    opt_hms = f"00:00:{cum_opt:02d}"
    map_hms = f"00:{cum_map // 60:02d}:{cum_map % 60:02d}"
    io_hms = f"00:{cum_io // 60:02d}:{cum_io % 60:02d}"
    rep_hms = f"00:{cum_rep // 60:02d}:{cum_rep % 60:02d}"
    tot_hms = f"00:{synth_total // 60:02d}:{synth_total % 60:02d}"
    lines = [
        "# Start of session at: Mon Nov 24 11:00:00 2026",
        "Command: synth_design -top demo_top -part xc7a35tcpg236-1 -mode out_of_context",
        "Starting synth_design",
        "Starting RTL Elaboration : Time (s): cpu = 00:00:03 ; elapsed = 00:00:04 .",
        f"Finished RTL Elaboration : Time (s): cpu = 00:00:08 ; elapsed = 00:00:{cum_elab:02d} .",
        "Start RTL Optimization Phase 1",
        "Finished RTL Optimization Phase 1 : Time (s): cpu = 00:00:12 ; elapsed = 00:00:15 .",
        "Start Cross Boundary and Area Optimization",
        f"Finished Cross Boundary and Area Optimization : Time (s): cpu = 00:00:28 ; "
        f"elapsed = {opt_hms} .",
        "Start Technology Mapping",
        f"Finished Technology Mapping : Time (s): cpu = 00:00:55 ; elapsed = {map_hms} .",
        "Start IO Insertion",
        f"Finished IO Insertion : Time (s): cpu = 00:01:05 ; elapsed = {io_hms} .",
        "Start Writing Synthesis Report",
        f"Finished Writing Synthesis Report : Time (s): cpu = 00:01:10 ; elapsed = {rep_hms} .",
        f"synth_design: Time (s): cpu = 00:01:15 ; elapsed = {tot_hms} .",
        "synth_design completed successfully",
        "INFO: [Common 17-206] Exiting Vivado...",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_runme_step_timings_impl_sessions_and_synth_deltas(tmp_path):
    impl_log = tmp_path / "impl_runme.log"
    # Earlier failed session should be ignored in favor of last session
    impl_log.write_text(
        "# Start of session at: Sun Nov 23 09:00:00 2026\n"
        "Command: init_design\n"
        "ERROR: [Old 1-1] stale error\n"
        "Exiting Vivado...\n"
        "# Start of session at: Mon Nov 24 10:00:00 2026\n"
        "Command: init_design\n"
        "init_design: Time (s): cpu = 00:00:10 ; elapsed = 00:00:12 .\n"
        "init_design completed successfully\n"
        "Command: opt_design\n"
        "opt_design: Time (s): cpu = 00:00:20 ; elapsed = 25.5 .\n"
        "opt_design completed successfully\n"
        "Command: place_design\n"
        "Phase 4.1 Global Placement | Checksum: abc\n",
        encoding="utf-8",
    )
    parsed = parse_runme_step_timings(impl_log)
    assert parsed["flow"] == "impl"
    assert parsed["has_error"] is False
    assert parsed["is_completed"] is False
    assert parsed["completed_step_ids"] == ["init_design", "opt_design"]
    assert parsed["completed_steps_sec"] == {"init_design": 12.0, "opt_design": 25.5}
    assert parsed["active_step_id"] == "place_design"
    assert parsed["active_subphase"] == "Phase 4.1 Global Placement"

    # Synth cumulative -> per-stage delta conversion
    synth_log = tmp_path / "synth_runme.log"
    _write_completed_synth_log(
        synth_log,
        cum_elab=10,
        cum_opt=35,
        cum_map=65,
        cum_io=75,
        cum_rep=80,
        synth_total=84,
    )
    sparsed = parse_runme_step_timings(synth_log)
    assert sparsed["flow"] == "synth"
    assert sparsed["is_completed"] is True
    assert sparsed["has_error"] is False
    assert sparsed["completed_steps_sec"] == {
        "elaborate": 10.0,
        "opt_rtl": 25.0,
        "tech_map": 30.0,
        "io_buf": 10.0,
        "netlist": 9.0,  # max(80, 84) - 75
    }
    assert sparsed["total_sec"] == 84.0

    # In-progress synth stays in opt_rtl during Cross Boundary after Phase 1 finished
    synth_live = tmp_path / "synth_live.log"
    synth_live.write_text(
        "Command: synth_design -top x -part xc7a35t\n"
        "Finished RTL Elaboration : Time (s): cpu = 00:00:04 ; elapsed = 00:00:06 .\n"
        "Start RTL Optimization Phase 1\n"
        "Finished RTL Optimization Phase 1 : Time (s): cpu = 00:00:08 ; elapsed = 00:00:10 .\n"
        "Start Cross Boundary and Area Optimization\n",
        encoding="utf-8",
    )
    slive = parse_runme_step_timings(synth_live)
    assert slive["flow"] == "synth"
    assert slive["active_step_id"] == "opt_rtl"
    assert slive["is_completed"] is False

    # Symlink log safely skipped
    sym = tmp_path / "sym.log"
    try:
        sym.symlink_to(impl_log)
    except OSError:
        sym = None
    if sym is not None:
        assert parse_runme_step_timings(sym)["flow"] == ""


def test_find_best_log_for_target_follows_runs_reference(tmp_path):
    proj_dir = tmp_path / "proj"
    impl_dir = proj_dir / "demo.runs" / "impl_1"
    impl_dir.mkdir(parents=True)
    runme = impl_dir / "runme.log"
    _write_completed_impl_log(runme)

    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    batch_log = job_dir / "vivado.log"
    batch_log.write_text(
        f"Command: launch_runs impl_1\nINFO: writing log to {runme}\nExiting Vivado...\n",
        encoding="utf-8",
    )

    best, rdir, rname = find_best_log_for_target(log_path=batch_log, output_dir=job_dir)
    assert best == runme
    assert rdir == impl_dir
    assert rname == "impl_1"


def test_build_history_store_rolling_dedup_bootstrap_and_part_fallback(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("OTTER_VIVADO_CACHE_DIR", str(cache_dir))

    proj_dir = tmp_path / "fpga_proj"
    impl_dir = proj_dir / "top.runs" / "impl_1"
    impl_dir.mkdir(parents=True)
    xpr_path = proj_dir / "top.xpr"
    xpr_path.write_text("<Project/>", encoding="utf-8")

    store = BuildHistoryStore()
    assert store.store_path == cache_dir / "history" / "step_durations.json"

    key_with_part = make_history_key(
        xpr_path=str(xpr_path), run_name="impl_1", part="xc7z020clg400-1"
    )
    key_no_part = make_history_key(xpr_path=str(xpr_path), run_name="impl_1", part="")

    # 1. Incomplete / error logs are NOT bootstrapped
    runme = impl_dir / "runme.log"
    _write_completed_impl_log(runme, with_error=True)
    assert store.bootstrap_from_disk(key_with_part, impl_dir, runme) is False

    # Running (.vivado.begin.rst without .end.rst) is NOT bootstrapped
    (impl_dir / ".vivado.begin.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    _write_completed_impl_log(runme)
    assert store.bootstrap_from_disk(key_with_part, impl_dir, runme) is False

    # Clean completed run IS bootstrapped once and deduplicated across repeated polls
    (impl_dir / ".vivado.end.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    assert store.bootstrap_from_disk(key_with_part, impl_dir, runme) is True
    assert store.bootstrap_from_disk(key_with_part, impl_dir, runme) is False
    # Never writes inside user project
    assert not any(p.name == "step_durations.json" for p in proj_dir.rglob("*"))

    # 2. Rolling window keeps last 5 samples & computes medians
    # (first 400s sample from bootstrap is evicted after 5 more)
    totals_expected = [400, 420, 440, 500, 600, 800]
    for idx, route_extra in enumerate((220, 240, 300, 400, 600), 1):
        parsed = {
            "flow": "impl",
            "is_completed": True,
            "has_error": False,
            "completed_steps_sec": {
                "init_design": 15.0,
                "opt_design": 35.0,
                "place_design": 120.0,
                "phys_opt_design": 30.0,
                "route_design": float(route_extra),
            },
            "total_sec": float(200 + route_extra),
        }
        assert store.record_if_completed(key_with_part, parsed, f"sig-{idx}") is True
        # Same sig deduplicated
        assert store.record_if_completed(key_with_part, parsed, f"sig-{idx}") is False

    stats_exact = store.lookup(key_with_part)
    assert stats_exact["has_history"] is True
    assert stats_exact["sample_count"] == 5
    # Last 5 totals: [420, 440, 500, 600, 800] -> median 500.0
    assert stats_exact["hist_total_sec"] == 500.0
    assert stats_exact["step_medians"]["route_design"] == 300.0

    # Fallback lookup ignoring part_key matches when part=""
    stats_fallback = store.lookup(key_no_part)
    assert stats_fallback["has_history"] is True
    assert stats_fallback["sample_count"] == 5
    assert stats_fallback["hist_total_sec"] == 500.0
    assert totals_expected[0] == 400


def test_compute_build_eta_honest_first_run_historical_overrun_and_bitstream(tmp_path):
    store = BuildHistoryStore(store_path=tmp_path / "step_durations.json")

    proj_dir = tmp_path / "eta_proj"
    impl_dir = proj_dir / "demo.runs" / "impl_1"
    impl_dir.mkdir(parents=True)
    xpr_path = proj_dir / "demo.xpr"
    xpr_path.write_text("<Project/>", encoding="utf-8")
    runme = impl_dir / "runme.log"

    # 1. First run (no history): honest nulls, step tracking only
    runme.write_text(
        "Command: init_design\n"
        "init_design: Time (s): cpu = 00:00:10 ; elapsed = 00:00:15 .\n"
        "init_design completed successfully\n"
        "Command: opt_design\n"
        "opt_design: Time (s): cpu = 00:00:25 ; elapsed = 00:00:35 .\n"
        "opt_design completed successfully\n"
        "Command: place_design\n"
        "Phase 4.1 Global Placement\n",
        encoding="utf-8",
    )
    first_eta = compute_build_eta(
        status="running",
        run_name="impl_1",
        target_step="route_design",
        xpr_path=str(xpr_path),
        run_dir=impl_dir,
        log_path=runme,
        elapsed_sec=110.0,
        store=store,
    )
    assert first_eta is not None
    assert first_eta["flow"] == "impl"
    assert first_eta["has_history"] is False
    assert first_eta["sample_count"] == 0
    assert first_eta["basis"] == "首次运行 · 按阶段追踪"
    assert first_eta["hist_total_sec"] is None
    assert first_eta["estimated_pct"] is None
    assert first_eta["overrun"] is False
    assert first_eta["overrun_sec"] == 0.0
    assert first_eta["remaining_min_sec"] is None
    assert first_eta["remaining_max_sec"] is None
    assert first_eta["active_step_id"] == "place_design"
    assert first_eta["active_step_label"] == "布局"
    assert first_eta["active_subphase"] == "Phase 4.1 Global Placement"
    assert first_eta["completed_steps"] == 2
    # route_design target omits write_bitstream -> 5 steps
    assert first_eta["total_steps"] == 5
    assert [s["id"] for s in first_eta["steps"]] == [
        "init_design",
        "opt_design",
        "place_design",
        "phys_opt_design",
        "route_design",
    ]
    assert abs(sum(s["weight_pct"] for s in first_eta["steps"]) - 100.0) <= 0.2
    active_step = next(s for s in first_eta["steps"] if s["id"] == "place_design")
    assert active_step["state"] == "active"
    assert active_step["elapsed_sec"] == 60.0  # 110 - (15 + 35)
    assert active_step["step_pct"] is None

    # 2. Seed 3 historical route_design-only samples (no write_bitstream)
    key = make_history_key(xpr_path=str(xpr_path), run_name="impl_1", part="xc7k325t")
    for idx, scale in enumerate((0.9, 1.0, 1.1), 1):
        steps = {
            "init_design": 20.0 * scale,
            "opt_design": 40.0 * scale,
            "place_design": 140.0 * scale,
            "phys_opt_design": 40.0 * scale,
            "route_design": 260.0 * scale,
        }
        store.record_if_completed(
            key,
            {
                "flow": "impl",
                "is_completed": True,
                "has_error": False,
                "completed_steps_sec": steps,
                "total_sec": sum(steps.values()),
            },
            f"seed-{idx}",
        )

    # Even with target_step="complete", historical runs never ran write_bitstream -> omit bitstream
    hist_eta = compute_build_eta(
        status="running",
        run_name="impl_1",
        target_step="complete",
        xpr_path=str(xpr_path),
        run_dir=impl_dir,
        log_path=runme,
        elapsed_sec=120.0,  # 15+35 done, 70s in place_design (hist 140s -> 50%)
        store=store,
    )
    assert hist_eta is not None
    assert hist_eta["has_history"] is True
    assert hist_eta["sample_count"] == 3
    assert hist_eta["basis"] == "近 3 次成功中位数"
    assert hist_eta["hist_total_sec"] == 500.0
    assert hist_eta["overrun"] is False
    assert hist_eta["overrun_sec"] == 0.0
    assert "write_bitstream" not in [s["id"] for s in hist_eta["steps"]]
    assert 15.0 < hist_eta["estimated_pct"] < 40.0
    assert hist_eta["remaining_min_sec"] is not None
    assert hist_eta["remaining_max_sec"] is not None
    assert 10.0 <= hist_eta["remaining_min_sec"] < hist_eta["remaining_max_sec"]

    # 3. Overrun when elapsed exceeds historical total
    over_eta = compute_build_eta(
        status="running",
        run_name="impl_1",
        target_step="route_design",
        xpr_path=str(xpr_path),
        run_dir=impl_dir,
        log_path=runme,
        elapsed_sec=625.0,
        store=store,
    )
    assert over_eta is not None
    assert over_eta["overrun"] is True
    assert over_eta["overrun_sec"] == 125.0
    assert over_eta["remaining_min_sec"] is None
    assert over_eta["remaining_max_sec"] is None
    assert over_eta["estimated_pct"] <= 99.0

    # 4. Target step write_bitstream includes write_bitstream even if history omitted it
    bit_eta = compute_build_eta(
        status="running",
        run_name="impl_1",
        target_step="write_bitstream",
        xpr_path=str(xpr_path),
        run_dir=impl_dir,
        log_path=runme,
        elapsed_sec=120.0,
        store=store,
    )
    assert bit_eta is not None
    assert bit_eta["steps"][-1]["id"] == "write_bitstream"
    assert bit_eta["steps"][-1]["state"] == "pending"

    # 5. Completed & failed states
    _write_completed_impl_log(runme, bit_s=None)
    done_eta = compute_build_eta(
        status="completed",
        run_name="impl_1",
        target_step="route_design",
        xpr_path=str(xpr_path),
        run_dir=impl_dir,
        log_path=runme,
        store=store,
    )
    assert done_eta is not None
    assert done_eta["estimated_pct"] == 100.0
    assert done_eta["overrun"] is False
    assert done_eta["remaining_min_sec"] is None
    assert all(s["state"] == "done" for s in done_eta["steps"])

    fail_log = tmp_path / "fail_runme.log"
    fail_log.write_text(
        "Command: init_design\n"
        "init_design: Time (s): cpu = 00:00:05 ; elapsed = 00:00:10 .\n"
        "init_design completed successfully\n"
        "Command: opt_design\n"
        "ERROR: [Opt 31-1] fail\n",
        encoding="utf-8",
    )
    fail_eta = compute_build_eta(
        status="failed",
        run_name="impl_1",
        xpr_path=str(xpr_path),
        log_path=fail_log,
        store=store,
    )
    assert fail_eta is not None
    assert fail_eta["remaining_min_sec"] is None
    assert fail_eta["remaining_max_sec"] is None
    assert fail_eta["active_step_id"] == "opt_design"
    by_id = {s["id"]: s for s in fail_eta["steps"]}
    assert by_id["init_design"]["state"] == "done"
    assert by_id["opt_design"]["state"] == "failed"
    assert by_id["place_design"]["state"] == "pending"

    # 6. Idle / empty session returns None
    assert compute_build_eta(status="idle", store=store) is None
    assert compute_build_eta(status="connected", store=store) is None
    assert os.path.exists(store.store_path)
    loaded_disk = json.loads(store.store_path.read_text(encoding="utf-8"))
    assert loaded_disk["version"] == 1
    assert time.time() > 0


def test_build_eta_live_safeguards_overwritten_log_partial_impl_anon_batch_and_churn(tmp_path):
    store = BuildHistoryStore(store_path=tmp_path / "step_durations.json")

    # 1a. Older completed batch_tcl job MUST NOT adopt newer overwritten .runs/impl_1/runme.log
    proj_dir = tmp_path / "live_proj"
    impl_dir = proj_dir / "XCZU7EV_board.runs" / "impl_1"
    impl_dir.mkdir(parents=True)
    runme = impl_dir / "runme.log"
    (impl_dir / ".vivado.begin.rst").write_text("<ProcessHandle/>", encoding="utf-8")
    runme.write_text(
        "Command: init_design\n"
        "init_design: Time (s): cpu = 00:00:10 ; elapsed = 00:00:20 .\n"
        "init_design completed successfully\n"
        "Command: opt_design\n"
        "opt_design: Time (s): cpu = 00:00:20 ; elapsed = 00:00:40 .\n"
        "opt_design completed successfully\n"
        "Command: place_design\n"
        "place_design: Time (s): cpu = 00:02:00 ; elapsed = 00:05:00 .\n"
        "place_design completed successfully\n"
        "Command: phys_opt_design\n"
        "phys_opt_design: Time (s): cpu = 00:00:30 ; elapsed = 00:01:00 .\n"
        "phys_opt_design completed successfully\n"
        "Command: route_design\n"
        "Phase 4.1 Global Routing\n",
        encoding="utf-8",
    )
    old_job_dir = tmp_path / "old_batch"
    old_job_dir.mkdir()
    old_log = old_job_dir / "vivado.log"
    old_log.write_text(
        f"Command: launch_runs impl_1\nINFO: log {runme}\nExiting Vivado...\n",
        encoding="utf-8",
    )
    old_finished_at = time.time() - 600.0
    best_old, _, _ = find_best_log_for_target(
        log_path=old_log,
        output_dir=old_job_dir,
        finished_at=old_finished_at,
        status="completed",
    )
    assert best_old == old_log

    # 1b + 1c. Partial impl log (missing route_design & no Exiting/end.rst) MUST NOT be recorded
    partial_key = make_history_key(
        best_log=str(runme), run_name="impl_1", part="xczu7ev-ffvc1156-2-e"
    )
    assert partial_key[0] == "proj:xczu7ev_board"
    partial_parsed = parse_runme_step_timings(runme, run_name="impl_1")
    assert (
        store.record_if_completed(partial_key, partial_parsed, "sig-partial", run_dir=impl_dir)
        is False
    )
    # Even if forced is_completed=True without route_design, 1c blocks impl baseline!
    fake_partial = {
        "flow": "impl",
        "is_completed": True,
        "has_error": False,
        "completed_steps_sec": {"init_design": 20.0, "opt_design": 40.0, "place_design": 300.0},
        "total_sec": 360.0,
    }
    assert store.record_if_completed(partial_key, fake_partial, "sig-no-route") is False

    # Even with finished job without finished_at, active runme.log is NOT recorded
    compute_build_eta(
        status="completed",
        kind="batch_tcl",
        log_path=old_log,
        output_dir=old_job_dir,
        store=store,
    )
    assert store.lookup(partial_key)["has_history"] is False

    # 1d. Running OOC job after "synth_design completed successfully" (no Exiting Vivado yet)
    # MUST NOT bootstrap mid-run into history
    ooc_dir = tmp_path / "ooc_active"
    ooc_dir.mkdir()
    ooc_log = ooc_dir / "vivado.log"
    ooc_log.write_text(
        "Command: synth_design -top uart_tx -part xc7a35t -mode out_of_context\n"
        "Finished RTL Elaboration : Time (s): cpu = 00:00:04 ; elapsed = 00:00:08 .\n"
        "Finished Writing Synthesis Report : Time (s): cpu = 00:00:20 ; elapsed = 00:00:40 .\n"
        "synth_design: Time (s): cpu = 00:00:22 ; elapsed = 00:00:45 .\n"
        "synth_design completed successfully\n"
        "Command: report_timing_summary -file timing.rpt\n",
        encoding="utf-8",
    )
    ooc_eta = compute_build_eta(
        status="running",
        kind="ooc_synth",
        top="uart_tx",
        part="xc7a35t",
        output_dir=ooc_dir,
        log_path=ooc_log,
        elapsed_sec=48.0,
        store=store,
    )
    assert ooc_eta is not None
    assert ooc_eta["has_history"] is False
    ooc_key = make_history_key(top="uart_tx", kind="ooc_synth", part="xc7a35t")
    assert store.lookup(ooc_key)["has_history"] is False

    # 2. Anonymous batch_tcl jobs NEVER pool under shared/blank key
    for idx, generic_name in enumerate(("batch_run.tcl", "run.tcl", "tmp_job1.tcl"), 1):
        anon_dir = tmp_path / f"anon_{idx}"
        anon_dir.mkdir()
        anon_log = anon_dir / "vivado.log"
        anon_log.write_text(
            "Command: read_verilog a.v\n"
            "read_verilog: Time (s): cpu = 00:00:01 ; elapsed = 00:00:05 .\n"
            "Exiting Vivado...\n",
            encoding="utf-8",
        )
        anon_eta = compute_build_eta(
            status="completed",
            kind="batch_tcl",
            output_dir=anon_dir,
            log_path=anon_log,
            tcl_path=anon_dir / generic_name,
            elapsed_sec=5.0,
            store=store,
        )
        assert anon_eta is not None
        assert anon_eta["has_history"] is False
        assert anon_eta["sample_count"] == 0

    named_key = make_history_key(
        kind="batch_tcl",
        tcl_path=str(tmp_path / "my_custom_flow.tcl"),
    )
    assert named_key[0] == "tcl:my_custom_flow"

    # 3. >5 jobs seen_sigs dedup prevents 5s-poll rewrite churn + external cache deletion resets
    churn_key = make_history_key(xpr_path=str(proj_dir / "churn.xpr"), run_name="impl_1")
    for idx in range(1, 8):
        sample = {
            "flow": "impl",
            "is_completed": True,
            "has_error": False,
            "completed_steps_sec": {
                "init_design": 10.0,
                "opt_design": 20.0,
                "place_design": 60.0,
                "route_design": float(100 + idx * 10),
            },
            "total_sec": float(190 + idx * 10),
        }
        assert store.record_if_completed(churn_key, sample, f"churn-{idx}") is True
    mtime_before = store.store_path.stat().st_mtime_ns
    # Re-polling job #1 (already evicted from 5-sample window, still in seen_sigs) -> NO rewrite!
    assert (
        store.record_if_completed(
            churn_key,
            {
                "flow": "impl",
                "is_completed": True,
                "has_error": False,
                "completed_steps_sec": {"route_design": 110.0},
                "total_sec": 200.0,
            },
            "churn-1",
        )
        is False
    )
    assert store.store_path.stat().st_mtime_ns == mtime_before
    assert store.lookup(churn_key)["sample_count"] == 5

    # External deletion resets cached in-memory state cleanly
    store.store_path.unlink()
    assert store.lookup(churn_key)["has_history"] is False

    # 1c defense-in-depth: missing active step hist_sec prevents false overrun
    partial_hist_key = make_history_key(xpr_path=str(proj_dir / "cov.xpr"), run_name="impl_1")
    store.record_if_completed(
        partial_hist_key,
        {
            "flow": "impl",
            "is_completed": True,
            "has_error": False,
            "completed_steps_sec": {
                "init_design": 10.0,
                "opt_design": 20.0,
                "place_design": 50.0,
                "route_design": 80.0,
            },
            "total_sec": 160.0,
        },
        "cov-1",
    )
    cov_log = tmp_path / "cov_runme.log"
    cov_log.write_text(
        "Command: init_design\n"
        "init_design: Time (s): cpu = 00:00:05 ; elapsed = 00:00:10 .\n"
        "init_design completed successfully\n"
        "Command: power_opt_design\n",
        encoding="utf-8",
    )
    cov_eta = compute_build_eta(
        status="running",
        run_name="impl_1",
        xpr_path=str(proj_dir / "cov.xpr"),
        log_path=cov_log,
        elapsed_sec=200.0,
        store=store,
    )
    assert cov_eta is not None
    assert cov_eta["overrun"] is False
    assert cov_eta["remaining_min_sec"] is None

