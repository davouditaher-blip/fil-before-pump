"""Final wallet-first Fil confluence layer.

This module combines independent evidence already collected by scanner.py.
Wallet evidence is represented only by Wallet Conviction to avoid double
counting. Technical indicators are context-only and never a hard gate.
Read-only: this module never places orders.
"""

def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

def _project(result):
    w = result.get("wallet") or {}
    ratio = w.get("buy_sell_ratio_7d")
    buyers = int(w.get("buyers_7d", 0) or 0)
    sellers = int(w.get("sellers_7d", 0) or 0)
    top5 = _num(w.get("top5_holder_pct"))
    top20 = _num(w.get("top20_holder_pct"))
    score = 0.0
    reasons = []
    if ratio is not None:
        r = _num(ratio)
        if r >= 1.5:
            score += 7; reasons.append("project buy/sell flow strongly positive")
        elif r > 1.10:
            score += 5; reasons.append("project buy/sell flow positive")
        elif r < 0.75:
            score -= 4; reasons.append("project sell pressure elevated")
    if buyers > sellers:
        score += 3; reasons.append("buyers exceed sellers")
    elif sellers > buyers and sellers:
        score -= 2; reasons.append("sellers exceed buyers")
    if top20:
        if top20 <= 35: score += 5
        elif top20 <= 55: score += 3
        elif top20 >= 75: score -= 4; reasons.append("top-20 holder concentration high")
    if top5 >= 60:
        score -= 4; reasons.append("top-5 holder concentration high")
    elif top5 and top5 <= 30:
        score += 2
    return _clamp(score, 0, 20), reasons, {
        "buy_sell_ratio_7d": ratio, "buyers_7d": buyers, "sellers_7d": sellers,
        "top5_holder_pct": top5 or None, "top20_holder_pct": top20 or None,
    }

def _volume(result):
    v = result.get("vol_changes") or {}
    score = 0.0
    reasons = []
    for key, base in (("1d", 8), ("2d", 7)):
        value = v.get(key)
        if value is None: continue
        n = _num(value)
        if n >= 25: score += base; reasons.append(f"{key} volume +25%+")
        elif n >= 10: score += base * .8; reasons.append(f"{key} volume +10%+")
        elif n >= 5: score += base * .6; reasons.append(f"{key} volume +5%+")
        elif n >= 2: score += base * .4; reasons.append(f"{key} volume +2%+")
        elif n > 0: score += base * .2
    if v.get("3d") is not None and _num(v.get("3d")) > 0: score += 1
    if v.get("7d") is not None and _num(v.get("7d")) > 0: score += 1
    if result.get("current_volume"): score += 1
    return _clamp(score, 0, 20), reasons, {
        "1d": v.get("1d"), "2d": v.get("2d"), "3d": v.get("3d"),
        "7d": v.get("7d"), "acceleration": max((_num(v.get(k)) for k in ("1d","2d") if v.get(k) is not None), default=0),
    }

def _market(market):
    btc24, eth24 = _num(market.get("btc24")), _num(market.get("eth24"))
    btc7, eth7 = _num(market.get("btc7")), _num(market.get("eth7"))
    avg24, avg7 = (btc24 + eth24) / 2, (btc7 + eth7) / 2
    score = 5 if avg24 > 0 else 3 if avg24 >= -2 else 0
    reasons = ["BTC/ETH 24h context supportive" if avg24 > 0 else "BTC/ETH 24h context neutral" if avg24 >= -2 else "BTC/ETH 24h weakness"]
    if avg7 > 0: score += 3
    return _clamp(score, 0, 10), reasons, {"btc24": btc24, "eth24": eth24, "btc7": btc7, "eth7": eth7}

def _safety(result):
    q = result.get("quote") or {}
    cap = _num(q.get("market_cap"))
    volume = _num(result.get("current_volume") or q.get("volume_24h"))
    ch24 = _num(result.get("ch24"))
    score = 0.0
    reasons = []
    if cap and volume:
        ratio = volume / cap
        score += 7 if .02 <= ratio <= .50 else 2 if ratio > 1 else 4
        if ratio > 1: reasons.append("very high volume/market-cap requires caution")
    score += 5 if abs(ch24) <= 8 else 3 if abs(ch24) <= 15 else 1
    if abs(ch24) > 15: reasons.append("large 24h move requires caution")
    if result.get("futures_source") or result.get("futures_contract"): score += 3
    return _clamp(score, 0, 20), reasons, {
        "market_cap": cap, "current_volume": volume,
        "volume_market_cap": round(volume / cap, 4) if cap else None,
        "abs_24h_move": abs(ch24),
    }

def build_confluence(result, market=None):
    market = market or {}
    wallet = _clamp(_num(result.get("wallet_conviction_score")), 0, 30)
    project, project_reasons, project_evidence = _project(result)
    volume, volume_reasons, volume_evidence = _volume(result)
    market_score, market_reasons, market_evidence = _market(market)
    safety, safety_reasons, safety_evidence = _safety(result)
    final = round(_clamp(wallet + project + volume + market_score + safety, 0, 100), 1)
    result.update({
        "project_intelligence_score": round(project, 1),
        "project_intelligence": project_evidence,
        "volume_intelligence_score": round(volume, 1),
        "volume_intelligence": volume_evidence,
        "market_context_score": round(market_score, 1),
        "market_context": market_evidence,
        "safety_context_score": round(safety, 1),
        "safety_context": safety_evidence,
        "fil_confluence_score": final,
        "fil_confluence_components": {
            "wallet": round(wallet,1), "project": round(project,1),
            "volume": round(volume,1), "market": round(market_score,1),
            "safety": round(safety,1),
        },
        "fil_confluence_reasons": [f"Wallet Conviction {wallet:.1f}/30"] + project_reasons[:3] + volume_reasons[:3] + market_reasons[:1] + safety_reasons[:2],
    })
    return result
