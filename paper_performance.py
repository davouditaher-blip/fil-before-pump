"""Performance feedback for the wallet-first paper pipeline.

Read-only analytics: closed paper trades are attributed back to the wallet
signals captured at entry. No exchange orders are created.
"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone

STATE = Path("paper_trades.json")
OUT = Path("paper_performance.json")

def _num(v, d=0.0):
    try: return float(v)
    except (TypeError, ValueError): return d

def build():
    data = {}
    if STATE.exists():
        try: data = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception: data = {}
    closed = data.get("closed") or []
    wins = [x for x in closed if _num(x.get("pnl_pct")) > 0]
    losses = [x for x in closed if _num(x.get("pnl_pct")) <= 0]
    total = len(closed)
    result = {
        "mode": "PAPER_ONLY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "closed_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / total * 100, 2) if total else None,
        "avg_pnl_pct": round(sum(_num(x.get("pnl_pct")) for x in closed) / total, 4) if total else None,
        "avg_winner_pct": round(sum(_num(x.get("pnl_pct")) for x in wins) / len(wins), 4) if wins else None,
        "avg_loser_pct": round(sum(_num(x.get("pnl_pct")) for x in losses) / len(losses), 4) if losses else None,
        "wallet_evidence": {
            "proven_entry_trades": sum(1 for x in closed if int(x.get("wallet_proven") or 0) > 0),
            "shared_entry_trades": sum(1 for x in closed if int(x.get("wallet_shared") or 0) > 0),
            "high_conviction_entries": sum(1 for x in closed if _num(x.get("wallet_conviction_score")) >= 18),
        },
        "sample_status": "INSUFFICIENT_SAMPLE" if total < 20 else "MEASURABLE",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result

if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
