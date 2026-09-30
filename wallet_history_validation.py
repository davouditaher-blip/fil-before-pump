"""Offline historical wallet validation for Fil Before Pump.

Why this module exists
----------------------
``gmgn_layer.analyze_wallet_activity`` is the only thing in the repository that
sets ``proven`` for the GMGN wallet-track gate, and until now it could only
reconstruct history from a *live* ``gmgn portfolio activity`` call plus a *live*
``gmgn market kline`` call. When the provider is slow, rate limited, or the key
is absent, both calls return nothing, the wallet profile collapses to
``opportunities=0``, and every wallet reports as unproven:

    "smart-money buy, historical proof not yet established"
    "Proven wallets (current historical test): 0"
    "سابقه کافی نیست"

That is a *provider availability* fault being reported as a *wallet quality*
fact. Meanwhile the repository already stores 160k+ GMGN records for 1.9k+
wallets with buys in ``gmgn_wallet_history.json`` and that stored history was
never replayed through the proven-wallet decision. This module closes that gap:
it reconstructs the honest post-entry outcome of every stored historical buy
offline, so the proven classification is driven by evidence the project already
owns rather than by live API reachability.

Design rules (these are the project's existing historical-validation rules)
-------------------------------------------------------------------------
- Canonical token identity is ``chain + contract address``. Ticker symbols are
  labels, not identity: 1975 of 7396 stored symbols map to more than one
  contract and one symbol ("SI") maps to 63. Joining on symbol silently mixes
  unrelated tokens, so this module never joins on symbol alone.
- Insufficient evidence is UNKNOWN, never 0% performance and never a loss. A
  wallet with an entry but no forward observation is
  ``HISTORICAL_ACTIVITY_BUT_UNPROVEN``; a wallet with no qualifying entry at all
  is ``NO_HISTORY`` / COLD_START. Neither is ever recorded as a losing wallet.
- Proven is a conjunctive evidence threshold, never derived from transaction
  count, wallet overlap, or a Smart Money label. See ``PROVEN_*`` below.
- Deduplication must not destroy chronological order: rows are ordered by a
  stable key and the original index is retained as the tie-breaker.
- Read-only. This module never writes an artifact, never places an order and
  never enables exchange execution.

Evidence threshold for PROVEN
-----------------------------
A wallet becomes PROVEN only when *all* of the following hold, evaluated over
entries that have a valid entry price *and* at least one genuine post-entry
price observation inside the window:

1. ``observed_entries >= MIN_OBSERVED_ENTRIES`` (3) - enough independent
   reconstructed outcomes that a win rate is not a coin flip.
2. ``win_rate >= MIN_WIN_RATE_PCT`` (60%) - and that win rate is computed only
   over observed entries, so an unknown window can never drag a wallet down.
3. ``successful_entries >= MIN_SUCCESSFUL_ENTRIES`` (2) - at least two genuine
   pre-pump successes. This is currently *implied* by (1) and (2): the smallest
   success count satisfying ``observed >= 3`` and ``rate >= 60%`` is 2. It is
   kept as an explicit, independently-testable bound so the criterion reads as
   what the project means ("a track record, not one lucky trade") and so
   loosening (1) or (2) later cannot silently weaken the proof standard.

``TARGET_MULTIPLE`` (2.0x) is the pre-pump success definition already used by
``gmgn_layer.analyze_wallet_activity`` and ``wallet_track_profile``; it is kept
unchanged so this module strengthens evidence handling without moving the goal
posts to manufacture a count.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HISTORY_FILE = Path("gmgn_wallet_history.json")

# EVM addresses are 0x + exactly 40 hex characters. Only these are case-folded;
# base58 Solana addresses are case-sensitive and must never be lowercased.
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Pre-pump success definition. Matches the existing rule in
# gmgn_layer.analyze_wallet_activity / wallet_track_profile.
TARGET_MULTIPLE = 2.0

# Observation window after an entry. Matches LOOKAHEAD_SECONDS in
# wallet_quality_engine (14 days) and gmgn_layer (14 days).
WINDOW_SECONDS = 14 * 24 * 60 * 60

# Entry capital floor. Deliberately 0.0.
#
# The layer this module repairs (``gmgn_layer.analyze_wallet_activity``) counts
# every buy with a real price as an entry and applies no capital floor, so
# keeping that semantics here means the offline reconstruction measures the
# same population as the live path it replaces. The project's $5K
# "qualified buy" rule stays owned by the layers that already apply it
# (``wallet_quality_engine``, ``wallet_radar``); re-applying it here would be a
# second, divergent definition and would silently discard most of the stored
# history. Raise this only deliberately, never to move a count.
MIN_ENTRY_USD = 0.0

# ---- PROVEN threshold -----------------------------------------------------
# Depth. Three observed entries is the smallest sample the existing scanner
# already accepts for a proven radar wallet (scanner.apply_wallet_radar_signals)
# and the existing quality engine (>= 3 qualified buys AND >= 3 attempts).
MIN_OBSERVED_ENTRIES = 3
# Productivity, measured over observed entries only.
MIN_WIN_RATE_PCT = 60.0
# At least two genuine pre-pump successes.
MIN_SUCCESSFUL_ENTRIES = 2

# ---- Classification states ------------------------------------------------
PROVEN = "PROVEN"
ACTIVITY_BUT_UNPROVEN = "HISTORICAL_ACTIVITY_BUT_UNPROVEN"
NO_HISTORY = "NO_HISTORY"

CLASSES = (PROVEN, ACTIVITY_BUT_UNPROVEN, NO_HISTORY)

# Entry outcome states. "unknown" is an explicit UNKNOWN, never 0% and never a
# loss.
OUTCOME_SUCCESS = "success"
OUTCOME_OBSERVED_NO_TARGET = "observed_no_2x"
OUTCOME_UNKNOWN = "unknown"

# Position states derived from the reconstructed flow.
POSITION_EXITED = "exited"
POSITION_TRIMMED = "trimmed"
POSITION_HOLDING = "holding"


def _num(value: Any, default: float = 0.0) -> float:
    """Parse a number without ever raising on provider-shaped junk."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    return int(_num(value, float(default)))


def event_ts(row: dict[str, Any]) -> int:
    """Event time in seconds.

    GMGN rows written by the collector carry ``trade_timestamp`` (the on-chain
    trade time) and ``timestamp`` (the time the row was observed). Using the
    observation time as the trade time shifts every entry forward and is one of
    the ways a genuine early entry is misread as a late one, so the trade
    timestamp is preferred. Millisecond values are normalised to seconds.
    """
    raw = row.get("trade_timestamp")
    if raw in (None, "", 0, "0"):
        raw = row.get("timestamp")
    value = _int(raw)
    return value // 1000 if value > 10**12 else value


def event_price(row: dict[str, Any]) -> float:
    return _num(row.get("price_usd") or row.get("price") or row.get("priceUsd"))


def event_usd(row: dict[str, Any]) -> float:
    return _num(
        row.get("amount_usd")
        or row.get("usd")
        or row.get("usd_value")
        or row.get("value_usd")
        or row.get("valueUsd")
    )


def wallet_identity(value: Any) -> str:
    """Canonical wallet identity key for lookup and dedupe.

    EVM addresses (0x + 40 hex) are case-insensitive by definition: the same
    address is ``0xAbC…`` and ``0xabc…`` and they are the same account. Base58
    (Solana) addresses *are* case-sensitive, so those are only stripped of
    surrounding whitespace and otherwise preserved byte for byte. Lowercasing a
    base58 address would be data corruption, not normalisation.

    The chain is deliberately *not* part of the wallet key: one EVM account
    address is the same account on every EVM chain, and the token identity
    already carries its own chain. Solidity-style chain prefixes (``eth:0x…``)
    are accepted so a provider that returns them resolves to the same wallet.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" in text:
        prefix, _, remainder = text.partition(":")
        if prefix.lower() in {"eth", "ethereum", "bsc", "bnb", "base", "eth-mainnet",
                              "bsc-mainnet", "base-mainnet", "sol", "solana"}:
            text = remainder.strip()
            if not text:
                return ""
    if _EVM_ADDRESS.fullmatch(text):
        return text.lower()
    return text


def wallet_index(history: dict[str, Any]) -> dict[str, str]:
    """Map canonical wallet key -> the original stored key.

    Stored history mixes casing (1,131 of 2,131 keys are not lowercase, because
    the GMGN CLI returns checksummed EVM addresses) while callers look wallets up
    with whatever casing the provider just handed them. A case-sensitive
    ``history.get(wallet)`` therefore missed 35% of the qualified buy rows. This
    index resolves both directions without rewriting or deleting any stored key.

    When two stored keys canonicalize to the same wallet, the first wins as the
    stored key and the rows of the others stay on disk untouched, so no record
    is deleted and no wallet is duplicated by casing alone.
    """
    index: dict[str, str] = {}
    for key in history or {}:
        canonical = wallet_identity(key)
        if not canonical:
            continue
        index.setdefault(canonical, key)
    return index


def rows_for_wallet(history: dict[str, Any], wallet: Any) -> list[dict[str, Any]]:
    """Return every stored row for a wallet, matched case-insensitively.

    Merges the rows of all stored keys that canonicalize to the same wallet so a
    wallet that was stored under mixed and lowercase keys is not silently split
    into two half-histories.
    """
    canonical = wallet_identity(wallet)
    if not canonical:
        return []
    # Every stored key that canonicalizes to this wallet is merged, including an
    # exact-case hit. Short-circuiting on the exact key first would return only
    # that slice and hide the rows stored under another casing of the same
    # wallet, splitting one wallet's history in two.
    rows: list[dict[str, Any]] = []
    for key, value in (history or {}).items():
        if wallet_identity(key) == canonical and isinstance(value, list):
            rows.extend(value)
    return rows


def event_side(row: dict[str, Any]) -> str:
    return str(row.get("side") or row.get("type") or "").strip().lower()


def asset_identity(row: dict[str, Any]) -> tuple[str, str] | None:
    """Canonical token identity: chain + contract address.

    A row without a contract address has no usable identity. Returning ``None``
    forces the caller to bucket such rows separately instead of attributing
    them to an arbitrary token.
    """
    address = str(row.get("address") or row.get("base_address") or "").strip()
    if not address:
        return None
    chain = str(row.get("chain") or "").strip().lower()
    return (chain, address)


def event_hash(row: Any) -> str:
    """The transaction hash of a row, or "" when the provider supplied none.

    Provider-neutral and deliberately tolerant: GMGN omits the hash on a large
    share of its stored rows, and treating a missing hash as a distinct value
    keyed against a present one is what let one on-chain event be counted twice.
    """
    if not isinstance(row, dict):
        return ""
    return str(row.get("transaction_hash") or row.get("tx_hash") or "").strip()


def event_core(row: Any) -> tuple | None:
    """The part of the event key that does not depend on a transaction hash.

    Asset identity plus event time, side, size and price. ``None`` means the row
    has no usable identity or no timestamp, i.e. the rows every consumer
    discards.
    """
    if not isinstance(row, dict):
        return None
    identity = asset_identity(row)
    if identity is None:
        return None
    timestamp = event_ts(row)
    if timestamp <= 0:
        return None
    return (
        identity,
        timestamp,
        event_side(row),
        round(event_usd(row), 6),
        round(event_price(row), 12),
    )


def has_trade_timestamp(row: Any) -> bool:
    """Whether the row's event time is a real trade time, not an observation.

    ``event_ts`` falls back to ``timestamp`` (when the row was *seen*) when
    ``trade_timestamp`` is absent. That fallback is the right thing for
    chronological ordering, but it is weak evidence for deciding that two rows
    are the same event: an observation time can coincide with another row's
    trade time by accident. Legacy GMGN rows carry no trade timestamp at all, so
    this is the discriminator that keeps cross-provider matching from collapsing
    them.
    """
    if not isinstance(row, dict):
        return False
    raw = row.get("trade_timestamp")
    return raw not in (None, "", 0, "0")


def same_event(candidate: Any, existing: Any) -> bool:
    """Whether two provider rows describe the same on-chain event.

    The single, provider-neutral rule used by both the history merge and the
    reconstruction, so a merge can never disagree with the consumer that later
    reads what was merged.

    The rule is deliberately asymmetric in what it will merge, because a false
    merge destroys a real record:

    * both rows carry a hash -> the hashes must be equal. Two different hashes
      are two different trades, whatever else matches, and the hash is the only
      thing that distinguishes two same-second trades of the same size. Two
      matching hashes are conclusive, so no further check is needed.
    * exactly one row carries a hash -> the hashless row cannot be checked
      against the hash, so the remaining stable fields decide. This is the
      cross-provider case that occurs whenever one provider omits the hash.
      Additionally both rows must carry a real ``trade_timestamp``; see
      :func:`has_trade_timestamp` for why an observation-time fallback is not
      strong enough evidence.
    * neither carries a hash -> the remaining fields decide, which is the
      pre-existing behaviour for two hashless rows and is left untouched.
    """
    left, right = event_core(candidate), event_core(existing)
    if left is None or right is None or left != right:
        return False
    left_hash, right_hash = event_hash(candidate), event_hash(existing)
    if left_hash and right_hash:
        return left_hash == right_hash
    if bool(left_hash) == bool(right_hash):
        # Both hashless: pre-existing rule, unchanged. This also means a row
        # always matches an identical copy of itself.
        return True
    return has_trade_timestamp(candidate) and has_trade_timestamp(existing)


def dedupe(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate observations while preserving chronological order.

    GMGN re-lists the same transaction across scans. Deduplicating is required
    for an honest hit rate, but a naive ``set`` would also drop the repeat buys
    that are the point of a "multiple buys of the same asset" test, so rows are
    matched on the full event identity via :func:`same_event` and sorted with
    the original index as a stable tie-breaker.

    Two rows whose hashes are both known and differ are always kept, so two real
    same-second trades stay two events. A hashless row is only ever collapsed
    against a hashed one when both carry a real ``trade_timestamp``, which keeps
    the existing stored dataset byte-identical in its counts.
    """
    kept: list[tuple[int, dict[str, Any]]] = []
    # Buckets are keyed by the hash-free core, so only rows that could possibly
    # be the same event are ever compared. A naive pairwise scan is O(n^2) and
    # takes minutes over the committed dataset.
    state: dict[tuple, list[Any]] = {}
    for index, row in enumerate(rows or []):
        core = event_core(row)
        if core is None:
            continue
        row_hash = event_hash(row)
        row_has_trade_time = has_trade_timestamp(row)
        bucket = state.get(core)
        if bucket is None:
            bucket = state[core] = [set(), False, False]
        seen_hashes, hashless_kept, hashed_with_trade_time_kept = bucket
        if row_hash:
            if row_hash in seen_hashes:
                continue
            # A kept hashless row with a real trade timestamp is this same event.
            if hashless_kept == "trade_time" and row_has_trade_time:
                continue
            seen_hashes.add(row_hash)
            if row_has_trade_time:
                bucket[2] = True
        else:
            # Identical to a kept hashless row, or to a kept hashed row that has
            # a real trade timestamp to compare against.
            if hashless_kept or (row_has_trade_time and hashed_with_trade_time_kept):
                continue
            bucket[1] = "trade_time" if row_has_trade_time else True
        kept.append((index, row))
    kept.sort(key=lambda pair: (event_ts(pair[1]), pair[0]))
    return [row for _, row in kept]


# Retained for existing callers and tests; the implementation is now shared.
_dedupe = dedupe


def _position_state(buy_usd: float, sell_usd: float) -> str:
    """Derive holding/trimmed/exited from qualified value, not row counts.

    One large exit can outnumber many small buys, so a transaction-count
    comparison misclassifies a closed position as still held.
    """
    if sell_usd <= 0:
        return POSITION_HOLDING
    if sell_usd >= buy_usd * 0.9:
        return POSITION_EXITED
    return POSITION_TRIMMED


def reconstruct_asset(rows: list[dict[str, Any]], window: int = WINDOW_SECONDS) -> dict[str, Any] | None:
    """Reconstruct one honest post-entry outcome for a single asset position.

    ``rows`` must all belong to one canonical identity and be chronologically
    sorted. The wallet's earliest qualified buy is the entry, per the existing
    first-entry rule. Everything after the entry inside the window is
    observation. A sell inside the window is a real exit; no sell is a current
    holding. No forward observation at all is UNKNOWN.
    """
    buys = [
        r for r in rows
        if event_side(r) == "buy" and event_price(r) > 0 and event_usd(r) >= MIN_ENTRY_USD
    ]
    if not buys:
        return None

    entry_row = min(buys, key=event_ts)
    entry_ts = event_ts(entry_row)
    entry_price = event_price(entry_row)
    if entry_ts <= 0 or entry_price <= 0:
        return None

    # The entry is this wallet's *earliest* buy of the asset, so a second buy is
    # a genuine re-entry rather than a duplicate snapshot of the same one.
    repeated_entry = len(buys) > 1

    observations = [
        r for r in rows
        if entry_ts < event_ts(r) <= entry_ts + window and event_price(r) > 0
    ]
    sells = [
        r for r in rows
        if event_side(r) == "sell" and event_usd(r) >= MIN_ENTRY_USD
        and entry_ts < event_ts(r) <= entry_ts + window
    ]

    buy_usd = sum(event_usd(r) for r in buys)
    sell_usd = sum(event_usd(r) for r in sells)

    chain, address = asset_identity(entry_row) or ("", "")

    record: dict[str, Any] = {
        "chain": chain,
        "address": address,
        "symbol": str(entry_row.get("symbol") or "").upper() or None,
        "entry_timestamp": entry_ts,
        "entry_price": round(entry_price, 12),
        "entry_usd": round(buy_usd, 2),
        "repeated_entry": repeated_entry,
        "qualified_buy_count": len(buys),
        "window_seconds": window,
        "observation_count": len(observations),
        "observation_complete": bool(observations),
        "exit_timestamp": None,
        "exit_price": None,
        "exit_usd": round(sell_usd, 2),
        "position_state": _position_state(buy_usd, sell_usd),
        "peak_multiple": None,
        "peak_timestamp": None,
        "mfe_pct": None,
        "mae_pct": None,
        "target_multiple": TARGET_MULTIPLE,
        "target_reached": None,
        "days_to_peak": None,
        "outcome": OUTCOME_UNKNOWN,
    }

    if sells:
        first_sell = min(sells, key=event_ts)
        record["exit_timestamp"] = event_ts(first_sell)
        record["exit_price"] = event_price(first_sell) or None
        record["exit_return_pct"] = (
            round((event_price(first_sell) / entry_price - 1) * 100, 3)
            if event_price(first_sell) > 0 else None
        )

    if observations:
        peak_row = max(observations, key=event_price)
        trough_row = min(observations, key=event_price)
        peak_price = event_price(peak_row)
        multiple = peak_price / entry_price
        mfe = (peak_price / entry_price - 1) * 100
        mae = (event_price(trough_row) / entry_price - 1) * 100
        peak_ts = event_ts(peak_row)
        record.update({
            "peak_multiple": round(multiple, 4),
            "peak_timestamp": peak_ts,
            "peak_price": peak_price,
            "mfe_pct": round(mfe, 3),
            "mae_pct": round(mae, 3),
            "target_reached": bool(multiple >= TARGET_MULTIPLE),
            "days_to_peak": round((peak_ts - entry_ts) / 86400.0, 3),
            "outcome": OUTCOME_SUCCESS if multiple >= TARGET_MULTIPLE else OUTCOME_OBSERVED_NO_TARGET,
        })

    return record


def classify(
    opportunities: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], float | None]:
    """Apply the PROVEN threshold to a wallet's reconstructed entries.

    Returns ``(history_class, observed, successes, unknown, win_rate)``.

    The win rate is computed over *observed* entries only, so an entry whose
    forward window is unobservable can never be counted as a loss. A wallet
    with entries but no observation is UNKNOWN, not 0%.
    """
    observed = [o for o in opportunities if o["outcome"] != OUTCOME_UNKNOWN]
    successes = [o for o in opportunities if o["outcome"] == OUTCOME_SUCCESS]
    unknown = [o for o in opportunities if o["outcome"] == OUTCOME_UNKNOWN]

    if not opportunities:
        return NO_HISTORY, observed, successes, unknown, None
    if not observed:
        # Genuine historical entries exist but no forward window is observable.
        # This is UNKNOWN, never a loss and never 0% performance.
        return ACTIVITY_BUT_UNPROVEN, observed, successes, unknown, None

    win_rate = round(len(successes) / len(observed) * 100.0, 1)
    proven = (
        len(observed) >= MIN_OBSERVED_ENTRIES
        and len(successes) >= MIN_SUCCESSFUL_ENTRIES
        and win_rate >= MIN_WIN_RATE_PCT
    )
    history_class = PROVEN if proven else ACTIVITY_BUT_UNPROVEN
    return history_class, observed, successes, unknown, win_rate


def reconstruct_wallet(
    wallet: str,
    rows: list[dict[str, Any]],
    chain: str = "",
) -> dict[str, Any]:
    """Reconstruct every historical entry of one wallet and classify it.

    Returns the same field names that ``gmgn_layer.analyze_wallet_activity``
    already publishes, so the GMGN layer and the Telegram report keep working
    unchanged, plus an explicit ``history_class`` and the evidence counters the
    report needs.
    """
    clean = dedupe(rows or [])
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in clean:
        identity = asset_identity(row)
        if identity is not None:
            grouped[identity].append(row)

    opportunities: list[dict[str, Any]] = []
    for identity in sorted(grouped):
        record = reconstruct_asset(grouped[identity])
        if record is not None:
            opportunities.append(record)

    # Chain scoping is provenance-aware. Only 17% of the stored rows carry a
    # chain (the GMGN CLI omits it on most rows, and only rows fetched after
    # the collector started stamping it have it), so a strict
    # ``chain == requested`` filter would silently discard the other 83% of the
    # history and manufacture a cold start. A row whose chain is unknown is
    # not evidence that it belongs to a different chain, and the contract
    # address is the real discriminator, so an unattributed row is kept and a
    # *known* mismatch is dropped. This never credits a wallet with a
    # different asset, because that is decided by the address.
    if chain:
        wanted = str(chain).strip().lower()
        scoped = [
            o for o in opportunities
            if not o["chain"] or o["chain"] == wanted
        ]
        if len(scoped) != len(opportunities):
            opportunities = scoped

    history_class, observed, successes, unknown, win_rate = classify(opportunities)

    return {
        "wallet": wallet,
        "chain": chain or None,
        "history_class": history_class,
        "proven": history_class == PROVEN,
        "evidence_source": "OFFLINE_STORED_HISTORY",
        "opportunities": len(opportunities),
        "observed_opportunities": len(observed),
        "unknown_opportunities": len(unknown),
        "successful_pre_pump_entries": len(successes),
        "pre_pump_win_rate": win_rate,
        "entries_with_exit": sum(1 for o in opportunities if o["exit_timestamp"] is not None),
        "entries_holding": sum(
            1 for o in opportunities
            if o["exit_timestamp"] is None and o["outcome"] != OUTCOME_UNKNOWN
        ),
        "criteria": {
            "min_observed_entries": MIN_OBSERVED_ENTRIES,
            "min_win_rate_pct": MIN_WIN_RATE_PCT,
            "min_successful_entries": MIN_SUCCESSFUL_ENTRIES,
            "target_multiple": TARGET_MULTIPLE,
            "min_entry_usd": MIN_ENTRY_USD,
            "window_seconds": WINDOW_SECONDS,
        },
        "recent_examples": sorted(
            successes, key=lambda o: o.get("peak_multiple") or 0, reverse=True
        )[:5],
        # ``all_examples`` is a display-sized sample for the Telegram report.
        # ``entries`` is the complete reconstructed set, so summary statistics
        # are never computed from a truncated view.
        "all_examples": sorted(opportunities, key=lambda o: o["entry_timestamp"], reverse=True)[:12],
        "entries": opportunities,
    }


def load_history(path: Path | str = HISTORY_FILE) -> dict[str, list[dict[str, Any]]]:
    """Load stored GMGN history. A missing/malformed file yields no evidence."""
    target = Path(path)
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(wallet): rows
        for wallet, rows in raw.items()
        if isinstance(rows, list)
    }


def validate_history(
    data: dict[str, list[dict[str, Any]]] | None = None,
    path: Path | str = HISTORY_FILE,
) -> dict[str, Any]:
    """Replay every stored wallet and return the classification summary."""
    data = data if data is not None else load_history(path)
    now_ts = int(datetime.now(timezone.utc).timestamp())

    profiles: dict[str, dict[str, Any]] = {}
    counts = {name: 0 for name in CLASSES}
    entries = observed_entries = with_peak = with_exit = holding = 0

    for wallet, rows in (data or {}).items():
        profile = reconstruct_wallet(wallet, rows)
        profiles[wallet] = profile
        counts[profile["history_class"]] = counts.get(profile["history_class"], 0) + 1
        for record in profile["entries"]:
            entries += 1
            if record["observation_complete"]:
                observed_entries += 1
            if record["peak_multiple"] is not None:
                with_peak += 1
            if record["exit_timestamp"] is not None:
                with_exit += 1
            elif record["observation_complete"]:
                holding += 1

    proven = [w for w, p in profiles.items() if p["history_class"] == PROVEN]
    return {
        "mode": "OFFLINE_HISTORICAL_VALIDATION",
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "history_path": str(path),
        "wallets_evaluated": len(profiles),
        "wallets_with_sufficient_evidence": sum(
            1 for p in profiles.values() if p["observed_opportunities"] > 0
        ),
        "proven_wallets": len(proven),
        "proven_wallet_list": sorted(proven),
        "classification_counts": counts,
        "reconstructed_entries": entries,
        "entries_with_observation": observed_entries,
        "entries_with_valid_peak": with_peak,
        "entries_with_valid_exit": with_exit,
        "entries_holding_no_exit": holding,
        "unknown_windows": sum(p["unknown_opportunities"] for p in profiles.values()),
        "criteria": {
            "min_observed_entries": MIN_OBSERVED_ENTRIES,
            "min_win_rate_pct": MIN_WIN_RATE_PCT,
            "min_successful_entries": MIN_SUCCESSFUL_ENTRIES,
            "target_multiple": TARGET_MULTIPLE,
            "min_entry_usd": MIN_ENTRY_USD,
            "window_seconds": WINDOW_SECONDS,
            "identity": "chain + contract address (never symbol alone)",
        },
        "notes": [
            "UNKNOWN windows are never recorded as 0% performance or as a loss.",
            "Proven requires depth AND productivity AND multiple genuine successes.",
            "Read-only: no artifact written, no order placed, no execution enabled.",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(validate_history(), ensure_ascii=False, indent=2))
