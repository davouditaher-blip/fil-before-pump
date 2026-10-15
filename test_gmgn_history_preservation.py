import json
import tempfile
import unittest
from pathlib import Path

import validate_gmgn_history_preservation as guard


class GMGNHistoryPreservationTests(unittest.TestCase):
    def test_additions_and_field_enrichment_preserve_old_events(self):
        before = {"0x" + "a" * 40: [
            {"chain": "eth", "transaction_hash": "0xABC", "timestamp": 1000, "side": "buy"}
        ]}
        after = {"0x" + "A" * 40: [
            {"chain": "eth", "transaction_hash": "0xabc", "timestamp": 1000, "side": "buy", "symbol": "TOK"},
            {"chain": "eth", "transaction_hash": "0xdef", "timestamp": 2000, "side": "sell"},
        ]}
        result = guard.compare_histories(before, after)
        self.assertTrue(result["preserved"])
        self.assertEqual(result["baseline_rows"], 1)
        self.assertEqual(result["current_rows"], 2)

    def test_deleted_old_event_fails_closed(self):
        before = {"wallet": [
            {"chain": "sol", "address": "TokenABC", "timestamp": 1000, "side": "buy", "amount_usd": 5, "price_usd": 0.5}
        ]}
        after = {"wallet": []}
        result = guard.compare_histories(before, after)
        self.assertFalse(result["preserved"])
        self.assertEqual(result["missing_row_instances"], 1)

    def test_solana_token_address_case_is_preserved(self):
        before = {"wallet": [
            {"chain": "sol", "address": "AbC", "timestamp": 1000, "side": "buy", "amount_usd": 5, "price_usd": 0.5}
        ]}
        after = {"wallet": [
            {"chain": "sol", "address": "abc", "timestamp": 1000, "side": "buy", "amount_usd": 5, "price_usd": 0.5}
        ]}
        self.assertFalse(guard.compare_histories(before, after)["preserved"])

    def test_invalid_history_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaises(ValueError):
                guard.load_history(path)


if __name__ == "__main__":
    unittest.main()
