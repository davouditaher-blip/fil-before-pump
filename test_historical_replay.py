import tempfile
from pathlib import Path

from historical_replay import FIRST_ENTRY, REPEAT_ENTRY, replay, prior_qualified_buys


def _replay(data):
    """Replay into a temporary file so the committed dataset is never touched."""
    with tempfile.TemporaryDirectory() as tmp:
        return replay(data, out_path=Path(tmp) / "historical_replay.json")


def test_forward_only_replay():
    data = {
        "w1": [
            {"symbol": "TEST", "side": "buy", "amount_usd": 6000, "price_usd": 100, "timestamp": 1000},
            {"symbol": "TEST", "side": "sell", "amount_usd": 1000, "price_usd": 104, "timestamp": 1100},
            {"symbol": "TEST", "side": "buy", "amount_usd": 6000, "price_usd": 100, "timestamp": 1200},
            {"symbol": "TEST", "side": "buy", "amount_usd": 6000, "price_usd": 110, "timestamp": 2000},
        ]
    }
    result = _replay(data)
    assert result["orders_enabled"] is False
    assert result["observation_count"] == 2
    assert result["statistics"]["24"]["hit_rate_pct"]["5"] == 100.0

def test_ignores_small_buys():
    result = _replay({"w": [{"symbol":"X","side":"buy","amount_usd":100,"price_usd":10,"timestamp":1000}]})
    assert result["observation_count"] == 0

def test_cohort_label_is_forward_only():
    """The cohort must be decided from rows strictly before the entry."""
    rows = [
        {"symbol": "X", "side": "buy", "amount_usd": 6000, "price_usd": 10, "timestamp": 1000},
        {"symbol": "X", "side": "buy", "amount_usd": 6000, "price_usd": 11, "timestamp": 2000},
        {"symbol": "Y", "side": "buy", "amount_usd": 6000, "price_usd": 12, "timestamp": 1500},
    ]
    assert prior_qualified_buys(rows, "X", 2000) == 1
    assert prior_qualified_buys(rows, "X", 1000) == 0
    # A different symbol's earlier buy does not count as prior experience.
    assert prior_qualified_buys(rows, "Y", 2000) == 1
    assert prior_qualified_buys(rows, "Z", 2000) == 0
    # A row at exactly t0 is not "prior".
    assert prior_qualified_buys(rows, "X", 1000) == 0

def test_cohort_split_is_reported():
    data = {
        "w1": [
            {"symbol": "X", "side": "buy", "amount_usd": 6000, "price_usd": 10, "timestamp": 1000},
            {"symbol": "X", "side": "sell", "amount_usd": 500, "price_usd": 11, "timestamp": 1100},
            {"symbol": "X", "side": "buy", "amount_usd": 6000, "price_usd": 12, "timestamp": 2000},
            {"symbol": "X", "side": "sell", "amount_usd": 500, "price_usd": 13, "timestamp": 2200},
        ]
    }
    result = _replay(data)
    assert sorted(o["entry_type"] for o in result["observations"]) == [FIRST_ENTRY, REPEAT_ENTRY]
    assert result["cohort_statistics"][FIRST_ENTRY]["6"]["observations"] >= 1
    assert result["cohort_statistics"][REPEAT_ENTRY]["6"]["observations"] >= 1
    # A cohort with no observations must report None rather than crash or fake a rate.
    blank = _replay({"w": [{"symbol": "X", "side": "buy", "amount_usd": 100, "price_usd": 10, "timestamp": 1000}]})
    assert blank["observation_count"] == 0
    assert blank["cohort_statistics"][FIRST_ENTRY]["6"]["observations"] == 0
    assert blank["cohort_statistics"][FIRST_ENTRY]["6"]["hit_rate_pct"]["5"] is None

if __name__ == "__main__":
    test_forward_only_replay()
    test_ignores_small_buys()
    test_cohort_label_is_forward_only()
    test_cohort_split_is_reported()
    print("Historical replay tests: PASS")
