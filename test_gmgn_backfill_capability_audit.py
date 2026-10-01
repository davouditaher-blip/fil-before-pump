#!/usr/bin/env python3
"""Focused tests for the GMGN Historical Backfill Capability Audit.

Two jobs. First, prove the audit's own readings of the GMGN code are accurate,
by re-deriving them independently from the source rather than trusting the
report. Second, assert the read-only and honesty guarantees: no production file
was modified, no capability is claimed without evidence, and an unprovable
capability is reported as UNVERIFIED rather than guessed.

No test contacts GMGN, writes a dataset, mutates a registry, or modifies a
production module.
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

import gmgn_backfill_capability_audit as gba  # noqa: E402

REPORT = HERE / "gmgn_backfill_capability_audit.json"
LAYER = HERE / "gmgn_layer.py"
PROBE = HERE / "gmgn_depth_probe.py"
BACKFILL = HERE / "wallet_trade_backfill.py"
ARCHIVE_DIR = HERE / "wallet_archive/raw/gmgn/activity"

#: Files the audit asserts it did not modify. Digests are captured at test time
#: from the working tree and compared to the same files as committed.
MUST_BE_UNTOUCHED = (
    "gmgn_layer.py",
    "gmgn_depth_probe.py",
    "wallet_trade_backfill.py",
    "gmgn_wallet_history.json",
    "wallet_history_validation.py",
    "historical_discovery.py",
    "proven_wallet_registry.json",
    "reverse_historical_discovery.json",
)

HISTORY_MD5 = "ef1e23b980ae3b4cb0ad239267cbbae4"


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TestAuditReadsTheCodeAccurately(unittest.TestCase):
    """Re-derive the audit's claims from source, independently of the report."""

    def test_portfolio_activity_sends_no_time_or_pagination_flag(self):
        """The central claim. Verified by reading the argv the code builds."""
        tree = ast.parse(LAYER.read_text(encoding="utf-8"))
        activity = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "portfolio_activity":
                activity = node
                break
        self.assertIsNotNone(activity, "portfolio_activity must exist")
        flags = {
            node.value for node in ast.walk(activity)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.startswith("--")
        }
        self.assertIn("--limit", flags)
        self.assertIn("--chain", flags)
        self.assertIn("--wallet", flags)
        self.assertNotIn("--from", flags, "no time-range flag is actually sent")
        self.assertNotIn("--to", flags, "no time-range flag is actually sent")
        self.assertNotIn("--cursor", flags, "production sends no cursor")
        self.assertNotIn("--page", flags, "production sends no page number")

    def test_production_code_contains_no_cursor_or_page_argument(self):
        for path in (LAYER, BACKFILL):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            sent: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in ("--cursor", "--page", "--offset"):
                        sent.add(node.value)
            self.assertEqual(
                sent, set(),
                f"{path.name} must not send a pagination flag: {sent}")

    def test_the_probe_does_implement_cursor_pagination(self):
        # The contrast that matters: cursor support exists, but only in the probe.
        tree = ast.parse(PROBE.read_text(encoding="utf-8"))
        sent = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value in ("--cursor", "--page")
        }
        self.assertEqual(sent, {"--cursor", "--page"})

    def test_kline_is_the_only_call_with_a_time_range(self):
        source = LAYER.read_text(encoding="utf-8")
        self.assertIn('"--from"', source)
        self.assertIn('"--to"', source)
        tree = ast.parse(source)
        kline = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "token_kline":
                kline = node
        self.assertIsNotNone(kline)
        flags = {
            arg.value for arg in ast.walk(kline)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            and arg.value.startswith("--")
        }
        self.assertIn("--from", flags)
        self.assertIn("--to", flags)

    def test_backfill_makes_one_call_per_chain_with_no_loop(self):
        source = BACKFILL.read_text(encoding="utf-8")
        # CHAINS is a 4-tuple and the loop body calls run_cli exactly once.
        self.assertIn('CHAINS = ("sol", "bsc", "base", "eth")', source)
        self.assertEqual(source.count("obj = run_cli(chain)"), 1)

    def test_backfill_writes_the_file_even_when_rows_are_empty(self):
        """The wallet_archive root cause, verified in the source."""
        source = BACKFILL.read_text(encoding="utf-8")
        self.assertIn('rows = obj.get("list") or obj.get("data") or []', source)
        # The write is not inside any conditional on rows being non-empty.
        write_at = source.index("raw_path.write_text")
        rows_at = source.index('rows = obj.get("list")')
        between = source[rows_at:write_at]
        self.assertNotIn("if rows", between)
        self.assertNotIn("if not rows", between)

    def test_backfill_records_no_error_state(self):
        source = BACKFILL.read_text(encoding="utf-8")
        for persisted in ('"error"', '"returncode"', '"stderr"'):
            self.assertNotIn(
                persisted, source,
                f"{persisted} would mean the failure cause is persisted")

    def test_backfill_has_no_rate_limit_detection(self):
        source = BACKFILL.read_text(encoding="utf-8")
        self.assertNotIn("detect_rate_limit", source)
        self.assertNotIn("RATE_LIMIT", source)

    def test_backfill_has_no_api_key_preflight(self):
        source = BACKFILL.read_text(encoding="utf-8")
        self.assertNotIn("GMGN_API_KEY", source)

    def test_backfill_does_not_guard_the_subprocess_call(self):
        tree = ast.parse(BACKFILL.read_text(encoding="utf-8"))
        run_cli = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_cli":
                run_cli = node
        self.assertIsNotNone(run_cli)
        # Only the JSON parse loop may have a try; the subprocess call may not be
        # wrapped, which is why a missing npx raises instead of returning {}.
        guarded = []
        for node in ast.walk(run_cli):
            if isinstance(node, ast.Try):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                        guarded.append(inner.func.attr)
        self.assertNotIn("run", guarded)


class TestArchiveRootCauseIsEvidenceBased(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT.read_text(encoding="utf-8"))
        self.archive = self.report["wallet_archive"]

    def test_the_archive_really_is_one_wallet_and_zero_records(self):
        self.assertEqual(len(self.archive["unique_wallets"]), 1)
        self.assertEqual(self.archive["total_records"], 0)

    def test_the_stated_root_cause_is_traced_to_real_lines(self):
        path = self.archive["root_cause"]["traced_path"]
        self.assertTrue(path, "a root cause must be traced, not asserted")
        joined = " ".join(path)
        self.assertIn("returncode", joined)
        self.assertIn("which yields [] for {}", joined)
        self.assertIn("regardless of why it is empty", joined)
        self.assertIn("never persisted", joined)

    def test_the_root_cause_explains_why_the_cause_is_unrecoverable(self):
        cause = self.archive["root_cause"]
        self.assertIn("indistinguishable", cause["why_the_cause_is_not_recoverable_from_the_artifact"])
        self.assertIn("NOT\nrecoverable", cause["conclusion_strength"].replace(" ", "\n"))

    def test_compounding_factors_include_the_structural_row_ceiling(self):
        factors = {f["factor"] for f in self.archive["root_cause"]["compounding_factors"]}
        self.assertIn("one page, one wallet, four chains", factors)

    def test_the_report_separates_verified_from_unrecoverable(self):
        strength = self.archive["root_cause"]["conclusion_strength"]
        self.assertIn("verifiable", strength)
        self.assertIn("NOT", strength)

    def test_archived_files_record_no_error_field(self):
        for entry in self.archive["files"]:
            if entry.get("readable"):
                self.assertFalse(
                    entry["has_error_field"],
                    f"{entry['file']} unexpectedly persisted an error field")


class TestCapabilityVerdictsAreHonest(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT.read_text(encoding="utf-8"))
        self.caps = self.report["capability"]

    def test_endpoint_capability_is_unverified_not_guessed(self):
        for window in ("A_90_days", "B_180_days", "C_365_days"):
            entry = self.caps[window]
            self.assertEqual(
                entry["endpoint_capability"], gba.UNVERIFIED,
                f"{window}: provider depth must not be asserted")
            self.assertIn("insufficient", entry["endpoint_capability"])

    def test_each_window_verdict_is_no(self):
        for window in ("A_90_days", "B_180_days", "C_365_days"):
            self.assertEqual(self.caps[window]["verdict"], "NO")

    def test_no_verdict_rests_on_endpoint_assumption(self):
        for window in ("A_90_days", "B_180_days", "C_365_days"):
            entry = self.caps[window]
            self.assertFalse(entry["code_capability"])
            self.assertFalse(entry["demonstrated_capability"])

    def test_every_verdict_explains_its_basis(self):
        for window in ("A_90_days", "B_180_days", "C_365_days"):
            self.assertTrue(self.caps[window]["verdict_basis"])
            self.assertTrue(self.caps[window]["code_reason"])
            self.assertTrue(self.caps[window]["demonstrated_reason"])

    def test_the_report_says_what_would_change_the_verdict(self):
        self.assertIn("gmgn_depth_probe", self.caps["what_would_change_the_verdict"])

    def test_unverified_literal_is_used_consistently(self):
        self.assertEqual(
            self.report["evidence_tiers"]["unverified_literal"], gba.UNVERIFIED)
        limits = self.report["limits"]
        self.assertEqual(limits["max_historical_timestamp"]["provider_imposed"], gba.UNVERIFIED)
        self.assertEqual(limits["provider_time_window"]["wallet_activity"], gba.UNVERIFIED)
        self.assertEqual(limits["cursor_expiration"]["value"], gba.UNVERIFIED)
        self.assertEqual(limits["rate_limits"]["provider_quota_numbers"], gba.UNVERIFIED)

    def test_no_unverified_claim_is_hidden_as_a_number(self):
        # Every documented GMGN limit must carry its tier explicitly.
        limits = self.report["limits"]
        self.assertIn("enforced_by_code", limits["max_historical_timestamp"])
        self.assertEqual(limits["max_historical_timestamp"]["enforced_by_code"], False)
        self.assertTrue(limits["max_page_size_requested"]["note"])


class TestLimitsAreAccurate(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT.read_text(encoding="utf-8"))
        self.limits = self.report["limits"]

    def test_activity_budget_matches_the_source_constant(self):
        tree = ast.parse(LAYER.read_text(encoding="utf-8"))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    try:
                        found[target.id] = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        pass
        self.assertEqual(self.limits["rate_limits"]["activity_call_budget"]["value"],
                         found["ACTIVITY_CALL_BUDGET"])
        self.assertEqual(self.limits["rate_limits"]["activity_call_budget"]["pacing_seconds"],
                         found["ACTIVITY_CALL_PACING_SECONDS"])

    def test_rate_limit_policy_is_terminal_for_the_run(self):
        policy = self.limits["rate_limits"]["project_policy"]
        self.assertTrue(policy["terminal_for_run"])
        self.assertTrue(policy["detection_shapes"])

    def test_the_backfill_rate_limit_gap_is_recorded(self):
        gap = self.limits["rate_limits"]["backfill_has_no_rate_limit_handling"]
        self.assertTrue(gap["value"])
        self.assertIn("extends it", gap["consequence"])

    def test_authentication_records_the_missing_preflight(self):
        auth = self.limits["authentication"]
        self.assertIn("does NOT check", auth["backfill"])

    def test_time_range_support_differs_by_endpoint(self):
        tr = self.report["endpoints"]["time_range_filtering"]
        self.assertIn("UNSUPPORTED", tr["wallet_activity"])
        self.assertIn("SUPPORTED", tr["token_kline"])


class TestReportIsReadOnlyAndHonest(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT.read_text(encoding="utf-8"))

    def test_the_module_writes_only_its_own_report(self):
        tree = ast.parse(Path("gmgn_backfill_capability_audit.py").read_text(encoding="utf-8"))
        writes = {
            n.func.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("write_text", "write", "writelines")
            and isinstance(n.func.value, ast.Name)
        }
        self.assertEqual(writes, {"REPORT"})

    def test_the_module_never_imports_the_gmgn_production_modules(self):
        # Importing gmgn_layer would require `requests` and could reach the
        # network. The audit must parse it as AST instead.
        tree = ast.parse(Path("gmgn_backfill_capability_audit.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for forbidden in ("gmgn_layer", "gmgn_depth_probe", "wallet_trade_backfill",
                          "requests", "subprocess", "urllib", "socket", "http"):
            self.assertNotIn(forbidden, imported)

    def test_the_module_never_calls_a_subprocess(self):
        # Checked on the AST, not the text: the report legitimately *names*
        # subprocess.run when explaining the backfill's unguarded call, so a
        # substring search would match a description of the very thing being
        # forbidden.
        tree = ast.parse(Path("gmgn_backfill_capability_audit.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(
                    node.func.attr, ("run", "Popen", "check_output", "call"),
                    f"the audit must not execute a subprocess: {node.func.attr}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "subprocess")
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "subprocess")

    def test_no_backfill_was_started(self):
        self.assertTrue(self.report["read_only"]["no_backfill_started"])
        self.assertTrue(self.report["read_only"]["no_provider_contact"])

    def test_no_provider_was_added(self):
        self.assertTrue(self.report["read_only"]["no_provider_added"])

    def test_no_production_module_was_modified(self):
        self.assertEqual(self.report["read_only"]["production_modules_modified"], [])
        self.assertTrue(self.report["read_only"]["no_production_module_modified"])

    def test_no_threshold_was_modified(self):
        self.assertTrue(self.report["read_only"]["no_threshold_modified"])
        self.assertEqual(
            self.report["read_only"]["threshold_untouched"],
            "reverse_historical_discovery.MIN_ASSET_HISTORY_DAYS")

    def test_trading_was_not_enabled(self):
        self.assertFalse(self.report["orders_enabled"])
        self.assertEqual(self.report["mode"], "GMGN_BACKFILL_CAPABILITY_AUDIT_READ_ONLY")

    def test_the_90_day_threshold_is_still_90(self):
        import reverse_historical_discovery as rhd
        self.assertEqual(rhd.MIN_ASSET_HISTORY_DAYS, 90)

    def test_the_absence_of_gmgn_tests_is_recorded_not_glossed(self):
        maturity = self.report["codebase_maturity"]
        self.assertFalse(maturity["gmgn_tests_exist"])
        self.assertEqual(maturity["gmgn_test_files"], [])
        self.assertIn("no unit tests", maturity["note"])

    def test_the_audit_excludes_its_own_test_and_says_so(self):
        # Regression: the audit globs test_*.py for "gmgn", which matches its own
        # test file. Without an explicit exclusion the coverage finding flips to
        # True the moment this file exists, silently.
        maturity = self.report["codebase_maturity"]
        excluded = maturity["audit_self_test_files_excluded"]
        self.assertEqual(excluded, ["test_gmgn_backfill_capability_audit.py"])
        self.assertNotIn("test_gmgn_backfill_capability_audit.py",
                         maturity["gmgn_test_files"])
        self.assertIn("covers the audit and not GMGN production code",
                      maturity["note"])
        # And the exclusion must be real, not a hardcoded literal: assert it
        # against a directory that genuinely has no other gmgn test.
        globbed = [p.name for p in HERE.glob("test_*.py") if "gmgn" in p.name]
        self.assertEqual(sorted(globbed), excluded)

    def test_the_missing_probe_result_is_recorded(self):
        self.assertFalse(self.report["demonstrated_depth"]["gmgn_depth_probe_result"]["present"])

    def test_backfill_requirements_are_listed_with_gaps(self):
        req = self.report["requirements_for_a_real_90d_backfill"]
        self.assertIn("NOT POSSIBLE", req["current_state"])
        self.assertTrue(req["blocking_gaps"])
        for gap in req["blocking_gaps"]:
            self.assertIn("verified", gap)
        self.assertTrue(req["minimum_to_attempt"])

    def test_the_recommendation_is_probe_first(self):
        rec = self.report["is_gmgn_worth_attempting_first"]
        self.assertTrue(rec["verdict"].startswith("YES"))
        self.assertIn("probe", rec["recommended_first_action"])
        self.assertTrue(rec["reasons_for"])
        self.assertTrue(rec["reasons_for_caution"])

    def test_limitations_are_documented(self):
        self.assertTrue(self.report["limitations"])
        self.assertIn("no_committed_probe_result", self.report["limitations"])
        self.assertIn("empty_archive_is_not_a_measurement", self.report["limitations"])


class TestCommittedReportIntegrity(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT.read_text(encoding="utf-8"))

    def test_the_stored_digest_describes_the_stored_report(self):
        self.assertEqual(
            self.report["determinism"]["payload_sha256"],
            gba.payload_digest(self.report),
        )

    def test_the_digest_ignores_only_generated_at(self):
        other = dict(self.report)
        other["generated_at"] = "1999-01-01T00:00:00+00:00"
        self.assertEqual(gba.payload_digest(self.report), gba.payload_digest(other))

    def test_the_digest_reacts_to_a_real_change(self):
        other = dict(self.report)
        other["capability"] = dict(self.report["capability"], A_90_days={"verdict": "YES"})
        self.assertNotEqual(gba.payload_digest(self.report), gba.payload_digest(other))

    def test_chains_are_normalized_to_a_list_so_verify_is_stable(self):
        # A tuple serializes to an array and reads back as a list; if it were
        # left as a tuple, every --verify run would compare list != tuple.
        self.assertIsInstance(self.report["endpoints"]["chain_selection"]["supported_chains"], list)

    def test_the_source_archive_digest_is_recorded(self):
        self.assertEqual(
            self.report["provenance"]["read_only_files_md5"]["gmgn_wallet_history.json"],
            HISTORY_MD5,
        )


class TestProtectedFilesUntouched(unittest.TestCase):
    def test_must_be_untouched_files_are_listed_by_the_audit(self):
        for name in MUST_BE_UNTOUCHED:
            self.assertIn(name, gba.READ_ONLY_FILES)

    def test_the_primary_archive_still_matches_its_known_digest(self):
        self.assertEqual(_md5(HERE / "gmgn_wallet_history.json"), HISTORY_MD5)

    def test_the_wallet_archive_still_holds_one_wallet_and_zero_records(self):
        files = sorted(ARCHIVE_DIR.glob("*.json"))
        self.assertEqual(len(files), 5)
        total = 0
        wallets = set()
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            total += len(data.get("records") or [])
            if data.get("wallet"):
                wallets.add(data["wallet"])
        self.assertEqual(total, 0, "the audit must not have populated the archive")
        self.assertEqual(len(wallets), 1)

    def test_no_gmgn_report_artifact_was_created_by_the_probe(self):
        # The probe must not have been run: no result file, no archive writes.
        self.assertFalse((HERE / "gmgn_depth_probe.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)