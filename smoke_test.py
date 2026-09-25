"""Deterministic smoke tests for the wallet-first Fil Before Pump pipeline."""
from scanner import is_primary_crypto_asset
from trade_readiness import build_trade_plan

def test_asset_exclusions():
    assert not is_primary_crypto_asset({"symbol": "PAXG", "name": "PAX Gold"})
    assert not is_primary_crypto_asset({"symbol": "XAUT", "name": "Tether Gold"})
    assert not is_primary_crypto_asset({"symbol": "USDT", "name": "Tether USDt"})
    assert not is_primary_crypto_asset({"symbol": "AAPLX", "name": "Tokenized Apple Stock"})
    assert is_primary_crypto_asset({"symbol": "LINK", "name": "Chainlink"})

def test_trade_plan_is_deterministic_and_paper_only():
    plan = build_trade_plan({
        "symbol": "LINK",
        "rank": 13,
        "price_usd": 10,
        "quote": {"market_cap": 1_000_000_000, "volume_24h": 20_000_000},
        "current_volume": 20_000_000,
        "wallet_conviction_score": 18,
        "wallet_unique_active_count": 2,
        "wallet_unique_proven_count": 1,
        "wallet_unique_shared_count": 1,
        "wallet_exit_pressure": 10,
        "fil_confluence_score": 75,
        "ch24": 3,
        "vol_changes": {"1d": 5, "2d": 2},
    })
    assert plan["state"] == "PAPER_READY"
    assert "risk" in plan
    assert plan["risk"]["leverage_cap"] == 3

if __name__ == "__main__":
    test_asset_exclusions()
    test_trade_plan_is_deterministic_and_paper_only()
    print("Fil Before Pump smoke tests: PASS")
