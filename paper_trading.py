"""Paper-trading simulator for Fil Before Pump.

This module is deliberately exchange-free. It consumes trade_readiness plans,
opens simulated LONG positions only for PAPER_READY candidates, and marks
exits using the configured risk envelope. It persists a compact audit trail
so later performance can feed Wallet Quality and the real execution engine.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILE = Path("paper_trades.json")
MAX_HISTORY = 500
MAX_OPEN = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _load() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"mode": "PAPER_ONLY", "open": [], "closed": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("mode", "PAPER_ONLY")
            data.setdefault("open", [])
            data.setdefault("closed", [])
            return data
    except Exception as exc:
        print(f"paper trading state warning: {exc}")
    return {"mode": "PAPER_ONLY", "open": [], "closed": []}


def _save(state: dict[str, Any]) -> None:
    state["closed"] = state.get("closed", [])[-MAX_HISTORY:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _close(position: dict[str, Any], exit_price: float, reason: str, at: str) -> None:
    entry = _num(position.get("entry_price"))
    if entry <= 0:
        return
    pnl_pct = ((exit_price - entry) / entry) * 100.0
    position.update(
        {
            "status": "CLOSED",
            "exit_price": round(exit_price, 12),
            "exit_reason": reason,
            "exit_time": at,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_multiple_of_risk": round(pnl_pct / 3.0, 4),
        }
    )


def update(plans: list[dict[str, Any]]) -> dict[str, Any]:
    state = _load()
    now = _now()
    by_symbol = {
        str(p.get("symbol") or "").upper(): p
        for p in plans
        if str(p.get("symbol") or "")
    }

    still_open = []
    for pos in state["open"]:
        symbol = str(pos.get("symbol") or "").upper()
        plan = by_symbol.get(symbol)
        current = _num((plan or {}).get("price_usd"))
        if current <= 0:
            pos["last_seen_at"] = now
            still_open.append(pos)
            continue

        entry = _num(pos.get("entry_price"))
        sl = _num(pos.get("stop_price"))
        tp1 = _num(pos.get("take_profit_1_price"))
        tp2 = _num(pos.get("take_profit_2_price"))
        pos["last_price"] = current
        pos["last_seen_at"] = now
        if entry > 0:
            move = ((current - entry) / entry) * 100.0
            pos["mfe_pct"] = max(_num(pos.get("mfe_pct")), move)
            pos["mae_pct"] = min(_num(pos.get("mae_pct")), move)

        reason = None
        if sl > 0 and current <= sl:
            reason = "STOP_LOSS"
        elif tp2 > 0 and current >= tp2:
            reason = "TAKE_PROFIT_2"
        elif tp1 > 0 and current >= tp1 and not pos.get("tp1_touched"):
            pos["tp1_touched"] = True
            pos["tp1_touched_at"] = now
            # Keep the paper position open; TP1 is a milestone, not a full exit.

        if reason:
            _close(pos, current, reason, now)
            state["closed"].append(pos)
        else:
            still_open.append(pos)

    state["open"] = still_open

    open_symbols = {str(p.get("symbol") or "").upper() for p in state["open"]}
    for plan in plans:
        if str(plan.get("state")) != "PAPER_READY":
            continue
        symbol = str(plan.get("symbol") or "").upper()
        entry = _num(plan.get("price_usd"))
        if not symbol or entry <= 0 or symbol in open_symbols:
            continue
        if len(state["open"]) >= MAX_OPEN:
            break

        risk = plan.get("risk") or {}
        sl_pct = abs(_num(risk.get("stop_loss_pct"), 3.0))
        tp1_pct = abs(_num(risk.get("take_profit_1_pct"), 6.0))
        tp2_pct = abs(_num(risk.get("take_profit_2_pct"), 10.0))
        position = {
            "id": f"PT-{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            "symbol": symbol,
            "rank": plan.get("rank"),
            "direction": "LONG_PAPER",
            "status": "OPEN",
            "entry_price": round(entry, 12),
            "entry_time": now,
            "stop_price": round(entry * (1 - sl_pct / 100.0), 12),
            "take_profit_1_price": round(entry * (1 + tp1_pct / 100.0), 12),
            "take_profit_2_price": round(entry * (1 + tp2_pct / 100.0), 12),
            "wallet_conviction_score": plan.get("wallet_conviction_score"),
            "wallet_active": plan.get("wallet_active"),
            "wallet_proven": plan.get("wallet_proven"),
            "wallet_shared": plan.get("wallet_shared"),
            "signal_wallets": plan.get("signal_wallets") or [],
            "wallet_exit_pressure": plan.get("wallet_exit_pressure"),
            "fil_confluence_score": plan.get("fil_confluence_score"),
            "leverage_cap": risk.get("leverage_cap", 3),
            "max_account_risk_pct": risk.get("max_account_risk_pct", 1.0),
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "tp1_touched": False,
            "created_at": now,
        }
        state["open"].append(position)
        open_symbols.add(symbol)

    state["updated_at"] = now
    state["summary"] = {
        "open_count": len(state["open"]),
        "closed_count": len(state["closed"]),
        "win_count": sum(_num(x.get("pnl_pct")) > 0 for x in state["closed"]),
        "loss_count": sum(_num(x.get("pnl_pct")) <= 0 for x in state["closed"]),
        "orders_enabled": False,
    }
    _save(state)
    return state
