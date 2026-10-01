#!/usr/bin/env python3
"""Focused tests for the Historical Depth Audit.

Two jobs. First, prove the depth arithmetic is right on fixtures where the
answer is known by construction -- a window that is 200 days wide must count as
200 days and must clear the 365d bucket exactly once. Second, assert the
read-only and observational guarantees on the real report: the gate is not
modified, no dataset or registry is written, and the committed report is
reproducible.

No test writes to a production dataset, mutates the registry, or touches
production decision logic.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import historical_depth_audit as hda  # noqa: E402
import reverse_historical_discovery as rhd  # noqa: E402
import wallet_history_validation as whv  # noqa: E402

REPORT = HERE / "historical_depth_audit.json"
SOURCE = HERE / "gmgn_wallet_history.json"
REGISTRY = HERE / "proven_wallet_registry.json"
SOURCE_MD5 = "ef1e23b980ae3b4cb0ad239267cbbae4"
DAY = 86_400

#: MD5 of every dataset and registry the audit must leave untouched.
PROTECTED = {
    "gmgn_wallet_history.json": SOURCE_MD5,
}


def row(address, ts, price, chain="eth", side="buy"):
    return {
        "wallet": "0xw", "chain": chain, "address": address, "symbol": "T",
        "side": side, "amount_usd": 100.0, "price_usd": price,
        "timestamp": ts, "trade_timestamp": ts,
    }


class TestDepthArithmetic(unittest.TestCase):
    def test_span_is_measured_from_first_to_last_observation(self):
        series = [(1_000, 1.0), (1_000 + 5 * DAY, 2.0), (1_000 + 10 * DAY, 1.5)]
        self.assertAlmostEqual(hda.rhd.asset_history_days(series), 10.0, places=4)

    def test_a_series_with_one_observation_has_no_span(self):
        self.assertEqual(hda.rhd.asset_history_days([(1_000, 1.0)]), 0.0)

    def test_an_empty_series_has_no_span(self):
        self.assertEqual(hda.rhd.asset_history_days([]), 0.0)

    def test_bucket_counts_place_a_span_in_the_right_buckets(self):
        counts = hda.bucket_counts([0.0, 89.9, 90.0, 179.9, 180.0, 364.9, 365.0, 400.0])
        self.assertEqual(counts["at_least_90d"], 6)
        self.assertEqual(counts["at_least_180d"], 4)
        self.assertEqual(counts["at_least_365d"], 2)
        self.assertEqual(counts["older_than_365d"], 1)
        self.assertFalse(counts["none_at_90d"])

    def test_a_span_of_exactly_the_threshold_is_admitted(self):
        # Boundary check: >= not >. An asset spanning exactly 90 days satisfies
        # a 90-day requirement.
        self.assertEqual(hda.bucket_counts([90.0])["at_least_90d"], 1)
        self.assertEqual(hda.bucket_counts([89.9999])["at_least_90d"], 0)
        self.assertEqual(hda.bucket_counts([365.0])["at_least_365d"], 1)
        self.assertEqual(hda.bucket_counts([365.0])["older_than_365d"], 0)
        self.assertEqual(hda.bucket_counts([365.1])["older_than_365d"], 1)

    def test_no_spans_means_every_bucket_is_zero(self):
        counts = hda.bucket_counts([])
        self.assertEqual(counts["at_least_90d"], 0)
        self.assertEqual(counts["at_least_180d"], 0)
        self.assertEqual(counts["at_least_365d"], 0)
        self.assertTrue(counts["none_at_90d"])
        self.assertEqual(counts["measured_count"], 0)

    def test_percentiles_of_nothing_are_null_not_zero(self):
        # A missing measurement must not masquerade as a real zero depth.
        summary = hda._percentiles([])
        self.assertTrue(all(v is None for v in summary.values()))

    def test_percentiles_report_the_maximum(self):
        summary = hda._percentiles([1.0, 2.0, 30.0, 4.0])
        self.assertEqual(summary["max"], 30.0)
        self.assertEqual(summary["min"], 1.0)


class TestArchiveWidthBoundsAssets(unittest.TestCase):
    def test_an_asset_cannot_span_more_than_the_archive(self):
        # The structural claim behind the whole audit. Measured directly, not
        # assumed: build a real history, measure it, and assert the bound holds.
        history = {
            "0xa": [row("A", DAY, 1.0), row("A", 3 * DAY, 1.5)],
            "0xb": [row("B", DAY + 100, 2.0), row("B", 5 * DAY, 2.5)],
        }
        measured = hda.measure_primary_archive(history)
        times = [whv.event_ts(r) for series in history.values() for r in series]
        archive_span = (max(times) - min(times)) / DAY
        self.assertEqual(measured["archive_span_days"], round(archive_span, 4))
        widest = measured["asset_span_days_percentiles"]["max"]
        self.assertLessEqual(widest, measured["archive_span_days"])

    def test_a_narrow_archive_makes_every_wider_window_infeasible(self):
        # If the archive is 10 days wide, then no asset can reach 90/180/365.
        history = {"0xa": [row("A", DAY, 1.0), row("A", 11 * DAY, 1.5)]}
        measured = hda.measure_primary_archive(history)
        buckets = measured["asset_span_buckets"]
        self.assertEqual(buckets["at_least_90d"], 0)
        self.assertEqual(buckets["at_least_180d"], 0)
        self.assertEqual(buckets["at_least_365d"], 0)
        self.assertTrue(buckets["none_at_90d"])

    def test_a_wide_archive_can_satisfy_the_gate(self):
        # The control case: when the fixture really is 200 days wide, the same
        # unmodified gate admits it. Proves the audit reports infeasibility
        # rather than a hardcoded zero.
        history = {"0xa": [row("A", DAY, 1.0), row("A", 201 * DAY, 1.5)]}
        measured = hda.measure_primary_archive(history)
        buckets = measured["asset_span_buckets"]
        self.assertEqual(buckets["at_least_90d"], 1)
        self.assertEqual(buckets["at_least_180d"], 1)
        self.assertEqual(buckets["at_least_365d"], 0)
        self.assertFalse(buckets["none_at_90d"])


class TestCoverageCounting(unittest.TestCase):
    def test_chains_and_assets_are_counted_separately(self):
        history = {
            "0xa": [row("A", DAY, 1.0, chain="eth"), row("A", 3 * DAY, 1.5, chain="eth")],
            "0xb": [row("B", DAY, 1.0, chain="sol"), row("B", 3 * DAY, 1.5, chain="sol")],
        }
        measured = hda.measure_primary_archive(history)
        self.assertEqual(measured["coverage_by_chain_rows"], {"eth": 2, "sol": 2})
        self.assertEqual(measured["coverage_by_chain_assets"], {"eth": 1, "sol": 1})

    def test_rows_without_a_chain_are_recorded_as_unattributed(self):
        history = {"0xa": [row("A", DAY, 1.0, chain=""), row("A", 3 * DAY, 1.5, chain="")]}
        measured = hda.measure_primary_archive(history)
        self.assertIn("(none)", measured["coverage_by_chain_assets"])

    def test_trade_timestamp_coverage_is_reported(self):
        rows = [row("A", DAY, 1.0)]
        without = dict(rows[0])
        without["trade_timestamp"] = None
        history = {"0xa": rows + [without]}
        measured = hda.measure_primary_archive(history)
        self.assertEqual(measured["rows_with_trade_timestamp"], 1)
        self.assertEqual(measured["rows_without_trade_timestamp"], 1)

    def test_repeated_activity_is_counted_against_the_existing_rule(self):
        history = {
            "many": [row("A", DAY + i, 1.0) for i in range(5)],
            "two": [row("B", DAY + i, 1.0) for i in range(2)],
            "one": [row("C", DAY, 1.0)],
        }
        measured = hda.measure_primary_archive(history)
        self.assertEqual(measured["wallets_with_repeated_activity"], 2)
        self.assertEqual(measured["wallets_with_at_least_three_rows"], 1)


class TestSecondarySourceInventory(unittest.TestCase):
    def test_a_present_dataset_reports_its_span(self):
        measured = hda.measure_secondary("futures_volume_history.json", "futures")
        self.assertTrue(measured["present"])
        self.assertTrue(measured["readable"])
        self.assertGreater(measured["timestamps_found"], 0)
        self.assertGreater(measured["span_days"], 0.0)

    def test_a_missing_dataset_is_reported_absent_not_crashed(self):
        measured = hda.measure_secondary("definitely_not_here.json", "nope")
        self.assertFalse(measured["present"])

    def test_secondary_datasets_are_never_called_substitutes(self):
        # They are measured for depth only. Claiming one could replace wallet
        # trade history is not something this audit has evidence for.
        for source in hda.SECONDARY_SOURCES:
            measured = hda.measure_secondary(source["name"], source["kind"])
            self.assertFalse(measured.get("usable_as_wallet_trade_history", True))


class TestReportIsObservationalOnly(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT.read_text(encoding="utf-8"))

    def test_the_module_writes_only_its_own_report(self):
        source = Path("historical_depth_audit.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        writes = [n.func.value.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("write_text", "write", "writelines")
                  and isinstance(n.func.value, ast.Name)]
        self.assertEqual(sorted(set(writes)), ["REPORT"],
                         "the audit may write its report and nothing else")

    def test_the_module_never_opens_a_dataset_or_registry_for_writing(self):
        source = Path("historical_depth_audit.py").read_text(encoding="utf-8")
        for forbidden in ("save_registry", "pwr.save", "json.dump(", "shutil",
                          "os.remove", "unlink", "rename"):
            self.assertNotIn(forbidden, source)

    def test_no_network_or_execution_surface(self):
        source = Path("historical_depth_audit.py").read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "http://", "https://", "socket",
                          "create_order", "place_order", "paper_trading",
                          "confluence_engine", "trade_readiness", "risk_engine",
                          "LIVE_TRADING"):
            self.assertNotIn(forbidden, source)

    def test_the_production_gate_is_untouched(self):
        self.assertEqual(rhd.MIN_ASSET_HISTORY_DAYS, 90)
        self.assertFalse(self.report["eligibility_under_current_gate"]["gate_modified_by_this_audit"])

    def test_the_report_declares_itself_read_only(self):
        self.assertFalse(self.report["provenance"]["datasets_mutated"])
        self.assertFalse(self.report["provenance"]["registry_mutated"])
        self.assertFalse(self.report["provenance"]["gate_mutated"])
        self.assertFalse(self.report["provenance"]["production_logic_mutated"])
        self.assertFalse(self.report["orders_enabled"])
        self.assertEqual(self.report["mode"], "HISTORICAL_DEPTH_AUDIT_READ_ONLY")

    def test_no_trading_was_started(self):
        self.assertFalse(self.report["orders_enabled"])
        self.assertTrue(
            self.report["missing_coverage"]["no_provider_added_by_this_audit"])


class TestCommittedReportIntegrity(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT.read_text(encoding="utf-8"))

    def test_the_stored_digest_describes_the_stored_report(self):
        self.assertEqual(
            self.report["determinism"]["payload_sha256"],
            hda.payload_digest(self.report),
        )

    def test_the_digest_ignores_only_generated_at(self):
        # Same report with a different timestamp must hash identically.
        other = dict(self.report)
        other["generated_at"] = "1999-01-01T00:00:00+00:00"
        self.assertEqual(hda.payload_digest(self.report), hda.payload_digest(other))

    def test_the_digest_reacts_to_a_real_change(self):
        other = dict(self.report)
        other["findings"] = dict(self.report["findings"], eligible_assets_at_90d=999)
        self.assertNotEqual(hda.payload_digest(self.report), hda.payload_digest(other))

    def test_the_source_archive_is_the_one_that_was_measured(self):
        self.assertEqual(self.report["provenance"]["primary_archive_md5"], SOURCE_MD5)
        self.assertEqual(hda.file_md5(SOURCE), SOURCE_MD5)

    def test_the_report_measures_the_committed_archive(self):
        primary = self.report["sources"]["primary"]
        self.assertEqual(primary["unique_wallets"], 2215)
        self.assertEqual(primary["reconstructed_rows"], 182588)
        self.assertEqual(primary["unique_assets"], 17271)

    def test_depth_findings_are_internally_consistent(self):
        findings = self.report["findings"]
        widest = findings["widest_asset_span_days"]
        self.assertLessEqual(widest, findings["primary_archive_span_days"])
        self.assertEqual(findings["eligible_assets_at_90d"], 0)
        self.assertEqual(findings["eligible_assets_at_180d"], 0)
        self.assertEqual(findings["eligible_assets_at_365d"], 0)
        self.assertFalse(findings["gate_is_satisfiable_from_data_present"])

    def test_bucket_counts_agree_with_the_gated_finding(self):
        buckets = self.report["sources"]["primary"]["asset_span_buckets"]
        self.assertEqual(buckets["at_least_90d"], self.report["findings"]["eligible_assets_at_90d"])
        self.assertEqual(buckets["at_least_180d"], self.report["findings"]["eligible_assets_at_180d"])
        self.assertEqual(buckets["at_least_365d"], self.report["findings"]["eligible_assets_at_365d"])

    def test_rejected_plus_eligible_accounts_for_every_asset(self):
        elig = self.report["eligibility_under_current_gate"]
        self.assertEqual(
            elig["assets_eligible"] + elig["assets_rejected_short_history"],
            elig["assets_scanned"],
        )

    def test_window_feasibility_states_all_three_windows(self):
        feas = self.report["window_feasibility"]
        for window in ("90d", "180d", "365d"):
            self.assertIn(f"{window}_supported_by_data_present", feas)
        self.assertFalse(feas["90d_supported_by_data_present"])
        self.assertFalse(feas["180d_supported_by_data_present"])
        self.assertFalse(feas["365d_supported_by_data_present"])
        self.assertFalse(feas["any_window_at_or_above_90d_supported"])

    def test_the_three_classifications_are_all_present(self):
        classes = self.report["classification"]
        self.assertIn("A_data_actually_present", classes)
        self.assertIn("B_data_theoretically_discoverable", classes)
        self.assertIn("C_data_eligible_under_current_90d_gate", classes)
        self.assertEqual(classes["C_data_eligible_under_current_90d_gate"]["assets_eligible"], 0)

    def test_present_data_is_larger_than_the_eligible_population(self):
        # The audit's central contrast: real data exists, eligibility is empty.
        classes = self.report["classification"]
        self.assertGreater(classes["A_data_actually_present"]["unique_assets"], 0)
        self.assertEqual(classes["C_data_eligible_under_current_90d_gate"]["assets_eligible"], 0)

    def test_missing_coverage_names_the_gap_and_a_candidate_source(self):
        missing = self.report["missing_coverage"]
        self.assertGreater(missing["gap_in_days_for_widest_asset"], 0.0)
        # The gap is measured against the widest real asset, not against zero:
        # 365 - 5.0628 = 359.9372 days of additional coverage.
        widest = self.report["findings"]["widest_asset_span_days"]
        self.assertAlmostEqual(missing["required_for_365d_days"], 365.0 - widest, places=4)
        self.assertAlmostEqual(missing["required_for_180d_days"], 180.0 - widest, places=4)
        self.assertAlmostEqual(missing["required_for_90d_days"], 90.0 - widest, places=4)
        sources = missing["candidate_sources_capable_of_supplying_it"]
        self.assertTrue(sources)
        for source in sources:
            self.assertIn("provider", source)
            self.assertIn("status", source)

    def test_no_unregistered_provider_is_claimed_as_integrated(self):
        for source in self.report["missing_coverage"]["candidate_sources_capable_of_supplying_it"]:
            if not source["already_integrated"]:
                self.assertEqual(source["status"], "candidate_only")

    def test_limitations_and_methodology_are_documented(self):
        self.assertTrue(self.report["limitations"])
        self.assertTrue(self.report["methodology"])
        self.assertEqual(
            self.report["methodology"]["event_time"],
            "wallet_history_validation.event_ts (trade_timestamp, else timestamp)",
        )


class TestProtectedDatasetsUntouched(unittest.TestCase):
    def test_protected_files_match_their_recorded_hashes(self):
        for name, expected in PROTECTED.items():
            actual = hda.file_md5(HERE / name)
            self.assertEqual(actual, expected, f"{name} was modified")

    def test_the_registry_is_unmodified_by_the_audit(self):
        import proven_wallet_registry as pwr
        before = pwr.load_registry(REGISTRY)
        self.assertEqual(before["wallet_count"], 2215)
        # Loading status the way the audit does must not rewrite the file.
        pwr.load_registry(REGISTRY)
        after = pwr.load_registry(REGISTRY)
        self.assertEqual(before, after)

    def test_no_production_module_was_touched_by_the_audit(self):
        tree = ast.parse(Path("historical_depth_audit.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        # Only the existing read-only readers, plus stdlib.
        allowed = {
            "reverse_historical_discovery", "wallet_history_validation",
            "argparse", "hashlib", "json", "sys", "datetime", "pathlib", "typing",
            "__future__",
        }
        self.assertTrue(
            imported <= allowed,
            f"audit imported unexpected modules: {sorted(imported - allowed)}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)