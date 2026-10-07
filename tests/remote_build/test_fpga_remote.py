import json
import shutil
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from vivado_mcp.remote_build import core as fpga_remote

TOOL_ROOT = Path(__file__).resolve().parents[2]


class ProcessTests(unittest.TestCase):
    @mock.patch("vivado_mcp.remote_build.core.subprocess.run")
    def test_stdin_uses_utf8_bytes_with_lf_line_endings(self, run_mock):
        run_mock.return_value = fpga_remote.subprocess.CompletedProcess(
            ["ssh"], 0, stdout=b"ok\n", stderr=b""
        )

        result = fpga_remote.run_process(["ssh"], input_text="set -eu\r\nprintf 'ok\\n'\r\n")

        self.assertEqual(run_mock.call_args.kwargs["input"], b"set -eu\nprintf 'ok\\n'\n")
        self.assertNotIn("text", run_mock.call_args.kwargs)
        self.assertEqual(result.stdout, "ok\n")


class RemoteScriptContractTests(unittest.TestCase):
    def test_vivado_script_rebuilds_generated_bd_content(self):
        script = (fpga_remote.ASSET_ROOT / "vivado_project_build.tcl").read_text(encoding="utf-8")
        self.assertIn("generate_target all $bd_file", script)
        self.assertIn("make_wrapper -files [list $bd_file] -top -import", script)
        self.assertIn("set_property INCREMENTAL_CHECKPOINT {} $run_object", script)

    def test_runner_publishes_final_state_after_package_checksum(self):
        script = (fpga_remote.ASSET_ROOT / "run_vivado_build.sh").read_text(encoding="utf-8")
        packaging = script.index("printf 'PACKAGING\\n' > \"$state_file\"")
        package_checksum = script.index('sha256sum "$(basename "$package")"')
        final_state = script.rindex('printf \'%s\\n\' "$build_result" > "$state_file"')
        self.assertLess(packaging, package_checksum)
        self.assertLess(package_checksum, final_state)


class ConfigTests(unittest.TestCase):
    def test_shipped_config_loads(self):
        config = fpga_remote.load_config(TOOL_ROOT / "config" / "remote-hosts.example.json")
        self.assertIn("example", config["hosts"])
        self.assertEqual(config["hosts"]["example"]["scheduler"], "nohup")
        self.assertEqual(config["hosts"]["example"]["retention"]["keep_succeeded"], 1)

    def test_root_work_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "hosts.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "hosts": {
                            "bad": {
                                "ssh_alias": "bad",
                                "work_root": "/",
                                "scheduler": "nohup",
                                "max_jobs": 1,
                                "max_parallel_builds": 1,
                                "warn_free_gib": 1,
                                "min_free_gib": 1,
                                "tools": {"vivado": {}, "petalinux": {}},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(fpga_remote.CliError):
                fpga_remote.load_config(path)


class HandleTests(unittest.TestCase):
    def test_workspace_handle_and_paths_are_isolated(self):
        profile = {"work_root": "/work", "shared": {"windows_root": "C:/share"}}
        handles = [
            fpga_remote.parse_handle(f"example/{name}/demo/20260908T120000-abcd1234")
            for name in ("task-a", "task-b")
        ]
        self.assertEqual(str(handles[0]), "example/task-a/demo/20260908T120000-abcd1234")
        self.assertNotEqual(
            fpga_remote.remote_build_root(profile, handles[0]),
            fpga_remote.remote_build_root(profile, handles[1]),
        )
        self.assertNotEqual(
            fpga_remote.shared_result_dir(profile, handles[0]),
            fpga_remote.shared_result_dir(profile, handles[1]),
        )
        for workspace in ("..", "../other", "/", ""):
            with self.assertRaises(fpga_remote.CliError):
                fpga_remote.require_workspace(workspace)

    def test_handle_round_trip(self):
        handle = fpga_remote.parse_handle("example/demo/20260822T120000")
        self.assertEqual(str(handle), "example/demo/20260822T120000")

    def test_invalid_handle_rejected(self):
        with self.assertRaises(fpga_remote.CliError):
            fpga_remote.parse_handle("example/../../etc")


class RetentionTests(unittest.TestCase):
    def test_retention_does_not_mix_workspaces_or_select_queued_jobs(self):
        records = [
            fpga_remote.BuildRecord(
                fpga_remote.parse_handle(f"example/{workspace}/demo/{build_id}"), state
            )
            for workspace, build_id, state in (
                ("task-a", "20260908T100000", "SUCCEEDED"),
                ("task-a", "20260908T110000", "SUCCEEDED"),
                ("task-b", "20260908T100000", "SUCCEEDED"),
                ("task-a", "20260908T120000", "QUEUED"),
            )
        ]
        candidates = fpga_remote.select_prune_candidates(
            records, keep_succeeded=1, keep_failed=0, keep_other=0
        )
        self.assertEqual(
            [str(record.handle) for record in candidates], ["example/task-a/demo/20260908T100000"]
        )

    @staticmethod
    def record(project: str, build_id: str, state: str):
        return fpga_remote.BuildRecord(
            handle=fpga_remote.BuildHandle("example", project, build_id),
            state=state,
        )

    def test_retention_is_per_project_and_never_selects_active_builds(self):
        records = [
            self.record("alpha", "20260822T100000", "SUCCEEDED"),
            self.record("alpha", "20260822T110000", "SUCCEEDED"),
            self.record("alpha", "20260822T120000", "FAILED"),
            self.record("alpha", "20260822T130000", "FAILED"),
            self.record("alpha", "20260822T140000", "FAILED"),
            self.record("alpha", "20260822T150000", "UNKNOWN"),
            self.record("alpha", "20260822T160000", "UNKNOWN"),
            self.record("alpha", "20260822T170000", "RUNNING"),
            self.record("beta", "20260822T100000", "SUCCEEDED"),
        ]

        candidates = fpga_remote.select_prune_candidates(
            records,
            keep_succeeded=1,
            keep_failed=2,
            keep_other=1,
        )

        self.assertEqual(
            [str(record.handle) for record in candidates],
            [
                "example/alpha/20260822T100000",
                "example/alpha/20260822T120000",
                "example/alpha/20260822T150000",
            ],
        )

    def test_invalid_remote_record_is_rejected(self):
        with self.assertRaises(fpga_remote.CliError):
            fpga_remote.parse_build_records("example", "../../etc\t20260822T120000\tFAILED\n")

    @mock.patch("vivado_mcp.remote_build.core.ssh_bash")
    def test_purge_script_checks_resolved_path_marker_state_and_pid(self, ssh_mock):
        ssh_mock.return_value = "PURGED=/safe\n"
        profile = {"work_root": "/srv/vivado-builds", "ssh_alias": "example"}
        handle = fpga_remote.BuildHandle("example", "demo", "20260822T120000")

        fpga_remote.purge_remote_build(profile, handle)

        script = ssh_mock.call_args.args[1]
        delete_index = script.index('rm -rf -- "$root_real"')
        for guard in (
            'root_real=$(realpath -e -- "$root")',
            'test "$root_real" = "$work_root_real/demo/20260822T120000"',
            'test "$(cat "$root_real/.fpga-remote-work")" = fpga-remote-v1',
            'case "$state" in',
            "Invalid runner PID",
            'kill -0 "$pid"',
        ):
            self.assertLess(script.index(guard), delete_index)


class ArtifactSummaryTests(unittest.TestCase):
    @staticmethod
    def make_artifacts(root: Path):
        artifacts = root / "artifacts"
        artifacts.mkdir()
        contents = {
            "build_info.txt": (
                "vivado_version=2024.2\npart=xczu7ev-ffvc1156-2-i\ntop=XCZU7EV_TOP\n"
            ),
            "timing_summary.rpt": (
                "    WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints "
                "WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints "
                "WPWS(ns) TPWS(ns) TPWS Failing Endpoints TPWS Total Endpoints\n"
                "    ------- ------- --------------------- ------------------- "
                "------- ------- --------------------- ------------------- "
                "-------- -------- ---------------------- --------------------\n"
                "      0.262 0.000 0 100 0.010 0.000 0 100 0.000 0.000 0 50\n"
                "All user specified timing constraints are met.\n"
                "5. checking no_input_delay (30)\n"
                "6. checking no_output_delay (31)\n"
            ),
            "utilization.rpt": "utilization\n",
            "drc.rpt": ("Checks found: 2\n| DRC-1 | Warning | test warning | 2 |\n"),
            "methodology.rpt": (
                "Checks found: 1\n| TIMING-1 | Critical Warning | test warning | 1 |\n"
            ),
            "route_status.rpt": (
                "# of fully routed nets............. : 100 :\n"
                "# of nets with routing errors...... : 0 :\n"
            ),
            "cdc.rpt": ("Info source destination Safely Timed None 10 10 0 0 0\n"),
            "XCZU7EV_TOP.bit": "bit\n",
            "post_route.dcp": "dcp\n",
        }
        for name, content in contents.items():
            (artifacts / name).write_text(content, encoding="utf-8")
        return artifacts

    def test_success_artifacts_are_summarized_without_rehashing(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            self.make_artifacts(root)

            with mock.patch(
                "vivado_mcp.remote_build.core.sha256_file",
                side_effect=AssertionError("Unnecessary artifact hash"),
            ):
                summary = fpga_remote.summarize_artifacts(root, require_success=True)

            self.assertNotIn("sha256_verified", summary)
            self.assertEqual(summary["file_count"], 9)
            self.assertEqual(summary["quality_status"], "PASS_WITH_WARNINGS")
            self.assertEqual(summary["timing"]["wns_ns"], 0.262)
            self.assertEqual(summary["timing"]["whs_ns"], 0.01)
            self.assertEqual(summary["timing"]["no_input_delay"], 30)
            self.assertEqual(summary["route"]["routing_errors"], 0)
            self.assertEqual(summary["methodology"]["critical_warnings"], 1)
            self.assertEqual(summary["cdc"]["unsafe"], 0)

    def test_missing_bitstream_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            artifacts = self.make_artifacts(root)
            (artifacts / "XCZU7EV_TOP.bit").unlink()
            with self.assertRaises(fpga_remote.CliError):
                fpga_remote.summarize_artifacts(root, require_success=True)


class RunWorkflowTests(unittest.TestCase):
    def test_package_delivery_controls_cleanup(self):
        for failure in (None, "corrupt_package", "missing_bitstream"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                artifacts = ArtifactSummaryTests.make_artifacts(root)
                if failure == "missing_bitstream":
                    (artifacts / "XCZU7EV_TOP.bit").unlink()
                package = root / "result.tar.gz"
                with tarfile.open(package, "w:gz") as archive:
                    archive.add(artifacts, arcname="artifacts")
                digest = fpga_remote.sha256_file(package)
                if failure == "corrupt_package":
                    with package.open("ab") as stream:
                        stream.write(b"corrupted")

                def download(profile, remote_path, local_path):
                    if remote_path.endswith(".sha256"):
                        local_path.write_text(digest + "  result.tar.gz\n", encoding="utf-8")
                    else:
                        shutil.copyfile(package, local_path)

                with (
                    mock.patch(
                        "vivado_mcp.remote_build.core.submit_remote_build",
                        return_value=self.submission(),
                    ),
                    mock.patch(
                        "vivado_mcp.remote_build.core.get_profile",
                        return_value={"work_root": "/remote"},
                    ),
                    mock.patch(
                        "vivado_mcp.remote_build.core.wait_for_build",
                        return_value={"STATE": "SUCCEEDED"},
                    ),
                    mock.patch("vivado_mcp.remote_build.core.scp_download", side_effect=download),
                    mock.patch(
                        "vivado_mcp.remote_build.core.purge_remote_build", return_value="PURGED"
                    ) as purge,
                ):
                    result = fpga_remote.command_run(self.args(root / "output", "purge"), {})
                self.assertEqual(result, 0 if failure is None else 1)
                self.assertEqual(purge.call_count, 1 if failure is None else 0)
                summary_path = root / "output/demo/20260822T120000/remote-build-summary.json"
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                self.assertEqual(summary["schema_version"], 5)
                self.assertEqual(summary["local_delivery"]["verified"], failure is None)

    @staticmethod
    def submission() -> fpga_remote.BuildSubmission:
        info = fpga_remote.ProjectInfo(
            root=Path("C:/demo"),
            xpr=Path("C:/demo/demo.xpr"),
            srcs=Path("C:/demo/demo.srcs"),
            name="demo",
            vivado_version="2024.2",
            part="xczu7ev-ffvc1156-2-i",
            top="top",
            source_file_count=1,
            source_tree_sha256="0" * 64,
            referenced_file_count=1,
            generated_reference_count=0,
        )
        return fpga_remote.BuildSubmission(
            info=info,
            handle=fpga_remote.BuildHandle("example", "demo", "20260822T120000"),
            jobs=8,
            probe={"VIVADO_VERSION_LINE": "vivado v2024.2 (64-bit)"},
            verify_output="verified",
            launch_output="launched",
        )

    @staticmethod
    def args(output: Path, cleanup: str):
        return fpga_remote.argparse.Namespace(
            host="example",
            project="C:/demo",
            output=str(output),
            vivado_version=None,
            jobs=None,
            target="bitstream",
            reference=None,
            workspace="test-workspace",
            poll_seconds=1,
            timeout=10,
            remote_cleanup=cleanup,
        )

    def test_success_is_summarized_before_remote_purge(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name)
            submission = self.submission()
            run_dir = output / "demo" / "20260822T120000"
            extract_dir = run_dir / "demo-20260822T120000"
            fetch = fpga_remote.FetchResult(
                package=run_dir / "demo.tar.gz",
                package_sha256="1" * 64,
                checksum_file=run_dir / "demo.tar.gz.sha256",
                extract_dir=extract_dir,
            )

            def purge_after_summary(profile, handle):
                summary_path = run_dir / "remote-build-summary.json"
                self.assertTrue(summary_path.is_file())
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                self.assertEqual(summary["remote_cleanup"]["state"], "AUTHORIZED_PENDING")
                return "PURGED=/remote/demo\n"

            with (
                mock.patch(
                    "vivado_mcp.remote_build.core.submit_remote_build", return_value=submission
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.get_profile",
                    return_value={
                        "work_root": "/remote",
                        "ssh_alias": "example",
                    },
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.wait_for_build",
                    return_value={"STATE": "SUCCEEDED", "EXIT_CODE": "0"},
                ),
                mock.patch("vivado_mcp.remote_build.core.fetch_remote_build", return_value=fetch),
                mock.patch(
                    "vivado_mcp.remote_build.core.summarize_artifacts",
                    return_value={
                        "quality_status": "PASS_WITH_WARNINGS",
                    },
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.purge_remote_build",
                    side_effect=purge_after_summary,
                ) as purge_mock,
            ):
                result = fpga_remote.command_run(self.args(output, "purge"), {"hosts": {}})

            self.assertEqual(result, 0)
            purge_mock.assert_called_once()
            summary = json.loads(
                (run_dir / "remote-build-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["remote_cleanup"]["state"], "PURGED")

    def test_failed_build_never_runs_requested_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name)
            submission = self.submission()
            run_dir = output / "demo" / "20260822T120000"
            fetch = fpga_remote.FetchResult(
                package=run_dir / "demo.tar.gz",
                package_sha256="1" * 64,
                checksum_file=run_dir / "demo.tar.gz.sha256",
                extract_dir=run_dir / "demo-20260822T120000",
            )
            with (
                mock.patch(
                    "vivado_mcp.remote_build.core.submit_remote_build", return_value=submission
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.get_profile",
                    return_value={
                        "work_root": "/remote",
                        "ssh_alias": "example",
                    },
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.wait_for_build",
                    side_effect=fpga_remote.CliError("build failed"),
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.get_build_status",
                    return_value={"STATE": "FAILED", "EXIT_CODE": "1"},
                ),
                mock.patch("vivado_mcp.remote_build.core.fetch_remote_build", return_value=fetch),
                mock.patch(
                    "vivado_mcp.remote_build.core.summarize_artifacts",
                    return_value={
                        "quality_status": "UNKNOWN",
                    },
                ),
                mock.patch("vivado_mcp.remote_build.core.purge_remote_build") as purge_mock,
            ):
                result = fpga_remote.command_run(self.args(output, "purge"), {"hosts": {}})

            self.assertEqual(result, 1)
            purge_mock.assert_not_called()
            summary = json.loads(
                (run_dir / "remote-build-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["remote_cleanup"]["state"], "SKIPPED_UNSAFE")


class ProjectTests(unittest.TestCase):
    def make_project(
        self,
        root: Path,
        file_path: str = "$PSRCDIR/sources_1/new/top.v",
        extra_file_paths: tuple[str, ...] = (),
    ):
        src = root / "demo.srcs" / "sources_1" / "new"
        src.mkdir(parents=True)
        (src / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
        file_nodes = "\n".join(
            f'      <File Path="{path}"/>' for path in (file_path, *extra_file_paths)
        )
        xpr = root / "demo.xpr"
        xpr.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Product Version: Vivado v2024.2 (64-bit) -->
<Project>
  <Configuration><Option Name="Part" Val="xczu7ev-ffvc1156-2-i"/></Configuration>
  <FileSets>
    <FileSet Name="sources_1">
{file_nodes}
      <Config><Option Name="TopModule" Val="top"/></Config>
    </FileSet>
  </FileSets>
</Project>
""",
            encoding="utf-8",
        )
        return xpr

    def test_portable_project_is_inspected(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            self.make_project(root)
            info = fpga_remote.inspect_project(root)
            self.assertEqual(info.name, "demo")
            self.assertEqual(info.vivado_version, "2024.2")
            self.assertEqual(info.top, "top")
            self.assertEqual(info.source_file_count, 1)
            self.assertEqual(info.referenced_file_count, 1)
            self.assertEqual(info.generated_reference_count, 0)
            self.assertRegex(info.source_tree_sha256, r"^[0-9a-f]{64}$")

    def test_generated_reference_is_allowed_and_counted(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            self.make_project(
                root,
                extra_file_paths=("$PGENDIR/sources_1/bd/demo/hdl/demo_wrapper.v",),
            )
            info = fpga_remote.inspect_project(root)
            self.assertEqual(info.referenced_file_count, 1)
            self.assertEqual(info.generated_reference_count, 1)

    def test_external_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            self.make_project(root, "E:/outside/top.v")
            with self.assertRaises(fpga_remote.CliError):
                fpga_remote.inspect_project(root)

    def test_source_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            outside = root / "outside"
            outside.mkdir()
            (outside / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
            self.make_project(root, "$PSRCDIR/../outside/top.v")
            with self.assertRaises(fpga_remote.CliError):
                fpga_remote.inspect_project(root)

    def test_archive_contains_only_xpr_and_srcs(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            self.make_project(root)
            info = fpga_remote.inspect_project(root)
            archive_path = root / "source.tar.gz"
            fpga_remote.create_source_archive(info, archive_path)
            import tarfile

            with tarfile.open(archive_path, "r:gz") as archive:
                names = archive.getnames()
            self.assertIn("demo.xpr", names)
            self.assertIn("demo.srcs/sources_1/new/top.v", names)
            self.assertFalse(any(".runs" in name for name in names))


class SharedDeliveryTests(unittest.TestCase):
    def test_managed_output_area_cannot_become_authoritative_source(self):
        with tempfile.TemporaryDirectory() as temp_name:
            profile = fpga_remote.load_config(TOOL_ROOT / "config/remote-hosts.example.json")[
                "hosts"
            ]["example"]
            profile["shared"]["windows_root"] = temp_name
            info = replace(
                RunWorkflowTests.submission().info,
                root=Path(temp_name) / "fpga-remote/workspaces/task-a/source",
            )
            with (
                mock.patch("vivado_mcp.remote_build.core.inspect_project", return_value=info),
                mock.patch("vivado_mcp.remote_build.core.probe_host") as probe,
            ):
                with self.assertRaises(fpga_remote.CliError):
                    fpga_remote.submit_remote_build(
                        {"hosts": {"example": profile}},
                        host="example",
                        project=info.root,
                        vivado_version=None,
                        jobs=None,
                        workspace="task-a",
                    )
                probe.assert_not_called()

    def test_synth_and_route_have_distinct_delivery_requirements(self):
        for target in ("synth", "route"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                artifacts = ArtifactSummaryTests.make_artifacts(root)
                with (artifacts / "build_info.txt").open("a", encoding="utf-8") as stream:
                    stream.write(f"build_target={target}\n")
                (artifacts / "XCZU7EV_TOP.bit").unlink()
                if target == "synth":
                    (artifacts / "timing_summary.rpt").rename(artifacts / "post_synth_timing.rpt")
                    (artifacts / "route_status.rpt").unlink()
                    (artifacts / "post_route.dcp").unlink()
                summary = fpga_remote.summarize_artifacts(
                    root, require_success=True, expected_target=target
                )
                self.assertEqual(summary["stages"]["bitstream"], "NOT_RUN")
                self.assertEqual(
                    summary["stages"]["implementation"], "NOT_RUN" if target == "synth" else "PASS"
                )
                self.assertEqual(
                    summary["quality_status"],
                    "SYNTH_PASS" if target == "synth" else "PASS_WITH_WARNINGS",
                )
                with self.assertRaises(fpga_remote.CliError):
                    fpga_remote.summarize_artifacts(
                        root, require_success=True, expected_target="bitstream"
                    )

    def test_incremental_reference_requires_matching_target_and_finished_route(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            profile = {"shared": {"windows_root": str(root), "linux_root": "/share"}}
            info = RunWorkflowTests.submission().info
            handle = fpga_remote.parse_handle("example/demo/20260908T120000")
            result_dir = fpga_remote.shared_result_dir(profile, handle)
            result_dir.mkdir(parents=True)
            artifacts = ArtifactSummaryTests.make_artifacts(result_dir)
            (result_dir / "delivery.complete").write_text("SUCCEEDED\n", encoding="utf-8")
            (artifacts / "build_info.txt").write_text(
                f"vivado_version={info.vivado_version}\npart={info.part}\ntop={info.top}\n",
                encoding="utf-8",
            )
            options = fpga_remote.resolve_build_options(
                profile, info, "example", "bitstream", str(handle)
            )
            self.assertTrue(options.reference_checkpoint.endswith("/artifacts/post_route.dcp"))
            with self.assertRaises(fpga_remote.CliError):
                fpga_remote.resolve_build_options(profile, info, "example", "synth", str(handle))
            with (artifacts / "build_info.txt").open("a", encoding="utf-8") as stream:
                stream.write("part=wrong-part\n")
            with self.assertRaises(fpga_remote.CliError):
                fpga_remote.resolve_build_options(
                    profile, info, "example", "bitstream", str(handle)
                )

    def test_direct_shared_results_and_optional_local_copy(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            profile = {"shared": {"windows_root": str(root / "share")}}
            handle = fpga_remote.parse_handle("example/demo/20260908T120000")
            result_dir = fpga_remote.shared_result_dir(profile, handle)
            result_dir.mkdir(parents=True)
            ArtifactSummaryTests.make_artifacts(result_dir)
            with self.assertRaises(fpga_remote.CliError):
                fpga_remote.fetch_remote_build(profile, handle, output=result_dir, extract=True)
            (result_dir / "delivery.complete").write_text("SUCCEEDED\n", encoding="utf-8")
            with (
                mock.patch(
                    "vivado_mcp.remote_build.core.scp_download",
                    side_effect=AssertionError("No SCP for shared results"),
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.sha256_file",
                    side_effect=AssertionError("No package hash for shared results"),
                ),
            ):
                direct = fpga_remote.fetch_remote_build(
                    profile, handle, output=result_dir, extract=True
                )
                self.assertIsNone(direct.package)
                self.assertEqual(direct.extract_dir, result_dir)
                copied = fpga_remote.fetch_remote_build(
                    profile, handle, output=root / "archive", extract=False
                )
                self.assertEqual(
                    (copied.extract_dir / "artifacts/XCZU7EV_TOP.bit").read_bytes(),
                    (result_dir / "artifacts/XCZU7EV_TOP.bit").read_bytes(),
                )

    def test_local_and_shared_project_submission_routes(self):
        for on_share in (False, True):
            with self.subTest(on_share=on_share), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                share = root / "share"
                share.mkdir()
                project = (share if on_share else root) / "project"
                ProjectTests().make_project(project)
                info = fpga_remote.inspect_project(project)
                profile = {
                    "work_root": "/work",
                    "shared": {"windows_root": str(share), "linux_root": "/share"},
                }
                handle = fpga_remote.parse_handle("example/demo/20260908T120000")
                with mock.patch("vivado_mcp.remote_build.core.ssh_bash", return_value="ok") as ssh:
                    fpga_remote.prepare_shared_build(profile, info, handle, 1, "/vivado")
                script = ssh.call_args_list[0].args[1]
                stage = share / "fpga-remote/inbox/demo/20260908T120000"
                if on_share:
                    self.assertIn("cp -a -- /share/project/demo.xpr", script)
                    self.assertFalse((stage / "work").exists())
                else:
                    self.assertEqual((stage / "work/demo.xpr").read_bytes(), info.xpr.read_bytes())
                self.assertIn(
                    "FPGA_REMOTE_RESULT_ROOT=/share/fpga-remote/results/demo/20260908T120000",
                    ssh.call_args_list[1].args[1],
                )


if __name__ == "__main__":
    unittest.main()
