import json
import tempfile
import unittest
from pathlib import Path

import wallet_history_validation as whv


class GMGNHistoryMergeTests(unittest.TestCase):
    def test_same_transaction_hash_keeps_distinct_asset_legs(self):
        wallet = "wallet"
        history = {wallet: [
            {"chain": "eth", "transaction_hash": "0xabc", "address": "0xTokenA", "side": "buy", "timestamp": 1000, "price_usd": 1.0},
            {"chain": "eth", "transaction_hash": "0xabc", "address": "0xTokenB", "side": "buy", "timestamp": 1000, "price_usd": 2.0},
        ]}
        merged = whv.merge_history_data(history)
        self.assertEqual(len(merged[wallet]), 2)
        self.assertEqual({row["address"] for row in merged[wallet]}, {"0xTokenA", "0xTokenB"})

    def test_unknown_chain_rows_do_not_collapse_on_transaction_hash_alone(self):
        history = {"wallet": [
            {"transaction_hash": "0xabc", "address": "TokenA", "timestamp": 1000, "side": "buy", "price_usd": 1.0},
            {"transaction_hash": "0xabc", "address": "TokenA", "timestamp": 1000, "side": "buy", "price_usd": 2.0},
        ]}
        merged = whv.merge_history_data(history)
        self.assertEqual(len(merged["wallet"]), 2)

    def test_merge_preserves_static_fields_peak_and_first_hit_timestamps(self):
        old = {
            "chain": "eth",
            "transaction_hash": "0xabc",
            "timestamp": 1000,
            "trade_timestamp": 1000,
            "price_usd": 0.5,
            "amount_usd": 25,
            "peak_multiple": 4.2,
            "first_2x_timestamp": 2000,
            "maker_tags": ["smart_money"],
            "maker_info": {"source": "gmgn"},
        }
        new = {
            "chain": "eth",
            "transaction_hash": "0xabc",
            "timestamp": 1000,
            "price_usd": 0.5,
            "amount_usd": 25,
            "peak_multiple": 1.0,
            "first_2x_timestamp": 3000,
            "maker_tags": ["whale"],
            "maker_info": {"source": ""},
            "price_change": 1.4,
        }
        merged = whv.merge_history_record(old, new)
        self.assertEqual(merged["trade_timestamp"], 1000)
        self.assertEqual(merged["peak_multiple"], 4.2)
        self.assertEqual(merged["first_2x_timestamp"], 2000)
        self.assertEqual(set(merged["maker_tags"]), {"smart_money", "whale"})
        self.assertEqual(merged["maker_info"]["source"], "gmgn")
        self.assertEqual(merged["price_change"], 1.4)

    def test_merge_history_data_restores_missing_rows_and_fields(self):
        baseline = {"wallet": [{
            "chain": "sol",
            "transaction_hash": "tx-1",
            "timestamp": 1000,
            "trade_timestamp": 1000,
            "peak_multiple": 3.0,
            "maker_tags": ["smart_money"],
        }]}
        current = {"wallet": [{
            "chain": "sol",
            "transaction_hash": "tx-1",
            "timestamp": 1000,
            "peak_multiple": 1.0,
            "maker_tags": [],
            "price_change": 1.2,
        }, {
            "chain": "sol",
            "transaction_hash": "tx-2",
            "timestamp": 2000,
        }]}
        merged = whv.merge_history_data(baseline, current)
        self.assertEqual(len(merged["wallet"]), 2)
        first = next(row for row in merged["wallet"] if row.get("transaction_hash") == "tx-1")
        self.assertEqual(first["trade_timestamp"], 1000)
        self.assertEqual(first["peak_multiple"], 3.0)
        self.assertEqual(first["maker_tags"], ["smart_money"])
        self.assertEqual(first["price_change"], 1.2)

    def test_atomic_writer_outputs_valid_newline_terminated_json(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.json"
            history = {"wallet": [{"chain": "eth", "transaction_hash": "tx-1"}]}
            whv.atomic_write_history(path, history)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), history)
            self.assertTrue(path.read_bytes().endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
