import json, tempfile, unittest
from pathlib import Path
import wallet_trade_backfill as b

class GMGNBackfillTests(unittest.TestCase):
    def test_parse_cli_output(self):
        obj = b.parse_output('progress\\n{"activities": [], "next": null}\\n')
        self.assertEqual(obj["activities"], [])

    def test_documented_activities_and_cursor(self):
        rows, cursor, more, last = b.extract_rows_cursor({"activities": [{"tx_hash": "abc"}], "next": "c2"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(cursor, "c2")
        self.assertIsNone(more)
        self.assertIsNone(last)

    def test_legacy_shapes(self):
        self.assertEqual(b.extract_rows_cursor({"list": [{"id": "1"}]})[0], [{"id": "1"}])
        self.assertEqual(b.extract_rows_cursor({"data": {"activities": [{"id": "2"}]}})[0], [{"id": "2"}])

    def test_malformed_response_is_not_empty_history(self):
        with self.assertRaises(b.BackfillError):
            b.extract_rows_cursor({"error": "rate limit"})

    def test_merge_preserves_fields(self):
        rows = b.merge_rows([{"tx_hash": "0xABC", "old": "keep"}], [{"tx_hash": "0xabc", "new": "keep"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["old"], "keep")
        self.assertEqual(rows[0]["new"], "keep")

    def test_paginated_fetch_merges_prior_archive(self):
        pages = [
            {"activities": [{"tx_hash": "a", "timestamp": 10}], "next": "c2"},
            {"activities": [{"tx_hash": "b", "timestamp": 20}], "next": None},
        ]
        def runner(chain, wallet, limit, cursor):
            return pages.pop(0)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / "sol_wallet.json"
            archive.write_text(json.dumps({"wallet": "wallet", "chain": "sol", "records": [{"tx_hash": "old", "timestamp": 1}]}))
            result = b.fetch_chain("sol", "wallet", 100, out_dir=root, runner=runner)
            saved = json.loads(archive.read_text())
            status = json.loads((root / "sol_wallet.status.json").read_text())
            self.assertEqual(result["pages"], 2)
            self.assertEqual(saved["total_records"], 3)
            self.assertEqual(status["status"], "complete")
            self.assertFalse((root / "sol_wallet.partial.json").exists())

    def test_full_page_without_cursor_does_not_overwrite_archive(self):
        def runner(chain, wallet, limit, cursor):
            return {"activities": [{"tx_hash": str(i)} for i in range(limit)]}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / "sol_wallet.json"
            archive.write_text(json.dumps({"wallet": "wallet", "chain": "sol", "records": [{"tx_hash": "old"}]}))
            with self.assertRaises(b.BackfillError):
                b.fetch_chain("sol", "wallet", 2, out_dir=root, runner=runner)
            self.assertEqual(json.loads(archive.read_text())["records"], [{"tx_hash": "old"}])
            self.assertEqual(len(json.loads((root / "sol_wallet.partial.json").read_text())["records"]), 2)

if __name__ == "__main__":
    unittest.main()
