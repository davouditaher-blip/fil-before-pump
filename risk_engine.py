"""Deterministic risk gate for paper-trading plans.

No exchange execution. This layer rejects malformed or oversized paper plans
before they can enter the simulator and provides an audit-friendly decision.
"""
from __future__ import annotations

# Most adverse calibration the bounded paper memory is allowed to publish.
MAX_ADVERSE_CALIBRATION = -5.0

def evaluate(plan: dict) -> dict:
    risk = plan.get("risk") or {}
    blockers = []
    account_risk = float(risk.get("max_account_risk_pct") or 0)
    leverage = float(risk.get("leverage_cap") or 0)
    price = float(plan.get("price_usd") or 0)
    if plan.get("state") != "PAPER_READY":
        blockers.append("not_paper_ready")
    if price <= 0:
        blockers.append("invalid_price")
    if account_risk <= 0 or account_risk > 1.0:
        blockers.append("account_risk_out_of_bounds")
    if leverage <= 0 or leverage > 3:
        blockers.append("leverage_cap_out_of_bounds")
    if float(plan.get("wallet_exit_pressure") or 0) >= 60:
        blockers.append("high_exit_pressure")

    # Long-term wallet history stays advisory in the risk layer, but a fully
    # measurable and maximally adverse paper calibration is a real rejection
    # reason rather than a note.
    calibration_status = str(plan.get("wallet_calibration_status") or "UNAVAILABLE")
    try:
        calibration_bonus = float(plan.get("wallet_calibration_bonus") or 0)
    except (TypeError, ValueError):
        calibration_bonus = 0.0
    if calibration_status == "MEASURABLE" and calibration_bonus <= MAX_ADVERSE_CALIBRATION:
        blockers.append("adverse_paper_calibration")

    return {
        "approved": not blockers,
        "blockers": blockers,
        "risk_pct": account_risk,
        "leverage_cap": leverage,
        "mode": "PAPER_ONLY",
        "wallet_intel": {
            "long_term_proven": int(plan.get("wallet_longterm_proven") or 0),
            "profile_score": float(plan.get("wallet_profile_score") or 0),
            "calibration_bonus": calibration_bonus,
            "calibration_status": calibration_status,
        },
    }

def filter_plans(plans: list[dict]) -> list[dict]:
    return [p for p in plans if evaluate(p)["approved"]]
