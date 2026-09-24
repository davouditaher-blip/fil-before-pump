"""Wallet Quality Intelligence engine for Fil Before Pump.

Read-only intelligence layer. It profiles provider-labelled GMGN observations
and never places trades. "Forward hit rate" is explicitly an observational
metric: it measures whether a qualified buy was followed by a >=10% observed
price expansion within 14 days on the same asset, not a realized trading PnL claim.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HISTORY = Path("gmgn_wallet_history.json")
OUTPUT = Path("wallet_quality.json")
THRESHOLD_USD = 5000.0
LOOKAHEAD_SECONDS = 14 * 24 * 60 * 60
HIT_RETURN = 0.10


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _load() -> dict[str, list[dict[str, Any]]]:
    if not HISTORY.exists():
        return {}
    try:
        raw = json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(wallet): rows
        for wallet, rows in raw.items()
        if isinstance(rows, list)
    }


def _side(row: dict[str, Any]) -> str:
    return str(row.get("side") or row.get("type") or "").strip().lower()


def _ts(row: dict[str, Any]) -> int:
    value = _int(row.get("timestamp") or row.get("time") or row.get("ts"))
    return value // 1000 if value > 10**12 else value


def _price(row: dict[str, Any]) -> float:
    return _num(row.get("price_usd") or row.get("price") or row.get("priceUsd"))


def _usd(row: dict[str, Any]) -> float:
    return _num(
        row.get("amount_usd")
        or row.get("usd")
        or row.get("value_usd")
        or row.get("valueUsd")
        or row.get("quote_usd")
        or row.get("quoteUsd")
    )


def _pnl(row: dict[str, Any]) -> float | None:
    for key in ("pnl_percent", "pnl_pct", "profit_percent", "return_pct"):
        if row.get(key) is not None:
            return _num(row.get(key))
    for key in ("pnl_usd", "realized_pnl_usd", "profit_usd"):
        if row.get(key) is not None:
            usd = _num(row.get(key))
            invested = _usd(row)
            if invested > 0:
                return usd / invested * 100.0
    return None


def _forward_hit_rate(buys: list[dict[str, Any]], rows: list[dict[str, Any]]) -> tuple[int, int]:
    attempts = hits = 0
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            by_symbol.setdefault(symbol, []).append(row)

    for buy in buys:
        symbol = str(buy.get("symbol") or "").upper()
        entry = _price(buy)
        t0 = _ts(buy)
        if not symbol or entry <= 0 or t0 <= 0:
            continue
        future = [
            _price(r)
            for r in by_symbol.get(symbol, [])
            if _ts(r) > t0 and _ts(r) - t0 <= LOOKAHEAD_SECONDS and _price(r) > 0
        ]
        if not future:
            continue
        attempts += 1
        if max(future) >= entry * (1.0 + HIT_RETURN):
            hits += 1
    return attempts, hits


def _tier(score: float, proven: bool) -> str:
    if proven and score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def build_profiles(data: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, dict[str, Any]]:
    data = data if data is not None else _load()
    profiles: dict[str, dict[str, Any]] = {}

    for wallet, raw_rows in data.items():
        rows = [r for r in raw_rows if isinstance(r, dict)]
        qualified_buys = [r for r in rows if _side(r) == "buy" and _usd(r) >= THRESHOLD_USD]
        qualified_sells = [r for r in rows if _side(r) == "sell" and _usd(r) >= THRESHOLD_USD]
        if not qualified_buys:
            continue

        symbols = sorted({str(r.get("symbol") or "").upper() for r in qualified_buys if r.get("symbol")})
        sell_symbols = sorted({str(r.get("symbol") or "").upper() for r in qualified_sells if r.get("symbol")})

        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                by_symbol.setdefault(symbol, []).append(row)

        active_assets = []
        for symbol, symbol_rows in by_symbol.items():
            buys = sum(1 for r in symbol_rows if _side(r) == "buy" and _usd(r) >= THRESHOLD_USD)
            sells = sum(1 for r in symbol_rows if _side(r) == "sell" and _usd(r) >= THRESHOLD_USD)
            if buys > sells:
                active_assets.append(symbol)

        attempts, hits = _forward_hit_rate(qualified_buys, rows)
        forward_hit_rate = round(hits / attempts * 100.0, 1) if attempts else None

        pnl_rows = [p for p in (_pnl(r) for r in qualified_sells) if p is not None]
        realized_win_rate = (
            round(sum(1 for p in pnl_rows if p > 0) / len(pnl_rows) * 100.0, 1)
            if pnl_rows else None
        )

        buy_usd = sum(_usd(r) for r in qualified_buys)
        sell_usd = sum(_usd(r) for r in qualified_sells)
        repeat_bonus = min(20.0, max(0.0, (len(symbols) - 1) * 5.0))
        capital_score = min(20.0, (buy_usd / 25000.0) * 20.0)
        activity_score = min(20.0, len(qualified_buys) * 3.0)
        hit_score = (forward_hit_rate / 100.0 * 30.0) if forward_hit_rate is not None else 0.0
        realized_score = (realized_win_rate / 100.0 * 10.0) if realized_win_rate is not None else 0.0

        score = round(min(100.0, activity_score + capital_score + repeat_bonus + hit_score + realized_score), 1)
        proven = bool(
            len(qualified_buys) >= 3
            and attempts >= 3
            and forward_hit_rate is not None
            and forward_hit_rate >= 60.0
        )

        profiles[wallet] = {
            "wallet": wallet,
            "qualified_buys": len(qualified_buys),
            "qualified_buy_usd": round(buy_usd, 2),
            "qualified_sells": len(qualified_sells),
            "qualified_sell_usd": round(sell_usd, 2),
            "assets": symbols,
            "sell_assets": sell_symbols,
            "active_assets": sorted(active_assets),
            "repeat_asset_count": len(symbols),
            "forward_14d_attempts": attempts,
            "forward_14d_hits": hits,
            "forward_14d_hit_rate": forward_hit_rate,
            "realized_sell_observations": len(pnl_rows),
            "realized_win_rate": realized_win_rate,
            "quality_score": score,
            "quality_tier": _tier(score, proven),
            "proven_pre_pump_wallet": proven,
        }

    return profiles


def save_profiles(profiles: dict[str, dict[str, Any]]) -> None:
    OUTPUT.write_text(
        json.dumps(profiles, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_profiles() -> dict[str, dict[str, Any]]:
    if not OUTPUT.exists():
        return {}
    try:
        raw = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def build_profiles_from_disk() -> dict[str, dict[str, Any]]:
    profiles = build_profiles()
    save_profiles(profiles)
    return profiles


def run_quality_scan() -> None:
    profiles = build_profiles_from_disk()
    proven = sum(1 for p in profiles.values() if p.get("proven_pre_pump_wallet"))
    tier_a = sum(1 for p in profiles.values() if p.get("quality_tier") == "A")
    print(
        f"wallet quality profiles: {len(profiles)} | "
        f"proven pre-pump: {proven} | tier A: {tier_a}"
    )


def run_integrated_scanner() -> None:
    """Run the existing wallet-first scanner with quality intelligence attached."""
    import wallet_enhancement

    profiles = load_profiles()
    original_apply = wallet_enhancement.scanner.apply_wallet_signals

    def quality_apply(result: dict[str, Any], layer: dict[str, Any] | None):
        if layer:
            accumulating = layer.get("accumulating_wallets") or []
            quality_wallets = []
            proven_wallets = []
            for item in accumulating:
                wallet = str(item.get("wallet") or "")
                profile = profiles.get(wallet)
                if not profile:
                    continue
                row = {
                    "wallet": wallet,
                    "quality_score": profile.get("quality_score"),
                    "quality_tier": profile.get("quality_tier"),
                    "proven_pre_pump_wallet": bool(profile.get("proven_pre_pump_wallet")),
                    "forward_14d_hit_rate": profile.get("forward_14d_hit_rate"),
                    "qualified_buy_usd": profile.get("qualified_buy_usd"),
                    "active_assets": profile.get("active_assets", []),
                }
                quality_wallets.append(row)
                if row["proven_pre_pump_wallet"]:
                    proven_wallets.append(row)

            layer["quality_wallets"] = quality_wallets
            layer["proven_quality_wallets"] = proven_wallets
            layer["quality_wallet_count"] = len(quality_wallets)
            layer["proven_quality_wallet_count"] = len(proven_wallets)
            layer["quality_wallet_score_avg"] = (
                round(
                    sum(float(x.get("quality_score") or 0) for x in quality_wallets)
                    / len(quality_wallets),
                    1,
                )
                if quality_wallets else None
            )

        result = original_apply(result, layer)
        if layer:
            q = int(layer.get("quality_wallet_count", 0) or 0)
            p = int(layer.get("proven_quality_wallet_count", 0) or 0)
            if q:
                result["score"] = round(result["score"] + min(10, 3 * q), 1)
                result["reasons"].append(f"quality wallets accumulating: {q}")
            if p:
                result["score"] = round(result["score"] + min(12, 4 * p), 1)
                result["reasons"].append(f"proven pre-pump wallets: {p}")
            result["quality_wallet_count"] = q
            result["proven_quality_wallet_count"] = p
            result["quality_wallet_score_avg"] = layer.get("quality_wallet_score_avg")
            result["quality_wallets"] = layer.get("quality_wallets", [])
        return result

    wallet_enhancement.scanner.apply_wallet_signals = quality_apply
    print(
        "🐋 Wallet Quality Intelligence attached: "
        ">= $5K buys + forward 14d hit-rate + active assets + "
        "proven-wallet evidence. Read-only; no trade execution."
    )
    wallet_enhancement.scanner.main()

    refreshed = build_profiles_from_disk()
    proven = sum(1 for p in refreshed.values() if p.get("proven_pre_pump_wallet"))
    print(
        f"wallet quality refreshed: {len(refreshed)} profiles | "
        f"proven pre-pump: {proven}"
    )


if __name__ == "__main__":
    run_integrated_scanner()
