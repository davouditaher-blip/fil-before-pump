"""Paper-trade feedback bridge for Fil Before Pump.

Aggregates closed PAPER_ONLY outcomes by wallet-evidence signature. This is
observational feedback only: it never changes live execution settings and
never places orders. The artifact is intended to become an input to later
wallet-quality calibration once the sample is large enough.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PAPER = Path("paper_trades.json")
OUT = Path("wallet_paper_feedback.json")
MIN_SAMPLE = 5


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def main():
    if PAPER.exists():
        try:
            data = json.loads(PAPER.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}

    closed = data.get("closed") if isinstance(data, dict) else []
    closed = closed if isinstance(closed, list) else []

    groups = defaultdict(list)
    for trade in closed:
        if not isinstance(trade, dict):
            continue
        symbol = str(trade.get("symbol") or "").upper()
        if not symbol:
            continue
        # Wallet evidence is deliberately kept separate from price outcome.
        signature = (
            int(trade.get("wallet_proven") or 0) > 0,
            int(trade.get("wallet_shared") or 0) > 0,
            num(trade.get("wallet_conviction_score")) >= 18,
        )
        groups[signature].append(trade)

    rows = []
    for signature, trades in groups.items():
        pnls = [num(t.get("pnl_pct")) for t in trades]
        wins = sum(p > 0 for p in pnls)
        n = len(pnls)
        rows.append({
            "evidence_signature": {
                "proven_wallet": signature[0],
                "shared_wallet": signature[1],
                "high_conviction": signature[2],
            },
            "closed_trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate_pct": round(wins / n * 100.0, 2) if n else None,
            "avg_pnl_pct": round(sum(pnls) / n, 4) if n else None,
            "sample_status": "MEASURABLE" if n >= MIN_SAMPLE else "INSUFFICIENT_SAMPLE",
        })

    rows.sort(key=lambda x: x["closed_trades"], reverse=True)
    result = {
        "mode": "PAPER_ONLY",
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "closed_trade_count": len(closed),
        "groups": rows,
        "minimum_group_sample": MIN_SAMPLE,
        "note": "Feedback is descriptive; it does not automatically modify wallet scores.",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
