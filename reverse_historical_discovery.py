#!/usr/bin/env python3
"""Reverse Historical Discovery.

Finds wallets that entered an asset *before* a historical move completed, and
then asks the only question that separates a lucky entry from a real edge: did
the same wallet do it again, on a different move, on a different asset?

This is a discovery layer. It produces **candidates**, not PROVEN wallets. A
Reverse Discovery Candidate is explicitly not a PROVEN Wallet: nothing here
writes to ``proven_wallet_registry.json``, and ``proven_status`` is copied
read-only from the committed registry so evidence and standing can be compared
side by side without either one contaminating the other. The only source of
PROVEN truth remains ``wallet_history_validation.reconstruct_wallet``.

What the move definition is, and is not
    A "move" here is a >=20% rise between two *observed trade prices* of the
    same asset in this archive. That is not an OHLC series: the archive stores
    the price a wallet transacted at, so prices exist only at moments some
    wallet traded, and the true market path between two trades is unknown. A
    +20% move is therefore confirmed at the first observed trade at or above
    1.2x the start price, which is an observation, not a tick. The limitation is
    recorded in the artifact rather than papered over, and no price is ever
    interpolated or invented.

Determinism
    Everything except ``generated_at`` is a pure function of the input archive.
    ``payload_sha256`` is a SHA-256 over the envelope minus ``generated_at`` and
    minus the digest itself, matching the convention already used by
    ``proven_wallet_registry.json``.

Usage
    python3 reverse_historical_discovery.py            # write the artifact
    python3 reverse_historical_discovery.py --verify   # rebuild and compare
    python3 reverse_historical_discovery.py --no-gate  # diagnostic, ungated

The ``--no-gate`` mode relaxes the asset-history requirement purely to measure
what the archive actually contains. It is recorded in the artifact under
``diagnostics`` and is never the deliverable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import historical_discovery as hd
import proven_wallet_registry as pwr
import wallet_history_validation as whv

HERE = Path(__file__).resolve().parent
SOURCE_ARCHIVE = HERE / "gmgn_wallet_history.json"
REGISTRY = HERE / "proven_wallet_registry.json"
ARTIFACT = HERE / "reverse_historical_discovery.json"

SCHEMA_VERSION = 1
MODE = "REVERSE_HISTORICAL_DISCOVERY_READ_ONLY"

#: A discovery parameter, NOT a PROVEN threshold. It defines what counts as a
#: move worth arriving before. It deliberately does not touch, and is not
#: derived from, whv.TARGET_MULTIPLE / MIN_*_ENTRIES / MIN_WIN_RATE_PCT.
MOVE_THRESHOLD = 0.20

#: The asset-history requirement for this first version. An asset needs at least
#: this much observed history before its moves are considered meaningful.
MIN_ASSET_HISTORY_DAYS = 90

#: Deterministic independent-event rule, in words so the grouping is auditable:
#: moves are cut at their confirmation point. Once a move is recorded, no new
#: move may begin until after that confirmation, so every start point inside a
#: single continuous rise collapses into the one event it belongs to.
EVENT_SEGMENTED_AT_CONFIRMATION = True

NON_DETERMINISTIC = ("generated_at",)

LIMITATIONS = {
    "no_ohlc_price_series": (
        "The archive stores the price a wallet transacted at, not an OHLC or "
        "tick series. A move is confirmed at the first observed trade at or "
        "above 1.2x the start price, so the exact moment price crossed +20% is "
        "unknown and only bounded by the surrounding trades. No price is "
        "interpolated, carried forward or invented."
    ),
    "prices_only_where_wallets_traded": (
        "A price observation exists only where some wallet traded the asset. A "
        "move between two distant trades is not evidence of a continuous rise."
    ),
    "provider_move_fields_unusable": (
        "The archive carries peak_multiple, first_1_2x_timestamp, "
        "first_1_5x_timestamp and first_2x_timestamp on 92,767 rows, but every "
        "value is 0 and every timestamp is null, and the fields are absent on "
        "the remaining 89,821 rows. They are schema placeholders and were not "
        "used. The provider's own move confirmations would be the ideal source "
        "and are simply not populated in this dataset."
    ),
    "observation_time_fallback": (
        "38.6% of rows carry no trade_timestamp and fall back to the collector's "
        "timestamp. Entry ordering for those rows is approximate; an entry could "
        "therefore be attributed slightly late or early relative to a move."
    ),
    "no_intra_asset_intraday_path": (
        "Multiple observations share a second across different wallets. They are "
        "collapsed deterministically, so sub-second ordering is not available."
    ),
    "entry_window_is_defined_not_inferred": (
        "A pre-move entry is a buy at or after the move's start observation and "
        "strictly before its +20% confirmation. Requiring entry before "
        "confirmation is what prevents look-ahead; a wallet that bought after "
        "the move was already +20% is not counted as early."
    ),
    "discovery_is_not_conviction": (
        "A candidate here is evidence, not a rating. proven_status is copied "
        "read-only from the committed registry and is never written, and no "
        "PROVEN threshold was added, relaxed or reused as a discovery cut."
    ),
}

DEFINITIONS = {
    "move_threshold": MOVE_THRESHOLD,
    "move_threshold_basis": "percentage rise between two observed trade prices",
    "min_asset_history_days": MIN_ASSET_HISTORY_DAYS,
    "asset_identity": "wallet_history_validation.asset_identity (chain + contract)",
    "event_time": "wallet_history_validation.event_ts (trade_timestamp, else timestamp)",
    "price": "wallet_history_validation.event_price (price_usd, else price, else priceUsd)",
    "dedup": "wallet_history_validation.dedupe per wallet; identical (ts,price) observations collapsed per asset",
    "move_start": "the first observation of a segment",
    "confirmation": "the first later observation with price >= start_price * (1 + move_threshold)",
    "independent_event_rule": (
        "moves are segmented at confirmation: after a move is recorded, no new "
        "move may begin until after that confirmation, so a continuous rise "
        "yields one event no matter how many entries it contains"
    ),
    "pre_move_entry_window": (
        "entry_ts >= move_start_ts and entry_ts < confirmation_ts; the strict "
        "inequality is the no-look-ahead guard"
    ),
    "event_identity": "(chain, asset_address, move_start_ts, confirmation_ts)",
    "one_event_per_wallet": "a wallet contributes at most one record per event identity",
}


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_digest(envelope: Mapping[str, Any]) -> str:
    """SHA-256 over everything that must not change between runs.

    Two exclusions, both required. ``generated_at`` is the wall-clock stamp.
    ``determinism.payload_sha256`` is the digest itself and is nested, so
    removing only the top level would leave the real digest inside the hashed
    bytes and the stored value could never verify on reload.
    """
    payload = {k: v for k, v in envelope.items() if k not in NON_DETERMINISTIC}
    determinism = dict(payload.get("determinism") or {})
    determinism.pop("payload_sha256", None)
    payload["determinism"] = determinism
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Step 1: the price evidence
# ---------------------------------------------------------------------------

def asset_observations(
    history: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[tuple[str, str], list[tuple[int, float]]]:
    """Per-asset observed ``(timestamp, price)`` points, from every wallet.

    Built across the whole archive rather than per wallet, so a move is not
    defined by the very trades of the candidate being tested for it. Identical
    ``(timestamp, price)`` points -- several wallets filling at the same price in
    the same second -- are collapsed, because they are one observation of one
    price, not several.
    """
    seen: dict[tuple[str, str], set[tuple[int, float]]] = defaultdict(set)
    for rows in (history or {}).values():
        for row in whv.dedupe([dict(r) for r in (rows or [])]):
            price = whv.event_price(row)
            ts = whv.event_ts(row)
            if price and price > 0 and ts:
                identity = whv.asset_identity(row)
                if identity is not None:
                    seen[identity].add((ts, price))
    return {identity: sorted(points) for identity, points in seen.items()}


def find_moves(
    observations: Sequence[tuple[int, float]],
    threshold: float = MOVE_THRESHOLD,
) -> list[dict[str, Any]]:
    """+20% moves in one asset's observed prices, in chronological order.

    Scans forward once. A candidate start is the current observation; the move is
    confirmed at the first later observation reaching ``start * (1+threshold)``.
    On confirmation the scan resumes *after* the confirming observation, which is
    what makes a continuous rise one event instead of one per entry. If no later
    observation reaches the target, that start is not a move and the scan
    advances by one.

    Returns a list of dicts with the start and confirmation timestamps and
    prices. No price is synthesised: a confirmation is always a real observation.
    """
    moves: list[dict[str, Any]] = []
    total = len(observations)
    index = 0
    while index < total:
        start_ts, start_price = observations[index]
        target = start_price * (1.0 + threshold)
        confirm = index + 1
        while confirm < total and observations[confirm][1] < target:
            confirm += 1
        if confirm < total:
            moves.append({
                "move_start_ts": start_ts,
                "move_start_price": start_price,
                "confirmation_ts": observations[confirm][0],
                "confirmation_price": observations[confirm][1],
                "price_change_pct": round(
                    (observations[confirm][1] / start_price - 1.0) * 100.0, 4
                ),
                "observations_scanned": confirm - index,
            })
            index = confirm
        else:
            index += 1
    return moves


def asset_history_days(observations: Sequence[tuple[int, float]]) -> float:
    if len(observations) < 2:
        return 0.0
    return (observations[-1][0] - observations[0][0]) / 86400.0


# ---------------------------------------------------------------------------
# Step 2: who entered before the move completed
# ---------------------------------------------------------------------------

def collect_entries(
    history: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Per-asset buy events across all wallets, deduplicated by the shared rules.

    The buy timestamp is ``event_ts`` (the on-chain trade time when present), so
    ordering reflects when the entry happened rather than when a scan saw it.
    """
    entries: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for wallet, rows in (history or {}).items():
        key = whv.wallet_identity(wallet)
        if not key:
            continue
        for row in whv.dedupe([dict(r) for r in (rows or [])]):
            if whv.event_side(row) != "buy":
                continue
            price = whv.event_price(row)
            ts = whv.event_ts(row)
            identity = whv.asset_identity(row)
            if identity is None or not ts:
                continue
            entries[identity].append({
                "wallet": key,
                "ts": ts,
                "price": price or None,
                "usd": whv.event_usd(row) or None,
                "has_trade_timestamp": bool(row.get("trade_timestamp")),
                "symbol": str(row.get("symbol") or "") or None,
                "chain": identity[0] or None,
            })
    for identity in entries:
        entries[identity].sort(key=lambda e: (e["ts"], e["wallet"]))
    return dict(entries)


def pre_move_entries(
    move: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Buys landing at/after the move start and strictly before confirmation.

    The strict upper bound is the whole point. An entry at or after the
    confirming observation has already missed the move, and counting it would be
    look-ahead: it would reward the wallet for a price it could only see after
    the rise.
    """
    start = int(move["move_start_ts"])
    confirm = int(move["confirmation_ts"])
    return [e for e in entries if start <= int(e["ts"]) < confirm]


def build_events(
    observations: Mapping[tuple[str, str], Sequence[tuple[int, float]]],
    entries: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    *,
    threshold: float = MOVE_THRESHOLD,
    min_history_days: float = MIN_ASSET_HISTORY_DAYS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every qualifying pre-move entry, one record per wallet per move."""
    events: list[dict[str, Any]] = []
    stats = {
        "assets_seen": 0,
        "assets_with_priced_history": 0,
        "assets_rejected_short_history": 0,
        "assets_eligible": 0,
        "assets_eligible_with_moves": 0,
        "moves_found": 0,
        "moves_with_no_pre_move_entry": 0,
    }
    for identity in sorted(observations):
        stats["assets_seen"] += 1
        series = observations[identity]
        if len(series) < 2:
            stats["assets_rejected_short_history"] += 1
            continue
        stats["assets_with_priced_history"] += 1
        if asset_history_days(series) < min_history_days:
            stats["assets_rejected_short_history"] += 1
            continue
        stats["assets_eligible"] += 1

        asset_entries = entries.get(identity, ())
        moves = find_moves(series, threshold)
        if not moves:
            continue
        stats["assets_eligible_with_moves"] += 1
        stats["moves_found"] += len(moves)

        chain, address = identity
        for ordinal, move in enumerate(moves, start=1):
            event_id = "|".join([
                chain or "-", address, str(move["move_start_ts"]),
                str(move["confirmation_ts"]),
            ])
            hits = pre_move_entries(move, asset_entries)
            if not hits:
                stats["moves_with_no_pre_move_entry"] += 1
            # (event_id, wallet) is unique, so one move can never contribute two
            # records for the same wallet however many times it bought.
            seen_wallets: set[str] = set()
            for entry in hits:
                if entry["wallet"] in seen_wallets:
                    continue
                seen_wallets.add(entry["wallet"])
                events.append({
                    "event_id": event_id,
                    "event_ordinal": ordinal,
                    "chain": chain or None,
                    "asset_address": address,
                    "symbol": entry.get("symbol"),
                    "wallet": entry["wallet"],
                    "entry_ts": int(entry["ts"]),
                    "entry_price": entry["price"],
                    "entry_usd": entry["usd"],
                    "entry_from_trade_timestamp": bool(entry["has_trade_timestamp"]),
                    "move_start_ts": int(move["move_start_ts"]),
                    "move_start_price": move["move_start_price"],
                    "confirmation_ts": int(move["confirmation_ts"]),
                    "confirmation_price": move["confirmation_price"],
                    "price_change_pct": move["price_change_pct"],
                    "lead_time_seconds": int(move["confirmation_ts"]) - int(entry["ts"]),
                    "entry_precedes_confirmation": int(entry["ts"]) < int(move["confirmation_ts"]),
                    "source": hd.SOURCE_GMGN,
                })
    return events, stats


def attach_exits(
    events: Sequence[Mapping[str, Any]],
    history: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Attach exit information when the archive actually records one.

    Exit is only reported when a sell of the same asset by the same wallet exists
    after the entry and before the confirmation. Nothing is inferred: a wallet
    with no recorded sell gets ``exit_ts: null`` rather than a guessed one.
    """
    sells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for wallet, rows in (history or {}).items():
        key = whv.wallet_identity(wallet)
        if not key:
            continue
        for row in whv.dedupe([dict(r) for r in (rows or [])]):
            if whv.event_side(row) != "sell":
                continue
            identity = whv.asset_identity(row)
            if identity is None:
                continue
            sells[(key, identity)].append({
                "ts": whv.event_ts(row),
                "usd": whv.event_usd(row) or None,
                "price": whv.event_price(row) or None,
            })

    out: list[dict[str, Any]] = []
    for event in events:
        record = dict(event)
        candidates = [
            s for s in sells.get((event["wallet"], (event["chain"] or "", event["asset_address"])), ())
            if int(event["entry_ts"]) < int(s["ts"]) <= int(event["confirmation_ts"])
        ]
        if candidates:
            first = min(candidates, key=lambda s: s["ts"])
            record["exit_ts"] = first["ts"]
            record["exit_usd"] = first["usd"]
            record["exit_price"] = first["price"]
        else:
            record["exit_ts"] = None
            record["exit_usd"] = None
            record["exit_price"] = None
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Step 3: per-wallet evidence
# ---------------------------------------------------------------------------

def build_wallets(
    events: Sequence[Mapping[str, Any]],
    proven_status: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, int]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(event["wallet"])].append(event)

    wallets: dict[str, Any] = {}
    for wallet in sorted(grouped):
        rows = grouped[wallet]
        rows.sort(key=lambda e: (e["confirmation_ts"], e["event_id"], e["wallet"]))
        leads = sorted(int(r["lead_time_seconds"]) for r in rows)
        mid = len(leads) // 2
        median = leads[mid] if len(leads) % 2 else (leads[mid - 1] + leads[mid]) / 2.0
        assets = sorted({(r["chain"] or "", r["asset_address"]) for r in rows})
        entry_prices = [r["entry_price"] for r in rows if r["entry_price"]]
        wallets[wallet] = {
            "wallet": wallet,
            "independent_pre_pump_events": len(rows),
            "distinct_assets": len(assets),
            "asset_identities": [f"{c}:{a}" if c else a for c, a in assets],
            "first_event_ts": min(r["confirmation_ts"] for r in rows),
            "last_event_ts": max(r["confirmation_ts"] for r in rows),
            "mean_lead_time_seconds": round(sum(leads) / len(leads), 1),
            "median_lead_time_seconds": round(median, 1),
            "min_lead_time_seconds": leads[0],
            "max_lead_time_seconds": leads[-1],
            "mean_entry_usd": (
                round(sum(r["entry_usd"] for r in rows if r["entry_usd"]) / len([r for r in rows if r["entry_usd"]]), 2)
                if any(r["entry_usd"] for r in rows) else None
            ),
            "events_with_exit": sum(1 for r in rows if r.get("exit_ts") is not None),
            "events_using_trade_timestamp": sum(1 for r in rows if r["entry_from_trade_timestamp"]),
            "proven_status": proven_status.get(wallet, "NOT_IN_REGISTRY"),
            "data_quality_flags": _quality_flags(rows),
            "event_ids": sorted({r["event_id"] for r in rows}),
            "events": [dict(r) for r in rows],
        }

    repeated = sum(1 for w in wallets.values() if w["independent_pre_pump_events"] >= 2)
    stats = {
        "wallets_observed_before_moves": len(wallets),
        "wallets_with_repeated_independent_events": repeated,
    }
    return wallets, stats


def _quality_flags(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """Per-wallet data-quality notes, so weak evidence is visible not hidden.

    Reads defensively: this is also called on events that have not been through
    :func:`attach_exits`, and a quality note must never be the thing that raises.
    """
    flags: set[str] = set()
    if any(not e.get("entry_from_trade_timestamp") for e in events):
        flags.add("entry_timestamp_from_collector_scan_for_some_events")
    if all(not e.get("entry_from_trade_timestamp") for e in events):
        flags.add("all_entry_timestamps_from_collector_scan")
    if any(e.get("entry_price") is None for e in events):
        flags.add("missing_entry_price_on_some_events")
    if all(e.get("exit_ts") is None for e in events):
        flags.add("no_recorded_exit_for_any_event")
    if any(e.get("chain") is None for e in events):
        flags.add("unattributed_chain_on_some_events")
    if any(int(e.get("lead_time_seconds") or 0) <= 0 for e in events):
        flags.add("non_positive_lead_time")
    return sorted(flags)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def build(
    history: Mapping[str, Sequence[Mapping[str, Any]]],
    proven_status: Mapping[str, str],
    *,
    min_history_days: float = MIN_ASSET_HISTORY_DAYS,
    source_md5: str = "",
    source_bytes: int = 0,
    rows_total: int = 0,
) -> dict[str, Any]:
    observations = asset_observations(history)
    entries = collect_entries(history)
    events, stats = build_events(
        observations, entries, min_history_days=min_history_days
    )
    events = attach_exits(events, history)
    wallets, wallet_stats = build_wallets(events, proven_status)
    wallets = dict(sorted(wallets.items()))

    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "definitions": DEFINITIONS,
        "limitations": LIMITATIONS,
        "source": {
            "artifact": SOURCE_ARCHIVE.name,
            "md5": source_md5,
            "bytes": source_bytes,
            "provider": hd.SOURCE_GMGN,
            "wallets_in_source": len(history or {}),
            "rows_in_source": rows_total,
            "proven_status_source": REGISTRY.name,
            "proven_status_mutated": False,
        },
        "eligibility": {
            "min_asset_history_days": min_history_days,
            "gate_applied": min_history_days > 0,
            "note": (
                "Assets need this much observed history before their moves are "
                "considered. Assets below it are counted in "
                "assets_rejected_short_history and are not scanned for moves."
            ),
        },
        "totals": {
            "total_wallets_scanned": len(history or {}),
            "total_assets_scanned": stats["assets_seen"],
            "total_plus20_events": stats["moves_found"],
            "total_wallets_observed_before_moves": wallet_stats["wallets_observed_before_moves"],
            "wallets_with_repeated_independent_events":
                wallet_stats["wallets_with_repeated_independent_events"],
            "total_pre_move_entries": len(events),
            "assets_with_priced_history": stats["assets_with_priced_history"],
            "assets_rejected_short_history": stats["assets_rejected_short_history"],
            "assets_eligible": stats["assets_eligible"],
            "assets_eligible_with_moves": stats["assets_eligible_with_moves"],
            "moves_with_no_pre_move_entry": stats["moves_with_no_pre_move_entry"],
        },
        "candidates": wallets,
        "candidate_count": len(wallets),
        "determinism": {
            "non_deterministic_fields": list(NON_DETERMINISTIC),
            "digest": "sha256 over the envelope minus " + ", ".join(NON_DETERMINISTIC),
            "payload_sha256": "",
            "note": (
                "generated_at is the only wall-clock field. payload_sha256 "
                "covers every other field, so rebuilding from an unchanged "
                "archive reproduces it exactly."
            ),
        },
    }
    envelope["determinism"]["payload_sha256"] = payload_digest(envelope)
    return envelope


def load_proven_status() -> dict[str, str]:
    """Read PROVEN standing from the committed registry, read-only.

    Never written, never merged, never recomputed here. The registry file is
    opened for reading only so a discovery run cannot alter a conviction record.
    """
    registry = pwr.load_registry(REGISTRY)
    return {
        wallet: str(entry.get("proven_status") or whv.NO_HISTORY)
        for wallet, entry in (registry.get("wallets") or {}).items()
    }


def diagnose(
    history: Mapping[str, Sequence[Mapping[str, Any]]],
    proven_status: Mapping[str, str],
) -> dict[str, Any]:
    """What the archive contains once the history gate is relaxed.

    Reported because "zero eligible assets" is only actionable if the owner can
    see what the shortfall costs. This is a measurement of real data, never a
    substitute for the gated result. Takes the already-parsed archive and the
    already-loaded registry rather than reading them again, so the diagnostic
    cannot disagree with the gated run about the input.
    """
    ungated = build(history, proven_status, min_history_days=0)
    observations = asset_observations(history)
    spans = sorted(asset_history_days(v) for v in observations.values() if len(v) >= 2)
    return {
        "purpose": (
            "measurement only: the same algorithm with the asset-history gate "
            "removed, to quantify how much history is missing. Not a deliverable."
        ),
        "assets_scanned": ungated["totals"]["total_assets_scanned"],
        "plus20_events": ungated["totals"]["total_plus20_events"],
        "wallets_observed_before_moves": ungated["totals"]["total_wallets_observed_before_moves"],
        "wallets_with_repeated_independent_events":
            ungated["totals"]["wallets_with_repeated_independent_events"],
        "candidate_count": ungated["candidate_count"],
        "asset_history_span_days": {
            "p50": round(spans[len(spans) // 2], 4) if spans else None,
            "p90": round(spans[int(len(spans) * 0.9)], 4) if spans else None,
            "p99": round(spans[int(len(spans) * 0.99)], 4) if spans else None,
            "max": round(spans[-1], 4) if spans else None,
        },
        "required_days": MIN_ASSET_HISTORY_DAYS,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="rebuild and compare, do not write")
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="write the ungated diagnostic result instead of the gated deliverable",
    )
    args = parser.parse_args(argv)

    before = file_md5(SOURCE_ARCHIVE)
    history = json.loads(SOURCE_ARCHIVE.read_text(encoding="utf-8"))
    proven = load_proven_status()

    envelope = build(
        history, proven,
        min_history_days=0 if args.no_gate else MIN_ASSET_HISTORY_DAYS,
        source_md5=before,
        source_bytes=SOURCE_ARCHIVE.stat().st_size,
        rows_total=sum(len(v) for v in history.values()),
    )
    if not args.no_gate:
        envelope["diagnostics"] = diagnose(history, load_proven_status())
        # diagnostics is part of the artifact, so it must be inside the digest.
        # build() sealed the digest before this key existed; re-seal or the stored
        # value would not describe the stored envelope.
        envelope["determinism"]["payload_sha256"] = payload_digest(envelope)

    totals = envelope["totals"]
    print(f"assets scanned            : {totals['total_assets_scanned']}")
    print(f"assets with priced history: {totals['assets_with_priced_history']}")
    print(f"assets rejected (history) : {totals['assets_rejected_short_history']}")
    print(f"assets eligible           : {totals['assets_eligible']}")
    print(f"+20% events (eligible)    : {totals['total_plus20_events']}")
    print(f"wallets pre-move observed : {totals['total_wallets_observed_before_moves']}")
    print(f"wallets >=2 events        : {totals['wallets_with_repeated_independent_events']}")
    print(f"orders_enabled            : {envelope['orders_enabled']}")
    print(f"payload_sha256            : {envelope['determinism']['payload_sha256']}")

    if args.no_gate:
        print("WARNING: ungated diagnostic; the gated result is the deliverable")
        return 0

    if args.verify:
        if not ARTIFACT.exists():
            print(f"FAIL: no committed artifact at {ARTIFACT.name}")
            return 1
        committed = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        if not committed:
            print(f"FAIL: no committed artifact at {ARTIFACT.name}")
            return 1
        got = envelope["determinism"]["payload_sha256"]
        want = (committed.get("determinism") or {}).get("payload_sha256")
        if got != want:
            print(f"FAIL: digest moved {want} -> {got}")
            return 1
        if committed.get("totals") != envelope["totals"]:
            print("FAIL: totals moved")
            return 1
        if (committed.get("source") or {}).get("md5") != before:
            print("FAIL: source archive md5 changed")
            return 1
        print(f"OK: deterministic reproduction confirmed ({got[:16]}...)")
        return 0

    after = file_md5(SOURCE_ARCHIVE)
    if after != before:
        raise SystemExit(
            f"source archive changed during the run: {before} -> {after}"
        )
    ARTIFACT.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {ARTIFACT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
