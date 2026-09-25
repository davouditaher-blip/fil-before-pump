"""Final integration gate for Fil Before Pump.

Deterministic pre-persistence validation for the wallet-first pipeline.
This gate is read-only and never enables exchange execution.
"""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED_CODE = [
    "scanner.py",
    "gmgn_layer.py",
    "wallet_quality_engine.py",
    "wallet_clustering.py",
    "wallet_radar.py",
    "wallet_performance_memory.py",
    "confluence_engine.py",
    "trade_readiness.py",
    "risk_engine.py",
    "paper_trading.py",
    "paper_performance.py",
    "wallet_paper_feedback.py",
    "e2e_validate.py",
]

REQUIRED_ARTIFACTS = [
    "wallet_quality.json",
    "wallet_clusters.json",
    "wallet_radar.json",
    "trade_readiness.json",
    "paper_trades.json",
    "paper_performance.json",
    "wallet_paper_feedback.json",
]

def load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def main() -> None:
    errors = []
    for name in REQUIRED_CODE:
        if not Path(name).is_file():
            errors.append(f"missing code module: {name}")

    data = {}
    for name in REQUIRED_ARTIFACTS:
        path = Path(name)
        if not path.is_file():
            errors.append(f"missing artifact: {name}")
            continue
        try:
            data[name] = load(name)
        except Exception as exc:
            errors.append(f"invalid artifact {name}: {exc}")

    readiness = data.get("trade_readiness.json", {})
    if readiness.get("mode") != "READ_ONLY_PAPER":
        errors.append("trade readiness mode is not READ_ONLY_PAPER")
    if readiness.get("orders_enabled") is not False:
        errors.append("trade readiness orders_enabled is not false")
    plans = readiness.get("plans", [])
    if not isinstance(plans, list):
        errors.append("trade readiness plans is not a list")
    for plan in plans:
        if plan.get("state") != "PAPER_READY":
            errors.append(f"non-ready plan leaked through risk gate: {plan.get('symbol')}")
        risk = plan.get("risk") or {}
        if float(risk.get("max_account_risk_pct") or 0) <= 0 or float(risk.get("max_account_risk_pct") or 0) > 1:
            errors.append(f"invalid account risk for {plan.get('symbol')}")
        if float(risk.get("leverage_cap") or 0) <= 0 or float(risk.get("leverage_cap") or 0) > 3:
            errors.append(f"invalid leverage cap for {plan.get('symbol')}")

    paper = data.get("paper_trades.json", {})
    if paper.get("mode") != "PAPER_ONLY":
        errors.append("paper trading mode is not PAPER_ONLY")
    if (paper.get("summary") or {}).get("orders_enabled") is not False:
        errors.append("paper trading orders_enabled is not false")

    feedback = data.get("wallet_paper_feedback.json", {})
    if feedback.get("mode") != "PAPER_ONLY":
        errors.append("wallet feedback mode is not PAPER_ONLY")
    if feedback.get("orders_enabled") is not False:
        errors.append("wallet feedback orders_enabled is not false")

    if errors:
        print("FINAL INTEGRATION GATE: FAIL")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    print("FINAL INTEGRATION GATE: PASS")
    print(f"modules: {len(REQUIRED_CODE)}")
    print(f"validated artifacts: {len(data)}")
    print(f"risk-approved paper plans: {len(plans)}")
    print("live exchange execution: DISABLED")

if __name__ == "__main__":
    main()
