from fil_engine.gmgn_history import qualified_buys, outcome, wallet_stats

def test_qualified_buy_and_future_outcome():
    h={"w":[{"trade_timestamp":10,"side":"buy","amount_usd":6000,"symbol":"X","peak_multiple":2.1},
            {"trade_timestamp":11,"side":"buy","amount_usd":4000,"symbol":"Y","peak_multiple":3}]}
    q=qualified_buys(h)
    assert len(q)==1
    o=outcome(q[0])
    assert o["hit_2x"] is True

def test_wallet_stats_unknown_is_not_loss():
    h={"w":[{"trade_timestamp":10,"side":"buy","amount_usd":6000,"symbol":"X","peak_multiple":2},
            {"trade_timestamp":11,"side":"buy","amount_usd":7000,"symbol":"Y","peak_multiple":None}]}
    s=wallet_stats(h)["w"]
    assert s["qualified_buys"]==2
    assert s["known_outcomes"]==1
    assert s["win_rate_2x"]==100.0
