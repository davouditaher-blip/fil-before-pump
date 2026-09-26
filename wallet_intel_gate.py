"""Wallet-intelligence integration gate for Fil Before Pump.

Connects the long-term signal-wallet profile layer and the bounded paper
calibration memory to the wallet-first decision path.

Why this layer exists
---------------------
``wallet_signal_profiles.json`` and ``wallet_performance_memory.json`` were both
produced by the pipeline and validated for shape, but no decision path read
their contents. Long-term wallet history is priority 3 of the signal priority
declared in AGENTS.md::

    Smart Money -> shared/common wallets -> wallet history -> exit/distribution
    -> whale activity -> volume

so a wallet with a deep, provably productive record contributed nothing to the
final scoring, readiness or risk gate unless it also happened to appear in the
current Radar, Cluster, Quality or GMGN snapshot.

Guarantees
----------
- Read-only. This module never places an order, never enables exchange
  execution and never writes an artifact.
- Deduplicated. Profile evidence is emitted as wallet rows using the same
  identity contract as the other layers, so the caller merges it into one
  deduplicated pool and a wallet is never rewarded twice.
- Bounded. Every contribution is capped, and the caller keeps its own cap.
- Honest. Thresholds are descriptive statistics of the observed provider data
  that is actually present in the artifact. Nothing is fabricated, and a
  missing, empty or malformed artifact simply contributes nothing.

The calibration lookup uses the conviction score *before* calibration is added.
The evidence signature recorded by ``wallet_paper_feedback.py`` is derived from
the final stored plan score, so this deliberately avoids a circular dependency
between the score and the prior that adjusts it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROFILES_FILE = Path("wallet_signal_profiles.json")
MEMORY_FILE = Path("wallet_performance_memory.json")

# Depth floor for trusting a long-term wallet record. Observed provider data
# has a median of roughly 22 observations per tracked wallet, so 10 separates a
# wallet with a real history from a wallet seen once or twice.
MIN_PROFILE_OBSERVATIONS = 10

# Observed forward 14d >=10% expansion rates sit well below these values
# (median near 9%), so reaching either bound is genuinely informative.
LONG_TERM_HIT_RATE_PCT = 20.0
PRE_PUMP_24H_RATE_PCT = 5.0

# Bounded long-term profile contribution to the wallet conviction bonus.
PROFILE_WALLET_WEIGHT = 2.0
PROFILE_SCORE_CAP = 12.0
# Observation depth that counts as full reliability.
PROFILE_DEPTH_OBSERVATIONS = 40
# Observed outcome rate that counts as full reliability.
PROFILE_OUTCOME_RATE_PCT = 25.0

# Fallback bound; the real bound is read from the memory artifact.
DEFAULT_MAX_CALIBRATION_BONUS = 5.0

# Position states that describe a wallet as no longer accumulating.
DISTRIBUTION_STATES = {"exited", "trimmed"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def wallet_identity(wallet: Any, chain: Any = None) -> str:
    """Return a stable wallet identity while preserving chain when known."""
    wallet = str(wallet or "").strip().lower()
    chain = str(chain or "").strip().lower()
    return f"{chain}:{wallet}" if wallet and chain else wallet


def load_signal_profiles() -> dict[str, Any]:
    """Load the descriptive long-term profile artifact.

    Returns an empty mapping when the artifact is missing or malformed, so an
    absent layer degrades to "no evidence" instead of failing the scan.
    """
    return _load_profiles(PROFILES_FILE)


def load_performance_memory() -> dict[str, Any]:
    """Load the bounded paper calibration memory artifact."""
    if not MEMORY_FILE.exists():
        return {}
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_profiles(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # A descriptive artifact is only usable while it stays read-only.
    if data.get("orders_enabled") is not False:
        return {}
    profiles = data.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def profile_observations(profile: dict[str, Any]) -> int:
    """Observed provider-labelled entries behind a long-term wallet record."""
    return max(
        _int(profile.get("pre_pump_observations")),
        _int(profile.get("forward_14d_attempts")),
    )


def is_long_term_proven(profile: dict[str, Any]) -> bool:
    """Report whether a wallet record is both deep and productive.

    Depth is ``MIN_PROFILE_OBSERVATIONS`` observed entries. Productivity is an
    observed forward 14d >=10% expansion rate of at least
    ``LONG_TERM_HIT_RATE_PCT``, or an observed pre-pump 24h >=10% rate of at
    least ``PRE_PUMP_24H_RATE_PCT``. Every value is read from the artifact.
    """
    if profile_observations(profile) < MIN_PROFILE_OBSERVATIONS:
        return False
    if _num(profile.get("forward_14d_hit_rate")) >= LONG_TERM_HIT_RATE_PCT:
        return True
    return _num(profile.get("pre_pump_24h_10pct_rate")) >= PRE_PUMP_24H_RATE_PCT


def profile_reliability(profile: dict[str, Any]) -> float:
    """Bounded 0..1 confidence from observation depth and observed outcome."""
    depth = min(1.0, profile_observations(profile) / float(PROFILE_DEPTH_OBSERVATIONS))
    outcome = max(
        _num(profile.get("forward_14d_hit_rate")),
        _num(profile.get("pre_pump_24h_10pct_rate")),
    ) / float(PROFILE_OUTCOME_RATE_PCT)
    return round(max(0.0, min(1.0, depth * min(1.0, outcome))), 4)


def _profile_symbols(profile: dict[str, Any]) -> set[str]:
    symbols = {str(a).upper() for a in (profile.get("active_assets") or [])}
    states = profile.get("radar_position_states")
    if isinstance(states, dict):
        symbols |= {str(a).upper() for a in states}
    return {s for s in symbols if s}


def build_profile_index(profiles: dict[str, Any]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Group profile records by asset so each candidate lookup stays cheap."""
    index: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for wallet, profile in (profiles or {}).items():
        if not isinstance(profile, dict):
            continue
        for symbol in _profile_symbols(profile):
            index.setdefault(symbol, []).append((str(wallet), profile))
    return index


def rows_for_symbol(
    symbol: str,
    entries: list[tuple[str, dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Normalize long-term profile records into wallet rows for one asset.

    The row contract matches the Radar, Cluster and Quality layers so the
    caller can merge every source into a single deduplicated wallet pool.
    """
    target = str(symbol or "").upper()
    if not target or not entries:
        return []
    rows = []
    for wallet, profile in entries:
        if not isinstance(profile, dict):
            continue
        states = profile.get("radar_position_states")
        states = states if isinstance(states, dict) else {}
        active_assets = {str(a).upper() for a in (profile.get("active_assets") or [])}
        proven = is_long_term_proven(profile)
        state = str(states.get(target) or ("holding" if target in active_assets else "unknown"))
        address = str(profile.get("wallet") or wallet)
        rows.append({
            "wallet": address,
            "chain": profile.get("chain"),
            "identity": wallet_identity(address, profile.get("chain")),
            "active": state not in DISTRIBUTION_STATES,
            "proven": proven,
            "pre_pump_first_entry_rate": profile.get("pre_pump_first_entry_rate"),
            "pre_pump_24h_10pct_rate": profile.get("pre_pump_24h_10pct_rate"),
            "pre_pump_proof_attempts": profile_observations(profile),
            "reliability": profile_reliability(profile),
            "long_term_proven": proven,
            "profile_state": state,
            "quality_score": profile.get("quality_score"),
            "quality_tier": profile.get("quality_tier"),
            "active_asset_count": _int(profile.get("active_asset_count")),
            "qualified_buy_usd": _num(profile.get("qualified_buy_usd")),
            "entry_timing_windows": profile.get("entry_timing_windows") or {},
            "sources": ["profile"],
        })
    rows.sort(
        key=lambda r: (
            r["long_term_proven"],
            r["reliability"],
            r["pre_pump_proof_attempts"],
            r["wallet"],
        ),
        reverse=True,
    )
    return rows


def profile_wallet_rows(symbol: str, profiles: dict[str, Any]) -> list[dict[str, Any]]:
    """Convenience lookup that scans every profile for one asset."""
    return rows_for_symbol(symbol, build_profile_index(profiles).get(str(symbol or "").upper()))


def profile_component(rows: list[dict[str, Any]] | None) -> float:
    """Bounded long-term profile contribution to the wallet conviction bonus."""
    total = 0.0
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("long_term_proven"):
            continue
        if str(row.get("profile_state") or "") in DISTRIBUTION_STATES:
            continue
        total += PROFILE_WALLET_WEIGHT * _num(row.get("reliability"))
    return round(min(PROFILE_SCORE_CAP, total), 2)


def calibration_signature(
    proven_wallet: bool,
    shared_wallet: bool,
    high_conviction: bool,
) -> dict[str, bool]:
    """Build the evidence signature used by the paper feedback bridge."""
    return {
        "proven_wallet": bool(proven_wallet),
        "shared_wallet": bool(shared_wallet),
        "high_conviction": bool(high_conviction),
    }


def memory_key(signature: dict[str, Any]) -> str:
    """Reproduce the memory key published by wallet_performance_memory.py."""
    return "|".join(
        f"{name}={1 if signature.get(name) else 0}"
        for name in ("proven_wallet", "shared_wallet", "high_conviction")
    )


def lookup_calibration(
    memory: dict[str, Any],
    signature: dict[str, Any],
) -> tuple[float, str]:
    """Return ``(bonus, status)`` for an evidence signature.

    The bonus is advisory and always zero unless the artifact is paper-only and
    the matching sample is measurable, so paper history can calibrate the
    wallet gate without ever dominating live wallet evidence.
    """
    if not isinstance(memory, dict) or not memory:
        return 0.0, "UNAVAILABLE"
    if memory.get("orders_enabled") is not False:
        return 0.0, "UNAVAILABLE"
    if str(memory.get("mode") or "") != "PAPER_ONLY":
        return 0.0, "UNAVAILABLE"
    rows = {
        memory_key(row.get("evidence_signature") or {}): row
        for row in (memory.get("memory") or [])
        if isinstance(row, dict)
    }
    row = rows.get(memory_key(signature))
    if not row:
        return 0.0, "NO_HISTORY"
    status = str(row.get("sample_status") or "INSUFFICIENT_SAMPLE")
    if status != "MEASURABLE":
        return 0.0, status
    cap = _num(memory.get("max_calibration_bonus"), DEFAULT_MAX_CALIBRATION_BONUS)
    cap = max(0.0, min(DEFAULT_MAX_CALIBRATION_BONUS, cap))
    return round(max(-cap, min(cap, _num(row.get("calibration_bonus")))), 2), status


def meaningful_wallet_intel(result: dict[str, Any]) -> bool:
    """Report whether long-term wallet evidence alone is substantive.

    Used by the fresh-volume guard so a severe 48h volume contraction cannot
    delete a candidate that is backed by a proven long-term wallet record. This
    mirrors the existing GMGN smart-money override: it protects a candidate and
    never rejects one.
    """
    proven = _int(result.get("wallet_longterm_proven_count"))
    active = _int(result.get("wallet_unique_active_count"))
    if proven >= 1 and active >= 1:
        return True
    return _num(result.get("wallet_profile_score")) >= PROFILE_WALLET_WEIGHT * 3


if __name__ == "__main__":
    loaded = load_signal_profiles()
    index = build_profile_index(loaded)
    print(json.dumps({
        "mode": "READ_ONLY_WALLET_INTEL",
        "orders_enabled": False,
        "profile_count": len(loaded),
        "tracked_assets": len(index),
        "min_profile_observations": MIN_PROFILE_OBSERVATIONS,
        "profile_score_cap": PROFILE_SCORE_CAP,
        "note": "Diagnostic only; scoring happens in scanner.wallet_conviction_signals.",
    }, ensure_ascii=False, indent=2))
