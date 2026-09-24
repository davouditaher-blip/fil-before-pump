def score_market(features:dict)->tuple[float,list[str]]:
    # Context only. No single market field can hard-reject a candidate.
    score=50.0; ev=[]
    btc=features.get("btc_regime"); eth=features.get("eth_regime")
    if btc in ("bull","risk_on"): score+=15; ev.append("BTC risk-on")
    if btc in ("bear","risk_off"): score-=15; ev.append("BTC risk-off")
    if eth in ("bull","risk_on"): score+=10; ev.append("ETH risk-on")
    if eth in ("bear","risk_off"): score-=10; ev.append("ETH risk-off")
    return round(max(0,min(100,score)),2),ev
