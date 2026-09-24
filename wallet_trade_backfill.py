"""Backfill one wallet's real GMGN buy/sell activity.

Research-only: no trade execution. Stores raw GMGN activity plus a normalized
trade archive so later analysis can calculate entry -> future Kline multiple.
"""
import json
import os
import subprocess
import time
from pathlib import Path

CHAINS = ("sol", "bsc", "base", "eth")
WALLET = os.environ.get(
    "BACKFILL_WALLET",
    "0x7bf30399dba8051c2bf191afe2cd07ea4e064625",
)
LIMIT = int(os.environ.get("GMGN_ACTIVITY_LIMIT", "200"))
OUT = Path("wallet_archive/raw/gmgn/activity")
OUT.mkdir(parents=True, exist_ok=True)


def run_cli(chain):
    env = os.environ.copy()
    cmd = [
        "npx", "--yes", "gmgn-cli",
        "portfolio", "activity",
        "--chain", chain,
        "--wallet", WALLET,
        "--limit", str(LIMIT),
        "--type", "buy",
        "--type", "sell",
        "--raw",
    ]
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    print(f"chain={chain} returncode={p.returncode}")
    if p.returncode != 0:
        print(p.stderr[-1500:])
        return {}
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return {}


def normalize(row, chain):
    token = row.get("token") or {}
    side = str(row.get("event_type") or row.get("side") or "").lower()
    address = (
        token.get("address")
        or row.get("base_address")
        or row.get("token_address")
        or row.get("address")
    )
    symbol = (
        token.get("symbol")
        or row.get("base_token_symbol")
        or row.get("symbol")
        or ""
    )
    return {
        "source": "gmgn",
        "chain": chain,
        "wallet": WALLET,
        "timestamp": row.get("timestamp") or row.get("trade_timestamp"),
        "side": side,
        "tx_hash": row.get("tx_hash") or row.get("transaction_hash"),
        "token_address": address,
        "symbol": str(symbol).upper(),
        "amount_usd": row.get("cost_usd") or row.get("buy_cost_usd") or row.get("amount_usd"),
        "price_usd": row.get("price_usd") or row.get("price"),
        "token_amount": row.get("token_amount"),
        "is_open_or_close": row.get("is_open_or_close"),
    }


all_trades = []
summary = {}

for chain in CHAINS:
    obj = run_cli(chain)
    rows = obj.get("list") or obj.get("data") or []
    if not isinstance(rows, list):
        rows = []

    raw_path = OUT / f"{chain}_{WALLET}.json"
    raw_path.write_text(json.dumps({
        "source": "gmgn",
        "chain": chain,
        "wallet": WALLET,
        "fetched_at": int(time.time()),
        "limit": LIMIT,
        "records": rows,
    }, ensure_ascii=False, indent=2) + "\n")

    normalized = [normalize(r, chain) for r in rows]
    normalized = [r for r in normalized if r["side"] in {"buy", "sell"}]
    all_trades.extend(normalized)
    summary[chain] = {
        "raw_rows": len(rows),
        "buy_rows": sum(r["side"] == "buy" for r in normalized),
        "sell_rows": sum(r["side"] == "sell" for r in normalized),
    }
    print(f"{chain}: raw={len(rows)} normalized={len(normalized)}")

all_trades.sort(key=lambda r: int(r.get("timestamp") or 0))
combined = OUT / f"{WALLET}.json"
combined.write_text(json.dumps({
    "schema_version": 1,
    "source": "gmgn",
    "wallet": WALLET,
    "chains": list(CHAINS),
    "fetched_at": int(time.time()),
    "summary": summary,
    "records": all_trades,
}, ensure_ascii=False, indent=2) + "\n")

qualified = [
    r for r in all_trades
    if r["side"] == "buy"
    and float(r.get("amount_usd") or 0) >= 5000
]

print("=== BACKFILL RESULT ===")
print(f"WALLET={WALLET}")
print(f"TOTAL_TRADES={len(all_trades)}")
print(f"QUALIFIED_BUYS_GE_5000={len(qualified)}")
print(f"ARCHIVE={combined}")
print(json.dumps(summary, ensure_ascii=False))
