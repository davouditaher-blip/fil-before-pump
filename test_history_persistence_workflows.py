import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class HistoryPersistenceWorkflowTests(unittest.TestCase):
    def test_history_writers_share_a_non_cancelling_concurrency_group(self):
        main = (ROOT / ".github/workflows/fil-before-pump.yml").read_text(encoding="utf-8")
        gmgn = (ROOT / ".github/workflows/gmgn-smart-money.yml").read_text(encoding="utf-8")
        self.assertIn("group: scanner-history-writer", main)
        self.assertIn("group: scanner-history-writer", gmgn)
        self.assertIn("cancel-in-progress: false", main)
        self.assertIn("cancel-in-progress: false", gmgn)

    def test_history_writers_never_prefer_local_archive_on_rebase_conflict(self):
        for name in (".github/workflows/fil-before-pump.yml", ".github/workflows/gmgn-smart-money.yml"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("git rebase -X theirs", source, name)
            self.assertIn("git rebase origin/main", source, name)
            self.assertIn("refusing to prefer either history archive", source, name)

    def test_live_orders_remain_disabled_in_discovery(self):
        source = (ROOT / "reverse_historical_discovery.py").read_text(encoding="utf-8")
        self.assertIn('"orders_enabled": False', source)


if __name__ == "__main__":
    unittest.main()
