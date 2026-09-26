"""Wallet performance memory for Fil Before Pump.

Conservative paper-only calibration memory. It converts closed paper outcomes
into reusable evidence-signature priors. It never places orders and never
overrides live wallet evidence.

The memory is intentionally bounded:
- minimum sample before a signal is considered measurable
- capped score adjustment
- descriptive provenance retained for auditability
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FEEDBACK = Path("wallet_paper_feedback.json")
OUT = Path("wallet_performance_memory.json")
MIN_SAMPLE = 5
MAX_BONUS = 5.0
MIN_BONUS = -5.0


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _load() -> dict[str, Any]:
    if not FEEDBACK.exists():
        return {}
    try:
        raw = json.loads(FEEDBACK.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _key(signature: dict[str, Any]) -> str:
    return "|".join(
        f"{name}={1 if signature.get(name) else 0}"
        for name in ("proven_wallet", "shared_wallet", "high_conviction")
    )


def build() -> dict[str, Any]:
    feedback = _load()
    groups = feedback.get("groups") or []
    wallet_rows = feedback.get("wallets") or []
    memory = []
    wallet_memory = []

    for row in groups:
        if not isinstance(row, dict):
            continue
        signature = row.get("evidence_signature") or {}
        n = int(_num(row.get("closed_trades")))
        win_rate = row.get("win_rate_pct")
        avg_pnl = row.get("avg_pnl_pct")
        measurable = n >= MIN_SAMPLE and win_rate is not None

        # Center on 50% win rate and bound the effect so paper history can
        # calibrate, but never dominate fresh wallet evidence.
        raw_bonus = 0.0
        if measurable:
            raw_bonus = (float(win_rate) - 50.0) / 10.0
            if avg_pnl is not None:
                raw_bonus += max(-1.0, min(1.0, float(avg_pnl) / 5.0))
        bonus = round(max(MIN_BONUS, min(MAX_BONUS, raw_bonus)), 2) if measurable else 0.0

        memory.append({
            "memory_key": _key(signature),
            "evidence_signature": {
                "proven_wallet": bool(signature.get("proven_wallet")),
                "shared_wallet": bool(signature.get("shared_wallet")),
                "high_conviction": bool(signature.get("high_conviction")),
            },
            "closed_trades": n,
            "win_rate_pct": win_rate,
            "avg_pnl_pct": avg_pnl,
            "sample_status": "MEASURABLE" if measurable else "INSUFFICIENT_SAMPLE",
            "calibration_bonus": bonus,
        })

    for row in wallet_rows:
        if not isinstance(row, dict):
            continue
        n = int(_num(row.get("closed_trades")))
        win_rate = row.get("win_rate_pct")
        avg_pnl = row.get("avg_pnl_pct")
        measurable = n >= MIN_SAMPLE and win_rate is not None
        raw_bonus = 0.0
        if measurable:
            raw_bonus = (float(win_rate) - 50.0) / 10.0
            if avg_pnl is not None:
                raw_bonus += max(-1.0, min(1.0, float(avg_pnl) / 5.0))
        bonus = round(max(MIN_BONUS, min(MAX_BONUS, raw_bonus)), 2) if measurable else 0.0
        wallet_memory.append({
            "wallet": str(row.get("wallet") or ""),
            "closed_trades": n,
            "win_rate_pct": win_rate,
            "avg_pnl_pct": avg_pnl,
            "sample_status": "MEASURABLE" if measurable else "INSUFFICIENT_SAMPLE",
            "calibration_bonus": bonus,
        })

    memory.sort(key=lambda x: (x["sample_status"] == "MEASURABLE", x["closed_trades"]), reverse=True)
    wallet_memory.sort(key=lambda x: (x["sample_status"] == "MEASURABLE", x["closed_trades"]), reverse=True)
    result = {
        "mode": "PAPER_ONLY",
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "minimum_sample": MIN_SAMPLE,
        "max_calibration_bonus": MAX_BONUS,
        "memory": memory,
        "wallet_memory": wallet_memory,
        "note": "Paper outcomes calibrate evidence signatures and sufficiently sampled individual signal-wallet histories; live wallet evidence remains primary.",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
