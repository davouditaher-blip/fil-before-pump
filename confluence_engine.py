"""Final wallet-first Fil confluence layer.

This module combines independent evidence already collected by scanner.py.
Wallet evidence is represented only by Wallet Conviction to avoid double
counting. Technical indicators are context-only and never a hard gate.
Read-only: this module never places orders.

The wallet bucket is a single capped Wallet Conviction value. Long-term
signal-wallet profile evidence and the bounded paper calibration memory are
already folded into that value by scanner.wallet_conviction_signals, so they
are reported here for auditability and are deliberately not counted twice.

Scale contract
--------------
Every bucket must be able to reach its declared cap, otherwise the advertised
100-point scale is really a smaller scale and any fixed threshold silently
becomes unreachable. The four missing criteria that previously made 12 points
structurally unreachable are now scored from evidence the layers already
collect, and ``_caps_reachable`` is asserted by the smoke tests so a future
edit cannot quietly reintroduce a dead cap.

The project bucket is exactly saturated: 13 points of 7-day flow plus 7 points
of holder concentration equals PROJECT_CAP. It therefore gains no new criteria.
EVM coverage is instead extended by filling the existing flow fields from
provider-labelled GMGN trades, which lifts the EVM ceiling above the old
concentration-only 7/20. ``_project`` itself is deliberately unchanged.
"""

WALLET_CAP = 30.0
PROJECT_CAP = 20.0
VOLUME_CAP = 20.0
MARKET_CAP = 10.0
SAFETY_CAP = 20.0
BUCKET_CAPS = {
    "wallet": WALLET_CAP,
    "project": PROJECT_CAP,
    "volume": VOLUME_CAP,
    "market": MARKET_CAP,
    "safety": SAFETY_CAP,
}
SCALE_MAX = sum(BUCKET_CAPS.values())

# Single source of truth for the paper-entry state. trade_readiness imports this
# so the threshold and the advertised scale can never drift apart.
PAPER_READY_MIN_CONFLUENCE = 70.0

# Operational execution floors for the safety bucket. These describe whether a
# paper entry is operationally executable, not a price prediction.
MARKET_CAP_FLOOR_USD = 500_000_000.0
FUTURES_VOLUME_FLOOR_USD = 10_000_000.0
# Buyer-participation breadth floor for the project bucket.
BROAD_BUYER_FLOOR = 25


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
    if buyers >= BROAD_BUYER_FLOOR:
        score += 3; reasons.append("broad buyer participation")
    if top20:
        if top20 <= 35: score += 5
        elif top20 <= 55: score += 3
        elif top20 >= 75: score -= 4; reasons.append("top-20 holder concentration high")
    if top5 >= 60:
        score -= 4; reasons.append("top-5 holder concentration high")
    elif top5 and top5 <= 30:
        score += 2
    return _clamp(score, 0, PROJECT_CAP), reasons, {
        "buy_sell_ratio_7d": ratio, "buyers_7d": buyers, "sellers_7d": sellers,
        "top5_holder_pct": top5 or None, "top20_holder_pct": top20 or None,
        # Provenance only, passed straight through from the layer. Solana flow
        # is Solscan's all-participant token flow; EVM flow is GMGN's labelled
        # smart-money trades. They measure different populations, so the source
        # is reported rather than presented as one measurement.
        "flow_provider": w.get("flow_provider"),
        "flow_semantics": w.get("flow_semantics"),
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
    # Fresh acceleration: the most recent day is at least as strong as the day
    # before it. This is the pre-pump signature and was previously computed as
    # evidence but never scored, which left two points of the cap unreachable.
    one_day, two_days = _num(v.get("1d")), _num(v.get("2d"))
    if one_day > 0 and two_days > 0 and one_day >= two_days:
        score += 2; reasons.append("volume accelerating into the signal")
    return _clamp(score, 0, VOLUME_CAP), reasons, {
        "1d": v.get("1d"), "2d": v.get("2d"), "3d": v.get("3d"),
        "7d": v.get("7d"), "acceleration": max((_num(v.get(k)) for k in ("1d","2d") if v.get(k) is not None), default=0),
        "fresh_acceleration": bool(one_day > 0 and two_days > 0 and one_day >= two_days),
    }

def _market(market):
    btc24, eth24 = _num(market.get("btc24")), _num(market.get("eth24"))
    btc7, eth7 = _num(market.get("btc7")), _num(market.get("eth7"))
    avg24, avg7 = (btc24 + eth24) / 2, (btc7 + eth7) / 2
    score = 5 if avg24 > 0 else 3 if avg24 >= -2 else 0
    reasons = ["BTC/ETH 24h context supportive" if avg24 > 0 else "BTC/ETH 24h context neutral" if avg24 >= -2 else "BTC/ETH 24h weakness"]
    if avg7 > 0: score += 3
    # Breadth, not just the average: a positive average can hide one strongly
    # negative major. Requiring both majors positive was the missing criterion
    # that left two points of the cap unreachable.
    if btc24 > 0 and eth24 > 0:
        score += 2; reasons.append("BTC and ETH both positive on 24h")
    return _clamp(score, 0, MARKET_CAP), reasons, {"btc24": btc24, "eth24": eth24, "btc7": btc7, "eth7": eth7}

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
    # Operational execution floors. Below these a paper entry is not reliably
    # executable, so they are safety evidence rather than a price forecast.
    if cap >= MARKET_CAP_FLOOR_USD:
        score += 3
    else:
        reasons.append("market cap below execution floor")
    if volume >= FUTURES_VOLUME_FLOOR_USD:
        score += 2
    else:
        reasons.append("futures volume below execution floor")
    return _clamp(score, 0, SAFETY_CAP), reasons, {
        "market_cap": cap, "current_volume": volume,
        "volume_market_cap": round(volume / cap, 4) if cap else None,
        "abs_24h_move": abs(ch24),
        "market_cap_above_execution_floor": cap >= MARKET_CAP_FLOOR_USD,
        "futures_volume_above_execution_floor": volume >= FUTURES_VOLUME_FLOOR_USD,
    }

def _caps_reachable() -> bool:
    """Assert every declared bucket cap is actually attainable.

    A cap that no evidence can reach silently shrinks the advertised scale and
    makes any fixed threshold unreachable. The smoke tests call this so a
    future edit cannot quietly reintroduce a dead cap.
    """
    best = {
        "project": _project({
            "wallet": {
                "buy_sell_ratio_7d": 1.8, "buyers_7d": BROAD_BUYER_FLOOR + 25,
                "sellers_7d": 5, "top5_holder_pct": 20.0, "top20_holder_pct": 30.0,
            }
        })[0],
        "volume": _volume({
            "vol_changes": {"1d": 30, "2d": 25, "3d": 5, "7d": 5},
            "current_volume": FUTURES_VOLUME_FLOOR_USD * 10,
        })[0],
        "market": _market({"btc24": 3.0, "eth24": 3.0, "btc7": 4.0, "eth7": 4.0})[0],
        "safety": _safety({
            "quote": {"market_cap": MARKET_CAP_FLOOR_USD * 10},
            "current_volume": FUTURES_VOLUME_FLOOR_USD * 10,
            "ch24": 0.0, "futures_source": "binance",
        })[0],
    }
    return all(best[name] >= cap for name, cap in BUCKET_CAPS.items() if name != "wallet")


def build_confluence(result, market=None):
    market = market or {}
    wallet = _clamp(_num(result.get("wallet_conviction_score")), 0, WALLET_CAP)
    project, project_reasons, project_evidence = _project(result)
    volume, volume_reasons, volume_evidence = _volume(result)
    market_score, market_reasons, market_evidence = _market(market)
    safety, safety_reasons, safety_evidence = _safety(result)
    final = round(_clamp(wallet + project + volume + market_score + safety, 0, SCALE_MAX), 1)
    # The project bucket depends on provider layers (Solscan flow/holders or
    # GoldRush holders) that only cover part of the futures universe. Recording
    # coverage makes it possible to tell a genuinely weak candidate apart from
    # one that simply has no project evidence available.
    project_layer_present = any(
        _num(project_evidence.get(k)) for k in ("buy_sell_ratio_7d", "buyers_7d", "top5_holder_pct", "top20_holder_pct")
    )
    components = {
        "wallet": round(wallet, 1), "project": round(project, 1),
        "volume": round(volume, 1), "market": round(market_score, 1),
        "safety": round(safety, 1),
    }
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
        "fil_confluence_components": components,
        "fil_confluence_scale": {
            "max_score": SCALE_MAX,
            "bucket_caps": dict(BUCKET_CAPS),
            "paper_ready_threshold": PAPER_READY_MIN_CONFLUENCE,
            "fill_pct": round(final / SCALE_MAX * 100, 1),
            "headroom_to_threshold": round(SCALE_MAX - final, 1),
            "components": components,
            "project_layer_present": project_layer_present,
            "evidence_coverage": {
                "wallet": True,
                "project": project_layer_present,
                "volume": bool(result.get("vol_changes")),
                "market": bool(market),
                "safety": bool(_num((result.get("quote") or {}).get("market_cap")) or result.get("current_volume")),
            },
            "note": (
                "Every bucket cap is reachable, so PAPER_READY at "
                f"{PAPER_READY_MIN_CONFLUENCE:.0f}/{SCALE_MAX:.0f} is attainable. "
                "Wallet evidence keeps its 0-30 cap and technical indicators "
                "remain context-only, never a rejection."
            ),
        },
        "fil_confluence_wallet_intel": {
            "conviction_score": round(wallet, 1),
            "conviction_pre_calibration": _num(result.get("wallet_conviction_score_pre_calibration")),
            "long_term_profile_score": _num(result.get("wallet_profile_score")),
            "long_term_proven_wallets": int(result.get("wallet_longterm_proven_count") or 0),
            "profile_wallets": int(result.get("wallet_profile_wallet_count") or 0),
            "calibration_bonus": _num(result.get("wallet_calibration_bonus")),
            "calibration_status": str(result.get("wallet_calibration_status") or "UNAVAILABLE"),
            "note": "Profile and calibration evidence is already included in conviction_score.",
        },
        "fil_confluence_reasons": [f"Wallet Conviction {wallet:.1f}/30"] + project_reasons[:3] + volume_reasons[:3] + market_reasons[:1] + safety_reasons[:2],
    })
    return result
