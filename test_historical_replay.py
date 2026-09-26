from historical_replay import replay

def test_forward_only_replay():
    data = {
        "w1": [
            {"symbol": "TEST", "side": "buy", "amount_usd": 6000, "price_usd": 100, "timestamp": 1000},
            {"symbol": "TEST", "side": "sell", "amount_usd": 1000, "price_usd": 104, "timestamp": 1100},
            {"symbol": "TEST", "side": "buy", "amount_usd": 6000, "price_usd": 100, "timestamp": 1200},
            {"symbol": "TEST", "side": "buy", "amount_usd": 6000, "price_usd": 110, "timestamp": 2000},
        ]
    }
    result = replay(data)
    assert result["orders_enabled"] is False
    assert result["observation_count"] == 2
    assert result["statistics"]["24"]["hit_rate_pct"]["5"] == 100.0

def test_ignores_small_buys():
    result = replay({"w": [{"symbol":"X","side":"buy","amount_usd":100,"price_usd":10,"timestamp":1000}]})
    assert result["observation_count"] == 0

if __name__ == "__main__":
    test_forward_only_replay()
    test_ignores_small_buys()
    print("Historical replay tests: PASS")
