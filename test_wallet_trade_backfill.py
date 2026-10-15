import json, os, tempfile, unittest
from unittest.mock import patch
from pathlib import Path
import wallet_trade_backfill as b

class GMGNBackfillTests(unittest.TestCase):
    def test_chain_selection_limits_backfill_to_requested_network(self):
        with patch.dict(os.environ, {"BACKFILL_CHAIN": "sol"}):
            self.assertEqual(b.selected_chains(), ("sol",))
        with patch.dict(os.environ, {"BACKFILL_CHAIN": "eth"}):
            self.assertEqual(b.selected_chains(), ("eth",))
        with patch.dict(os.environ, {"BACKFILL_CHAIN": "unknown"}):
            with self.assertRaises(SystemExit):
                b.selected_chains()

    def test_parse_cli_output(self):
        obj = b.parse_output("progress" + chr(10) + '{"activities": [], "next": null}' + chr(10))
        self.assertEqual(obj["activities"], [])

    def test_parse_pretty_printed_json(self):
        payload = """{
  "data": {
    "activities": [{"id": "x"}],
    "next": "cursor-2"
  }
}"""
        self.assertEqual(b.parse_output(payload)["data"]["next"], "cursor-2")

    def test_documented_activities_and_cursor(self):
        rows, cursor, more, last = b.extract_rows_cursor({"activities": [{"tx_hash": "abc"}], "next": "c2"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(cursor, "c2")
        self.assertIsNone(more)
        self.assertIsNone(last)

    def test_nested_activity_cursor_is_supported(self):
        rows, cursor, more, last = b.extract_rows_cursor({"data": {"activities": [{"id": "x"}], "next": "cursor-3"}})
        self.assertEqual(rows, [{"id": "x"}])
        self.assertEqual(cursor, "cursor-3")

    def test_same_transaction_hash_keeps_distinct_asset_legs(self):
        rows = b.merge_rows([
            {"chain": "eth", "transaction_hash": "0xabc", "address": "0xTokenA", "side": "buy", "amount_usd": 10, "price_usd": 1.0},
            {"chain": "eth", "transaction_hash": "0xabc", "address": "0xTokenB", "side": "buy", "amount_usd": 10, "price_usd": 1.0},
        ])
        self.assertEqual(len(rows), 2)

    def test_same_transaction_asset_leg_keeps_distinct_prices(self):
        rows = b.merge_rows([
            {"chain": "eth", "transaction_hash": "0xabc", "address": "0xTokenA", "side": "buy", "amount_usd": 10, "price_usd": 1.0},
            {"chain": "eth", "transaction_hash": "0xabc", "address": "0xTokenA", "side": "buy", "amount_usd": 10, "price_usd": 2.0},
        ])
        self.assertEqual(len(rows), 2)
    def test_merge_sorts_iso_and_epoch_timestamps_without_crashing(self):
        rows = b.merge_rows(
            [{"tx_hash": "later", "timestamp": "2026-01-01T00:00:00Z"}],
            [{"tx_hash": "earlier", "timestamp": 1000}],
        )
        self.assertEqual([row["tx_hash"] for row in rows], ["earlier", "later"])

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

    def test_wallet_and_token_identity_respects_chain_case_rules(self):
        self.assertNotEqual(b.wallet_key("sol", "AbC"), b.wallet_key("sol", "abc"))
        self.assertEqual(b.wallet_key("eth", "0xAbC"), b.wallet_key("eth", "0xabc"))
        sol_rows = b.merge_rows(
            [{"chain": "sol", "timestamp": 1, "token_address": "AbC", "side": "buy", "amount_usd": 1}],
            [{"chain": "sol", "timestamp": 1, "token_address": "abc", "side": "buy", "amount_usd": 1}],
        )
        self.assertEqual(len(sol_rows), 2)

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
