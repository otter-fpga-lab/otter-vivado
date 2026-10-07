import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vivado_mcp.remote_build import core as remote


class NativeTclTests(unittest.TestCase):
    def make_job(self, root):
        (root / "run.tcl").write_text("puts $argv\n", encoding="utf-8")
        (root / "inputs").mkdir()
        (root / "inputs/data.mem").write_bytes(b"00\n01\n")
        data = {
            "schema_version": 1,
            "name": "ooc-demo",
            "vivado_version": "2024.2",
            "entry": "run.tcl",
            "arguments": ["literal $value [command]", "", "中文"],
            "inputs": ["inputs"],
            "required_outputs": ["r/result.txt"],
            "reports": {"timing": "r/timing.rpt"},
            "source_identity": {"git_commit": "example"},
        }
        path = root / "job.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path, data

    def test_native_inputs_preserve_layout_and_literal_arguments(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manifest, data = self.make_job(root)
            info = remote.inspect_tcl_job(manifest)
            self.assertEqual(info.arguments, tuple(data["arguments"]))
            self.assertEqual(
                {p.relative_to(root).as_posix() for p in info.files}, {"run.tcl", "inputs/data.mem"}
            )
            self.assertEqual(remote.project_info_dict(info)["flow"], "tcl")

    def test_manifest_paths_cannot_escape_snapshot_or_outputs(self):
        for field, value in (
            ("entry", "../outside.tcl"),
            ("inputs", ["../other"]),
            ("required_outputs", ["../report"]),
            ("reports", {"timing": "C:/old.rpt"}),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as name:
                manifest, data = self.make_job(Path(name))
                data[field] = value
                manifest.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(remote.CliError):
                    remote.inspect_tcl_job(manifest)

    def test_native_outputs_need_no_project_checkpoint_or_bitstream(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manifest, _ = self.make_job(root)
            info = remote.inspect_tcl_job(manifest)
            output = root / "returned"
            artifacts = output / "artifacts/r"
            artifacts.mkdir(parents=True)
            with self.assertRaises(remote.CliError):
                remote.summarize_tcl_artifacts(output, info, require_success=True)
            (artifacts / "result.txt").write_text("STAGE=post_synth\n", encoding="utf-8")
            (artifacts / "timing.rpt").write_text(
                "1. checking no_clock (3082)\nThere are no user specified timing constraints.\n",
                encoding="utf-8",
            )
            summary = remote.summarize_tcl_artifacts(output, info, require_success=True)
            self.assertEqual(summary["quality_status"], "NOT_EVALUATED")
            self.assertEqual(summary["stages"], {})
            self.assertEqual(summary["constraints"]["no_clock"], 3082)
            self.assertEqual(summary["constraints"]["status"], "ISSUES_REPORTED")
            self.assertEqual(summary["file_count"], 2)

    def test_constraint_status_does_not_invent_coverage(self):
        self.assertEqual(remote.summarize_constraints("")["status"], "NOT_CHECKED")
        self.assertIsNone(remote.summarize_constraints("\n")["no_user_constraints_reported"])
        result = remote.summarize_constraints("checking no_clock (0)\nchecking no_input_delay (3)")
        self.assertEqual(result["status"], "ISSUES_REPORTED")
        self.assertIn("no_input_delay=3", result["issues"])

    def test_missing_native_output_prevents_requested_cleanup(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manifest, _ = self.make_job(root)
            info = remote.inspect_tcl_job(manifest)
            handle = remote.BuildHandle("example", info.name, "20260908T120000", "native-test")
            submission = remote.BuildSubmission(
                info, handle, 1, {}, "copied", "launched", remote.BuildOptions("script")
            )
            extracted = root / "returned"
            (extracted / "artifacts").mkdir(parents=True)
            args = remote.build_parser().parse_args(
                [
                    "run-tcl",
                    "--host",
                    "example",
                    "--manifest",
                    str(manifest),
                    "--output",
                    str(root / "out"),
                    "--remote-cleanup",
                    "purge",
                ]
            )
            with (
                mock.patch(
                    "vivado_mcp.remote_build.core.get_profile",
                    return_value={"work_root": "/remote"},
                ),
                mock.patch("vivado_mcp.remote_build.core.submit_tcl_job", return_value=submission),
                mock.patch(
                    "vivado_mcp.remote_build.core.wait_for_build",
                    return_value={"STATE": "SUCCEEDED"},
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.fetch_remote_build",
                    return_value=remote.FetchResult(None, "", None, extracted),
                ),
                mock.patch("vivado_mcp.remote_build.core.purge_remote_build") as purge,
            ):
                self.assertEqual(remote.command_run(args, {}), 1)
                purge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
