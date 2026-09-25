"""Execution-readiness layer for Fil Before Pump.

Creates a read-only, paper-trading-ready candidate plan from the existing
wallet-first scanner output. It never sends orders and never requires exchange
credentials. The purpose is to make the next auto-trading phase deterministic:
wallet evidence -> freshness -> exit pressure -> liquidity -> risk plan.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUT = Path("trade_readiness.json")


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _fresh_volume(x: dict[str, Any]) -> bool:
    v = x.get("vol_changes") or {}
    return any(_num(v.get(k), -999999) > 0 for k in ("1d", "2d"))


def build_trade_plan(x: dict[str, Any]) -> dict[str, Any]:
    price = _num(x.get("price_usd"))
    q = x.get("quote") or {}
    market_cap = _num(q.get("market_cap"))
    volume = _num(x.get("current_volume") or q.get("volume_24h"))
    wallet_score = _num(x.get("wallet_conviction_score"))
    active = _int(x.get("wallet_unique_active_count"))
    proven = _int(x.get("wallet_unique_proven_count"))
    shared = _int(x.get("wallet_unique_shared_count"))
    exit_pressure = _num(x.get("wallet_exit_pressure"))
    confluence = _num(x.get("fil_confluence_score"))
    ch24 = _num(x.get("ch24"))

    reasons = []
    blockers = []

    if wallet_score >= 12:
        reasons.append("wallet conviction >= 12/30")
    else:
        blockers.append("wallet conviction below paper-entry threshold")

    if active >= 1:
        reasons.append(f"{active} unique active wallet(s)")
    else:
        blockers.append("no active wallet evidence")

    if proven >= 1:
        reasons.append(f"{proven} proven pre-pump wallet(s)")
    if shared >= 1:
        reasons.append(f"{shared} shared wallet(s)")

    if _fresh_volume(x):
        reasons.append("fresh 1d/2d futures volume")
    else:
        blockers.append("no fresh 1d/2d volume confirmation")

    if exit_pressure < 35:
        reasons.append("exit pressure < 35%")
    elif exit_pressure >= 60:
        blockers.append("high wallet exit pressure")

    if price <= 0:
        blockers.append("missing price")

    if volume <= 0:
        blockers.append("missing futures volume")

    # Operational liquidity guard only; it is not a prediction of price.
    vol_mcap = volume / market_cap if market_cap > 0 else 0
    if vol_mcap > 0:
        if vol_mcap >= 0.01:
            reasons.append(f"volume/market-cap {vol_mcap:.2%}")
        else:
            blockers.append("low volume/market-cap for execution")

    # Late price extension is a caution, not a rejection of wallet intelligence.
    late_move = ch24 > 15
    if late_move:
        reasons.append("24h move > 15%: late-entry caution")

    # Deterministic paper-trading state. No order is placed.
    if not blockers and confluence >= 70:
        state = "PAPER_READY"
    elif wallet_score >= 12 and active >= 1 and not any(
        b in blockers for b in ("no fresh 1d/2d volume confirmation", "high wallet exit pressure")
    ):
        state = "WATCH_HIGH_CONVICTION"
    else:
        state = "WATCH"

    # Generic risk envelope for later execution integration.
    # It is intentionally expressed as percentages, not a live order.
    risk = {
        "max_account_risk_pct": 1.0,
        "stop_loss_pct": 3.0,
        "take_profit_1_pct": 6.0,
        "take_profit_2_pct": 10.0,
        "leverage_cap": 3,
        "position_size_mode": "risk_based",
    }

    return {
        "symbol": str(x.get("symbol") or "").upper(),
        "rank": x.get("rank"),
        "state": state,
        "price_usd": price,
        "fil_confluence_score": round(confluence, 1),
        "wallet_conviction_score": round(wallet_score, 1),
        "wallet_active": active,
        "wallet_proven": proven,
        "wallet_shared": shared,
        "wallet_exit_pressure": round(exit_pressure, 1),
        "fresh_volume": _fresh_volume(x),
        "volume_market_cap": round(vol_mcap, 6) if vol_mcap else None,
        "reasons": reasons[:12],
        "blockers": blockers[:12],
        "risk": risk,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def build(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    plans = [build_trade_plan(x) for x in candidates]
    plans.sort(
        key=lambda p: (
            p["state"] == "PAPER_READY",
            p["state"] == "WATCH_HIGH_CONVICTION",
            p["fil_confluence_score"],
            p["wallet_conviction_score"],
            p["wallet_proven"],
            p["wallet_shared"],
        ),
        reverse=True,
    )
    output = {
        "mode": "READ_ONLY_PAPER",
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(plans),
        "paper_ready_count": sum(p["state"] == "PAPER_READY" for p in plans),
        "high_conviction_count": sum(p["state"] == "WATCH_HIGH_CONVICTION" for p in plans),
        "plans": plans[:50],
    }
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print("Trade readiness is a library layer; scanner.main() supplies candidates.")
