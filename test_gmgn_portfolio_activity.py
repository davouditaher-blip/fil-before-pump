import unittest
from unittest.mock import patch

import gmgn_layer as gmgn


class GMGNPortfolioActivityTests(unittest.TestCase):
    def setUp(self):
        gmgn._ACTIVITY_CALLS["count"] = 0
        gmgn._ACTIVITY_CALLS["exhausted"] = False

    def tearDown(self):
        gmgn._ACTIVITY_CALLS["count"] = 0
        gmgn._ACTIVITY_CALLS["exhausted"] = False

    def test_cursor_pages_are_collected_and_budget_counts_each_page(self):
        pages = [
            {"activities": [{"id": "1"}], "next": "cursor-2"},
            {"activities": [{"id": "2"}], "next": None},
        ]
        with patch.object(gmgn, "run_gmgn_cli", side_effect=pages) as run, patch.object(gmgn.time, "sleep"):
            rows = gmgn.portfolio_activity("sol", "wallet", limit=2)
        self.assertEqual([row["id"] for row in rows], ["1", "2"])
        self.assertEqual(run.call_count, 2)
        self.assertIn("--cursor", run.call_args_list[1].args[0])
        self.assertEqual(gmgn.activity_call_count(), 2)

    def test_activities_and_nested_data_shapes_are_supported(self):
        with patch.object(gmgn, "run_gmgn_cli", return_value={"data": {"activities": [{"id": "x"}], "next": None}}), patch.object(gmgn.time, "sleep"):
            rows = gmgn.portfolio_activity("base", "wallet", limit=10)
        self.assertEqual(rows, [{"id": "x"}])

    def test_full_page_without_cursor_is_discarded_as_incomplete(self):
        page = {"activities": [{"id": "1"}, {"id": "2"}]}
        with patch.object(gmgn, "run_gmgn_cli", return_value=page), patch.object(gmgn.time, "sleep"):
            rows = gmgn.portfolio_activity("eth", "wallet", limit=2)
        self.assertEqual(rows, [])
        self.assertEqual(gmgn.activity_call_count(), 1)

    def test_max_page_guard_does_not_return_partial_history(self):
        def response(args, timeout):
            cursor = args[args.index("--cursor") + 1] if "--cursor" in args else "cursor-1"
            next_cursor = "cursor-2" if cursor == "cursor-1" else f"cursor-{int(cursor.split('-')[1])+1}"
            return {"activities": [{"id": cursor}], "next": next_cursor}
        with patch.object(gmgn, "run_gmgn_cli", side_effect=response), patch.object(gmgn.time, "sleep"):
            rows = gmgn.portfolio_activity("bsc", "wallet", limit=2)
        self.assertEqual(rows, [])
        self.assertEqual(gmgn.activity_call_count(), gmgn.MAX_ACTIVITY_PAGES_PER_WALLET)


if __name__ == "__main__":
    unittest.main()
