from fil_engine.historical import normalize_explicit_events, normalize_holder_snapshots

def test_explicit_event_normalization_rejects_unlabelled_holder_delta():
    rows=[{"wallet":"w","symbol":"X","timestamp":10,"event":"accumulation","price_usd":2},
          {"wallet":"w","symbol":"X","timestamp":11,"side":"buy","amount_usd":6000,"price_usd":2}]
    events=normalize_explicit_events(rows)
    assert len(events)==1 and events[0].side=="buy" and events[0].amount_usd==6000

def test_holder_delta_is_marked_proxy():
    rows=[{"wallet":"w","symbol":"X","timestamp":10,"balance":100,"price_usd":2},
          {"wallet":"w","symbol":"X","timestamp":11,"balance":110,"price_usd":2}]
    signals,prices=normalize_holder_snapshots(rows)
    assert len(signals)==1 and signals[0].source=="holder_delta_proxy"
    assert signals[0].side=="buy" and prices[-1].price_usd==2
