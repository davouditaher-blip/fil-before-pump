"""Cross-wallet clustering for Fil Before Pump.

Builds an asset-centric map of wallets that have qualified buys (>= $5K),
then separates wallets that are still active on the asset from wallets whose
qualified sells indicate a likely exit. This is read-only intelligence.
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

QUALITY = Path("wallet_quality.json")
OUTPUT = Path("wallet_clusters.json")

def main():
    try:
        data = json.loads(QUALITY.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    assets = defaultdict(list)
    for wallet, p in data.items():
        if not isinstance(p, dict):
            continue
        active = {str(x).upper() for x in (p.get("active_assets") or [])}
        all_assets = {str(x).upper() for x in (p.get("assets") or [])}
        sells = {str(x).upper() for x in (p.get("sell_assets") or [])}
        for asset in sorted(all_assets):
            if not asset:
                continue
            assets[asset].append({
                "wallet": wallet,
                "status": "holding" if asset in active else ("exited" if asset in sells else "unknown"),
                "quality_score": p.get("quality_score"),
                "quality_tier": p.get("quality_tier"),
                "qualified_buy_usd": p.get("qualified_buy_usd", 0),
                "proven_pre_pump_wallet": bool(p.get("proven_pre_pump_wallet")),
            })

    result = {}
    for asset, wallets in assets.items():
        wallets.sort(key=lambda x: (
            x["status"] == "holding",
            x["proven_pre_pump_wallet"],
            float(x.get("quality_score") or 0),
            float(x.get("qualified_buy_usd") or 0),
        ), reverse=True)
        holding = [w for w in wallets if w["status"] == "holding"]
        proven = [w for w in wallets if w["proven_pre_pump_wallet"] and w["status"] == "holding"]
        result[asset] = {
            "asset": asset,
            "wallet_count": len(wallets),
            "holding_wallet_count": len(holding),
            "proven_holding_wallet_count": len(proven),
            "qualified_buy_usd": round(sum(float(w.get("qualified_buy_usd") or 0) for w in wallets), 2),
            "wallets": wallets,
        }

    # Pair clusters: common wallets across assets, prioritizing active holdings.
    by_wallet = defaultdict(list)
    for asset, row in result.items():
        for w in row["wallets"]:
            by_wallet[w["wallet"]].append(asset)
    pairs = defaultdict(lambda: {"wallet_count": 0, "wallets": []})
    for wallet, asset_list in by_wallet.items():
        asset_list = sorted(set(asset_list))
        for i, a in enumerate(asset_list):
            for b in asset_list[i + 1:]:
                key = f"{a}|{b}"
                pairs[key]["wallet_count"] += 1
                pairs[key]["wallets"].append(wallet)
    pair_rows = [
        {"assets": k.split("|"), **v}
        for k, v in pairs.items() if v["wallet_count"] >= 2
    ]
    pair_rows.sort(key=lambda x: x["wallet_count"], reverse=True)

    OUTPUT.write_text(json.dumps({
        "assets": result,
        "pairs": pair_rows[:500],
    }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wallet clusters: {len(result)} assets | {len(pair_rows)} pairs with >=2 common wallets")

if __name__ == "__main__":
    main()
