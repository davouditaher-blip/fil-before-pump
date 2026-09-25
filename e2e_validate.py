"""End-to-end artifact validation for Fil Before Pump.

This guard checks that the wallet-first pipeline produced structurally valid
artifacts before GitHub Actions persists scanner history. It never places trades.
"""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED = {
    "wallet_quality.json": dict,
    "wallet_clusters.json": dict,
    "wallet_radar.json": dict,
    "trade_readiness.json": (dict, list),
    "paper_trades.json": (dict, list),
}

OPTIONAL = {
    "paper_performance.json": (dict, list),
    "gmgn_wallet_history.json": dict,
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
    for name, expected in REQUIRED.items():
        path = Path(name)
        try:
            data = load_json(path)
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
            if not isinstance(data, expected):
                errors.append(f"{name}: unexpected root type {type(data).__name__}")
        except AssertionError as exc:
            errors.append(str(exc))

    quality = None
    if Path("wallet_quality.json").exists():
        try:
            quality = load_json(Path("wallet_quality.json"))
        except AssertionError:
            pass
    if isinstance(quality, dict):
        for wallet, profile in quality.items():
            if not isinstance(wallet, str) or not isinstance(profile, dict):
                errors.append("wallet_quality.json: invalid wallet/profile entry")
                break
            for key in ("quality_score", "quality_tier", "active_assets"):
                if key not in profile:
                    errors.append(f"wallet_quality.json: profile missing {key}")
                    break

    clusters = None
    if Path("wallet_clusters.json").exists():
        try:
            clusters = load_json(Path("wallet_clusters.json"))
        except AssertionError:
            pass
    if isinstance(clusters, dict):
        for asset, row in clusters.items():
            if not isinstance(asset, str) or not isinstance(row, dict):
                errors.append("wallet_clusters.json: invalid asset row")
                break
            if "holding_wallet_count" not in row:
                errors.append(f"wallet_clusters.json: {asset} missing holding_wallet_count")
                break

    if errors:
        print("E2E validation: FAIL")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    print("E2E validation: PASS")
    print(f"wallet_quality profiles: {len(quality or {})}")
    print(f"wallet_clusters assets: {len(clusters or {})}")

if __name__ == "__main__":
    main()
