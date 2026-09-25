"""Deterministic risk gate for paper-trading plans.

No exchange execution. This layer rejects malformed or oversized paper plans
before they can enter the simulator and provides an audit-friendly decision.
"""
from __future__ import annotations

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
    return {
        "approved": not blockers,
        "blockers": blockers,
        "risk_pct": account_risk,
        "leverage_cap": leverage,
        "mode": "PAPER_ONLY",
    }

def filter_plans(plans: list[dict]) -> list[dict]:
    return [p for p in plans if evaluate(p)["approved"]]
