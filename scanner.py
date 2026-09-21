import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CMC_API_KEY = os.environ["CMC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_CMC = "https://pro-api.coinmarketcap.com"
BASE_BINANCE = "https://api.binance.com"
HISTORY_FILE = Path("volume_history.json")

CMC_HEADERS = {
    "X-CMC_PRO_API_KEY": CMC_API_KEY,
    "Accepts": "application/json",
}

session = requests.Session()
session.headers.update({"User-Agent": "fil-before-pump/1.0"})


def get_json(url, params=None, headers=None, timeout=30):
    r = session.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def cmc(endpoint, params=None):
    return get_json(BASE_CMC + endpoint, params=params, headers=CMC_HEADERS, timeout=60)


def get_market():
    return cmc(
        "/v1/cryptocurrency/listings/latest",
        {"start": 1, "limit": 400, "convert": "USD"},
    )["data"]


def load_history():
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return {}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def pct(now, old):
    if old in (None, 0):
        return None
    return (now / old - 1.0) * 100.0


def nearest(history, target, tolerance):
    best = None
    best_diff = None
    for item in history:
        ts = item.get("timestamp", 0)
        diff = abs(ts - target)
        if diff <= tolerance and (best_diff is None or diff < best_diff):
            best, best_diff = item, diff
    return best


def volume_signals(symbol, volume, price, history, now_ts):
    rows = history.setdefault(symbol, [])
    rows.append({"timestamp": now_ts, "volume": volume, "price": price})
    cutoff = now_ts - 30 * 86400
    history[symbol] = [x for x in rows if x.get("timestamp", 0) >= cutoff]
    rows = history[symbol]

    targets = {
        "1d": (1 * 86400, 18 * 3600),
        "3d": (3 * 86400, 24 * 3600),
        "7d": (7 * 86400, 36 * 3600),
        "14d": (14 * 86400, 36 * 3600),
    }
    changes = {}
    for name, (age, tol) in targets.items():
        old = nearest(rows, now_ts - age, tol)
        changes[name] = pct(volume, old["volume"]) if old else None

    available = [v for v in changes.values() if v is not None]
    positive = [v for v in available if v > 0]
    max_positive = max(positive) if positive else 0.0

    # Early-volume principle: even a small first increase is a signal.
    early = any(v > 0 for v in available)
    strong = any(v >= 3 for v in available)
    acceleration = any(v >= 10 for v in available)

    return changes, early, strong, acceleration, max_positive


def binance_symbol(symbol):
    # Binance symbols are not a perfect universe match. Failed lookups are simply skipped.
    return f"{symbol}USDT"


def candles(symbol, interval, limit=220):
    try:
        data = get_json(
            BASE_BINANCE + "/api/v3/klines",
            {"symbol": binance_symbol(symbol), "interval": interval, "limit": limit},
            timeout=15,
        )
        if not isinstance(data, list) or len(data) < 60:
            return []
        return data
    except requests.RequestException:
        return []


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = x * k + value * (1 - k)
    return value


def rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def technical_for_interval(symbol, interval):
    data = candles(symbol, interval)
    if not data:
        return {}

    closes = [float(x[4]) for x in data]
    highs = [float(x[2]) for x in data]
    lows = [float(x[3]) for x in data]
    vols = [float(x[5]) for x in data]

    e5, e13, e20 = ema(closes, 5), ema(closes, 13), ema(closes, 20)
    e50, e200 = ema(closes, 50), ema(closes, 200)
    r = rsi(closes)

    macd_fast = ema(closes, 12)
    macd_slow = ema(closes, 26)
    macd = (macd_fast - macd_slow) if macd_fast is not None and macd_slow is not None else None

    typical = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    vwap_window = min(50, len(typical))
    vol_sum = sum(vols[-vwap_window:])
    vwap = (
        sum(typical[-vwap_window + i] * vols[-vwap_window + i] for i in range(vwap_window)) / vol_sum
        if vol_sum > 0 else None
    )

    volume_ratio = (
        vols[-1] / (sum(vols[-21:-1]) / min(20, len(vols) - 1))
        if len(vols) >= 22 and sum(vols[-21:-1]) > 0 else None
    )

    return {
        "rsi": r,
        "ema5": e5,
        "ema13": e13,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "macd": macd,
        "vwap": vwap,
        "volume_ratio": volume_ratio,
        "close": closes[-1],
    }


def technical_signals(symbol):
    # Extra attention to 5m and 1h, while retaining 15m/4h/1d context.
    result = {}
    for interval in ("5m", "15m", "1h", "4h", "1d"):
        result[interval] = technical_for_interval(symbol, interval)
        time.sleep(0.03)
    return result


def score_coin(coin, volume_info, tech):
    quote = coin["quote"]["USD"]
    ch1 = float(quote.get("percent_change_1h") or 0)
    ch24 = float(quote.get("percent_change_24h") or 0)
    ch7 = float(quote.get("percent_change_7d") or 0)
    ch30 = float(quote.get("percent_change_30d") or 0)
    market_cap = float(quote.get("market_cap") or 0)
    volume = float(quote.get("volume_24h") or 0)

    changes, early, strong, acceleration, max_vol = volume_info
    score = 0
    reasons = []

    if early:
        score += 20
        reasons.append("early volume")
    if strong:
        score += 10
        reasons.append("volume +3%")
    if acceleration:
        score += 10
        reasons.append("volume acceleration")
    if volume > 0 and market_cap > 0 and volume / market_cap >= 0.10:
        score += 8
        reasons.append("high vol/mcap")

    # Do not require a price move. Penalize only obvious late-stage moves.
    if 0 <= ch24 <= 6:
        score += 8
        reasons.append("price still early")
    elif ch24 > 15:
        score -= 12
        reasons.append("late 24h move")
    if ch7 <= 15:
        score += 4
    if ch30 <= 35:
        score += 3
    if ch1 > 0:
        score += 3

    for tf, weight in (("5m", 8), ("1h", 10), ("15m", 6), ("4h", 5), ("1d", 4)):
        t = tech.get(tf, {})
        if not t:
            continue
        r = t.get("rsi")
        if r is not None and 45 <= r <= 65:
            score += weight * 0.35
            reasons.append(f"{tf} RSI developing")
        if t.get("ema5") and t.get("ema13") and t["ema5"] > t["ema13"]:
            score += weight * 0.20
        if t.get("ema20") and t.get("ema50") and t["ema20"] > t["ema50"]:
            score += weight * 0.15
        if t.get("vwap") and t.get("close") and t["close"] >= t["vwap"]:
            score += weight * 0.20
        if t.get("volume_ratio") and t["volume_ratio"] >= 1.20:
            score += weight * 0.25

    return round(score, 1), reasons



SOLSCAN_API_KEY = os.environ.get("SOLSCAN_API_KEY", "")
SOLSCAN_BASE = "https://pro-api.solscan.io/v2.0"
WALLET_HISTORY_FILE = Path("wallet_history.json")


def solscan_get(endpoint, params=None):
    if not SOLSCAN_API_KEY:
        return None
    try:
        r = session.get(
            SOLSCAN_BASE + endpoint,
            params=params or {},
            headers={"token": SOLSCAN_API_KEY, "accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("data")
    except requests.RequestException as e:
        print(f"Solscan warning: {e}")
        return None


def solana_token_for_symbol(symbol):
    data = solscan_get(
        "/token/search",
        {
            "keyword": symbol,
            "search_mode": "exact",
            "search_by": "symbol",
            "exclude_unverified_token": "true",
            "sort_by": "market_cap",
            "sort_order": "desc",
            "page": 1,
            "page_size": 10,
        },
    )
    if isinstance(data, dict):
        items = data.get("items", [])
    else:
        items = data or []
    return items[0] if items else None


def solscan_wallet_layer(symbol):
    """
    Solana-only wallet layer.

    This intentionally distinguishes:
      - holder concentration,
      - common top-holder wallets,
      - incoming transfer activity,
      - token-level buy/sell statistics.

    It does NOT call a transfer a 'buy' unless the provider explicitly labels it.
    """
    token = solana_token_for_symbol(symbol)
    if not token:
        return {}

    mint = token.get("address")
    if not mint:
        return {}

    meta = solscan_get("/token/meta", {"address": mint}) or {}
    holders_data = solscan_get(
        "/token/holders",
        {"address": mint, "page": 1, "page_size": 20},
    ) or {}
    items = holders_data.get("items", []) if isinstance(holders_data, dict) else []

    hist = solscan_get("/token/historical-data", {"address": mint, "range": 30}) or []
    hist_rows = hist.get("data", []) if isinstance(hist, dict) else hist
    hist_rows = hist_rows or []
    recent = hist_rows[-7:] if len(hist_rows) >= 7 else hist_rows

    buy7 = sum(float(x.get("total_volume_buying") or 0) for x in recent)
    sell7 = sum(float(x.get("total_volume_selling") or 0) for x in recent)
    buyers7 = sum(int(x.get("num_buyers") or 0) for x in recent)
    sellers7 = sum(int(x.get("num_sellers") or 0) for x in recent)

    top20_pct = sum(float(x.get("percentage") or 0) for x in items[:20])
    top5_pct = sum(float(x.get("percentage") or 0) for x in items[:5])

    # Save only wallet identity + holder stats, not private credentials.
    wallet_rows = []
    for h in items:
        owner = h.get("owner")
        if owner:
            wallet_rows.append({
                "wallet": owner,
                "symbol": symbol,
                "mint": mint,
                "rank": h.get("rank"),
                "percentage": h.get("percentage"),
                "value": h.get("value"),
                "timestamp": int(datetime.now(timezone.utc).timestamp()),
            })

    return {
        "chain": "solana",
        "mint": mint,
        "symbol": symbol,
        "top5_holder_pct": top5_pct,
        "top20_holder_pct": top20_pct,
        "holders": wallet_rows,
        "buy_volume_7d": buy7,
        "sell_volume_7d": sell7,
        "buyers_7d": buyers7,
        "sellers_7d": sellers7,
        "buy_sell_ratio_7d": (buy7 / sell7) if sell7 > 0 else None,
    }


def update_wallet_history(layer_results):
    try:
        history = json.loads(WALLET_HISTORY_FILE.read_text()) if WALLET_HISTORY_FILE.exists() else {}
    except Exception:
        history = {}

    now = int(datetime.now(timezone.utc).timestamp())
    for result in layer_results:
        for holder in result.get("holders", []):
            wallet = holder["wallet"]
            history.setdefault(wallet, [])
            history[wallet].append({
                "timestamp": now,
                "symbol": result["symbol"],
                "mint": result["mint"],
                "rank": holder.get("rank"),
                "percentage": holder.get("percentage"),
                "value": holder.get("value"),
            })
            history[wallet] = history[wallet][-200:]

    WALLET_HISTORY_FILE.write_text(json.dumps(history, indent=2))
    return history


def wallet_overlap(history, symbol):
    matches = []
    for wallet, rows in history.items():
        symbols = {r.get("symbol") for r in rows if r.get("symbol")}
        if symbol in symbols and len(symbols) >= 2:
            matches.append(wallet)
    return len(matches)


def apply_wallet_signals(result, layer):
    if not layer:
        return result

    result["wallet"] = layer
    score = result["score"]
    reasons = result["reasons"]

    ratio = layer.get("buy_sell_ratio_7d")
    if ratio is not None and ratio > 1.10:
        score += 12
        reasons.append("Solana buy>sell 7d")
    if layer.get("buyers_7d", 0) > layer.get("sellers_7d", 0):
        score += 6
        reasons.append("buyers > sellers 7d")
    if layer.get("top20_holder_pct", 0) > 0:
        score += 2
        reasons.append("holder map available")

    result["score"] = round(score, 1)
    return result


def format_coin(x):
    v = x["vol_changes"]
    def f(k):
        return "N/A" if v.get(k) is None else f"{v[k]:+.1f}%"

    t5, t1 = x["tech"].get("5m", {}), x["tech"].get("1h", {})
    r5 = "N/A" if t5.get("rsi") is None else f"{t5['rsi']:.0f}"
    r1 = "N/A" if t1.get("rsi") is None else f"{t1['rsi']:.0f}"

    return (
        f"🔹 {x['name']} ({x['symbol']})  #{x['rank']}\n"
        f"Score: {x['score']:.1f} | 1h {x['ch1']:+.2f}% | 24h {x['ch24']:+.2f}% | 7d {x['ch7']:+.2f}%\n"
        f"Vol 1d {f('1d')} | 3d {f('3d')} | 7d {f('7d')} | 14d {f('14d')}\n"
        f"RSI 5m {r5} | RSI 1h {r1}\n"
        f"Signals: {', '.join(x['reasons'][:6])}\n"
    )


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets not configured; skipping Telegram.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), 3900):
        r = session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i+3900]}, timeout=30)
        r.raise_for_status()


def main():
    print("🐋 FIL BEFORE PUMP — EARLY SCANNER")
    coins = get_market()
    history = load_history()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    results = []

    for coin in coins:
        q = coin.get("quote", {}).get("USD", {})
        symbol = coin.get("symbol", "")
        volume = float(q.get("volume_24h") or 0)
        price = float(q.get("price") or 0)
        if not symbol or volume <= 0 or price <= 0:
            continue

        vol_info = volume_signals(symbol, volume, price, history, now_ts)
        if not vol_info[1]:
            # No observed positive volume change yet; keep scanning, but don't spend
            # Binance requests on it.
            continue

        tech = technical_signals(symbol)
        score, reasons = score_coin(coin, vol_info, tech)
        results.append({
            "name": coin.get("name", symbol),
            "symbol": symbol,
            "rank": coin.get("cmc_rank"),
            "score": score,
            "reasons": reasons,
            "vol_changes": vol_info[0],
            "tech": tech,
            "ch1": float(q.get("percent_change_1h") or 0),
            "ch24": float(q.get("percent_change_24h") or 0),
            "ch7": float(q.get("percent_change_7d") or 0),
        })

    save_history(history)

    # Wallet/whale layer: only run on the strongest early candidates to
    # control API usage. Solscan currently covers Solana tokens.
    wallet_layers = []
    if SOLSCAN_API_KEY and results:
        results.sort(key=lambda x: x["score"], reverse=True)
        for result in results[:12]:
            layer = solscan_wallet_layer(result["symbol"])
            if layer:
                wallet_layers.append(layer)
                apply_wallet_signals(result, layer)
            time.sleep(0.10)

        wallet_history = update_wallet_history(wallet_layers)
        for result in results:
            result["wallet_overlap"] = wallet_overlap(wallet_history, result["symbol"])
            if result["wallet_overlap"] >= 1:
                result["score"] = round(result["score"] + min(10, 4 * result["wallet_overlap"]), 1)
                result["reasons"].append(f"wallet overlap {result['wallet_overlap']}")
    else:
        wallet_history = {}

    results.sort(key=lambda x: x["score"], reverse=True)

    # Top candidates are alerts, not trade signals. Wallet/whale confirmation is
    # intentionally a separate layer until a real wallet-data provider is connected.
    header = (
        "🐋 FIL BEFORE PUMP\n\n"
        "Top-400 early candidates\n"
        "Priority: volume → wallet/whale → technical\n"
        "Volume history: 1D / 3D / 7D / 14D\n"
        "Technical: 5m / 15m / 1h / 4h / 1d\n"
        "Price pump is NOT required.\n"
        "🐋 Solana wallet layer: holder map + buy/sell flow + overlap when API is connected.\n"
        "⚠️ Transfers are not labeled as buys unless the provider says so.\n\n"
    )
    message = header + ("\n".join(format_coin(x) for x in results[:20]) if results else "No early-volume candidates with available history.")
    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
