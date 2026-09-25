"""End-to-end artifact validation for Fil Before Pump.

This guard checks that the wallet-first -> readiness -> paper loop produced
structurally valid artifacts before GitHub Actions persists scanner history.
It never places trades.
"""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED = {
    "wallet_quality.json": dict,
    "wallet_clusters.json": dict,
    "wallet_radar.json": dict,
    "trade_readiness.json": dict,
    "paper_trades.json": dict,
}

OPTIONAL = {
    "paper_performance.json": (dict, list),
    "gmgn_wallet_history.json": dict,
    "wallet_paper_feedback.json": dict,
}


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AssertionError(f"missing required artifact: {path}")
    except json.JSONDecodeError as exc:
        raise AssertionError(f"invalid JSON in {path}: {exc}")


def main() -> None:
    errors = []
    data_by_name = {}

    for name, expected in REQUIRED.items():
        path = Path(name)
        try:
            data = load_json(path)
            data_by_name[name] = data
            if not isinstance(data, expected):
                errors.append(f"{name}: unexpected root type {type(data).__name__}")
        except AssertionError as exc:
            errors.append(str(exc))

    for name, expected in OPTIONAL.items():
        path = Path(name)
        if not path.exists():
            continue
        try:
            data = load_json(path)
            data_by_name[name] = data
            if not isinstance(data, expected):
                errors.append(f"{name}: unexpected root type {type(data).__name__}")
        except AssertionError as exc:
            errors.append(str(exc))

    quality = data_by_name.get("wallet_quality.json")
    if isinstance(quality, dict):
        for wallet, profile in quality.items():
            if not isinstance(wallet, str) or not isinstance(profile, dict):
                errors.append("wallet_quality.json: invalid wallet/profile entry")
                break
            for key in ("quality_score", "quality_tier", "active_assets"):
                if key not in profile:
                    errors.append(f"wallet_quality.json: profile missing {key}")
                    break

    clusters = data_by_name.get("wallet_clusters.json")
    if isinstance(clusters, dict):
        assets = clusters.get("assets")
        pairs = clusters.get("pairs")
        if not isinstance(assets, dict):
            errors.append("wallet_clusters.json: missing assets map")
        else:
            for asset, row in assets.items():
                if not isinstance(asset, str) or not isinstance(row, dict):
                    errors.append("wallet_clusters.json: invalid asset row")
                    break
                for key in ("holding_wallet_count", "wallets"):
                    if key not in row:
                        errors.append(f"wallet_clusters.json: {asset} missing {key}")
                        break
        if pairs is not None and not isinstance(pairs, list):
            errors.append("wallet_clusters.json: pairs must be a list")

    radar = data_by_name.get("wallet_radar.json")
    if isinstance(radar, dict):
        for key, row in radar.items():
            if not isinstance(row, dict):
                errors.append(f"wallet_radar.json: invalid row {key}")
                break

    readiness = data_by_name.get("trade_readiness.json")
    if isinstance(readiness, dict):
        if readiness.get("mode") != "READ_ONLY_PAPER":
            errors.append("trade_readiness.json: mode must be READ_ONLY_PAPER")
        if readiness.get("orders_enabled") is not False:
            errors.append("trade_readiness.json: orders_enabled must be false")
        plans = readiness.get("plans")
        if not isinstance(plans, list):
            errors.append("trade_readiness.json: plans must be a list")
        else:
            for plan in plans:
                if not isinstance(plan, dict):
                    errors.append("trade_readiness.json: invalid plan")
                    break
                if str(plan.get("state")) != "PAPER_READY":
                    errors.append("trade_readiness.json: risk-approved plan is not PAPER_READY")
                    break
                if float(plan.get("price_usd") or 0) <= 0:
                    errors.append("trade_readiness.json: PAPER_READY plan has invalid price")
                    break

    feedback = data_by_name.get("wallet_paper_feedback.json")
    if isinstance(feedback, dict):
        if feedback.get("mode") != "PAPER_ONLY":
            errors.append("wallet_paper_feedback.json: mode must be PAPER_ONLY")
        if feedback.get("orders_enabled") is not False:
            errors.append("wallet_paper_feedback.json: orders_enabled must be false")
        if not isinstance(feedback.get("groups"), list):
            errors.append("wallet_paper_feedback.json: groups must be a list")

    paper = data_by_name.get("paper_trades.json")
    if isinstance(paper, dict):
        if paper.get("mode") != "PAPER_ONLY":
            errors.append("paper_trades.json: mode must be PAPER_ONLY")
        if paper.get("summary", {}).get("orders_enabled") is not False:
            errors.append("paper_trades.json: orders_enabled must be false")
        if not isinstance(paper.get("open"), list) or not isinstance(paper.get("closed"), list):
            errors.append("paper_trades.json: open/closed must be lists")

    if errors:
        print("E2E validation: FAIL")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    print("E2E validation: PASS")
    print(f"wallet_quality profiles: {len(quality or {})}")
    print(f"wallet_clusters assets: {len((clusters or {}).get('assets', {}))}")
    print(f"trade_readiness plans: {len((readiness or {}).get('plans', []))}")
    print(f"paper open: {len((paper or {}).get('open', []))} | closed: {len((paper or {}).get('closed', []))}")
    print(f"paper feedback groups: {len((feedback or {}).get('groups', []))}")


if __name__ == "__main__":
    main()
