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

from confluence_engine import PAPER_READY_MIN_CONFLUENCE, SCALE_MAX

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
    longterm = _int(x.get("wallet_longterm_proven_count"))
    profile_score = _num(x.get("wallet_profile_score"))
    scale = x.get("fil_confluence_scale") if isinstance(x.get("fil_confluence_scale"), dict) else {}
    project_layer_present = scale.get("project_layer_present")
    evidence_coverage = scale.get("evidence_coverage") if isinstance(scale.get("evidence_coverage"), dict) else {}
    calibration_bonus = _num(x.get("wallet_calibration_bonus"))
    calibration_status = str(x.get("wallet_calibration_status") or "UNAVAILABLE")
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

    # Long-term wallet history is a first-class part of the wallet gate, so it
    # is reported on every plan. It reaches the state decision through
    # wallet_score, which already includes the bounded profile contribution;
    # it never relaxes a blocker on its own.
    if longterm >= 1:
        reasons.append(f"{longterm} long-term proven signal wallet(s)")
    if profile_score > 0:
        reasons.append(f"long-term profile score {profile_score:.1f}/12")
    if calibration_bonus:
        reasons.append(f"paper calibration {calibration_bonus:+.1f} ({calibration_status})")
    elif calibration_status not in ("UNAVAILABLE", "NO_HISTORY"):
        reasons.append(f"paper calibration pending ({calibration_status})")

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
    # PAPER_READY_MIN_CONFLUENCE and the 100-point scale are owned by
    # confluence_engine so the gate and the advertised scale cannot drift.
    if not blockers and confluence >= PAPER_READY_MIN_CONFLUENCE:
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
        "fil_confluence_max_score": SCALE_MAX,
        "fil_confluence_paper_ready_threshold": PAPER_READY_MIN_CONFLUENCE,
        "fil_confluence_fill_pct": _num(scale.get("fill_pct")) if scale else None,
        "fil_confluence_components": dict(x.get("fil_confluence_components") or {}),
        "project_layer_present": project_layer_present,
        "evidence_coverage": dict(evidence_coverage),
        # Why a candidate has no project evidence, so a coverage gap is never
        # mistaken for a genuinely weak score. None when a layer was built.
        "project_layer_missing_reason": x.get("project_layer_missing_reason"),
        "project_flow_provider": (x.get("project_intelligence") or {}).get("flow_provider")
        if isinstance(x.get("project_intelligence"), dict) else None,
        "wallet_conviction_score": round(wallet_score, 1),
        "wallet_conviction_pre_calibration": round(
            _num(x.get("wallet_conviction_score_pre_calibration"), wallet_score), 1
        ),
        "wallet_active": active,
        "wallet_proven": proven,
        "wallet_shared": shared,
        "wallet_longterm_proven": longterm,
        "wallet_profile_score": round(profile_score, 2),
        "wallet_calibration_bonus": round(calibration_bonus, 2),
        "wallet_calibration_status": calibration_status,
        "signal_wallets": [dict(w) for w in (x.get("wallet_conviction_wallets") or []) if isinstance(w, dict)][:30],
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
            p["wallet_longterm_proven"],
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
