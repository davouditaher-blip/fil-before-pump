"""Historical forward-only replay engine for Fil Before Pump.

Replays qualified wallet entries from gmgn_wallet_history.json without using
future information at decision time. It measures forward MFE/MAE and target
hits at 6/12/24h. This is an observational backtest layer, not exchange
execution and not a claim of realized trading PnL.
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HISTORY = Path("gmgn_wallet_history.json")
OUT = Path("historical_replay.json")
THRESHOLD_USD = 5000.0
WINDOWS = (6, 12, 24)
TARGETS = (5, 10, 20, 30)


def num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def ts(row: dict[str, Any]) -> int:
    try:
        value = int(float(row.get("timestamp") or row.get("time") or row.get("ts") or 0))
    except (TypeError, ValueError):
        return 0
    return value // 1000 if value > 10**12 else value


def price(row: dict[str, Any]) -> float:
    return num(row.get("price_usd") or row.get("price") or row.get("priceUsd"))


def usd(row: dict[str, Any]) -> float:
    return num(row.get("amount_usd") or row.get("usd") or row.get("value_usd") or row.get("valueUsd") or row.get("quote_usd"))


def side(row: dict[str, Any]) -> str:
    return str(row.get("side") or row.get("type") or "").strip().lower()


def load() -> dict[str, list[dict[str, Any]]]:
    if not HISTORY.exists():
        return {}
    try:
        raw = json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(w): v for w, v in raw.items() if isinstance(v, list)} if isinstance(raw, dict) else {}


def replay(data: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    data = data if data is not None else load()
    observations: list[dict[str, Any]] = []
    by_wallet = defaultdict(list)

    for wallet, rows in data.items():
        clean = [r for r in rows if isinstance(r, dict) and ts(r) > 0]
        by_symbol = defaultdict(list)
        for row in clean:
            symbol = str(row.get("symbol") or "").upper()
            if symbol and price(row) > 0:
                by_symbol[symbol].append(row)
        for symbol in by_symbol:
            by_symbol[symbol].sort(key=ts)

        buys = [r for r in clean if side(r) == "buy" and usd(r) >= THRESHOLD_USD and price(r) > 0]
        for buy in sorted(buys, key=ts):
            symbol = str(buy.get("symbol") or "").upper()
            t0, entry = ts(buy), price(buy)
            if not symbol or not t0 or entry <= 0:
                continue
            future = [r for r in by_symbol[symbol] if ts(r) > t0 and ts(r) - t0 <= 24 * 3600 and price(r) > 0]
            if not future:
                continue
            row = {
                "wallet": wallet,
                "symbol": symbol,
                "entry_timestamp": t0,
                "entry_price": entry,
                "entry_usd": usd(buy),
                "windows": {},
            }
            for hours in WINDOWS:
                prices = [price(r) for r in future if ts(r) - t0 <= hours * 3600]
                if not prices:
                    continue
                mfe = max(prices) / entry - 1
                mae = min(prices) / entry - 1
                row["windows"][str(hours)] = {
                    "mfe_pct": round(mfe * 100, 3),
                    "mae_pct": round(mae * 100, 3),
                    "targets_hit": {str(t): mfe * 100 >= t for t in TARGETS},
                }
            observations.append(row)
            by_wallet[wallet].append(row)

    stats = {}
    for hours in WINDOWS:
        rows = [r for r in observations if str(hours) in r["windows"]]
        stats[str(hours)] = {
            "observations": len(rows),
            "hit_rate_pct": {
                str(t): round(sum(r["windows"][str(hours)]["targets_hit"][str(t)] for r in rows) / len(rows) * 100, 2)
                if rows else None
                for t in TARGETS
            },
            "avg_mfe_pct": round(sum(r["windows"][str(hours)]["mfe_pct"] for r in rows) / len(rows), 3) if rows else None,
            "avg_mae_pct": round(sum(r["windows"][str(hours)]["mae_pct"] for r in rows) / len(rows), 3) if rows else None,
        }

    result = {
        "mode": "HISTORICAL_FORWARD_ONLY",
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "threshold_usd": THRESHOLD_USD,
        "observation_count": len(observations),
        "wallet_count": len(by_wallet),
        "statistics": stats,
        "observations": observations[-5000:],
        "methodology": {
            "entry": "provider-labelled GMGN buy >= $5K with valid entry price",
            "future_data": "only timestamps after entry, maximum 24h",
            "metrics": "forward MFE/MAE and target-hit rates",
            "interpretation": "observational evidence, not realized exchange PnL",
        },
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    result = replay()
    print(json.dumps({k: result[k] for k in ("mode", "observation_count", "wallet_count", "statistics")}, ensure_ascii=False, indent=2))
