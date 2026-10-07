import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vivado_mcp.remote_build import core as remote

SUMMARY = """Severity Source Clock Destination Clock CDC Type Exceptions Endpoints Safe Unsafe Unknown No ASYNC_REG
Critical native_clk scaler_clk No Common Primary Clock Max Delay Datapath Only 68 65 3 0 0
Warning scaler_clk native_clk No Common Primary Clock Max Delay Datapath Only 16 16 0 0 0
"""  # noqa: E501 - preserve native shell/report literal layout
DETAILS = """ID Severity Count Description
CDC-3 Info 5 1-bit synchronized with ASYNC_REG property
CDC-6 Warning 4 Multi-bit synchronized with ASYNC_REG property
CDC-11 Critical 3 Fan-out from launch flop to destination clock
CDC-15 Warning 48 Clock enable controlled CDC structure detected
Row ID Severity Description Depth Exception Source (From) Destination (To)
1 CDC-11 Critical Fan-out from launch flop to destination clock 2 False Path reset/C sync/PRE
"""
UTILIZATION = """| Instance | Module | Total LUTs | Logic LUTs | LUTRAMs | SRLs | FFs | RAMB36 | RAMB18 | DSP Blocks |
| top | (top) | 4552 | 4355 | 108 | 89 | 5612 | 22 | 1 | 36 |
|   (top) | (top) | 0 | 0 | 0 | 0 | 248 | 0 | 0 | 0 |
|   native_U0 | native_video_scaler_chain | 4552 | 4355 | 108 | 89 | 5364 | 22 | 1 | 36 |
|     fifo | fifo_module | 200 | 200 | 0 | 0 | 310 | 3 | 1 | 0 |
|   other | other_module | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
|     fifo | fifo_module | 20 | 20 | 0 | 0 | 31 | 0 | 0 | 0 |
"""  # noqa: E501 - preserve native shell/report literal layout


def header(command, state):
    return f"| Command : {command} -file arbitrary.rpt\n| Design : top\n| Design State : {state}\n"


class ReportParserTests(unittest.TestCase):
    def test_critical_clock_pair_is_included_in_endpoint_counts(self):
        result = remote.parse_cdc_report(SUMMARY)
        self.assertEqual(result["endpoints"], 84)
        self.assertEqual(result["safe"], 81)
        self.assertEqual(result["unsafe"], 3)
        self.assertEqual(result["clock_pairs_by_severity"]["Critical"], 1)

    def test_detailed_rule_counts_do_not_become_endpoint_counts(self):
        result = remote.parse_cdc_report(DETAILS)
        self.assertEqual(result["format"], "details")
        self.assertIsNone(result["endpoints"])
        self.assertIsNone(result["unsafe"])
        self.assertEqual(
            result["check_counts_by_severity"], {"Critical": 3, "Warning": 52, "Info": 5}
        )
        self.assertEqual(len(result["checks"]), 4)

    def test_unrecognized_cdc_is_not_zero_violations(self):
        self.assertIsNone(remote.parse_cdc_report("unrecognized format")["unsafe"])
        self.assertEqual(remote.parse_cdc_report(SUMMARY.splitlines()[0])["unsafe"], 0)

    def test_utilization_keeps_top_and_dut_scopes_distinct(self):
        text = header("report_utilization", "Routed") + UTILIZATION
        self.assertEqual(remote.parse_utilization_report(text)["scope"]["metrics"]["ffs"], 5612)
        dut = remote.parse_utilization_report(text, "native_U0")
        self.assertEqual(dut["scope"]["metrics"]["ffs"], 5364)
        self.assertEqual(dut["scope"]["instance"], "top/native_U0")
        self.assertEqual(dut["design_state"], "Routed")
        self.assertEqual(remote.parse_utilization_report(text, "fifo")["status"], "AMBIGUOUS")
        self.assertEqual(
            remote.parse_utilization_report(text, "top/native_U0/fifo")["status"], "PARSED"
        )

    def test_saved_reports_keep_stage_identity_and_do_not_mutate_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "before.rpt").write_text(
                header("report_utilization", "Synthesized") + UTILIZATION, encoding="utf-8"
            )
            (artifacts / "after.rpt").write_text(
                header("report_utilization", "Routed") + UTILIZATION, encoding="utf-8"
            )
            (artifacts / "cdc.rpt").write_text(
                header("report_cdc", "Routed") + SUMMARY, encoding="utf-8"
            )
            old = root / "remote-build-summary.json"
            old.write_text('{"historical":true}', encoding="utf-8")
            args = remote.build_parser().parse_args(
                [
                    "report",
                    "--results",
                    str(root),
                    "--instance",
                    "native_U0",
                    "--output",
                    str(root / "review.json"),
                ]
            )
            with (
                mock.patch(
                    "vivado_mcp.remote_build.core.ssh_bash",
                    side_effect=AssertionError("Must not connect"),
                ),
                mock.patch(
                    "vivado_mcp.remote_build.core.sha256_file",
                    side_effect=AssertionError("Must not hash"),
                ),
            ):
                self.assertEqual(remote.command_report(args, {}), 0)
                with self.assertRaises(FileExistsError):
                    remote.command_report(args, {})
            report = json.loads((root / "review.json").read_text(encoding="utf-8"))
            self.assertEqual(
                report["reports"]["before.rpt"]["metadata"]["design_state"], "Synthesized"
            )
            self.assertEqual(
                report["reports"]["after.rpt"]["data"]["scope"]["metrics"]["ffs"], 5364
            )
            self.assertEqual(old.read_text(encoding="utf-8"), '{"historical":true}')


if __name__ == "__main__":
    unittest.main()
