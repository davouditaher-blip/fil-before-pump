import unittest

import gmgn_layer
import scanner


def trade(tx="", price=1.0):
    row = {
        "maker": "wallet",
        "base_address": "TokenABC",
        "timestamp": 1_800_000_000,
        "chain": "sol",
        "side": "buy",
        "amount_usd": 10,
        "price_usd": price,
        "price_change": 1.0,
        "base_token": {"symbol": "TOK"},
    }
    if tx:
        row["transaction_hash"] = tx
    return row


class GMGNHistoryMergeTests(unittest.TestCase):
    def test_different_transaction_hashes_are_never_merged(self):
        history = {}
        gmgn_layer.update_history([trade("tx-1", 1.0), trade("tx-2", 1.0)], history)
        self.assertEqual(len(history["wallet"]), 2)
        self.assertEqual({row["transaction_hash"] for row in history["wallet"]}, {"tx-1", "tx-2"})

    def test_same_transaction_different_asset_legs_are_preserved(self):
        first = trade("tx-1", 1.0)
        second = dict(trade("tx-1", 2.0), base_address="TokenXYZ", base_token={"symbol": "OTHER"})
        history = {}
        gmgn_layer.update_history([first, second], history)
        self.assertEqual(len(history["wallet"]), 2)
        self.assertEqual({row["address"] for row in history["wallet"]}, {"TokenABC", "TokenXYZ"})

    def test_same_transaction_asset_leg_different_execution_prices_are_preserved(self):
        history = {}
        gmgn_layer.update_history([trade("tx-1", 1.0), trade("tx-1", 1.1)], history)
        self.assertEqual(len(history["wallet"]), 2)
        self.assertEqual({row["price_usd"] for row in history["wallet"]}, {1.0, 1.1})
    def test_different_prices_without_transaction_hash_are_preserved(self):
        history = {}
        gmgn_layer.update_history([trade("", 1.0), trade("", 1.1)], history)
        self.assertEqual(len(history["wallet"]), 2)
        self.assertEqual({row["price_usd"] for row in history["wallet"]}, {1.0, 1.1})

    def test_scanner_identity_keeps_distinct_transaction_hashes(self):
        first = {"chain": "eth", "tx_hash": "tx-1", "timestamp": 1000, "address": "0xabc"}
        second = {"chain": "eth", "tx_hash": "tx-2", "timestamp": 1000, "address": "0xabc"}
        self.assertNotEqual(scanner._gmgn_history_key(first), scanner._gmgn_history_key(second))

    def test_scanner_identity_keeps_distinct_same_second_prices(self):
        first = {"chain": "sol", "address": "Token", "timestamp": 1000, "side": "buy", "amount_usd": 5, "price_usd": 0.5}
        second = dict(first, price_usd=0.6)
        self.assertNotEqual(scanner._gmgn_history_key(first), scanner._gmgn_history_key(second))

    def test_exact_duplicate_without_transaction_hash_is_compacted(self):
        history = {}
        gmgn_layer.update_history([trade("", 1.0), trade("", 1.0)], history)
        self.assertEqual(len(history["wallet"]), 1)


if __name__ == "__main__":
    unittest.main()
