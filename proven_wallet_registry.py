"""Minimal schema and interface for the PROVEN_WALLET_REGISTRY.

This file defines what a durable historical wallet record looks like. It is
deliberately an *interface*, not a data set:

* it is not populated with unverified wallets;
* it does not decide who is PROVEN. The ``proven_status`` field is copied from
  ``wallet_history_validation``'s existing forward-only criteria, which this
  module does not touch;
* it is not imported by the scanner, the quality engine, confluence, risk,
  trade readiness, or paper trading.

Why it exists
-------------
Every artifact in this repository is keyed off *current* activity. A wallet that
stops trading simply stops appearing, and the evidence that made it interesting
disappears with it. ``PROVEN`` computed from a live feed is therefore not
monotonic: the same wallet can be proven today and gone next week, and a
scanner cannot tell "this wallet stopped being good" apart from "this wallet
stopped being visible". That distinction is the whole reason a registry has to
survive the feed going quiet.

Registry semantics
------------------
* A registry entry is a *statement of record*, not a live signal. ``active_now``
  is metadata about visibility, never a filter on inclusion.
* ``merge_registry`` is additive and never deletes. A wallet that disappears
  from every current feed keeps its history, its evidence references and its
  ``last_seen``, and is marked ``active_now=False``. Losing a wallet because a
  provider stopped listing it would be the exact failure mode this prevents.
* Every metric that the available data cannot support is ``None``. A fabricated
  0.0 reads as "measured, and it was zero"; ``None`` reads as "not measured",
  which is the honest answer and the one a later reader needs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import historical_discovery as hd
import wallet_history_validation as whv
import wallet_quality_engine as wqe

SCHEMA_VERSION = 1

MODE = "PROVEN_WALLET_REGISTRY_READ_ONLY"

#: The metrics that do not exist in this repository, and are therefore ``None``
#: rather than invented. Documented here so a reader can tell an honest gap from
#: an oversight.
UNSUPPORTED_METRICS = {
    "drawdown": (
        "No daily equity curve is stored, so a true maximum drawdown cannot be "
        "computed. worst_mae_pct reports the deepest observed adverse excursion "
        "instead, which is a different and weaker statement."
    ),
    "average_lead_time": (
        "Only derivable for entries that recorded a post-entry peak; reported as "
        "None when no entry has one."
    ),
}

#: Semantic caveats on metrics that *are* reported. These are not gaps, they are
#: limits in what the number means, and misreading them is worse than a None.
METRIC_CAVEATS = {
    "mae_pct": (
        "wallet_history_validation computes MAE as the lowest price inside the "
        "observed post-entry window, not the true adverse excursion of a held "
        "position. When a wallet's only observation after entry is its exit, the "
        "trough and the peak are the same print and MAE comes out equal to MFE, "
        "positive. Treat this as 'lowest print seen inside the window', never as "
        "downside risk or drawdown. provenance.evidence.mfe_sample and "
        "mae_sample show the coverage."
    ),
    "realized_win_rate": (
        "Reported only when every reconstructed entry has a recorded exit. A "
        "partial set of exits would make this an optimistic sample, so it is "
        "reported as None instead."
    ),
    "win_rate": (
        "The existing 2x-target rate over observed entries. An entry whose window "
        "never completed is excluded from the denominator rather than counted as "
        "a loss."
    ),
}

#: The full entry contract. A missing key is a bug; a None value is a fact.
REGISTRY_FIELDS: tuple[str, ...] = (
    "wallet",
    "chains",
    "sources",
    "historical_trades",
    "qualified_trades",
    "win_rate",
    "realized_win_rate",
    "pre_pump_rate",
    "first_entry_rate",
    "average_lead_time",
    "mfe_pct",
    "mae_pct",
    "drawdown",
    "repeatability",
    "assets_traded",
    "active_now",
    "last_seen",
    "historical_quality_score",
    "proven_status",
    "provenance",
)

#: Proven states, reused verbatim from the existing classifier so the registry
#: cannot introduce a third vocabulary.
PROVEN_STATES = whv.CLASSES


def _mean(values: Sequence[Any]) -> float | None:
    """Mean of the values that exist, or ``None`` when there are none.

    A metric computed from zero observations is not 0; it is unknown.
    """
    usable = [float(v) for v in values if v is not None]
    if not usable:
        return None
    return round(sum(usable) / len(usable), 4)


def _quality_profile(wallet: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The existing quality-engine profile for one wallet, or None.

    Reused rather than reimplemented, so the registry reports the same quality
    score and pre-pump rates the engine already publishes. The engine is called
    for one wallet at a time; ``build_profiles`` scores each wallet from its own
    rows, so this equals the whole-dataset figure (asserted by a test).
    """
    if not rows:
        return None
    try:
        profiles = wqe.build_profiles({wallet: [dict(r) for r in rows]})
    except Exception:
        return None
    return profiles.get(wallet)


def build_entry(
    wallet: Any,
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    active_now: bool | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Build one registry entry from a wallet's unified historical record.

    ``active_now`` is supplied by the caller from whatever current feed exists.
    ``None`` means "not known", which is different from ``False`` and is the
    honest value when no live feed was consulted.
    """
    key = hd.wallet_key(wallet)
    clean = [dict(r) for r in (rows or []) if isinstance(r, Mapping)]

    # The existing classifier, unmodified. This is the only PROVEN source.
    profile = whv.reconstruct_wallet(key, clean) if key else None
    proven_status = profile["history_class"] if profile else whv.NO_HISTORY
    entries = profile["entries"] if profile else []

    observed = [e for e in entries if e["outcome"] != whv.OUTCOME_UNKNOWN]
    successes = [e for e in entries if e["outcome"] == whv.OUTCOME_SUCCESS]
    with_exit = [e for e in entries if e["exit_timestamp"] is not None]

    quality = _quality_profile(key, clean) if key else None

    # A realized win rate is only honest when every entry actually closed.
    realized: float | None = None
    if observed and len(with_exit) == len(entries):
        realized = round(
            sum(1 for e in with_exit if e.get("target_reached")) / len(with_exit) * 100, 1
        )

    # Repeatability: the share of distinct assets this wallet came back to.
    # A wallet that traded one token once is not a repeat trader, and a wallet
    # that re-entered twelve times is. This is a count over observed history,
    # not a quality judgement.
    per_asset_buys: dict[tuple, int] = {}
    for row in clean:
        if whv.event_side(row) != "buy":
            continue
        identity = whv.asset_identity(row)
        if identity is not None:
            per_asset_buys[identity] = per_asset_buys.get(identity, 0) + 1
    repeated = sum(1 for count in per_asset_buys.values() if count > 1)
    assets = len(per_asset_buys)
    repeatability = round(repeated / assets, 4) if assets else None

    chains = sorted({str(r.get("chain") or "").lower() for r in clean if r.get("chain")})
    sources = sorted({s for r in clean for s in hd.zh.provenance_of(r)})
    trades = [r for r in clean if r.get("kind") == hd.KIND_TRADE]
    qualified = [
        r for r in trades
        if (whv.event_usd(r) or 0.0) >= hd.QUALIFIED_TRADE_USD
    ]

    entry: dict[str, Any] = {
        "wallet": key,
        "chains": chains,
        "sources": sources,
        "historical_trades": len(trades),
        "qualified_trades": len(qualified),
        # The existing 2x-target win rate over observed entries only.
        "win_rate": profile["pre_pump_win_rate"] if profile else None,
        "realized_win_rate": realized,
        # The engine's own pre-pump rates, reused rather than redefined.
        "pre_pump_rate": (quality or {}).get("pre_pump_24h_10pct_rate"),
        "first_entry_rate": (quality or {}).get("pre_pump_first_entry_rate"),
        "average_lead_time": _mean(
            [e.get("days_to_peak") for e in entries if e.get("days_to_peak") is not None]
        ),
        "mfe_pct": _mean([e.get("mfe_pct") for e in entries]),
        "mae_pct": _mean([e.get("mae_pct") for e in entries]),
        # Not measurable from stored history. See UNSUPPORTED_METRICS.
        "drawdown": None,
        "repeatability": repeatability,
        "assets_traded": assets,
        "active_now": active_now,
        "last_seen": max((whv.event_ts(r) for r in clean), default=0) or None,
        "historical_quality_score": (quality or {}).get("quality_score"),
        "proven_status": proven_status,
        "provenance": {
            "sources": sources,
            "multi_source": len(sources) > 1,
            "corroborated_events": sum(
                1 for r in clean if hd.zh.has_corroboration(r)
            ),
            "evidence": {
                "reconstructed_entries": len(entries),
                "observed_entries": len(observed),
                "successful_entries": len(successes),
                "entries_with_exit": len(with_exit),
                "unknown_windows": sum(
                    1 for e in entries if e["outcome"] == whv.OUTCOME_UNKNOWN
                ),
                # How many entries actually carried an observation window. MFE and
                # MAE are means over these, so the sample size is the honest
                # limit on how much either number is worth.
                "mfe_sample": sum(1 for e in entries if e.get("mfe_pct") is not None),
                "mae_sample": sum(1 for e in entries if e.get("mae_pct") is not None),
                "criteria": profile["criteria"] if profile else None,
            },
            "metric_caveats": METRIC_CAVEATS,
        },
    }

    # Name the fields this wallet could not fill, so a reader sees a deliberate
    # gap instead of wondering whether a None was a bug.
    entry["provenance"]["unknown_metrics"] = sorted(
        field for field in REGISTRY_FIELDS
        if field in entry and entry[field] is None
    )
    return entry


def build_registry(
    history: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    *,
    current_wallets: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Build a registry envelope from a unified historical record.

    ``current_wallets`` is the set of wallets a live feed still shows. It only
    sets ``active_now``; it never removes an entry, because a wallet leaving the
    feed is exactly the case the registry has to survive.
    """
    visible = {hd.wallet_key(w) for w in (current_wallets or []) if hd.wallet_key(w)}
    known_feed = current_wallets is not None

    entries: dict[str, Any] = {}
    for wallet, rows in (history or {}).items():
        key = hd.wallet_key(wallet)
        if not key:
            continue
        entries[key] = build_entry(
            key, rows, active_now=(key in visible) if known_feed else None
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entry_fields": list(REGISTRY_FIELDS),
        "unsupported_metrics": {k: v for k, v in UNSUPPORTED_METRICS.items()},
        "metric_caveats": {k: v for k, v in METRIC_CAVEATS.items()},
        "proven_criteria_source": "wallet_history_validation.reconstruct_wallet",
        # Whether a current feed was actually consulted. Without this, a merge
        # cannot tell "this wallet left the feed" from "nobody looked", and would
        # either keep a stale active flag forever or wrongly retire every wallet.
        "current_feed_observed": known_feed,
        "note": (
            "Read-only statement of record. proven_status is copied from the "
            "existing forward-only criteria and is never set by volume. "
            "A wallet missing from a current feed keeps its entry and is marked "
            "active_now=False; it is never deleted."
        ),
        "wallet_count": len(entries),
        "proven_count": sum(
            1 for e in entries.values() if e["proven_status"] == whv.PROVEN
        ),
        "wallets": entries,
    }


def merge_registry(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge two registry envelopes without ever losing a wallet.

    A wallet only in ``existing`` is carried over untouched. That is the whole
    point: the historical record outlives any single provider's current feed, so
    a merge that only reflected ``incoming`` would quietly delete proven wallets
    the moment a feed went quiet.
    """
    old_entries = (existing or {}).get("wallets") or {}
    new_entries = (incoming or {}).get("wallets") or {}
    # Did this run actually look at a current feed? If it did, then a wallet it
    # did not mention has gone quiet, and carrying its previous active_now=True
    # forward would leave the registry claiming visibility that no longer exists.
    feed_observed = bool((incoming or {}).get("current_feed_observed"))

    merged: dict[str, Any] = {k: dict(v) for k, v in old_entries.items()}
    for wallet, entry in new_entries.items():
        prior = merged.get(wallet)
        if prior is None:
            merged[wallet] = dict(entry)
            continue
        # A wallet seen again is still in the record. Keep the richer of the two
        # metric sets rather than letting a sparse re-read blank a known score.
        combined = dict(entry)
        for field, value in prior.items():
            if combined.get(field) is None and value is not None:
                combined[field] = value
        # A wallet that is not in the new current feed is no longer active, but
        # it is still here. This is the disappearing-wallet case.
        if entry.get("active_now") is False and prior.get("active_now") is True:
            combined["active_now"] = False
        combined["last_seen"] = max(
            int(prior.get("last_seen") or 0), int(entry.get("last_seen") or 0)
        ) or None
        merged[wallet] = combined

    if feed_observed:
        for wallet in old_entries:
            if wallet not in new_entries:
                merged[wallet]["active_now"] = False

    base = dict(incoming or existing or {})
    base.update({
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entry_fields": list(REGISTRY_FIELDS),
        "unsupported_metrics": {k: v for k, v in UNSUPPORTED_METRICS.items()},
        "metric_caveats": {k: v for k, v in METRIC_CAVEATS.items()},
        "proven_criteria_source": "wallet_history_validation.reconstruct_wallet",
        "current_feed_observed": feed_observed,
        "wallet_count": len(merged),
        "proven_count": sum(
            1 for e in merged.values() if e.get("proven_status") == whv.PROVEN
        ),
        "wallets": merged,
    })
    return base


def save_registry(registry: Mapping[str, Any], path: Path | str) -> Path:
    """Write a registry envelope to disk."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def load_registry(path: Path | str) -> dict[str, Any]:
    """Read a registry envelope; a missing or malformed file yields no wallets."""
    target = Path(path)
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def validate_entry(entry: Mapping[str, Any]) -> list[str]:
    """Structural problems with one entry. Empty list means valid.

    Checks shape, not worth. It cannot tell a well-formed entry from a truthful
    one; it only guarantees a consumer can rely on the keys being present and
    on ``proven_status`` being one of the existing states.
    """
    problems: list[str] = []
    for field in REGISTRY_FIELDS:
        if field not in entry:
            problems.append(f"missing field: {field}")
    if entry.get("proven_status") not in PROVEN_STATES:
        problems.append(f"unknown proven_status: {entry.get('proven_status')!r}")
    if not entry.get("wallet"):
        problems.append("missing wallet")
    if entry.get("orders_enabled"):
        problems.append("registry must never enable execution")
    win_rate = entry.get("win_rate")
    if win_rate is not None and not 0.0 <= float(win_rate) <= 100.0:
        problems.append(f"win_rate out of range: {win_rate}")
    return problems


def validate_registry(registry: Mapping[str, Any]) -> list[str]:
    """Structural problems across a whole envelope."""
    problems: list[str] = []
    if registry.get("orders_enabled"):
        problems.append("registry must never enable execution")
    for wallet, entry in (registry.get("wallets") or {}).items():
        problems.extend(f"{wallet}: {issue}" for issue in validate_entry(entry))
    return problems
