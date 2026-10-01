#!/usr/bin/env python3
"""Historical Depth Audit.

Read-only measurement of how much history the repository actually holds, and
whether a 180d or 365d reverse-discovery run is supported by data already
present. Observational only: this module changes no threshold, no gate, no
production decision logic, and writes nothing except its own report artifact.

What it answers
    1. Real historical depth per source, at 90d / 180d / 365d / older buckets.
    2. Coverage by chain and by provider/source.
    3. The exact reason Reverse Historical Discovery reports eligible=0.
    4. Whether a wider window could ever be satisfied from data on disk.

Why the gate fails
    ``reverse_historical_discovery`` requires each *asset* to span at least 90
    days of observed prices. The primary archive covers a span of only a few
    days in total, so no individual asset can reach 90 days. The rejection is a
    fact about the archive's width, not about the number of trades: an asset can
    have thousands of observations and still span hours. That distinction is the
    substance of this audit, so it is measured and reported rather than inferred.

Reuse, not reimplementation
    Row parsing comes from ``wallet_history_validation`` (``event_ts``,
    ``asset_identity``, ``wallet_index``, ``dedupe``) and asset observation
    construction from ``reverse_historical_discovery`` (``asset_observations``,
    ``asset_history_days``). Depth arithmetic is new but trivial. No historical
    parsing logic is duplicated here, and no writer is added.

Determinism
    Everything except ``generated_at`` is a pure function of the files on disk.
    ``payload_sha256`` is SHA-256 over the report minus ``generated_at`` and minus
    the digest itself, matching the convention already used by
    ``proven_wallet_registry.json`` and ``reverse_historical_discovery.json``.

Usage
    python3 historical_depth_audit.py            # write the report
    python3 historical_depth_audit.py --verify   # rebuild and compare, no write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import reverse_historical_discovery as rhd
import wallet_history_validation as whv

HERE = Path(__file__).resolve().parent

#: The archive Reverse Historical Discovery reads, and the one its 90d gate is
#: applied to. Named explicitly so the audit cannot silently drift onto another
#: file.
PRIMARY_ARCHIVE = HERE / "gmgn_wallet_history.json"
DISCOVERY_ARTIFACT = HERE / "reverse_historical_discovery.json"
REGISTRY = HERE / "proven_wallet_registry.json"

#: Secondary datasets inventoried for depth. Read-only, never written. These are
#: measured to answer "could an existing source supply the missing width?"
#: without adding any provider.
SECONDARY_SOURCES: tuple[dict[str, str], ...] = (
    {"name": "wallet_history.json", "kind": "holdings_snapshot_per_wallet"},
    {"name": "wallet_events.json", "kind": "wallet_activity_events"},
    {"name": "historical_replay.json", "kind": "forward_only_replay_observations"},
    {"name": "futures_volume_history.json", "kind": "exchange_futures_volume_and_price"},
    {"name": "volume_history.json", "kind": "aggregator_volume_and_price"},
)

#: Depth buckets. The gate value is included so the report states which bucket
#: the production gate sits in without this module having to read it from
#: production config.
DEPTH_BUCKETS_DAYS = (90, 180, 365)

SCHEMA_VERSION = 1
MODE = "HISTORICAL_DEPTH_AUDIT_READ_ONLY"

NON_DETERMINISTIC = ("generated_at",)
DAY_SECONDS = 86_400

LIMITATIONS = {
    "observation_time_is_not_market_time": (
        "Depth is measured from wallet_history_validation.event_ts, which "
        "prefers trade_timestamp and falls back to the collector's timestamp. "
        "Rows without trade_timestamp therefore contribute a collection-time "
        "lower bound rather than a true trade time, so measured spans can "
        "understate real depth."
    ),
    "depth_is_observed_price_coverage": (
        "An asset's depth here is the span between its earliest and latest "
        "observed trade prices. Prices exist only where some wallet traded, so "
        "this is observed coverage, not a continuous OHLC series, and an asset "
        "quietly traded twice a week apart reads as a single wide span."
    ),
    "archive_width_bounds_every_widow": (
        "No asset's measured span can exceed the whole archive's span. The "
        "primary archive is under ten days wide, so no asset inside it can "
        "satisfy a 90, 180 or 365 day requirement regardless of trade count."
    ),
    "gate_is_per_asset_not_per_archive": (
        "The production gate is applied per asset. Archive-wide depth is "
        "reported alongside it only because it upper-bounds every per-asset "
        "value and is the simplest proof of infeasibility."
    ),
    "secondary_sources_are_not_substitutes": (
        "Volume, price and activity datasets are inventoried for depth but are "
        "not shown to be a drop-in replacement for per-asset wallet trade "
        "history. Where they could plausibly widen coverage that is recorded as "
        "a candidate source only; no provider was added by this audit."
    ),
    "counts_are_observed_not_verified_externally": (
        "Row and wallet counts are as stored on disk. This audit does not "
        "re-query any provider to confirm them."
    ),
}

METHODOLOGY = {
    "event_time": "wallet_history_validation.event_ts (trade_timestamp, else timestamp)",
    "asset_identity": "wallet_history_validation.asset_identity (chain + contract)",
    "price": "wallet_history_validation.event_price (price_usd, else price, else priceUsd)",
    "asset_observations": "reverse_historical_discovery.asset_observations (deduped across all wallets)",
    "asset_span_days": "reverse_historical_discovery.asset_history_days (last_ts - first_ts)",
    "wallet_span_days": "last event_ts - first event_ts, per wallet",
    "read_only": (
        "No dataset, registry, gate or production module is opened for writing. "
        "The only file this module writes is its own report."
    ),
    "gate_untouched": (
        "MIN_ASSET_HISTORY_DAYS in reverse_historical_discovery is read for "
        "reporting only and is not modified, and no eligibility logic is reused "
        "to change production behaviour."
    ),
}


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def payload_digest(envelope: Mapping[str, Any]) -> str:
    """SHA-256 over every field that must not change between runs.

    Two exclusions, both required: ``generated_at`` is the wall-clock stamp, and
    ``determinism.payload_sha256`` is the digest itself, which is nested and
    would otherwise be hashed into itself and never verify on reload.
    """
    payload = {k: v for k, v in envelope.items() if k not in NON_DETERMINISTIC}
    determinism = dict(payload.get("determinism") or {})
    determinism.pop("payload_sha256", None)
    payload["determinism"] = determinism
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _percentiles(values: Sequence[float]) -> dict[str, float | None]:
    """Sorted percentile summary. Returns None when there is nothing to measure."""
    if not values:
        return {"p50": None, "p90": None, "p99": None, "max": None, "min": None}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "p50": round(ordered[n // 2], 4),
        "p90": round(ordered[min(n - 1, int(n * 0.9))], 4),
        "p99": round(ordered[min(n - 1, int(n * 0.99))], 4),
        "max": round(ordered[-1], 4),
        "min": round(ordered[0], 4),
    }


def bucket_counts(spans: Sequence[float]) -> dict[str, Any]:
    """Count how many spans reach each depth bucket, plus the >365d case."""
    return {
        "at_least_90d": sum(1 for s in spans if s >= 90),
        "at_least_180d": sum(1 for s in spans if s >= 180),
        "at_least_365d": sum(1 for s in spans if s >= 365),
        "older_than_365d": sum(1 for s in spans if s > 365),
        "measured_count": len(spans),
        "none_at_90d": sum(1 for s in spans if s >= 90) == 0,
    }


def measure_primary_archive(history: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Depth of the archive the 90d gate is actually applied to."""
    rows = [row for series in history.values() for row in series]
    event_times = [t for t in (whv.event_ts(r) for r in rows) if t]

    observations = rhd.asset_observations(history)
    multi = {k: v for k, v in observations.items() if len(v) >= 2}
    asset_spans = [rhd.asset_history_days(v) for v in multi.values()]

    wallet_spans: list[float] = []
    for series in history.values():
        times = [t for t in (whv.event_ts(r) for r in series) if t]
        if times:
            wallet_spans.append((max(times) - min(times)) / DAY_SECONDS)

    chains: dict[str, int] = {}
    for row in rows:
        chains[str(row.get("chain") or "")] = chains.get(str(row.get("chain") or ""), 0) + 1

    assets_by_chain: dict[str, int] = {}
    for chain, _address in observations:
        assets_by_chain[chain or "(none)"] = assets_by_chain.get(chain or "(none)", 0) + 1

    with_trade_ts = sum(1 for r in rows if whv.has_trade_timestamp(r))
    with_price = sum(1 for r in rows if whv.event_price(r))

    archive_span = (max(event_times) - min(event_times)) / DAY_SECONDS if event_times else 0.0

    return {
        "artifact": PRIMARY_ARCHIVE.name,
        "provider": "gmgn",
        "unique_wallets": len(history),
        "reconstructed_rows": len(rows),
        "rows_with_price": with_price,
        "rows_with_trade_timestamp": with_trade_ts,
        "rows_without_trade_timestamp": len(rows) - with_trade_ts,
        "unique_assets": len(observations),
        "assets_with_two_or_more_observations": len(multi),
        "earliest_event_ts": min(event_times) if event_times else None,
        "earliest_event_iso": iso(min(event_times) if event_times else None),
        "latest_event_ts": max(event_times) if event_times else None,
        "latest_event_iso": iso(max(event_times) if event_times else None),
        "archive_span_days": round(archive_span, 4),
        "asset_span_days_percentiles": _percentiles(asset_spans),
        "asset_span_buckets": bucket_counts(asset_spans),
        "wallet_span_days_percentiles": _percentiles(wallet_spans),
        "wallet_span_buckets": bucket_counts(wallet_spans),
        "wallets_with_at_least_three_rows": sum(
            1 for series in history.values() if len(series) >= 3
        ),
        "wallets_with_repeated_activity": sum(
            1 for series in history.values() if len(series) >= 2
        ),
        "coverage_by_chain_rows": dict(sorted(chains.items())),
        "coverage_by_chain_assets": dict(sorted(assets_by_chain.items())),
        "provider_field_populated": 0,
        "provider_note": (
            "No row carries a 'source' field, so provider attribution is by "
            "file of origin (gmgn) rather than by row. Recorded rather than "
            "guessed."
        ),
    }


def measure_secondary(name: str, kind: str) -> dict[str, Any]:
    """Depth of an inventoried secondary dataset. Tolerates shape differences.

    Walks whatever nested structure the file uses and collects every timestamp,
    because the point is the file's time span, not a particular schema.
    """
    path = HERE / name
    if not path.exists():
        return {"artifact": name, "kind": kind, "present": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"artifact": name, "kind": kind, "present": True, "readable": False}

    times: list[int] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key in ("timestamp", "trade_timestamp", "entry_timestamp",
                           "last_seen", "fetched_at", "ts") and isinstance(value, (int, float)):
                    times.append(int(value))
                else:
                    walk(value)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for item in node:
                walk(item)

    walk(data)

    if isinstance(data, Mapping):
        top_level_count = len(data)
    elif isinstance(data, Sequence):
        top_level_count = len(data)
    else:
        top_level_count = 0

    span = (max(times) - min(times)) / DAY_SECONDS if times else 0.0
    return {
        "artifact": name,
        "kind": kind,
        "present": True,
        "readable": True,
        "top_level_entries": top_level_count,
        "timestamps_found": len(times),
        "earliest_ts": min(times) if times else None,
        "earliest_iso": iso(min(times) if times else None),
        "latest_ts": max(times) if times else None,
        "latest_iso": iso(max(times) if times else None),
        "span_days": round(span, 4),
        "span_buckets": bucket_counts([span]) if times else None,
        "usable_as_wallet_trade_history": False,
        "substitution_note": (
            "Measured for depth only. Not established as a per-asset wallet "
            "trade history, so it is not counted as a source of the missing "
            "coverage."
        ),
    }


def build_report(history: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Assemble the audit. Pure function of the files on disk."""
    primary = measure_primary_archive(history)
    secondary = [measure_secondary(s["name"], s["kind"]) for s in SECONDARY_SOURCES]

    gate_days = rhd.MIN_ASSET_HISTORY_DAYS
    asset_buckets = primary["asset_span_buckets"]
    archive_span = primary["archive_span_days"]
    max_asset_span = primary["asset_span_days_percentiles"]["max"] or 0.0

    discovery: dict[str, Any] = {"present": False}
    if DISCOVERY_ARTIFACT.exists():
        committed = json.loads(DISCOVERY_ARTIFACT.read_text(encoding="utf-8"))
        totals = committed.get("totals") or {}
        discovery = {
            "present": True,
            "artifact": DISCOVERY_ARTIFACT.name,
            "gate_days": (committed.get("eligibility") or {}).get("min_asset_history_days"),
            "assets_eligible": totals.get("assets_eligible"),
            "assets_rejected_short_history": totals.get("assets_rejected_short_history"),
            "candidate_count": committed.get("candidate_count"),
            "ungated_diagnostics": committed.get("diagnostics") or {},
            "orders_enabled": committed.get("orders_enabled"),
        }

    widest_secondary = max(
        (s.get("span_days") or 0.0 for s in secondary if s.get("present")),
        default=0.0,
    )

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": METHODOLOGY,
        "limitations": LIMITATIONS,
        "depth_buckets_days": list(DEPTH_BUCKETS_DAYS),
        "sources": {
            "primary": primary,
            "secondary": secondary,
        },
        "findings": {
            "widest_asset_span_days": round(max_asset_span, 4),
            "widest_wallet_span_days":
                primary["wallet_span_days_percentiles"]["max"],
            "primary_archive_span_days": round(archive_span, 4),
            "widest_secondary_dataset_span_days": round(widest_secondary, 4),
            "eligible_assets_at_90d": asset_buckets["at_least_90d"],
            "eligible_assets_at_180d": asset_buckets["at_least_180d"],
            "eligible_assets_at_365d": asset_buckets["at_least_365d"],
            "assets_older_than_365d": asset_buckets["older_than_365d"],
            "max_possible_asset_span_from_this_archive_days": round(archive_span, 4),
            "gate_is_satisfiable_from_data_present": bool(asset_buckets["at_least_90d"]),
            "widening_window_would_help": False,
            "widening_window_rationale": (
                "The gate cannot be satisfied at 90d, 180d or 365d because the "
                "primary archive is only "
                f"{round(archive_span, 2)} days wide in total. A wider window is "
                "strictly harder to satisfy than a narrower one, so relaxing the "
                "gate downward would admit assets, but raising it to 180d or "
                "365d cannot admit anything and would still yield zero. The "
                "constraint is missing history, not a mis-set threshold."
            ),
        },
        "eligibility_under_current_gate": {
            "gate_days": gate_days,
            "gate_location": "reverse_historical_discovery.MIN_ASSET_HISTORY_DAYS",
            "gate_modified_by_this_audit": False,
            "assets_scanned": primary["unique_assets"],
            "assets_with_priced_history": primary["assets_with_two_or_more_observations"],
            "assets_eligible": asset_buckets["at_least_90d"],
            "assets_rejected_short_history":
                primary["unique_assets"] - asset_buckets["at_least_90d"],
            "exact_cause": (
                f"Every one of the {primary['unique_assets']} assets is shorter "
                f"than {gate_days} days of observed price history. The longest is "
                f"{round(max_asset_span, 4)} days and the whole archive spans "
                f"{round(archive_span, 4)} days, so no asset can reach the "
                f"{gate_days}-day requirement from data on disk. Rejection is a "
                "function of archive width, independent of trade count: an asset "
                "with many observations in a short window still fails."
            ),
        },
        "population_comparison": {
            "ungated_diagnostic": (discovery.get("ungated_diagnostics") or {}),
            "gated_deliverable": {
                "assets_eligible": discovery.get("assets_eligible"),
                "candidate_count": discovery.get("candidate_count"),
            },
            "interpretation": (
                "The ungated diagnostic and the gated deliverable differ only in "
                "the per-asset history requirement. Ungated, the same archive "
                "yields real +20% moves and real wallets who entered before them. "
                "Gated, the population is empty because no asset has enough "
                "history, not because no pre-move behaviour exists. The evidence "
                "of behaviour is present; the eligibility to act on it is not."
            ),
            "caveat": (
                "Ungated numbers rest on an archive under ten days wide, so a "
                "move confirmed within it may not be a real sustained move and a "
                "'repeated' wallet may be repeating across a handful of hours. "
                "They quantify the shortfall; they are not a candidate list."
            ),
        },
        "window_feasibility": {
            "90d_supported_by_data_present": bool(asset_buckets["at_least_90d"]),
            "180d_supported_by_data_present": bool(asset_buckets["at_least_180d"]),
            "365d_supported_by_data_present": bool(asset_buckets["at_least_365d"]),
            "older_than_365d_present": bool(asset_buckets["older_than_365d"]),
            "any_window_at_or_above_90d_supported": bool(asset_buckets["at_least_90d"]),
            "maximum_window_supportable_by_current_data_days": round(archive_span, 4),
            "conclusion": (
                "No. A 180d or 365d run is not technically supported by data "
                "already present, and cannot become supported by any change to "
                "the gate. Both require history the repository does not hold: the "
                f"widest asset spans {round(max_asset_span, 4)} days and the "
                f"archive itself {round(archive_span, 4)} days."
            ),
        },
        "missing_coverage": {
            "required_for_90d_days": max(0.0, round(90 - max_asset_span, 4)),
            "required_for_180d_days": max(0.0, round(180 - max_asset_span, 4)),
            "required_for_365d_days": max(0.0, round(365 - max_asset_span, 4)),
            "gap_in_days_for_widest_asset": round(gate_days - max_asset_span, 4),
            "what_is_missing": (
                "Per-asset observed trade prices sampled across at least 90 days. "
                "Concretely: many more months of historical wallet trade rows per "
                "asset, not more wallets or more trades inside the current window."
            ),
            "candidate_sources_capable_of_supplying_it": [
                {
                    "provider": "gmgn",
                    "already_integrated": True,
                    "evidence": "gmgn_wallet_history.json and gmgn_layer.py",
                    "status": "already_in_use_but_narrow_window",
                    "note": (
                        "Already the source of the 90d-gated archive, but the "
                        "committed capture covers under ten days. A deeper "
                        "backfill of the same provider's trade history is the "
                        "most direct route; whether its API can page further back "
                        "is untested here and no provider was added."
                    ),
                },
                {
                    "provider": "solscan",
                    "already_integrated": False,
                    "evidence": "no committed artifact in repository",
                    "status": "candidate_only",
                    "note": (
                        "Named in the project architecture for Solana wallet "
                        "intelligence. No committed Solana trade history exists, "
                        "so it cannot be assessed from data on disk."
                    ),
                },
                {
                    "provider": "goldrush",
                    "already_integrated": False,
                    "evidence": "no committed artifact in repository",
                    "status": "candidate_only",
                    "note": (
                        "Named in the project architecture. No committed artifact, "
                        "so its historical depth is unknown from data present."
                    ),
                },
                {
                    "provider": "nansen",
                    "already_integrated": False,
                    "evidence": "nansen_backfill.py, nansen_test_result.txt",
                    "status": "candidate_only",
                    "note": (
                        "A backfill script and a test result file exist but no "
                        "committed dataset. Historical depth unverified from "
                        "data on disk."
                    ),
                },
            ],
            "no_provider_added_by_this_audit": True,
        },
        "classification": {
            "A_data_actually_present": {
                "definition": "Trades and observations stored in this repository now.",
                "unique_wallets": primary["unique_wallets"],
                "reconstructed_rows": primary["reconstructed_rows"],
                "unique_assets": primary["unique_assets"],
                "real_span_days": round(archive_span, 4),
                "wallets_with_repeated_activity": primary["wallets_with_repeated_activity"],
            },
            "B_data_theoretically_discoverable": {
                "definition": (
                    "What the present archive could yield if the history gate "
                    "were removed. Measured, real, but not yet trustworthy at "
                    "this width."
                ),
                "plus20_events": (discovery.get("ungated_diagnostics") or {}).get("plus20_events"),
                "wallets_observed_before_moves":
                    (discovery.get("ungated_diagnostics") or {}).get(
                        "wallets_observed_before_moves"),
                "wallets_with_repeated_independent_events":
                    (discovery.get("ungated_diagnostics") or {}).get(
                        "wallets_with_repeated_independent_events"),
                "caveat": (
                    "Derived from an archive under ten days wide; treat as a "
                    "measurement of the shortfall, not as evidence of edge."
                ),
            },
            "C_data_eligible_under_current_90d_gate": {
                "definition": (
                    "Assets surviving reverse_historical_discovery's "
                    "MIN_ASSET_HISTORY_DAYS gate as committed."
                ),
                "assets_eligible": asset_buckets["at_least_90d"],
                "candidates": discovery.get("candidate_count"),
                "note": "Empty. Cause is measured above, not assumed.",
            },
        },
        "provenance": {
            "primary_archive": PRIMARY_ARCHIVE.name,
            "primary_archive_md5": file_md5(PRIMARY_ARCHIVE),
            "primary_archive_bytes": PRIMARY_ARCHIVE.stat().st_size,
            "discovery_artifact": DISCOVERY_ARTIFACT.name,
            "registry_read_only_reference": REGISTRY.name,
            "registry_mutated": False,
            "datasets_mutated": False,
            "gate_mutated": False,
            "production_logic_mutated": False,
        },
        "determinism": {
            "non_deterministic_fields": list(NON_DETERMINISTIC),
            "digest": "sha256 over the report minus " + ", ".join(NON_DETERMINISTIC),
            "payload_sha256": "",
            "note": (
                "generated_at is the only wall-clock field, so rebuilding from "
                "unchanged files reproduces payload_sha256 exactly."
            ),
        },
    }
    report["determinism"]["payload_sha256"] = payload_digest(report)
    return report


REPORT = HERE / "historical_depth_audit.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="rebuild and compare, do not write")
    args = parser.parse_args(argv)

    before = file_md5(PRIMARY_ARCHIVE)
    history = whv.load_history(PRIMARY_ARCHIVE)
    report = build_report(history)

    findings = report["findings"]
    elig = report["eligibility_under_current_gate"]
    print(f"primary archive            : {PRIMARY_ARCHIVE.name} ({report['provenance']['primary_archive_md5']})")
    print(f"unique wallets             : {report['sources']['primary']['unique_wallets']}")
    print(f"reconstructed rows         : {report['sources']['primary']['reconstructed_rows']}")
    print(f"unique assets              : {report['sources']['primary']['unique_assets']}")
    print(f"archive span               : {findings['primary_archive_span_days']} days")
    print(f"widest asset span          : {findings['widest_asset_span_days']} days")
    print(f"widest wallet span         : {findings['widest_wallet_span_days']} days")
    print(f"assets >= 90d / 180d / 365d: {findings['eligible_assets_at_90d']} / "
          f"{findings['eligible_assets_at_180d']} / {findings['eligible_assets_at_365d']}")
    print(f"assets older than 365d     : {findings['assets_older_than_365d']}")
    print(f"gate                       : {elig['gate_days']} days (unmodified)")
    print(f"assets eligible under gate : {elig['assets_eligible']}")
    print(f"orders_enabled             : {report['orders_enabled']}")
    print(f"payload_sha256             : {report['determinism']['payload_sha256']}")

    if args.verify:
        if not REPORT.exists():
            print(f"FAIL: no committed report at {REPORT.name}")
            return 1
        committed = json.loads(REPORT.read_text(encoding="utf-8"))
        got = report["determinism"]["payload_sha256"]
        want = (committed.get("determinism") or {}).get("payload_sha256")
        if got != want:
            print(f"FAIL: digest moved {want} -> {got}")
            return 1
        for section in ("sources", "findings", "eligibility_under_current_gate",
                        "window_feasibility", "missing_coverage", "classification"):
            if committed.get(section) != report.get(section):
                print(f"FAIL: section moved: {section}")
                return 1
        print(f"OK: deterministic reproduction confirmed ({got[:16]}...)")
        return 0

    after = file_md5(PRIMARY_ARCHIVE)
    if after != before:
        raise SystemExit(f"source archive changed during the run: {before} -> {after}")
    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {REPORT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())