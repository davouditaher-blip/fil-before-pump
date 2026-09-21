"""Wallet-first enhancement layer for Fil Before Pump.

Fixes:
- GoldRush token-holder requests use the currently supported page-size.
- GoldRush holder balances are normalized into percentage/value fields.
- Gate Futures candles are used as the technical fallback when Binance/Bybit
  are blocked on GitHub-hosted runners.
- Raw transfers are never treated as buys/sells.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
import scanner

HISTORY = Path("wallet_history.json")

_original_goldrush = scanner.goldrush_wallet_layer
_original_apply = scanner.apply_wallet_signals
_original_format = scanner.format_coin
_original_candles = scanner.candles


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


def wallet_win_stats(history, wallet, lookahead=336):
    """Estimate success from holder-accumulation events, not provider trade labels."""
    by_symbol = {}
    for row in history.get(wallet, []):
        symbol = row.get("symbol")
        if symbol:
            by_symbol.setdefault(symbol, []).append(row)

    attempts = wins = 0
    successful_symbols = set()
    for symbol, rows in by_symbol.items():
        rows = sorted(rows, key=lambda r: int(r.get("timestamp", 0)))
        for i in range(1, len(rows)):
            prev, row = rows[i - 1], rows[i]
            try:
                old_pct = float(prev.get("percentage"))
                new_pct = float(row.get("percentage"))
                entry = float(row.get("price_usd") or 0)
            except (TypeError, ValueError):
                continue
            if entry <= 0 or new_pct - old_pct < 0.01:
                continue
            attempts += 1
            t0 = int(row.get("timestamp", 0))
            for nxt in rows[i + 1:]:
                if int(nxt.get("timestamp", 0)) - t0 > lookahead * 60:
                    break
                try:
                    next_price = float(nxt.get("price_usd") or 0)
                except (TypeError, ValueError):
                    continue
                if next_price >= entry * 1.10:
                    wins += 1
                    successful_symbols.add(symbol)
                    break
    return attempts, wins, successful_symbols


def enhanced_goldrush_wallet_layer(coin):
    """Fetch current top holders from GoldRush with the supported 100-item page size."""
    if not scanner.GOLDRUSH_API_KEY:
        return {}

    contracts = scanner.coin_contracts(coin)
    if not contracts:
        return {}

    best = {}
    for chain, address in contracts:
        items = scanner.goldrush_get(
            f"/{chain}/tokens/{address}/token_holders_v2/",
            {"page-size": 100, "page-number": 0},
        )
        if not items:
            continue

        holders = []
        total_supply = None
        for h in items[:100]:
            wallet = h.get("address") or h.get("wallet_address") or h.get("walletAddress")
            if not wallet:
                continue

            # GoldRush V2 holder responses expose raw balance + total_supply.
            label = str(h.get("label") or h.get("name") or h.get("contract_name") or h.get("contractName") or "").lower()
            holder_type = str(h.get("type") or h.get("entity_type") or "").lower()
            infra_words = ("exchange", "binance", "coinbase", "kraken", "okx", "bybit", "gate.io", "gateio", "bitget", "mexc", "kucoin", "bitfinex", "uniswap", "pancake", "router", "liquidity", "lp", "pool", "bridge", "burn", "dead", "null", "staking", "treasury", "contract")
            if any(w in label for w in infra_words) or holder_type in {"contract", "exchange", "lp", "liquidity_pool", "burn"}:
                continue

            try:
                balance = float(h.get("balance") or h.get("balance_raw") or 0)
                supply = float(h.get("total_supply") or h.get("totalSupply") or 0)
            except (TypeError, ValueError):
                balance = 0.0
                supply = 0.0

            raw_pct = h.get("percentage_relative_to_total_supply") or h.get("percentage") or h.get("percentage_of_total_supply")
            try:
                percentage = float(raw_pct) if raw_pct is not None else None
            except (TypeError, ValueError):
                percentage = None

            if supply > 0 and balance >= 0:
                percentage = balance / supply * 100.0
            elif percentage is not None and 0 < percentage <= 1:
                percentage *= 100.0

            if percentage is None:
                continue

            holders.append({
                "wallet": wallet,
                "symbol": coin.get("symbol"),
                "mint": address,
                "chain": chain,
                "rank": h.get("rank"),
                "percentage": percentage,
                "value": h.get("balance_quote") or h.get("value_quote") or h.get("quote"),
                "timestamp": int(datetime.now(timezone.utc).timestamp()),
                "label": label,
                "holder_type": holder_type,
            })

        if holders:
            best = {
                "chain": chain,
                "mint": address,
                "symbol": coin.get("symbol"),
                "holders": holders,
                "top5_holder_pct": sum(float(x.get("percentage") or 0) for x in holders[:5]),
                "top20_holder_pct": sum(float(x.get("percentage") or 0) for x in holders[:20]),
                "buy_sell_ratio_7d": None,
                "buyers_7d": 0,
                "sellers_7d": 0,
                "provider": "GoldRush",
                "price_usd": float(((coin.get("quote") or {}).get("USD") or {}).get("price") or 0),
            }
            break

    return best


def enhanced_candles(symbol, interval, limit=220):
    """Keep Binance/Bybit first, then correctly fall back to Gate Futures."""
    data = _original_candles(symbol, interval, limit)
    if data:
        return data

    gate_pair = scanner.gate_contract(symbol)
    gate_interval = {
        "5m": "5m",
        "15m": "15m",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
    }.get(interval)
    if not gate_interval:
        return []

    try:
        data = scanner.get_json(
            scanner.BASE_GATE + "/api/v4/futures/usdt/candlesticks",
            {
                "contract": gate_pair,
                "interval": gate_interval,
                "limit": min(limit, 2000),
            },
            timeout=20,
        )
        rows = data if isinstance(data, list) else []
        if len(rows) < 60:
            return []
        rows = sorted(rows, key=lambda r: int(r.get("t", 0)))
        return [
            [
                int(r.get("t", 0)) * 1000,
                r.get("o"),
                r.get("h"),
                r.get("l"),
                r.get("c"),
                r.get("sum", 0),
            ]
            for r in rows
        ]
    except requests.RequestException as e:
        print(f"Gate technical candles warning ({symbol} {interval}): {e}")
        return []


def enhanced_apply(result, layer):
    result = _original_apply(result, layer)
    if not layer:
        return result

    score = result["score"]
    reasons = result["reasons"]

    acc = int(layer.get("accumulation_count", 0))
    overlap = int(layer.get("smart_wallet_overlap", 0))
    win_rate = layer.get("smart_wallet_win_rate")

    if acc >= 1:
        score += min(12, 4 + 2 * acc)
        reasons.append(f"{acc} wallet accumulation")
    if overlap >= 1:
        score += min(12, 4 * overlap)
        reasons.append(f"{overlap} recurring wallet(s) accumulating")

    result["score"] = round(score, 1)
    result["wallet_accumulation"] = acc
    result["smart_wallet_overlap"] = overlap
    result["wallet_reductions"] = int(layer.get("reduction_count", 0))
    result["wallet_win_rate"] = win_rate
    return result


def enhanced_format(result):
    base = _original_format(result)
    if not result.get("wallet"):
        return base

    extra = (
        f"Smart-wallet accumulation: {result.get('wallet_accumulation', 0)}"
        f" | recurring accumulating wallets: {result.get('smart_wallet_overlap', 0)}"
        f" | reducing: {result.get('wallet_reductions', 0)}" + (f" | est. wallet win-rate: {result.get('wallet_win_rate')}%" if result.get('wallet_win_rate') is not None else "") + "\n"
    )
    return base + extra


def enhanced_goldrush_with_history(coin):
    layer = enhanced_goldrush_wallet_layer(coin)
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

        try:
            delta = float(holder.get("percentage")) - float(previous.get("percentage"))
        except (TypeError, ValueError):
            continue

        if delta >= 0.01:
            accumulating.append({
                "wallet": wallet,
                "delta_pct": round(delta, 4),
                "new_pct": holder.get("percentage"),
            })
        elif delta <= -0.01:
            reducing.append({
                "wallet": wallet,
                "delta_pct": round(delta, 4),
                "new_pct": holder.get("percentage"),
            })

    layer["accumulating_wallets"] = accumulating
    layer["reducing_wallets"] = reducing
    layer["accumulation_count"] = len(accumulating)
    layer["reduction_count"] = len(reducing)

    smart_overlap = 0
    wallet_stats = []
    for item in accumulating:
        wallet = item["wallet"]
        wallet_rows = history.get(wallet, [])
        symbols = {r.get("symbol") for r in wallet_rows if r.get("symbol")}
        accumulation_symbols = set()
        for sym in symbols:
            rows = sorted(
                [r for r in wallet_rows if r.get("symbol") == sym],
                key=lambda r: int(r.get("timestamp", 0)),
            )
            for j in range(1, len(rows)):
                try:
                    d = float(rows[j].get("percentage")) - float(rows[j-1].get("percentage"))
                except (TypeError, ValueError):
                    continue
                if d >= 0.01:
                    accumulation_symbols.add(sym)
                    break

        attempts, wins, successful_symbols = wallet_win_stats(history, wallet)
        if len(accumulation_symbols) >= 2:
            smart_overlap += 1
        if attempts:
            wallet_stats.append({
                "wallet": wallet,
                "attempts": attempts,
                "wins": wins,
                "win_rate": round(wins / attempts * 100, 1),
                "successful_symbols": sorted(successful_symbols),
                "accumulation_symbols": sorted(accumulation_symbols),
            })

    layer["wallet_win_stats"] = wallet_stats
    total_attempts = sum(x["attempts"] for x in wallet_stats)
    total_wins = sum(x["wins"] for x in wallet_stats)
    layer["smart_wallet_win_rate"] = (
        round(total_wins / total_attempts * 100, 1) if total_attempts else None
    )
    layer["smart_wallet_overlap"] = smart_overlap
    return layer


scanner.goldrush_wallet_layer = enhanced_goldrush_with_history
scanner.apply_wallet_signals = enhanced_apply
scanner.format_coin = enhanced_format
scanner.candles = enhanced_candles

if __name__ == "__main__":
    print(
        "🐋 Wallet enhancement active — GoldRush holder accumulation + "
        "cross-asset overlap + Gate technical fallback; "
        "no raw-transfer buy/sell inference."
    )
    scanner.main()
