"""Offline smoke tests for wallet quality and GMGN history retention."""

from wallet_quality_engine import build_wallet_quality, historical_metrics, quality_label
from gmgn_layer import deduplicate_history

WALLET = "0xTEST"


def main():
    history = {
        WALLET: [
            {"side": "buy", "amount_usd": 7000, "peak_multiple": 2.4},
            {"side": "buy", "amount_usd": 8000, "peak_multiple": 1.4},
            {"side": "buy", "amount_usd": 9000, "peak_multiple": 2.1},
            {"side": "sell", "amount_usd": 3000, "peak_multiple": 1.0},
        ]
    }
    radar = {
        "eth:0xTEST": {
            "wallet": WALLET,
            "chain": "eth",
            "qualified_buy_count": 3,
            "qualified_buy_usd": 24000,
            "position_states": {"ABC": "holding"},
        }
    }

    m = historical_metrics(history, WALLET)
    assert m["qualified_historical_buys"] == 3
    assert m["observed_entries"] == 3
    assert m["successful_2x_entries"] == 2
    assert m["pre_pump_win_rate"] == 66.67

    rows = build_wallet_quality(radar, history)
    assert len(rows) == 1
    assert rows[0]["quality_label"] == "LOW"
    assert rows[0]["quality_score"] == 41.7
    assert 0 <= rows[0]["quality_score"] <= 100
    assert quality_label(0, 0) == "INSUFFICIENT_DATA"

    duplicate_history = {
        "0xDUP": [
            {
                "trade_timestamp": 100,
                "chain": "eth",
                "address": "0xTOKEN",
                "side": "buy",
                "amount_usd": 6000,
                "price_change": 1.2,
                "peak_multiple": 1.2,
            },
            {
                "trade_timestamp": 100,
                "chain": "eth",
                "address": "0xTOKEN",
                "side": "buy",
                "amount_usd": 6000,
                "price_change": 2.3,
                "peak_multiple": 2.3,
            },
        ]
    }
    cleaned = deduplicate_history(duplicate_history)
    assert len(cleaned["0xDUP"]) == 1
    assert cleaned["0xDUP"][0]["peak_multiple"] == 2.3

    # Retention must no longer truncate a wallet to 1,000 records.
    long_history = {
        "0xLONG": [
            {"trade_timestamp": i, "address": f"token-{i}", "side": "buy", "amount_usd": 1}
            for i in range(1200)
        ]
    }
    retained = deduplicate_history(long_history)
    assert len(retained["0xLONG"]) == 1200

    print("PASS: wallet_quality_engine + GMGN history tests")


if __name__ == "__main__":
    main()
