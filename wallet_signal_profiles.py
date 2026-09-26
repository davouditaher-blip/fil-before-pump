"""Long-term Signal Wallet profiles for Fil Before Pump.

Descriptive layer only. It records long-term wallet performance, pre-pump
timing windows, current activity, and data coverage. It does not authorize
trades and never places orders.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUALITY = Path("wallet_quality.json")
RADAR = Path("wallet_radar.json")
OUT = Path("wallet_signal_profiles.json")

def load(path: Path, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default

def profile_wallet(wallet: str, q: dict[str, Any], radar: dict[str, Any]):
    proof = q.get("pre_pump_proof") or {}
    rates = proof.get("hit_rates") or {}
    timing = {}
    for window in ("6", "12", "24"):
        row = rates.get(window) or {}
        timing[f"{window}h"] = {
            "10pct_rate": row.get("10"),
            "20pct_rate": row.get("20"),
            "30pct_rate": row.get("30"),
        }
    observations = proof.get("observations") or []
    timestamps = [
        int(x.get("timestamp"))
        for x in observations
        if str(x.get("timestamp", "")).isdigit()
    ]
    radar_row = radar.get(wallet) or {}
    return {
        "wallet": wallet,
        "profile_type": "SIGNAL_WALLET_CANDIDATE",
        "quality_score": q.get("quality_score"),
        "quality_tier": q.get("quality_tier"),
        "qualified_buy_usd": q.get("qualified_buy_usd"),
        "qualified_sell_usd": q.get("qualified_sell_usd"),
        "asset_count": len(q.get("assets") or []),
        "active_asset_count": len(q.get("active_assets") or []),
        "forward_14d_hit_rate": q.get("forward_14d_hit_rate"),
        "forward_14d_attempts": q.get("forward_14d_attempts"),
        "pre_pump_first_entry_rate": q.get("pre_pump_first_entry_rate"),
        "pre_pump_24h_10pct_rate": q.get("pre_pump_24h_10pct_rate"),
        "pre_pump_observations": len(observations),
        "entry_timing_windows": timing,
        "observation_start": min(timestamps) if timestamps else None,
        "observation_end": max(timestamps) if timestamps else None,
        "active_assets": q.get("active_assets") or [],
        "radar_position_states": radar_row.get("position_states") or {},
        "radar_performance": radar_row.get("performance") or {},
        "paper_feedback_calibration": 0.0,
    }

def main():
    quality = load(QUALITY, {})
    radar = load(RADAR, {})
    profiles = {}
    for wallet, q in quality.items():
        if isinstance(q, dict):
            profiles[wallet] = profile_wallet(wallet, q, radar)
    result = {
        "mode": "DESCRIPTIVE_READ_ONLY",
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile_count": len(profiles),
        "profiles": profiles,
        "note": "Profiles describe long-term wallet behavior and entry timing; they do not independently authorize trades.",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"profile_count": len(profiles), "mode": result["mode"]}, ensure_ascii=False))

if __name__ == "__main__":
    main()
