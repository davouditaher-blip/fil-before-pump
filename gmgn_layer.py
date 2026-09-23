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
    """Persist one record per GMGN transaction and keep its latest price_change.

    GMGN's price_change is the current-price / entry-price ratio. That lets us
    measure whether a wallet's earlier buy subsequently became a 2x+ move.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    for t in trades:
        wallet = t.get("maker")
        address = t.get("base_address")
        tx = t.get("transaction_hash") or t.get("id")
        if not wallet or not address:
            continue

        trade_ts = int(t.get("timestamp") or now)
        symbol = ((t.get("base_token") or {}).get("symbol") or "?").upper()
        row = {
            "timestamp": now,
            "trade_timestamp": trade_ts,
            "transaction_hash": tx or "",
            "chain": t.get("chain") or "",
            "address": address,
            "symbol": symbol,
            "side": str(t.get("side") or "").lower(),
            "amount_usd": float(t.get("amount_usd") or 0),
            "price_usd": float(t.get("price_usd") or 0),
            "price_change": float(t.get("price_change") or 0),
            "is_open_or_close": t.get("is_open_or_close"),
            "maker_tags": (t.get("maker_info") or {}).get("tags") or [],
        }
        bucket = history.setdefault(wallet, [])

        # Update the same transaction instead of appending a duplicate every
        # 30 minutes. This is important because price_change evolves over time.
        match = None
        if tx:
            for existing in bucket:
                if existing.get("transaction_hash") == tx:
                    match = existing
                    break
        if match is None:
            for existing in bucket:
                if (
                    existing.get("trade_timestamp") == trade_ts
                    and existing.get("chain") == row["chain"]
                    and existing.get("address") == address
                    and existing.get("side") == row["side"]
                    and abs(float(existing.get("amount_usd") or 0) - row["amount_usd"]) < 0.01
                ):
                    match = existing
                    break
        if match is None:
            bucket.append(row)
        else:
            match.update(row)

        history[wallet] = sorted(
            bucket,
            key=lambda x: int(x.get("trade_timestamp") or x.get("timestamp") or 0)
        )[-1000:]


def cross_asset_wallets(history, address, chain):
    out = []
    for wallet, rows in history.items():
        assets = {(r.get("chain"), r.get("address")) for r in rows if r.get("address")}
        if (chain, address) in assets and len(assets) >= 2:
            out.append(wallet)
    return out


def wallet_track_profile(history, wallet, current_symbol, current_trade_ts):
    """Measure whether this wallet previously bought other coins before large moves.

    A prior buy is considered a proven pre-pump example when GMGN currently
    reports price_change >= 2.0 (the asset reached at least 2x the entry).
    The 'recent' subset is limited to 1-14 days old, matching the user's
    pre-pump window. No prediction is made from this history alone.
    """
    rows = history.get(wallet, [])
    prior = []
    for r in rows:
        if str(r.get("side") or "").lower() != "buy":
            continue
        if str(r.get("symbol") or "").upper() == str(current_symbol or "").upper():
            continue
        ts = int(r.get("trade_timestamp") or r.get("timestamp") or 0)
        if not ts or ts >= int(current_trade_ts or 0):
            continue
        prior.append(r)

    now = int(datetime.now(timezone.utc).timestamp())
    winners = []
    recent_winners = []
    for r in prior:
        multiple = float(r.get("price_change") or 0)
        if multiple < 2.0:
            continue
        age_days = max(0.0, (now - int(r.get("trade_timestamp") or r.get("timestamp") or now)) / 86400.0)
        item = {
            "symbol": r.get("symbol"),
            "multiple": multiple,
            "age_days": round(age_days, 1),
            "timestamp": int(r.get("trade_timestamp") or r.get("timestamp") or 0),
        }
        winners.append(item)
        if 1.0 <= age_days <= 14.0:
            recent_winners.append(item)

    total_prior = len(prior)
    win_rate = (len(winners) / total_prior) if total_prior else 0.0
    return {
        "wallet": wallet,
        "prior_buys": total_prior,
        "successful_prior_buys": len(winners),
        "recent_successful_buys": len(recent_winners),
        "win_rate": round(win_rate * 100, 1),
        "recent_examples": sorted(recent_winners, key=lambda x: x["multiple"], reverse=True)[:5],
        "proven": bool(winners) or bool(recent_winners),
    }


def current_wallet_entry(history, wallet, symbol, trade_ts):
    rows = history.get(wallet, [])
    matches = [
        r for r in rows
        if str(r.get("side") or "").lower() == "buy"
        and str(r.get("symbol") or "").upper() == str(symbol or "").upper()
        and int(r.get("trade_timestamp") or r.get("timestamp") or 0) == int(trade_ts or 0)
    ]
    if not matches:
        return {}
    return max(matches, key=lambda r: float(r.get("price_change") or 0))


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

    tracked_signals = []
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

        # Follow the wallets, not the coin: score only current buyers whose
        # earlier buys subsequently reached 2x+. Current entry must still be
        # early (GMGN entry-to-now <= 15% and CMC 24h <= 8%).
        proven_wallets = []
        for wallet in x.get("wallets") or []:
            wallet_rows = [
                r for r in history.get(wallet, [])
                if str(r.get("side") or "").lower() == "buy"
                and str(r.get("symbol") or "").upper() == str(x["symbol"] or "").upper()
            ]
            if not wallet_rows:
                continue
            current_entry = max(
                wallet_rows,
                key=lambda r: int(r.get("trade_timestamp") or r.get("timestamp") or 0)
            )
            current_trade_ts = int(current_entry.get("trade_timestamp") or current_entry.get("timestamp") or 0)
            profile = wallet_track_profile(history, wallet, x["symbol"], current_trade_ts)
            entry = current_wallet_entry(history, wallet, x["symbol"], current_trade_ts)
            current_multiple = float(entry.get("price_change") or 0)
            if profile["proven"] and (current_multiple <= 1.15 or current_multiple <= 0):
                proven_wallets.append({
                    **profile,
                    "current_multiple": current_multiple,
                })

        x["proven_wallets"] = proven_wallets
        x["proven_wallet_count"] = len(proven_wallets)
        x["wallet_track_signal"] = bool(proven_wallets)

        if x.get("change24", 0) <= 8 and x["buy_count"]:
            x["score"] += 5
            x["reasons"].append("price still early")
        if x.get("change24", 0) > 15:
            x["reasons"].append("already +15% 24h")

        if x["overlap"] >= 2:
            x["score"] += min(12, x["overlap"] * 3)
            x["reasons"].append(f"wallet overlap {x['overlap']}")

        if x["proven_wallet_count"] >= 2:
            x["score"] += min(20, 10 + 5 * x["proven_wallet_count"])
            x["reasons"].append(f"proven pre-pump wallets {x['proven_wallet_count']}")
        elif x["proven_wallet_count"] == 1:
            x["score"] += 10
            x["reasons"].append("proven pre-pump wallet")
        elif x.get("buy_count"):
            x["reasons"].append("smart-money buy, historical proof not yet established")

        # This is the actual Fil wallet-tracking gate. A coin is surfaced by
        # the wallet-copy layer only when a currently buying wallet has a
        # documented prior 2x+ entry and the current entry is still early.
        if x["wallet_track_signal"] and x.get("change24", 0) <= 8:
            tracked_signals.append(x)

    save_history(history)

    # The wallet-track layer is primary. Fall back to the broader Smart Money
    # feed only when no proven-wallet early entries are available, so the bot
    # never goes silent during a cold-start history period.
    if tracked_signals:
        signals = tracked_signals
    else:
        signals = [x for x in signals if x.get("buy_count")]
        for x in signals:
            x["reasons"].append("cold-start / no proven wallet history yet")

    signals.sort(
        key=lambda x: (
            x.get("wallet_track_signal", False),
            x.get("proven_wallet_count", 0),
            x.get("score", 0),
            x.get("buy_usd", 0),
            x.get("buy_count", 0),
        ),
        reverse=True,
    )

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
        proven_text = []
        for w in x.get("proven_wallets", [])[:4]:
            examples = ", ".join(
                f"{e['symbol']} {e['multiple']:.1f}x"
                for e in w.get("recent_examples", [])[:2]
            )
            proven_text.append(
                f"{w['wallet'][:8]}… | سابقه {w['successful_prior_buys']}/{w['prior_buys']} "
                f"({w['win_rate']:.0f}%)"
                + (f" | نمونه: {examples}" if examples else "")
            )
        track = "🎯 ردپای ولت معتبر" if x.get("wallet_track_signal") else "🧠 Smart Money"
        lines.append(
            f"🔹 {x['symbol']} [{x['chain']}] {rank}\n"
            f"{track} | Score {x['score']} | SM buys {x['buy_count']} | wallets {len(x['wallets'])} | "
            f"buy volume {x['buy_usd']:,.0f}{move}\n"
            f"💡 ولت‌های دارای سابقه پامپ: {x.get('proven_wallet_count', 0)}\n"
            + (f"👛 {chr(10).join(proven_text)}\n" if proven_text else "")
            + f"Reasons: {', '.join(x['reasons'][:8])}\n"
            + f"Wallets: {wallets}"
        )

    send_telegram("\n\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
