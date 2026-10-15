import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class HistoryPersistenceWorkflowTests(unittest.TestCase):
    def test_history_writers_share_a_non_cancelling_concurrency_group(self):
        main = (ROOT / ".github/workflows/fil-before-pump.yml").read_text(encoding="utf-8")
        gmgn = (ROOT / ".github/workflows/gmgn-smart-money.yml").read_text(encoding="utf-8")
        self.assertIn("group: scanner-history-writer", main)
        self.assertIn('"gmgn_backfill_repair_status.json"', main)
        self.assertIn('"test_*.py"', main)
        self.assertIn("group: scanner-history-writer", gmgn)
        self.assertIn("cancel-in-progress: false", main)
        self.assertIn("cancel-in-progress: false", gmgn)

    def test_history_writers_never_prefer_local_archive_on_rebase_conflict(self):
        for name in (
            ".github/workflows/fil-before-pump.yml",
            ".github/workflows/gmgn-smart-money.yml",
            ".github/workflows/gmgn-wallet-backfill.yml",
        ):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("git rebase -X theirs", source, name)
            self.assertIn("git rebase origin/main", source, name)
        for name in (".github/workflows/fil-before-pump.yml", ".github/workflows/gmgn-smart-money.yml"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("ref: main", source, name)
            self.assertIn("fetch-depth: 10", source, name)
            self.assertIn("git fetch --deepen=20 origin main", source, name)
            self.assertIn("actions/upload-artifact@v4", source, name)
            self.assertIn("recover_gmgn_history_metadata.py", source, name)
            self.assertIn("merge_gmgn_history_metadata.py --baseline", source, name)
            self.assertIn("gmgn_history_recovery_done.json", source, name)
            self.assertIn("python build_proven_wallet_registry.py", source, name)
            self.assertIn("historical_depth_audit.py", source, name)
            self.assertIn("gmgn_backfill_capability_audit.py", source, name)
            if name == ".github/workflows/gmgn-smart-money.yml":
                self.assertIn("python reverse_historical_discovery.py", source, name)
                self.assertIn('id: reverse_discovery', source, name)
                self.assertIn('steps.reverse_discovery.outputs.changed', source, name)
                self.assertIn('git add reverse_historical_discovery.json', source, name)
            self.assertIn("refusing to prefer either history archive", source, name)
            self.assertIn("validate_gmgn_history_preservation.py --baseline", source, name)
            self.assertIn("gmgn_wallet_history.baseline.json", source, name)
            self.assertIn("python -m json.tool gmgn_wallet_history.json", source, name)
        backfill = (ROOT / ".github/workflows/gmgn-wallet-backfill.yml").read_text(encoding="utf-8")
        self.assertIn("refusing to overwrite a historical activity archive", backfill)
        self.assertIn("group: scanner-history-writer", backfill)
        self.assertIn("cancel-in-progress: false", backfill)
        self.assertIn("ref: main", backfill)
        self.assertIn("fetch-depth: 10", backfill)
        self.assertIn("git fetch --deepen=20 origin main", backfill)
        self.assertIn("actions/upload-artifact@v4", backfill)

    def test_scanner_dedup_keeps_different_prices_as_distinct_events(self):
        scanner = (ROOT / "scanner.py").read_text(encoding="utf-8")
        self.assertIn("def _gmgn_history_key(row):", scanner)
        self.assertIn('row.get("price_usd") or row.get("price")', scanner)
        self.assertIn("whv.merge_history_record(dedup[key], row)", scanner)
        self.assertIn("'trade_timestamp': trade.get('trade_timestamp') or trade_ts", scanner)

    def test_live_orders_remain_disabled_in_discovery(self):
        source = (ROOT / "reverse_historical_discovery.py").read_text(encoding="utf-8")
        self.assertIn('"orders_enabled": False', source)


if __name__ == "__main__":
    unittest.main()
