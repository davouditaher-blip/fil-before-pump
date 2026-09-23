"""Wallet Quality Engine v1 (offline/research layer)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

RADAR_FILE = Path("wallet_radar.json")
GMGN_HISTORY_FILE = Path("gmgn_wallet_history.json")
THRESHOLD_USD = 5000.0


def _num(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _load(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_sources(radar_path: Path = RADAR_FILE, history_path: Path = GMGN_HISTORY_FILE):
    radar = _load(radar_path)
    history = _load(history_path)
    return radar if isinstance(radar, dict) else {}, history if isinstance(history, dict) else {}


def _history_rows(history: dict[str, Any], wallet: str):
    rows = history.get(wallet, [])
    return [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []


def historical_metrics(history: dict[str, Any], wallet: str) -> dict[str, Any]:
    rows = _history_rows(history, wallet)
    buys = [
        r for r in rows
        if str(r.get("side", "")).lower() == "buy"
        and (_num(r.get("amount_usd")) or 0) >= THRESHOLD_USD
    ]

    observed, successes, multiples = [], [], []
    for row in buys:
        peak = _num(row.get("peak_multiple"))
        if peak is None:
            peak = _num(row.get("price_change"))
        if peak is None:
            continue
        observed.append(row)
        multiples.append(peak)
        if peak >= 2.0:
            successes.append(row)

    win_rate = len(successes) / len(observed) * 100.0 if observed else None
    return {
        "qualified_historical_buys": len(buys),
        "observed_entries": len(observed),
        "successful_2x_entries": len(successes),
        "pre_pump_win_rate": round(win_rate, 2) if win_rate is not None else None,
        "average_peak_multiple": round(mean(multiples), 3) if multiples else None,
        "best_peak_multiple": round(max(multiples), 3) if multiples else None,
    }


def _score_quality(metrics: dict[str, Any], qualified_buy_usd: float) -> float:
    score = 0.0
    attempts = metrics["observed_entries"]
    wins = metrics["successful_2x_entries"]
    win_rate = metrics["pre_pump_win_rate"]

    score += min(20.0, attempts * 2.0)
    score += min(30.0, wins * 6.0)
    if win_rate is not None:
        score += min(25.0, max(0.0, win_rate) * 0.25)

    if qualified_buy_usd >= 50000:
        score += 10
    elif qualified_buy_usd >= 20000:
        score += 7
    elif qualified_buy_usd >= 10000:
        score += 4
    elif qualified_buy_usd >= THRESHOLD_USD:
        score += 2

    return round(min(100.0, score), 1)


def quality_label(score: float, observed_entries: int) -> str:
    if observed_entries < 2:
        return "INSUFFICIENT_DATA"
    if score >= 70:
        return "HIGH"
    if score >= 45:
        return "MEDIUM"
    return "LOW"


def build_wallet_quality(radar: dict[str, Any], history: dict[str, Any]):
    result = []
    for _, row in radar.items():
        if not isinstance(row, dict):
            continue
        wallet = str(row.get("wallet") or "")
        if not wallet:
            continue

        qualified_buy_usd = _num(row.get("qualified_buy_usd")) or 0.0
        if qualified_buy_usd < THRESHOLD_USD:
            continue

        metrics = historical_metrics(history, wallet)
        score = _score_quality(metrics, qualified_buy_usd)
        result.append({
            "wallet": wallet,
            "chain": row.get("chain"),
            "qualified_buy_count": row.get("qualified_buy_count", 0),
            "qualified_buy_usd": round(qualified_buy_usd, 2),
            "historical": metrics,
            "quality_score": score,
            "quality_label": quality_label(score, metrics["observed_entries"]),
            "current_position_states": row.get("position_states", {}),
        })

    result.sort(
        key=lambda x: (
            x["quality_score"],
            x["historical"]["successful_2x_entries"],
            x["qualified_buy_usd"],
        ),
        reverse=True,
    )
    return result


def run(radar_path: Path = RADAR_FILE, history_path: Path = GMGN_HISTORY_FILE):
    radar, history = load_sources(radar_path, history_path)
    wallets = build_wallet_quality(radar, history)
    output = {
        "version": "wallet-quality-v1",
        "threshold_usd": THRESHOLD_USD,
        "wallet_count": len(wallets),
        "wallets": wallets,
    }
    Path("wallet_quality.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return output


if __name__ == "__main__":
    output = run()
    print(f"Wallet Quality Engine v1 | wallets={output['wallet_count']}")
    for item in output["wallets"][:20]:
        h = item["historical"]
        wr = h["pre_pump_win_rate"]
        wr_text = f"{wr:.1f}%" if wr is not None else "N/A"
        print(
            f"{item['wallet'][:10]}... | {item['chain']} | "
            f"USD={item['qualified_buy_usd']:,.0f} | "
            f"2x={h['successful_2x_entries']}/{h['observed_entries']} "
            f"({wr_text}) | score={item['quality_score']} | {item['quality_label']}"
        )
