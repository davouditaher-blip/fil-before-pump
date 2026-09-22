import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

CMC_API_KEY = os.environ.get("CMC_API_KEY", "")
GMGN_API_KEY = os.environ.get("GMGN_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HISTORY_FILE = Path("gmgn_wallet_history.json")
CHAINS = ("sol", "bsc", "base", "eth")


def run_gmgn(chain):
    env = os.environ.copy()
    env["GMGN_API_KEY"] = GMGN_API_KEY
    cmd = ["npx", "--yes", "gmgn-cli", "track", "smartmoney",
           "--chain", chain, "--limit", "200", "--raw"]
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=90)
        if p.returncode != 0:
            print(f"GMGN {chain} warning: {p.stderr[-1000:]}")
            return []
        for line in reversed(p.stdout.strip().splitlines()):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj.get("list") or []
            except json.JSONDecodeError:
                pass
    except Exception as e:
        print(f"GMGN {chain} warning: {e}")
    return []


def get_cmc():
    if not CMC_API_KEY:
        return {}
    r = requests.get(
        "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
        params={"start": 1, "limit": 300, "convert": "USD"},
        headers={"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accepts": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    return {str(x.get("symbol") or "").upper(): x for x in r.json().get("data", [])}


def load_history():
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return {}


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def build_signals(trades):
    by_token = defaultdict(list)
    for t in trades:
        address = t.get("base_address")
        if address:
            by_token[(t.get("chain", ""), address)].append(t)

    rows = []
    for (chain, address), items in by_token.items():
        buys = [x for x in items if str(x.get("side", "")).lower() == "buy"]
        sells = [x for x in items if str(x.get("side", "")).lower() == "sell"]
        wallets = {x.get("maker") for x in buys if x.get("maker")}
        opens = sum(1 for x in buys if str(x.get("is_open_or_close")) == "0")
        buy_usd = sum(float(x.get("amount_usd") or 0) for x in buys)
        latest = max(items, key=lambda x: int(x.get("timestamp") or 0))
        latest_side = str(latest.get("side") or "").lower()
        symbol = ((latest.get("base_token") or {}).get("symbol") or "?")
        tags = set()
        for x in buys:
            tags.update((x.get("maker_info") or {}).get("tags") or [])

        score = 0
        reasons = []
        if buys:
            score += 10
            reasons.append("smart-money buy")
        if len(wallets) >= 3:
            score += 18
            reasons.append("3+ smart-money wallets")
        elif len(wallets) == 2:
            score += 10
            reasons.append("2-wallet convergence")
        if opens:
            score += min(12, opens * 4)
            reasons.append("position opens")
        if buy_usd >= 10000:
            score += 8
            reasons.append("large aggregate buy")
        elif buy_usd >= 2500:
            score += 4
            reasons.append(">$2.5k aggregate buy")
        if "smart_degen" in tags:
            score += 4
            reasons.append("smart_degen tag")
        if sells and not buys:
            score -= 8
            reasons.append("recent smart-money selling")

        rows.append({
            "chain": chain, "address": address, "symbol": symbol,
            "score": score, "wallets": sorted(wallets),
            "buy_count": len(buys), "sell_count": len(sells),
            "opens": opens, "buy_usd": buy_usd,
            "latest_ts": int(latest.get("timestamp") or 0),
            "latest_side": latest_side,
            "position_status": (
                "🟢 خرید و نگهداری محتمل" if latest_side == "buy" and buys else
                "🟡 فروش جزئی" if latest_side == "sell" and len(sells) < len(buys) else
                "🔴 خروج/توزیع" if latest_side == "sell" and sells else
                "⚪ نامشخص"
            ),
            "reasons": reasons,
        })
    return rows


def update_history(trades, history):
    now = int(datetime.now(timezone.utc).timestamp())
    for t in trades:
        wallet, address = t.get("maker"), t.get("base_address")
        if not wallet or not address:
            continue
        history.setdefault(wallet, []).append({
            "timestamp": now,
            "trade_timestamp": int(t.get("timestamp") or 0),
            "chain": t.get("chain") or "",
            "address": address,
            "symbol": ((t.get("base_token") or {}).get("symbol") or "?"),
            "side": t.get("side"),
            "amount_usd": t.get("amount_usd"),
            "price_change": t.get("price_change"),
        })
        history[wallet] = history[wallet][-500:]


def cross_asset_wallets(history, address, chain):
    out = []
    for wallet, rows in history.items():
        assets = {(r.get("chain"), r.get("address")) for r in rows if r.get("address")}
        if (chain, address) in assets and len(assets) >= 2:
            out.append(wallet)
    return out


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(message), 3900):
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message[i:i+3900]}, timeout=30).raise_for_status()


def main():
    if not GMGN_API_KEY:
        raise SystemExit("GMGN_API_KEY is not configured.")

    trades = []
    for chain in CHAINS:
        rows = run_gmgn(chain)
        print(f"GMGN {chain}: {len(rows)} records")
        trades.extend(rows)
        time.sleep(0.25)

    if not trades:
        send_telegram("🐋 GMGN SMART MONEY LAYER\n\nNo Smart Money trade records returned.")
        return

    signals = build_signals(trades)
    history = load_history()
    update_history(trades, history)
    cmc = get_cmc()

    for x in signals:
        overlaps = cross_asset_wallets(history, x["address"], x["chain"])
        x["overlap"] = len(overlaps)
        x["overlap_wallets"] = overlaps[:5]

        c = cmc.get(x["symbol"].upper())
        if c:
            q = c.get("quote", {}).get("USD", {})
            x["rank"] = c.get("cmc_rank")
            x["change24"] = float(q.get("percent_change_24h") or 0)
            x["change7"] = float(q.get("percent_change_7d") or 0)
            if x["change24"] <= 8 and x["buy_count"]:
                x["score"] += 5
                x["reasons"].append("price still early")
            if x["change24"] > 15:
                x["reasons"].append("already +15% 24h")

        if x["overlap"] >= 2:
            x["score"] += min(12, x["overlap"] * 3)
            x["reasons"].append(f"wallet overlap {x['overlap']}")

    save_history(history)
    signals.sort(key=lambda x: (x["score"], x["buy_usd"], x["buy_count"]), reverse=True)

    lines = [
        "🐋 FIL BEFORE PUMP — GMGN SMART MONEY LAYER",
        "",
        "Priority: Smart Money / whale activity before technical confirmation.",
        "Source: GMGN Smart Money feed (read-only).",
        "No trade execution is enabled.",
        ""
    ]

    for x in signals[:25]:
        rank = f" #{x['rank']}" if x.get("rank") else ""
        move = f" | 24h {x['change24']:+.1f}% | 7d {x['change7']:+.1f}%" if x.get("change24") is not None else ""
        wallets = ", ".join(w[:8] + "…" for w in x["wallets"][:4])
        lines.append(
            f"🔹 {x['symbol']} [{x['chain']}] {rank}\n"
            f"Score {x['score']} | SM buys {x['buy_count']} | wallets {len(x['wallets'])} | "
            f"buy volume {x['buy_usd']:,.0f}{move}\n"
            f"Reasons: {', '.join(x['reasons'][:8])}\n"
            f"Wallets: {wallets}"
        )

    send_telegram("\n\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
