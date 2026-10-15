import unittest
import gmgn_layer


class GMGNChainAttributionTests(unittest.TestCase):
    def test_requested_chain_fills_missing_provider_metadata(self):
        source = [
            {"maker": "wallet", "base_address": "token"},
            {"maker": "wallet2", "chain": "eth", "base_address": "other"},
            None,
        ]
        result = gmgn_layer.with_chain_context(source, "sol")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["chain"], "sol")
        self.assertEqual(result[1]["chain"], "eth")
        self.assertNotIn("chain", source[0], "the source object must not be mutated")

    def test_each_requested_chain_is_preserved(self):
        for chain in ("sol", "bsc", "base", "eth"):
            self.assertEqual(gmgn_layer.with_chain_context([{"address": "token"}], chain)[0]["chain"], chain)


if __name__ == "__main__":
    unittest.main()
