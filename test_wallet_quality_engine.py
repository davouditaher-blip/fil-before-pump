"""Offline smoke tests for wallet_quality_engine.py."""

from wallet_quality_engine import build_wallet_quality, historical_metrics, quality_label

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
    assert rows[0]["quality_label"] in {"MEDIUM", "HIGH"}
    assert 0 <= rows[0]["quality_score"] <= 100
    assert quality_label(0, 0) == "INSUFFICIENT_DATA"

    print("PASS: wallet_quality_engine smoke tests")


if __name__ == "__main__":
    main()
