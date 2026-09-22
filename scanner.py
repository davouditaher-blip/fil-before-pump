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
BASE_BINANCE_FUTURES = "https://fapi.binance.com"
BASE_BYBIT = "https://api.bybit.com"
BASE_GATE = "https://api.gateio.ws"
HISTORY_FILE = Path("volume_history.json")

STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "USDE", "USD1", "TUSD", "USDD",
    "PYUSD", "FRAX", "LUSD", "USDP", "GUSD", "EURC", "EURT", "RLUSD",
}

TOKENIZED_MARKER_NAMES = (
    "tokenized", "tokenised", "bstock", "bstocks", "xstock",
    "etf token", "wrapped stock", "stock token",
)

NON_CRYPTO_MARKER_NAMES = (
    "global dollar", "digital dollar", "dollar", "stables",
    "stablecoin", "stable coin", "euro coinvertible",
    "gold", "pax gold", "tether gold", "tokenized gold", "gold-backed",
    "gold backed", "tokenized commodity", "tokenised commodity", "commodity-backed",
)

NON_CRYPTO_SYMBOLS = {
    "USDG", "U", "EUSX", "USDSUI", "FIDD", "EURCV", "PAXG", "XAUT",
    "KAU", "DGX",
}


def is_primary_crypto_asset(coin):
    symbol = str(coin.get("symbol") or "").upper()
    name = str(coin.get("name") or "").lower()
    if symbol in STABLE_SYMBOLS or symbol in NON_CRYPTO_SYMBOLS:
        return False
    if any(marker in name for marker in TOKENIZED_MARKER_NAMES):
        return False
    if any(marker in name for marker in NON_CRYPTO_MARKER_NAMES):
        return False
    return True


CMC_HEADERS = {
    "X-CMC_PRO_API_KEY": CMC_API_KEY,
    "Accepts": "application/json",
}

session = requests.Session()
session.headers.update({"User-Agent": "fil-before-pump/2.0"})


def get_json(url, params=None, headers=None, timeout=30):
    r = session.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def cmc(endpoint, params=None):
    return get_json(BASE_CMC + endpoint, params=params, headers=CMC_HEADERS, timeout=60)


def get_market():
    return cmc(
        "/v1/cryptocurrency/listings/latest",
        {"start": 1, "limit": 300, "convert": "USD"},
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


def historical_cmc_volume_batch(coins, history, now_ts):
    missing = []
    for coin in coins:
        if not is_primary_crypto_asset(coin):
            continue
        symbol = coin.get("symbol", "")
        cmc_id = coin.get("id")
        rows = history.get(symbol, [])
        has_14d = any(abs(x.get("timestamp", 0) - (now_ts - 14 * 86400)) <= 4 * 86400 for x in rows)
        if symbol and cmc_id and not has_14d:
            missing.append((symbol, cmc_id))

    # Keep historical requests bounded. CMC supports multiple comma-separated IDs.
    missing = missing[:1000]
    for i in range(0, len(missing), 40):
        batch = missing[i:i + 40]
        ids = ",".join(str(cmc_id) for _, cmc_id in batch)
        try:
            data = cmc(
                "/v3/cryptocurrency/quotes/historical",
                {"id": ids, "count": 15, "interval": "daily", "convert": "USD", "skip_invalid": "true"},
            )
            returned = data.get("data") or {}
            for symbol, cmc_id in batch:
                item = returned.get(str(cmc_id)) or {}
                rows = history.setdefault(symbol, [])
                existing = {int(x.get("timestamp", 0)): x for x in rows}
                for q in item.get("quotes", []) or []:
                    usd = q.get("quote", {}).get("USD", {})
                    volume = usd.get("volume_24h")
                    price = usd.get("price")
                    ts_text = q.get("timestamp")
                    if volume is None or price is None or not ts_text:
                        continue
                    try:
                        ts = int(datetime.fromisoformat(ts_text.replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        continue
                    existing[ts] = {"timestamp": ts, "volume": float(volume), "price": float(price), "source": "cmc_historical"}
                history[symbol] = sorted(existing.values(), key=lambda x: x.get("timestamp", 0))[-120:]
        except requests.RequestException as e:
            print(f"CMC batch history warning: {e}")
        time.sleep(0.5)



def volume_signals(symbol, volume, price, history, now_ts):
    rows = history.setdefault(symbol, [])
    rows.append({"timestamp": now_ts, "volume": volume, "price": price, "source": "snapshot"})
    cutoff = now_ts - 30 * 86400
    history[symbol] = [x for x in rows if x.get("timestamp", 0) >= cutoff]
    rows = history[symbol]

    targets = {
        "1d": (1 * 86400, 18 * 3600),
        "3d": (3 * 86400, 36 * 3600),
        "7d": (7 * 86400, 60 * 3600),
        "14d": (14 * 86400, 72 * 3600),
    }
    changes = {}
    for name, (age, tol) in targets.items():
        old = nearest(rows, now_ts - age, tol)
        changes[name] = pct(volume, old["volume"]) if old else None

    available = [v for v in changes.values() if v is not None]
    positive = [v for v in available if v > 0]
    max_positive = max(positive) if positive else 0.0

    early = any(v > 0 for v in available)
    strong = any(v >= 3 for v in available)
    acceleration = any(v >= 10 for v in available)

    return changes, early, strong, acceleration, max_positive


BINANCE_SYMBOLS = None
BINANCE_FUTURES_SYMBOLS = None
BYBIT_SPOT_SYMBOLS = None
BYBIT_LINEAR_SYMBOLS = None
GATE_FUTURES_SYMBOLS = None



def load_binance_futures_symbols():
    global BINANCE_FUTURES_SYMBOLS
    if BINANCE_FUTURES_SYMBOLS is not None:
        return BINANCE_FUTURES_SYMBOLS
    try:
        data = get_json(BASE_BINANCE_FUTURES + "/fapi/v1/exchangeInfo", timeout=30)
        BINANCE_FUTURES_SYMBOLS = {
            x["symbol"] for x in data.get("symbols", [])
            if x.get("status") == "TRADING"
            and x.get("quoteAsset") == "USDT"
            and x.get("contractType") == "PERPETUAL"
        }
    except requests.RequestException as e:
        print(f"Binance Futures exchangeInfo warning: {e}")
        BINANCE_FUTURES_SYMBOLS = set()
    return BINANCE_FUTURES_SYMBOLS


def load_binance_symbols():
    global BINANCE_SYMBOLS
    if BINANCE_SYMBOLS is not None:
        return BINANCE_SYMBOLS
    try:
        data = get_json(BASE_BINANCE + "/api/v3/exchangeInfo", timeout=30)
        BINANCE_SYMBOLS = {
            x["symbol"]
            for x in data.get("symbols", [])
            if x.get("status") == "TRADING"
            and x.get("quoteAsset") == "USDT"
            and x.get("isSpotTradingAllowed", True)
        }
    except requests.RequestException as e:
        print(f"Binance exchangeInfo warning: {e}")
        BINANCE_SYMBOLS = set()
    return BINANCE_SYMBOLS


def binance_symbol(symbol):
    candidate = f"{str(symbol).upper()}USDT"
    return candidate if candidate in load_binance_symbols() else None


def load_bybit_symbols():
    global BYBIT_SPOT_SYMBOLS, BYBIT_LINEAR_SYMBOLS
    if BYBIT_SPOT_SYMBOLS is not None and BYBIT_LINEAR_SYMBOLS is not None:
        return BYBIT_SPOT_SYMBOLS, BYBIT_LINEAR_SYMBOLS

    def fetch(category):
        symbols = set()
        cursor = None
        for _ in range(5):
            params = {"category": category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                data = get_json(BASE_BYBIT + "/v5/market/instruments-info", params, timeout=30)
                result = data.get("result", {})
                for item in result.get("list", []):
                    if item.get("status") == "Trading" and item.get("quoteCoin") == "USDT":
                        symbols.add(item.get("symbol"))
                cursor = result.get("nextPageCursor")
                if not cursor:
                    break
            except requests.RequestException as e:
                print(f"Bybit {category} instruments warning: {e}")
                break
        return symbols

    BYBIT_SPOT_SYMBOLS = fetch("spot")
    BYBIT_LINEAR_SYMBOLS = fetch("linear")
    return BYBIT_SPOT_SYMBOLS, BYBIT_LINEAR_SYMBOLS


def binance_symbol(symbol):
    candidate = f"{str(symbol).upper()}USDT"
    return candidate if candidate in load_binance_symbols() else None



def gate_contract(symbol):
    return f"{str(symbol).upper()}_USDT"


def load_gate_futures_symbols():
    global GATE_FUTURES_SYMBOLS
    if GATE_FUTURES_SYMBOLS is not None:
        return GATE_FUTURES_SYMBOLS
    symbols = set()
    try:
        data = get_json(BASE_GATE + "/api/v4/futures/usdt/contracts", timeout=30)
        for item in data if isinstance(data, list) else []:
            name = item.get("name")
            state = str(item.get("status") or item.get("state") or "normal").lower()
            if name and state in ("normal", "trading") and name.endswith("_USDT"):
                # Normalize Gate's BTC_USDT form to the scanner's BTCUSDT form.
                symbols.add(name.replace("_", ""))
    except requests.RequestException as e:
        print(f"Gate Futures contracts warning: {e}")
    GATE_FUTURES_SYMBOLS = symbols
    print(f"Gate Futures contract coverage: {len(symbols)} USDT perpetual contracts")
    return GATE_FUTURES_SYMBOLS


def futures_symbol_info(symbol):
    candidate = f"{str(symbol).upper()}USDT"
    exchanges = []
    if candidate in load_binance_futures_symbols():
        exchanges.append("Binance Futures")
    _, linear = load_bybit_symbols()
    if candidate in linear:
        exchanges.append("Bybit Futures")
    if candidate in load_gate_futures_symbols():
        exchanges.append("Gate Futures")
    return candidate if exchanges else None, exchanges


def bybit_symbol(symbol):
    candidate = f"{str(symbol).upper()}USDT"
    spot, linear = load_bybit_symbols()
    if candidate in spot:
        return candidate, "spot"
    if candidate in linear:
        return candidate, "linear"
    return None, None


def candles(symbol, interval, limit=220):
    pair = f"{str(symbol).upper()}USDT"
    if pair in load_binance_futures_symbols():
        try:
            data = get_json(
                BASE_BINANCE_FUTURES + "/fapi/v1/klines",
                {"symbol": pair, "interval": interval, "limit": limit},
                timeout=15,
            )
            if isinstance(data, list) and len(data) >= 60:
                return data
        except requests.RequestException:
            pass

    pair, category = bybit_symbol(symbol)
    if pair and category == "linear":
        bybit_interval = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}.get(interval)
        if bybit_interval:
            try:
                data = get_json(
                    BASE_BYBIT + "/v5/market/kline",
                    {"category": category, "symbol": pair, "interval": bybit_interval, "limit": min(limit, 1000)},
                    timeout=15,
                )
                rows = (data.get("result") or {}).get("list") or []
                if len(rows) >= 60:
                    rows = list(reversed(rows))
                    return [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows]
            except requests.RequestException:
                pass

    gate_pair = pair
    if gate_pair in load_gate_futures_symbols():
        gate_pair = gate_contract(symbol)
        gate_interval = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}.get(interval)
        if gate_interval:
            try:
                data = get_json(
                    BASE_GATE + "/api/v4/futures/usdt/candlesticks",
                    {"contract": gate_pair, "interval": gate_interval, "limit": min(limit, 2000)},
                    timeout=15,
                )
                rows = data if isinstance(data, list) else []
                if len(rows) >= 60:
                    rows = sorted(rows, key=lambda r: r.get("t", 0))
                    return [
                        [int(r.get("t", 0)) * 1000, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("sum", 0)]
                        for r in rows
                    ]
            except requests.RequestException:
                pass
    return []





FUTURES_HISTORY_FILE = Path("futures_volume_history.json")


def futures_tickers():
    out = {}
    sources = {}

    try:
        data = get_json(BASE_BINANCE_FUTURES + "/fapi/v1/ticker/24hr", timeout=30)
        for x in data if isinstance(data, list) else []:
            symbol = x.get("symbol")
            if symbol and symbol.endswith("USDT"):
                out[symbol] = {
                    "volume": float(x.get("quoteVolume") or 0),
                    "price": float(x.get("lastPrice") or 0),
                    "change24": float(x.get("priceChangePercent") or 0),
                    "source": "Binance Futures",
                }
                sources[symbol] = "Binance Futures"
    except requests.RequestException as e:
        print(f"Binance Futures ticker warning: {e}")

    try:
        data = get_json(BASE_BYBIT + "/v5/market/tickers", {"category": "linear"}, timeout=30)
        for x in ((data.get("result") or {}).get("list") or []):
            symbol = x.get("symbol")
            if not symbol or not symbol.endswith("USDT"):
                continue
            volume = float(x.get("turnover24h") or 0)
            price = float(x.get("lastPrice") or 0)
            if volume <= 0 or price <= 0:
                continue
            if symbol not in out or volume > out[symbol]["volume"]:
                out[symbol] = {
                    "volume": volume,
                    "price": price,
                    "change24": float(x.get("price24hPcnt") or 0) * 100,
                    "source": "Bybit Futures",
                }
                sources[symbol] = "Bybit Futures"
    except requests.RequestException as e:
        print(f"Bybit Futures ticker warning: {e}")

    # Emergency public fallback for GitHub-hosted runners when Binance/Bybit
    # are blocked by regional/IP restrictions. Gate's public Futures API is
    # unauthenticated and provides USDT perpetual tickers.
    try:
        data = get_json(BASE_GATE + "/api/v4/futures/usdt/tickers", timeout=30)
        for x in data if isinstance(data, list) else []:
            raw_symbol = x.get("contract")
            symbol = raw_symbol.replace("_", "") if raw_symbol else None
            if not symbol or not symbol.endswith("USDT"):
                continue
            volume = float(x.get("volume_24h_quote") or x.get("volume_24h_usd") or x.get("volume_24h") or 0)
            price = float(x.get("last") or 0)
            if volume <= 0 or price <= 0:
                continue
            if symbol not in out or volume > out[symbol]["volume"]:
                out[symbol] = {
                    "volume": volume,
                    "price": price,
                    "change24": float(x.get("change_percentage") or 0),
                    "source": "Gate Futures",
                }
                sources[symbol] = "Gate Futures"
    except requests.RequestException as e:
        print(f"Gate Futures ticker warning: {e}")

    print(f"Futures ticker coverage: {len(out)} contracts; sources={sorted(set(sources.values()))}")
    return out


def load_futures_history():
    if not FUTURES_HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(FUTURES_HISTORY_FILE.read_text())
    except Exception:
        return {}


def save_futures_history(history):
    FUTURES_HISTORY_FILE.write_text(json.dumps(history, indent=2))


def futures_daily_backfill(symbols, history):
    now = int(datetime.now(timezone.utc).timestamp())
    stale_before = now - 36 * 3600
    fetched = 0
    binance_futures = load_binance_futures_symbols()
    _, bybit_linear = load_bybit_symbols()
    gate_futures = load_gate_futures_symbols()

    for pair in symbols:
        rows = history.get(pair, [])
        if rows and max(int(x.get("timestamp", 0)) for x in rows) >= stale_before:
            continue
        got = False

        if pair in binance_futures:
            try:
                data = get_json(BASE_BINANCE_FUTURES + "/fapi/v1/klines", {"symbol": pair, "interval": "1d", "limit": 16}, timeout=15)
                if isinstance(data, list) and data:
                    history[pair] = [
                        {"timestamp": int(x[0]) // 1000, "volume": float(x[7]), "source": "binance_futures"}
                        for x in data if float(x[7] or 0) > 0
                    ]
                    got = True
            except requests.RequestException:
                pass

        if not got and pair in bybit_linear:
            try:
                data = get_json(BASE_BYBIT + "/v5/market/kline", {"category": "linear", "symbol": pair, "interval": "D", "limit": 16}, timeout=15)
                rows2 = ((data.get("result") or {}).get("list") or [])
                if rows2:
                    history[pair] = [
                        {"timestamp": int(x[0]) // 1000, "volume": float(x[6]), "source": "bybit_futures"}
                        for x in rows2 if float(x[6] or 0) > 0
                    ]
                    got = True
            except requests.RequestException:
                pass

        if not got and pair in gate_futures:
            try:
                data = get_json(
                    BASE_GATE + "/api/v4/futures/usdt/candlesticks",
                    {"contract": gate_contract(pair[:-4]), "interval": "1d", "limit": 16},
                    timeout=15,
                )
                rows3 = data if isinstance(data, list) else []
                if rows3:
                    history[pair] = [
                        {"timestamp": int(x.get("t", 0)), "volume": float(x.get("sum") or 0), "source": "gate_futures"}
                        for x in rows3 if float(x.get("sum") or 0) > 0
                    ]
                    got = True
            except requests.RequestException:
                pass

        if got:
            fetched += 1
        time.sleep(0.025)

    print(f"Futures daily history refreshed: {fetched}/{len(symbols)}")


def futures_volume_signals(pair, current_volume, history):
    rows = sorted(history.get(pair, []), key=lambda x: x.get("timestamp", 0))
    if not rows:
        return {"1d": None, "2d": None, "3d": None, "7d": None, "14d": None}
    now = int(datetime.now(timezone.utc).timestamp())
    out = {}
    # 1d/2d/3d are the short-term display signals.
    # 7d/14d remain part of the internal pre-pump analysis/scoring.
    for name, days in (("1d", 1), ("2d", 2), ("3d", 3), ("7d", 7), ("14d", 14)):
        old = nearest(rows, now - days * 86400, 36 * 3600 if days <= 3 else 72 * 3600)
        out[name] = pct(current_volume, old.get("volume")) if old else None
    return out


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
    return 100 - (100 / (1 + avg_gain / avg_loss))


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

    # Ichimoku (standard 9/26/52 settings). We keep the raw levels so the
    # scanner can distinguish developing bullish structure from late extension.
    ichimoku_tenkan = None
    ichimoku_kijun = None
    ichimoku_span_a = None
    ichimoku_span_b = None
    if len(closes) >= 52:
        tenkan_high = max(highs[-9:])
        tenkan_low = min(lows[-9:])
        kijun_high = max(highs[-26:])
        kijun_low = min(lows[-26:])
        ichimoku_tenkan = (tenkan_high + tenkan_low) / 2
        ichimoku_kijun = (kijun_high + kijun_low) / 2
        span_b_high = max(highs[-52:])
        span_b_low = min(lows[-52:])
        ichimoku_span_b = (span_b_high + span_b_low) / 2
        ichimoku_span_a = (ichimoku_tenkan + ichimoku_kijun) / 2

    # MACD line and signal line; a bullish cross is more useful than simply
    # checking whether MACD is positive.
    macd_signal = None
    macd_prev = None
    macd_signal_prev = None
    if len(closes) >= 35:
        macd_series = []
        for i in range(26, len(closes) + 1):
            fast = ema(closes[:i], 12)
            slow = ema(closes[:i], 26)
            if fast is not None and slow is not None:
                macd_series.append(fast - slow)
        if macd_series:
            macd = macd_series[-1]
            macd_signal = ema(macd_series, 9)
            if len(macd_series) >= 2:
                macd_prev = macd_series[-2]
                macd_signal_prev = ema(macd_series[:-1], 9)

    return {
        "rsi": r,
        "ema5": e5,
        "ema13": e13,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_bull_cross": (
            macd is not None and macd_signal is not None and
            macd_prev is not None and macd_signal_prev is not None and
            macd > macd_signal and macd_prev <= macd_signal_prev
        ),
        "vwap": vwap,
        "volume_ratio": volume_ratio,
        "ichimoku_tenkan": ichimoku_tenkan,
        "ichimoku_kijun": ichimoku_kijun,
        "ichimoku_span_a": ichimoku_span_a,
        "ichimoku_span_b": ichimoku_span_b,
        "close": closes[-1],
    }


def technical_signals(symbol):
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
    current_1d = changes.get("1d")
    current_2d = changes.get("2d")
    current_3d = changes.get("3d")
    context_7d = changes.get("7d")
    context_14d = changes.get("14d")
    score = 0.0
    reasons = []

    # Short-term volume priority: 1d and 2d capture fresh 24–48h inflow.
    # 3d is context only and is never a hard exclusion by itself.
    current_positive = [v for v in (current_1d, current_2d) if v is not None and v > 0]
    current_max = max(current_positive) if current_positive else 0.0

    if current_positive:
        score += 8
        reasons.append("early volume")
    if current_max >= 25:
        score += 16
        reasons.append("strong current volume expansion")
    elif current_max >= 10:
        score += 12
        reasons.append("current volume acceleration")
    elif current_max >= 5:
        score += 9
        reasons.append("current volume +5%")
    elif current_max >= 3:
        score += 7
        reasons.append("current volume +3%")
    elif current_max >= 2:
        score += 5
        reasons.append("current volume +2%")

    if context_7d is not None and context_7d > 0:
        score += 2
        reasons.append("7d volume context")
    if context_14d is not None and context_14d > 0:
        score += 2
        reasons.append("14d volume context")

    vol_mcap = (volume / market_cap) if volume > 0 and market_cap > 0 else 0
    if vol_mcap >= 0.10:
        score += 5
        reasons.append("high vol/mcap")
    elif vol_mcap >= 0.05:
        score += 3
        reasons.append("elevated vol/mcap")

    if -2 <= ch24 <= 3:
        score += 10
        reasons.append("price very early")
    elif 3 < ch24 <= 6:
        score += 6
        reasons.append("price still early")
    elif ch24 > 15:
        score -= 10
        reasons.append("late 24h move")
    elif ch24 > 8:
        score -= 4
        reasons.append("price already moving")

    if ch7 <= 12:
        score += 3
    if ch30 <= 30:
        score += 2
    if ch1 > 0:
        score += 2

    for tf, weight in (("5m", 10), ("15m", 9), ("1h", 12), ("4h", 5), ("1d", 4)):
        t = tech.get(tf, {})
        if not t:
            continue
        r = t.get("rsi")
        if r is not None and 45 <= r <= 65:
            score += weight * 0.30
            reasons.append(f"{tf} RSI developing")
        if t.get("ema5") and t.get("ema13") and t["ema5"] > t["ema13"]:
            score += weight * 0.18
        if t.get("ema20") and t.get("ema50") and t["ema20"] > t["ema50"]:
            score += weight * 0.15
        if t.get("ema50") and t.get("ema200") and t["ema50"] > t["ema200"]:
            score += weight * 0.10
        if t.get("vwap") and t.get("close") and t["close"] >= t["vwap"]:
            score += weight * 0.17
        if t.get("volume_ratio") and t["volume_ratio"] >= 1.20:
            score += weight * 0.25
            reasons.append(f"{tf} volume confirmation")
        if t.get("macd_bull_cross"):
            score += weight * 0.35
            reasons.append(f"{tf} MACD bullish cross")
        elif t.get("macd") is not None and t.get("macd_signal") is not None and t["macd"] > t["macd_signal"]:
            score += weight * 0.12
        tenkan = t.get("ichimoku_tenkan")
        kijun = t.get("ichimoku_kijun")
        close = t.get("close")
        span_a = t.get("ichimoku_span_a")
        span_b = t.get("ichimoku_span_b")
        if tenkan is not None and kijun is not None and tenkan > kijun:
            score += weight * 0.18
            reasons.append(f"{tf} Ichimoku TK bullish")
        if close is not None and span_a is not None and span_b is not None:
            cloud_top = max(span_a, span_b)
            if close >= cloud_top:
                score += weight * 0.12

    if current_max >= 10 and ch24 <= 6:
        stage = "ACCUMULATION"
    elif current_max >= 2 and ch24 <= 3:
        stage = "EARLY"
    elif ch24 > 15:
        stage = "LATE"
    else:
        stage = "WATCH"

    return round(score, 1), reasons, stage


SOLSCAN_API_KEY = os.environ.get("SOLSCAN_API_KEY", "")
GOLDRUSH_API_KEY = os.environ.get("GOLDRUSH_API_KEY", "")
GOLDRUSH_BASE = "https://api.covalenthq.com/v1"
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
    token = solana_token_for_symbol(symbol)
    if not token:
        return {}

    mint = token.get("address")
    if not mint:
        return {}

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


def goldrush_get(path, params=None):
    """Read-only multichain wallet/token data. GoldRush never labels a transfer as a buy/sell here."""
    if not GOLDRUSH_API_KEY:
        return None
    try:
        r = session.get(
            GOLDRUSH_BASE + path,
            params=params or {},
            headers={"Authorization": f"Bearer {GOLDRUSH_API_KEY}", "accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        return (payload.get("data") or {}).get("items", [])
    except requests.RequestException as e:
        print(f"GoldRush warning: {e}")
        return None


def coin_contracts(coin):
    """Return CMC platform contract mappings when present in the listing payload."""
    out = []
    platform = coin.get("platform") or {}
    if isinstance(platform, dict):
        name = str(platform.get("name") or "").lower()
        address = platform.get("token_address") or platform.get("tokenAddress")
        if address:
            chain = {
                "ethereum": "eth-mainnet",
                "bnb smart chain (bep20)": "bsc-mainnet",
                "bnb smart chain": "bsc-mainnet",
                "polygon": "matic-mainnet",
                "arbitrum one": "arbitrum-mainnet",
                "optimism": "optimism-mainnet",
                "base": "base-mainnet",
                "avalanche c-chain": "avalanche-mainnet",
            }.get(name)
            if chain:
                out.append((chain, address))
    return out


def goldrush_wallet_layer(coin):
    """Holder-map layer for EVM assets.

    We deliberately do NOT infer buys/sells from raw transfers. Those require
    provider-level trade classification and are handled separately by Solscan
    where available.
    """
    if not GOLDRUSH_API_KEY:
        return {}
    contracts = coin_contracts(coin)
    if not contracts:
        return {}

    best = {}
    for chain, address in contracts:
        items = goldrush_get(f"/{chain}/tokens/{address}/token_holders_v2/", {"page-size": 20, "page-number": 0})
        if not items:
            continue
        holders = []
        for h in items[:20]:
            wallet = h.get("address") or h.get("wallet_address") or h.get("walletAddress")
            if wallet:
                holders.append({
                    "wallet": wallet,
                    "symbol": coin.get("symbol"),
                    "mint": address,
                    "chain": chain,
                    "rank": h.get("rank"),
                    "percentage": h.get("percentage_relative_to_total_supply") or h.get("percentage"),
                    "value": h.get("balance_quote") or h.get("value_quote"),
                    "timestamp": int(datetime.now(timezone.utc).timestamp()),
                })
        if holders:
            best = {
                "chain": chain,
                "mint": address,
                "symbol": coin.get("symbol"),
                "holders": holders,
                "top5_holder_pct": sum(float(x.get("percentage") or 0) for x in holders[:5]),
                "top20_holder_pct": sum(float(x.get("percentage") or 0) for x in holders),
                "buy_sell_ratio_7d": None,
                "buyers_7d": 0,
                "sellers_7d": 0,
                "provider": "GoldRush",
            }
            break
    return best


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
                "balance": holder.get("balance"),
                "total_supply": holder.get("total_supply"),
                "value": holder.get("value"),
                "chain": holder.get("chain") or result.get("chain"),
                "price_usd": result.get("price_usd"),
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



GMGN_API_KEY = os.environ.get("GMGN_API_KEY", "")

def gmgn_enrichment():
    if not GMGN_API_KEY: return {}
    try:
        from gmgn_layer import run_gmgn, build_signals
        trades = []
        for chain in ('sol', 'bsc', 'base', 'eth'):
            trades.extend(run_gmgn(chain))
        signals = build_signals(trades)

        # Common wallets: wallets that bought at least two different assets in this scan.
        wallet_assets = {}
        for trade in trades:
            wallet = trade.get('maker')
            symbol = ((trade.get('base_token') or {}).get('symbol') or '').upper()
            side = str(trade.get('side') or '').lower()
            if wallet and symbol and side == 'buy':
                wallet_assets.setdefault(wallet, set()).add(symbol)

        common_by_symbol = {}
        for wallet, assets in wallet_assets.items():
            if len(assets) < 2: continue
            for symbol in assets:
                common_by_symbol.setdefault(symbol, []).append({'wallet': wallet, 'assets': sorted(assets)})

        best = {}
        for row in signals:
            symbol = str(row.get('symbol') or '').upper()
            if not symbol: continue
            if row.get('score', 0) > best.get(symbol, {}).get('score', -1):
                row['common_wallets'] = common_by_symbol.get(symbol, [])
                row['common_wallet_count'] = len(row['common_wallets'])
                row['common_assets'] = sorted({a for item in row['common_wallets'] for a in item['assets'] if a != symbol})
                best[symbol] = row

        best['__COMMON_WALLETS__'] = [
            {'wallet': wallet, 'assets': sorted(assets), 'asset_count': len(assets)}
            for wallet, assets in wallet_assets.items() if len(assets) >= 2
        ]
        return best
    except Exception as e:
        print(f'GMGN enrichment warning: {e}')
        return {}

def apply_gmgn_signals(result, signal):
    if not signal: return
    result["gmgn"] = signal
    score, reasons = result["score"], result["reasons"]
    wallets = len(signal.get("wallets") or [])
    buys = int(signal.get("buy_count", 0) or 0)
    opens = int(signal.get("opens", 0) or 0)
    buy_usd = float(signal.get("buy_usd", 0) or 0)
    overlap = int(signal.get("overlap", 0) or 0)
    if buys: score += min(8, 4 + buys); reasons.append(f"خرید اسمارت‌مانی GMGN: {buys}")
    if wallets >= 3: score += 8; reasons.append(f"{wallets} ولت اسمارت‌مانی GMGN")
    elif wallets == 2: score += 5; reasons.append("GMGN 2-wallet convergence")
    elif wallets == 1: score += 2; reasons.append("GMGN 1 Smart Money wallet")
    if opens: score += min(6, opens * 2); reasons.append(f"باز شدن پوزیشن در GMGN: {opens}")
    if buy_usd >= 10000: score += 7; reasons.append("خرید تجمیعی GMGN بیشتر از ۱۰هزار دلار")
    elif buy_usd >= 2500: score += 4; reasons.append("خرید تجمیعی GMGN بیشتر از ۲۵۰۰ دلار")
    if overlap >= 2: score += min(8, overlap * 2); reasons.append(f"همپوشانی ولت GMGN: {overlap}")
    result["score"] = round(score, 1)

def format_gmgn(x):
    g = x.get('gmgn') or {}
    if not g: return 'GMGN: داده اسمارت‌مانی منطبق در دسترس نیست'
    common_count = int(g.get('common_wallet_count', 0) or 0)
    common_assets = ', '.join(g.get('common_assets') or [])
    common_text = f' | common wallets {common_count}' if common_count else ''
    if common_assets: common_text += f' | مشترک با: {common_assets}'
    return (f"GMGN Smart Money: {int(g.get('buy_count', 0) or 0)} buys | "
            f"{len(g.get('wallets') or [])} wallets | Buy ${float(g.get('buy_usd', 0) or 0):,.0f} | "
            f"overlap {int(g.get('overlap', 0) or 0)}{common_text}")

def candidate_status(x):
    g = x.get("gmgn") or {}
    buys = int(g.get("buy_count", 0) or 0)
    wallets = len(g.get("wallets") or [])
    buy_usd = float(g.get("buy_usd", 0) or 0)
    v = x.get("vol_changes") or {}
    v1, v2 = v.get("1d"), v.get("2d")
    ch24 = float(x.get("ch24") or 0)
    smart = buys > 0 or wallets > 0 or buy_usd > 0
    fresh_volume = (v1 is not None and v1 > 0) or (v2 is not None and v2 > 0)
    if smart and ch24 <= 8 and fresh_volume:
        return "EARLY CANDIDATE / WATCH"
    if smart:
        return "SMART MONEY / WATCH"
    if fresh_volume and ch24 <= 8:
        return "EARLY VOLUME / WATCH"
    return "WATCH"


def format_coin(x):
    v = x["vol_changes"]

    def f(k):
        return "N/A" if v.get(k) is None else f"{v[k]:+.1f}%"

    t5, t1 = x["tech"].get("5m", {}), x["tech"].get("1h", {})
    r5 = "N/A" if t5.get("rsi") is None else f"{t5['rsi']:.0f}"
    r1 = "N/A" if t1.get("rsi") is None else f"{t1['rsi']:.0f}"
    t15 = x["tech"].get("15m", {})
    r15 = "N/A" if t15.get("rsi") is None else f"{t15['rsi']:.0f}"

    wallet_status = "موجود" if x.get("wallet") else "در انتظار داده"
    return (
        f"🔹 {x['name']} ({x['symbol']})  #{x['rank']}\n"
        f"وضعیت: {candidate_status(x)}\n"
        f"امتیاز داخلی: {x['score']:.1f} | ۱ساعت {x['ch1']:+.2f}% | ۲۴ساعت {x['ch24']:+.2f}% | ۷روز {x['ch7']:+.2f}%\n"
        f"حجم ۱روز {f('1d')} | ۲روز {f('2d')} | ۳روز {f('3d')}\n"
        f"RSI ۵دقیقه {r5} | RSI ۱۵دقیقه {r15} | RSI ۱ساعت {r1}\n"
        f"تکنیکال: ۵دقیقه/۱۵دقیقه/۱ساعت/۴ساعت/۱روز = {sum(bool(x.get('tech',{}).get(tf)) for tf in ('5m','15m','1h','4h','1d'))}/5\n"
        f"ولت: {wallet_status} | ارائه‌دهنده: {x.get('wallet_provider','N/A')} | همپوشانی: {x.get('wallet_overlap', 0)}\n"
        f"{format_gmgn(x)}\n"
        f"{format_coinglass(x)}\n"
        f"دلایل: {', '.join(x['reasons'][:10])}\n"
    )


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets not configured; skipping Telegram.")
        return

    # Quiet hours: do not send Telegram messages from 03:00 through 09:59.
    # The GitHub runner uses UTC, so use the scanner's configured local
    # timezone when available; default is Asia/Tehran.
    try:
        from zoneinfo import ZoneInfo
        local_now = datetime.now(ZoneInfo(os.environ.get("BOT_TIMEZONE", "Asia/Tehran")))
        if 3 <= local_now.hour < 10:
            print(f"Telegram quiet hours active ({local_now:%H:%M}); skipping Telegram.")
            return
    except Exception as e:
        print(f"Telegram quiet-hours timezone warning: {e}")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), 3900):
        r = session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i+3900]}, timeout=30)
        r.raise_for_status()



# Telegram interactive control panel
RANK_RANGES = {
    "top100": (1, 100),
    "101_200": (101, 200),
    "201_300": (201, 300),
    "all": (1, 300),
}

def telegram_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "⚡ اسکن لحظه‌ای", "callback_data": "run|all|all"},
                {"text": "🔥 فیل کامل", "callback_data": "run|all|all"},
            ],
            [
                {"text": "🥇 رتبه 1–100", "callback_data": "rank|top100|all"},
                {"text": "🥈 رتبه 101–200", "callback_data": "rank|101_200|all"},
            ],
            [
                {"text": "🥉 رتبه 201–300", "callback_data": "rank|201_300|all"},
                {"text": "🐋 اسکن ولت‌محور", "callback_data": "rank|all|all"},
            ],
            [
                {"text": "📊 حجم", "callback_data": "filter|all|volume"},
                {"text": "🐋 Smart Money", "callback_data": "filter|all|smart"},
            ],
            [
                {"text": "🐳 نهنگ", "callback_data": "filter|all|whale"},
                {"text": "👛 ولت‌ها", "callback_data": "filter|all|wallet"},
            ],
            [
                {"text": "📈 تکنیکال", "callback_data": "filter|all|technical"},
            ],
        ]
    }


def send_telegram_menu(chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    target = chat_id or TELEGRAM_CHAT_ID
    if not target:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = session.post(
            url,
            data={
                "chat_id": target,
                "text": "🐋 فیل قبل از پامپ\n\nنوع بررسی را انتخاب کن:",
                "reply_markup": json.dumps(telegram_menu_keyboard(), ensure_ascii=False),
            },
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Telegram menu warning: {e}")


def apply_bot_filters(results):
    """Apply Telegram rank/filter selections without weakening the core scanner."""
    rank_key = os.environ.get("BOT_RANK_RANGE", "all")
    filter_key = os.environ.get("BOT_FILTER", "all")
    selected_filters = [x for x in filter_key.split(",") if x and x != "all"]
    lo, hi = RANK_RANGES.get(rank_key, RANK_RANGES["all"])

    selected = [
        x for x in results
        if x.get("rank") is not None and lo <= int(x["rank"]) <= hi
    ]
    if not selected_filters:
        return selected

    def smart(x):
        g = x.get("gmgn") or {}
        w = x.get("wallet") or {}
        buy_usd = float(g.get("buy_usd", 0) or 0)
        buys = int(g.get("buy_count", 0) or 0)
        wallets = len(g.get("wallets") or [])
        ratio = w.get("buy_sell_ratio_7d")
        buyers = int(w.get("buyers_7d", 0) or 0)
        sellers = int(w.get("sellers_7d", 0) or 0)
        return (
            buy_usd >= 2500
            or buys >= 2
            or wallets >= 2
            or (ratio is not None and ratio > 1.10 and buyers > sellers)
        )

    def whale(x):
        g = x.get("gmgn") or {}
        return (
            float(g.get("buy_usd", 0) or 0) >= 10000
            or float(g.get("largest_buy_usd", 0) or 0) >= 10000
        )

    def wallet(x):
        return (
            bool(x.get("wallet"))
            or int(x.get("wallet_overlap", 0) or 0) >= 1
            or int(x.get("smart_wallet_overlap", 0) or 0) >= 1
            or int(x.get("wallet_accumulation", 0) or 0) >= 1
        )

    def volume(x):
        v = x.get("vol_changes") or {}
        v1, v2 = v.get("1d"), v.get("2d")
        return (v1 is not None and v1 > 0) or (v2 is not None and v2 > 0)

    def technical(x):
        tech = x.get("tech") or {}
        for tf in ("5m", "15m", "1h", "4h", "1d"):
            t = tech.get(tf) or {}
            if not t:
                continue
            close, vwap = t.get("close"), t.get("vwap")
            bullish = 0
            if t.get("rsi") is not None and 45 <= float(t["rsi"]) <= 70:
                bullish += 1
            if t.get("ema5") is not None and t.get("ema13") is not None and t["ema5"] > t["ema13"]:
                bullish += 1
            if t.get("macd_bull_cross") or (
                t.get("macd") is not None and t.get("macd_signal") is not None and t["macd"] > t["macd_signal"]
            ):
                bullish += 1
            if close is not None and vwap is not None and close >= vwap:
                bullish += 1
            if t.get("volume_ratio") is not None and t["volume_ratio"] >= 1.20:
                bullish += 1
            if t.get("ichimoku_tenkan") is not None and t.get("ichimoku_kijun") is not None and t["ichimoku_tenkan"] > t["ichimoku_kijun"]:
                bullish += 1
            if bullish >= 2:
                return True
        return False

    checks = {
        "smart": smart,
        "whale": whale,
        "wallet": wallet,
        "volume": volume,
        "technical": technical,
    }
    return [x for x in selected if all(checks[key](x) for key in selected_filters if key in checks)]


def main():
    print("🐋 FIL BEFORE PUMP — EARLY SCANNER")
    coins = [x for x in get_market() if x.get("cmc_rank") is not None and int(x.get("cmc_rank")) <= 300]
    history = load_history()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    # Futures are the primary volume source; CMC is metadata only.
    binance_futures = load_binance_futures_symbols()
    _, bybit_linear = load_bybit_symbols()
    gate_futures = load_gate_futures_symbols()
    futures_universe = binance_futures | bybit_linear | gate_futures
    ftickers = futures_tickers()
    fhistory = load_futures_history()

    eligible_pairs = []
    for coin in coins:
        symbol = str(coin.get("symbol") or "").upper()
        pair = f"{symbol}USDT"
        if symbol and is_primary_crypto_asset(coin) and pair in futures_universe and pair in ftickers and ftickers[pair]["volume"] > 0:
            eligible_pairs.append(pair)

    # Backfill history for the actual CMC top-300 Futures-eligible scan universe.
    futures_daily_backfill(eligible_pairs, fhistory)
    save_futures_history(fhistory)

    results = []
    for coin in coins:
        q = coin.get("quote", {}).get("USD", {})
        symbol = coin.get("symbol", "")
        pair = f"{str(symbol).upper()}USDT"
        ticker = ftickers.get(pair)
        if not symbol or not ticker or ticker["volume"] <= 0:
            continue
        if not is_primary_crypto_asset(coin) or pair not in futures_universe:
            continue

        changes = futures_volume_signals(pair, ticker["volume"], fhistory)
        available = [v for v in changes.values() if v is not None]
        early = any(v > 0 for v in available)
        strong = any(v >= 3 for v in available)
        acceleration = any(v >= 10 for v in available)
        vol_info = (changes, early, strong, acceleration, max([v for v in available if v > 0], default=0.0))

        coin_for_score = dict(coin)
        coin_for_score["quote"] = dict(coin["quote"])
        coin_for_score["quote"]["USD"] = dict(q)
        coin_for_score["quote"]["USD"]["volume_24h"] = ticker["volume"]
        if not available:
            score, reasons, stage = 1.0, ["futures contract", "futures volume history pending"], "WATCH"
        else:
            score, reasons, stage = score_coin(coin_for_score, vol_info, {})

        results.append({
            "name": coin.get("name", symbol),
            "symbol": symbol,
            "rank": coin.get("cmc_rank"),
            "score": score,
            "reasons": reasons,
            "stage": stage,
            "price_usd": float(q.get("price") or 0),
            "vol_changes": changes,
            "tech": {},
            "ch1": float(q.get("percent_change_1h") or 0),
            "ch24": float(q.get("percent_change_24h") or 0),
            "ch7": float(q.get("percent_change_7d") or 0),
        })

    # ================================================================
    # FIL COMPLETE PIPELINE
    # GMGN Smart Money -> volume -> wallet/whale -> technical.
    # Strong early Smart Money candidates are retained even when a
    # technical layer is weak or temporarily unavailable.
    # ================================================================

    results.sort(key=lambda x: (
        x["score"],
        x["vol_changes"].get("1d") or -999999,
        x["vol_changes"].get("3d") or -999999,
    ), reverse=True)

    requested_filters = [x for x in os.environ.get("BOT_FILTER", "all").split(",") if x and x != "all"]

    # Layer 1: GMGN Smart Money FIRST.
    gmgn = gmgn_enrichment()
    if gmgn:
        for result in results:
            apply_gmgn_signals(result, gmgn.get(str(result["symbol"]).upper()))
    results.sort(key=lambda x: (
        1 if x.get("gmgn") else 0,
        (x.get("gmgn") or {}).get("score", 0) or 0,
        x["score"],
        x["vol_changes"].get("1d") or -999999,
    ), reverse=True)

    # Layer 2: wallet / whale. Additive only; no wallet signal is a hard exclusion.
    wallet_layers = []
    if (SOLSCAN_API_KEY or GOLDRUSH_API_KEY) and results:
        coin_by_symbol = {str(c.get("symbol") or "").upper(): c for c in coins}
        wallet_filters_requested = any(x in requested_filters for x in ("wallet", "smart", "whale"))
        wallet_scan_limit = len(results) if wallet_filters_requested else min(300, len(results))
        for result in results[:wallet_scan_limit]:
            coin = coin_by_symbol.get(str(result["symbol"]).upper())
            layers = []
            if SOLSCAN_API_KEY:
                layer = solscan_wallet_layer(result["symbol"])
                if layer:
                    layers.append(layer)
            if GOLDRUSH_API_KEY and coin:
                layer = goldrush_wallet_layer(coin)
                if layer:
                    layers.append(layer)
            for layer in layers:
                layer["price_usd"] = float(result.get("price_usd") or 0)
                wallet_layers.append(layer)
                apply_wallet_signals(result, layer)
            if layers:
                result["wallet_provider"] = ",".join(sorted({l.get("provider", l.get("chain", "onchain")) for l in layers}))
            time.sleep(0.10)

        wallet_history = update_wallet_history(wallet_layers)
        for result in results:
            result["wallet_overlap"] = wallet_overlap(wallet_history, result["symbol"])
            if result["wallet_overlap"] >= 1:
                result["score"] = round(result["score"] + min(10, 4 * result["wallet_overlap"]), 1)
                result["reasons"].append(f"همپوشانی ولت: {result['wallet_overlap']}")

    # لایه تکنیکال در این نسخه اجرا نمی‌شود؛ فیلتر اصلی ولت‌محور است.
    save_history(history)

    # Fresh-volume guard: 2d is the main 48h inflow check. A strong 1d
    # rebound cannot by itself rescue a severe 2d contraction. Meaningful
    # Smart Money/Whale evidence may override this guard; wallet-overlap
    # history alone does not.
    MAX_RECOVERY_2D_DROP = -15.0

    def meaningful_smart_money(result):
        g = result.get("gmgn") or {}
        buy_usd = float(g.get("buy_usd", 0) or 0)
        buys = int(g.get("buy_count", 0) or 0)
        wallets = len(g.get("wallets") or [])
        if buy_usd >= 2500:
            return True
        if buys >= 2 and wallets >= 2:
            return True

        w = result.get("wallet") or {}
        ratio = w.get("buy_sell_ratio_7d")
        buyers = int(w.get("buyers_7d", 0) or 0)
        sellers = int(w.get("sellers_7d", 0) or 0)
        return ratio is not None and ratio > 1.10 and buyers > sellers

    before_guard = len(results)
    filtered_results = []
    for result in results:
        v = result.get("vol_changes") or {}
        v1 = v.get("1d")
        v2 = v.get("2d")
        override = meaningful_smart_money(result)

        # Keep assets with fresh 24-48h inflow. Keep a mild 2d recovery
        # (down to -15%) when 1d is strongly positive. Severe 2d weakness
        # is removed unless there is meaningful Smart Money/Whale evidence.
        if v2 is not None and v2 <= MAX_RECOVERY_2D_DROP and not override:
            result["reasons"].append("حذف: افت حجم ۲روزه بیشتر از ۱۵٪")
            continue
        if (
            v2 is not None and v2 < 0 and v2 > MAX_RECOVERY_2D_DROP
            and (v1 is None or v1 <= 5)
        ):
            result["reasons"].append("تحت نظر: بازیابی ضعیف حجم ۱روزه")
        filtered_results.append(result)

    results = filtered_results
    removed = before_guard - len(results)
    if removed:
        print(f"Fresh-volume guard removed {removed} severe 2d-volume contraction candidates.")

    # CoinGlass is a confirmation layer, not a hard filter. To keep the
    # free API quota under control, refresh only the top 5 candidates every
    # two hours; cached values are reused between refreshes.
    cg_cache = load_coinglass_cache()
    if COINGLASS_API_KEY and results:
        if datetime.now(timezone.utc).hour % 2 == 0:
            for result in results[:5]:
                signals = coinglass_signals(result["symbol"], cg_cache, now_ts)
                apply_coinglass_signals(result, signals)
        else:
            for result in results[:5]:
                cached = cg_cache.get(result["symbol"], {}).get("signals") or {}
                apply_coinglass_signals(result, cached)
        save_coinglass_cache(cg_cache)
    results.sort(key=lambda x: x["score"], reverse=True)

    # Apply the selection made from the Telegram control panel.
    results = apply_bot_filters(results)

    market_map = {str(c.get("symbol") or "").upper(): c for c in coins}
    btc = market_map.get("BTC", {}).get("quote", {}).get("USD", {})
    eth = market_map.get("ETH", {}).get("quote", {}).get("USD", {})
    btc24 = float(btc.get("percent_change_24h") or 0) if btc else 0
    eth24 = float(eth.get("percent_change_24h") or 0) if eth else 0
    btc7 = float(btc.get("percent_change_7d") or 0) if btc else 0
    eth7 = float(eth.get("percent_change_7d") or 0) if eth else 0
    market_regime_text = (
        f"BTC/ETH: BTC 24h {btc24:+.2f}% / 7d {btc7:+.2f}% | "
        f"ETH 24h {eth24:+.2f}% / 7d {eth7:+.2f}%\n"
    )

    header = (
        "🐋 فیل کامل قبل از پامپ — کاندیداهای اولیه\n\n"
        + market_regime_text
        + "بازار فیوچرز/پرپچوال: Binance + Bybit + Gate\n"
        "لایه‌ها: GMGN Smart Money → حجم ۱/۳/۷/۱۴روز → ولت/نهنگ → RSI ۵m/۱۵m/۱h → MACD/EMA/Ichimoku/VWAP → Structure/Compression → OI/Funding/Liquidation → BTC/ETH → Pre-Pump Check\n"
        "GMGN لایه اول و اولویت‌دار است؛ کاندید قوی به‌دلیل ضعف یک فیلتر تکنیکال حذف نمی‌شود.\n"
        "منبع حجم: فیوچرز زنده + تاریخچه روزانه فیوچرز\n"
        "نمایش حجم: ۱روز / ۲روز / ۳روز / ۷روز / ۱۴روز\n"
        "فیلتر حجم: ۱روز/۲روز معیار اصلی؛ ۳روز فقط زمینه است و حذف قطعی نمی‌کند.\n"
        "Fallback: Gate Futures در صورت محدودیت Binance/Bybit\n"
        "تکنیکال: ۵دقیقه / ۱۵دقیقه / ۱ساعت / ۴ساعت / ۱روز (اولویت با ۵دقیقه/۱۵دقیقه/۱ساعت)\n"
        "پامپ قبلی قیمت شرط نیست.\n"
        "🐋 اولویت ولت: نقشه هولدرها ← جریان خرید/فروش ← همپوشانی ولت ← سابقه نهنگ\n"
        "🐋 منابع آن‌چین: Solscan + GoldRush\n"
        "⚠️ انتقال خام هیچ‌وقت خرید/فروش محسوب نمی‌شود؛ برای این سیگنال باید جریان معامله توسط ارائه‌دهنده برچسب‌گذاری شده باشد.\n"
        "⚠️ استیبل‌کوین‌ها، سهام توکنیزه و دارایی‌های طلاپشتوانه حذف می‌شوند.\n\n"
    )
    message = header + (
        '\n'.join(format_coin(x) for x in results[:30])
        if results else 'No early-volume candidates with available history.'
    )

    # Common-wallet summary appended to every scheduled 30-minute Telegram report.
    common_rows = (gmgn.get('__COMMON_WALLETS__') or []) if gmgn else []
    candidate_symbols = {str(x.get('symbol') or '').upper() for x in results}
    common_rows = [x for x in common_rows if any(a in candidate_symbols for a in x.get('assets', []))]
    common_rows.sort(key=lambda x: (len([a for a in x.get('assets', []) if a in candidate_symbols]), x.get('asset_count', 0)), reverse=True)
    common_lines = ['', '🔗 COMMON WALLET SIGNAL — هر ۳۰ دقیقه', 'ولت‌هایی که در این اسکن روی حداقل ۲ ارز خرید داشته‌اند:']
    if common_rows:
        for item in common_rows[:15]:
            wallet = item.get('wallet', '')
            short = wallet[:8] + '…' + wallet[-6:] if len(wallet) > 18 else wallet
            assets = item.get('assets') or []
            candidate_assets = [a for a in assets if a in candidate_symbols]
            common_lines.append(f"👛 {short} | {len(assets)} ارز مشترک | خرید: {', '.join(assets[:12])}" + (f" | کاندید: {', '.join(candidate_assets)}" if candidate_assets else ''))
    else: common_lines.append('اطلاعات کافی برای ولت مشترک در این اسکن وجود ندارد.')
    message += '\n' + '\n'.join(common_lines)
    print(message)
    send_telegram(message)
    # The control panel is opened with /start; do not send unsolicited menu messages.

if __name__ == "__main__":
    main()