"""Wallet-first enhancement layer for Fil Before Pump.

Uses GoldRush holder snapshots as an accumulation/overlap signal.
Important: holder-balance changes are NOT labeled as buys/sells.
No trade direction is inferred from raw transfers.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import scanner

HISTORY = Path("wallet_history.json")


def _history():
    try:
        return json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    except Exception:
        return {}


def _prior_wallet_row(history, wallet, symbol, mint):
    rows = history.get(wallet, [])
    matches = [
        r for r in rows
        if r.get("symbol") == symbol or (mint and r.get("mint") == mint)
    ]
    if not matches:
        return None
    return max(matches, key=lambda r: int(r.get("timestamp", 0)))


_original_goldrush = scanner.goldrush_wallet_layer
_original_apply = scanner.apply_wallet_signals
_original_format = scanner.format_coin


def wallet_win_stats(history, wallet, symbol, lookahead=336):
    rows = [r for r in history.get(wallet, []) if r.get("symbol") == symbol]
    rows = sorted(rows, key=lambda r: int(r.get("timestamp", 0)))
    attempts = wins = 0
    for i, row in enumerate(rows):
        entry = float(row.get("price_usd") or 0)
        if entry <= 0:
            continue
        t0 = int(row.get("timestamp", 0))
        attempts += 1
        for nxt in rows[i + 1:]:
            dt = int(nxt.get("timestamp", 0)) - t0
            if dt > lookahead * 60:
                break
            px = float(nxt.get("price_usd") or 0)
            if px >= entry * 1.10:
                wins += 1
                break
    return attempts, wins


def enhanced_goldrush_wallet_layer(coin):
    layer = _original_goldrush(coin)
    if not layer:
        return layer

    history = _history()
    accumulating = []
    reducing = []

    for holder in layer.get("holders", []):
        wallet = holder.get("wallet")
        if not wallet:
            continue
        previous = _prior_wallet_row(
            history, wallet, layer.get("symbol"), layer.get("mint")
        )
        if not previous:
            continue

        old_pct = previous.get("percentage")
        new_pct = holder.get("percentage")
        try:
            delta = float(new_pct) - float(old_pct)
        except (TypeError, ValueError):
            continue

        if delta >= 0.05:
            accumulating.append({
                "wallet": wallet,
                "delta_pct": round(delta, 4),
                "new_pct": new_pct,
            })
        elif delta <= -0.05:
            reducing.append({
                "wallet": wallet,
                "delta_pct": round(delta, 4),
                "new_pct": new_pct,
            })

    layer["accumulating_wallets"] = accumulating
    layer["reducing_wallets"] = reducing
    layer["accumulation_count"] = len(accumulating)
    layer["reduction_count"] = len(reducing)
    layer["wallet_win_stats"] = win_stats\n    layer["smart_wallet_win_rate"] = (sum(x["wins"] for x in win_stats) / sum(x["attempts"] for x in win_stats) * 100) if win_stats and sum(x["attempts"] for x in win_stats) else None\n    layer["smart_wallet_overlap"] = sum(
        1 for wallet in accumulating
        if len({
            r.get("symbol")
            for r in history.get(wallet["wallet"], [])
            if r.get("symbol")
        }) >= 2
    )
    return layer


def enhanced_apply(result, layer):
    result = _original_apply(result, layer)
    if not layer:
        return result

    score = result["score"]
    reasons = result["reasons"]

    acc = int(layer.get("accumulation_count", 0))
    overlap = int(layer.get("smart_wallet_overlap", 0))\n    win_rate = layer.get("smart_wallet_win_rate")

    # Wallet-first: reward early holder accumulation, but do not require it.
    if acc >= 1:
        score += min(12, 4 + 2 * acc)
        reasons.append(f"{acc} wallet accumulation")
    if overlap >= 1:
        score += min(12, 4 * overlap)
        reasons.append(f"{overlap} recurring wallet(s) accumulating")

    result["score"] = round(score, 1)
    result["wallet_accumulation"] = acc
    result["smart_wallet_overlap"] = overlap
    result["wallet_reductions"] = int(layer.get("reduction_count", 0))\n    result["wallet_win_rate"] = win_rate
    return result


def enhanced_format(result):
    base = _original_format(result)
    if not result.get("wallet"):
        return base

    extra = (
        f"Smart-wallet accumulation: {result.get('wallet_accumulation', 0)}"
        f" | recurring accumulating wallets: {result.get('smart_wallet_overlap', 0)}"
        f" | reducing: {result.get('wallet_reductions', 0)}\n"
    )
    return base + extra


scanner.goldrush_wallet_layer = enhanced_goldrush_wallet_layer
scanner.apply_wallet_signals = enhanced_apply
scanner.format_coin = enhanced_format

if __name__ == "__main__":
    print(
        "🐋 Wallet enhancement active — GoldRush holder accumulation + "
        "cross-asset overlap; no raw-transfer buy/sell inference."
    )
    scanner.main()
