"""Provider adapter: fold archived Zerion history into the wallet-history model.

This module is a *storage and identity* layer only. It is deliberately not
imported by the scanner, by wallet quality, by the confluence engine, or by any
scoring or readiness path. Nothing here changes a threshold, a score, or a
trade decision, and nothing here can place an order.

Why it exists
--------------
``wallet_history_validation`` is the single authority on what counts as one
on-chain event. ``zerion_layer`` already merges through those same helpers, but
the stored history was collected before Zerion existed, so a Zerion row
describing an event GMGN already holds would be appended as a *second* trade.
That inflates every derived figure downstream: qualified buy counts, qualified
buy USD, entry prices, and wallet statistics.

The legacy collision
--------------------
Most stored GMGN rows predate hash propagation: they carry no
``transaction_hash`` and no ``trade_timestamp``. Two things make those rows
dangerous to match blindly:

1. A naive hash-free match collapses two genuinely different same-second trades.
   ``whv.same_event`` refuses that case unless both sides carry a real
   ``trade_timestamp``, which is why 17,487 existing GMGN-vs-GMGN legacy pairs
   stay separate.
2. The GMGN ``chain`` field is empty on 73% of stored rows, so a strict
   ``(chain, address)`` identity can never equal a Zerion row's, which always
   names a real chain.

So this adapter does not loosen ``same_event``. It adds one narrowly-scoped
rule on top of it: a Zerion row may *enrich* a hashless legacy GMGN row in
place, but never adds a second trade for it. The GMGN row is the record of
truth; Zerion only supplies the transaction hash and the authoritative trade
time that the legacy row is missing.

Public surface
--------------
``load_zerion_history``   read archived snapshots, trade time preserved
``merge_sources``         combine GMGN + Zerion without inflating counts
``to_quality_row``        project a row onto the wallet-quality row contract
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import wallet_history_validation as whv

SOURCE_GMGN = "gmgn"
SOURCE_ZERION = "zerion"

ARCHIVE_DIR = Path("wallet_archive/raw/zerion/transactions")

#: Fields GMGN populates that Zerion cannot know. They stay absent on Zerion
#: rows rather than being guessed, because a fabricated ``is_open_or_close``
#: would invent an entry and an empty-but-present ``maker_tags`` would read as
#: "not smart money" instead of "unknown".
GMGN_ONLY_FIELDS = (
    "is_open_or_close",
    "maker_tags",
    "maker_info",
    "price_change",
    "peak_multiple",
    "first_1_2x_timestamp",
    "first_1_5x_timestamp",
    "first_2x_timestamp",
    "pnl_percent",
    "pnl_usd",
    "realized_pnl_usd",
)

#: The row contract ``wallet_quality_engine`` reads. Zerion supplies all of it;
#: nothing here invents a value.
QUALITY_FIELDS = (
    "chain", "address", "symbol", "side", "amount_usd", "price_usd",
    "timestamp", "trade_timestamp", "transaction_hash", "source",
)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def event_key(row: Any) -> tuple | None:
    """A stable, provider-neutral key for one historical event.

    Wraps :func:`whv.event_core` rather than reimplementing it, so this module
    can never drift from the identity the consumer uses. The transaction hash is
    appended when the row has one, which is what keeps two real same-second
    trades apart.
    """
    core = whv.event_core(row)
    if core is None:
        return None
    return core + (whv.event_hash(row),)


def tag_provenance(row: dict[str, Any], sources: Iterable[str]) -> dict[str, Any]:
    """Record which providers stand behind a row, without losing the original.

    ``sources`` is the union of every provider that described this event, so a
    row corroborated by both GMGN and Zerion is identifiable as such. The
    original row fields are preserved; only provider bookkeeping is added.
    """
    ordered: list[str] = []
    for source in sources:
        if source and source not in ordered:
            ordered.append(source)
    row["sources"] = ordered
    if not row.get("source") and ordered:
        # The first provider to describe the event owns the row, which for an
        # enriched legacy row is GMGN, not the provider that supplied the hash.
        row["source"] = ordered[0]
    return row


def provenance_of(row: Any) -> list[str]:
    """The providers that stand behind a row; empty when untagged."""
    if not isinstance(row, dict):
        return []
    raw = row.get("sources")
    if not isinstance(raw, list):
        source = row.get("source")
        return [source] if source else []
    return [str(x) for x in raw if x]


def _corroborations(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("corroborated_by")
    return list(raw) if isinstance(raw, list) else []


def _record_corroboration(row: dict[str, Any], other: dict[str, Any]) -> None:
    """Note that a second provider independently saw the event ``row`` holds.

    The row itself is left alone: it is already one event, and re-deriving its
    fields from the other provider would let a lossy feed overwrite stored
    values. What is kept is the evidence trail, so a reader can tell the
    difference between a row only GMGN saw and a row two providers agree on.
    """
    sources = provenance_of(other)
    if not sources:
        return
    tag_provenance(row, provenance_of(row) + sources)
    seen = _corroborations(row)
    identity = whv.event_hash(other) or ""
    for note in seen:
        if note.get("transaction_hash") == identity and note.get("sources") == sources:
            return
    seen.append({
        "sources": sources,
        "transaction_hash": identity or None,
        "trade_timestamp": other.get("trade_timestamp"),
    })
    row["corroborated_by"] = seen


def has_corroboration(row: Any) -> bool:
    """True when more than one provider described this event."""
    if not isinstance(row, dict):
        return False
    return len(provenance_of(row)) > 1 or bool(_corroborations(row))


def same_event_tolerant(row: Any, other: Any) -> bool:
    """:func:`whv.same_event`, plus one narrow allowance for a missing chain.

    ``same_event`` compares event cores, and a core carries the chain. 73% of
    stored GMGN rows leave ``chain`` empty, while a Zerion row always names a
    real chain, so two descriptions of one trade never share a core. Measured
    over the committed dataset, trusting ``same_event`` alone duplicates 100% of
    hashed rows whose chain is empty.

    The allowance is deliberately narrow:

    * the chain has to be the *only* disagreement, and an empty chain is an
      absence rather than a contradiction, so it cannot outvote the rest;
    * two hashes must actually match;
    * when only one side carries a hash, both must carry a real
      ``trade_timestamp`` -- the same evidence ``whv.same_event`` already
      requires, and the reason the stored legacy pairs stay separate;
    * everything else (time, side, size, price) must agree.

    A chain that each side *names* and that disagrees is still two transfers
    and is never overruled, because 2 stored hashes already map to more than
    one core (a transaction with several transfers).
    """
    if whv.same_event(row, other):
        return True
    if not isinstance(row, dict) or not isinstance(other, dict):
        return False
    left_hash, right_hash = whv.event_hash(row), whv.event_hash(other)
    both_hashed = bool(left_hash) and bool(right_hash)
    one_hashed = bool(left_hash) != bool(right_hash)
    if not (both_hashed or one_hashed):
        # Neither side has a hash: leave the pre-existing rule alone.
        return False
    if both_hashed and left_hash != right_hash:
        return False
    if one_hashed and not (whv.has_trade_timestamp(row) and whv.has_trade_timestamp(other)):
        return False
    left, right = whv.asset_identity(row), whv.asset_identity(other)
    if left is None or right is None or not left[1] or not right[1]:
        return False
    if left[0] and right[0] and left[0] != right[0]:
        return False
    if left[1].lower() != right[1].lower():
        return False
    return _non_chain_axes_equal(row, other)


def _non_chain_axes_equal(row: dict[str, Any], other: dict[str, Any]) -> bool:
    """Trade time, side, size and price, with observation-time fallback.

    A missing price reads as 0.0 on both sides, which is equal and therefore
    not a contradiction. Treating 0.0 as "unknown" would be wrong: 0.0 is
    common in the stored data, and 11% of rows carry a zero price.
    """
    if whv.event_ts(row) != whv.event_ts(other):
        return False
    if whv.event_side(row) != whv.event_side(other):
        return False
    for reader in (whv.event_usd, whv.event_price):
        mine, theirs = reader(row), reader(other)
        if abs(mine - theirs) > 1e-9 * max(abs(mine), abs(theirs), 1.0):
            return False
    return True


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _iter_archive_files(directory: Path | str) -> list[Path]:
    folder = Path(directory)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.json") if p.is_file())


def load_zerion_history(
    directory: Path | str = ARCHIVE_DIR,
) -> dict[str, list[dict[str, Any]]]:
    """Load every archived Zerion snapshot into one wallet -> rows mapping.

    The directory holds one file per fetch, named ``<wallet>_<fetched_at>.json``
    so repeated backfills of the same wallet accumulate instead of overwriting.
    All snapshots are read, then combined through the shared dedup, so
    re-ingesting an overlapping fetch cannot duplicate a row.

    ``trade_timestamp`` is authoritative throughout. A row's ``timestamp`` is
    the moment the row was *seen*; replacing a real trade time with it would
    shift every entry forward by the collection lag, which is measured in
    seconds to hours. When a snapshot row has no ``trade_timestamp`` the
    observation time is used and flagged, so the gap stays visible rather than
    silently becoming a trade time.
    """
    history: dict[str, list[dict[str, Any]]] = {}
    for path in _iter_archive_files(directory):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A partially written snapshot must not take the history down.
            continue
        if not isinstance(payload, dict):
            continue
        wallet = whv.wallet_identity(payload.get("wallet")) or path.stem
        records = payload.get("records")
        records = records if isinstance(records, list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            row = dict(record)
            trade_ts = row.get("trade_timestamp")
            if trade_ts in (None, "", 0, "0"):
                # No trade time available: keep the observation time but say so.
                row["trade_timestamp"] = None
                row["trade_time_known"] = False
            else:
                row["trade_time_known"] = True
            row["source"] = row.get("source") or SOURCE_ZERION
            tag_provenance(row, [row["source"]])
            history.setdefault(str(wallet), []).append(row)

    # A wallet is usually archived more than once. Combining the snapshots with
    # the shared identity rule is what stops an overlapping fetch from turning
    # one event into several. This touches only Zerion rows, so it cannot alter
    # a stored GMGN count.
    return {
        wallet: whv.dedupe(rows)
        for wallet, rows in history.items()
    }


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------
def _chain_free_key(row: Any) -> tuple | None:
    """Index key that ignores the chain, for finding candidates to compare.

    This is a *lookup* key, not a decision. It only narrows the rows a Zerion row
    is compared against; whether two of them are the same event is always
    decided by :func:`same_event_tolerant`. Because the chain is omitted, one
    bucket can hold a handful of rows across chains, which is why the winner is
    still checked rather than taken.
    """
    if not isinstance(row, dict):
        return None
    identity = whv.asset_identity(row)
    if identity is None or not identity[1]:
        return None
    timestamp = whv.event_ts(row)
    if timestamp <= 0:
        return None
    return (
        identity[1].lower(),
        timestamp,
        whv.event_side(row),
        round(whv.event_usd(row), 6),
        round(whv.event_price(row), 12),
    )


def _legacy_match_key(row: Any) -> tuple | None:
    """Identity for a hashless legacy row, tolerant of the missing chain.

    Only usable for the enrichment rule below, and only for rows that carry no
    hash and no trade time. The address is matched without a chain because
    73% of stored GMGN rows have an empty ``chain`` while every Zerion row
    names one; the stored dataset never places one address on two real chains,
    so this cannot join two different tokens.
    """
    if not isinstance(row, dict):
        return None
    if whv.event_hash(row) or whv.has_trade_timestamp(row):
        return None
    identity = whv.asset_identity(row)
    if identity is None or not identity[1]:
        return None
    timestamp = whv.event_ts(row)
    if timestamp <= 0:
        return None
    return (
        identity[1],
        timestamp,
        whv.event_side(row),
        round(whv.event_usd(row), 6),
        round(whv.event_price(row), 12),
    )


def _zerion_match_key(row: Any) -> tuple | None:
    """The same key for a Zerion row, which always has a real trade time."""
    if not isinstance(row, dict):
        return None
    if not whv.has_trade_timestamp(row):
        return None
    identity = whv.asset_identity(row)
    if identity is None or not identity[1]:
        return None
    timestamp = whv.event_ts(row)
    if timestamp <= 0:
        return None
    return (
        identity[1],
        timestamp,
        whv.event_side(row),
        round(whv.event_usd(row), 6),
        round(whv.event_price(row), 12),
    )


def merge_sources(
    gmgn: dict[str, list[dict[str, Any]]] | None,
    zerion: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Combine GMGN and Zerion history without inflating any count.

    The arguments are never mutated; every row in the result is a copy. For each
    wallet, each incoming Zerion row is resolved in this order:

    1. **Already the same event.** ``whv.same_event`` says so -> skipped, no
       change. This covers a Zerion row repeating a hashed GMGN row with the
       same hash.
    2. **Enrichment of a hashless legacy GMGN row.** The row's asset address,
       event time, side, size and price all agree, the GMGN row carries no hash
       and no trade time, and the Zerion row has a real one. The GMGN row is
       kept and *filled in* with the transaction hash and the authoritative
       trade time, tagged as seen by both providers. This is the 73%-of-rows
       case, and it is the one that would otherwise double every count.
    3. **Genuinely new.** Appended exactly once.

    Two hashes that are both known and differ are never joined: ``same_event``
    returns False and no legacy key matches, so both rows survive as two real
    trades. When the evidence does not prove identity, the safe branch is to
    keep both rows, because a lost trade is a worse error than a duplicate one.
    """
    merged: dict[str, list[dict[str, Any]]] = {}
    gmgn = gmgn if isinstance(gmgn, dict) else {}
    zerion = zerion if isinstance(zerion, dict) else {}

    wallets: list[str] = []
    for wallet in list(gmgn) + [w for w in zerion if w not in gmgn]:
        key = whv.wallet_identity(wallet) or str(wallet)
        if key not in wallets:
            wallets.append(key)

    for wallet in wallets:
        rows: list[dict[str, Any]] = []
        # Legacy rows are indexed by the chain-tolerant key. Only rows that
        # qualify for enrichment are indexed, so a normal GMGN row is never a
        # candidate for this rule.
        legacy: dict[tuple, dict[str, Any]] = {}
        # Everything already present, indexed by a chain-free key. Cores cannot
        # be used as the index because a stored row with an empty chain and a
        # Zerion row that names one never share a core, even when they are the
        # same event. The candidate list per key stays small, and the winner is
        # still chosen by same_event_tolerant, not by the key.
        present: dict[tuple, list[dict[str, Any]]] = {}
        legacy: dict[tuple, list[dict[str, Any]]] = {}
        for raw in gmgn.get(wallet) or []:
            if not isinstance(raw, dict):
                continue
            row = tag_provenance(dict(raw), provenance_of(raw) or [SOURCE_GMGN])
            rows.append(row)
            key = _chain_free_key(row)
            if key is not None:
                present.setdefault(key, []).append(row)
            match_key = _legacy_match_key(row)
            if match_key is not None:
                legacy.setdefault(match_key, []).append(row)

        appended = 0
        for raw in zerion.get(wallet) or []:
            if not isinstance(raw, dict):
                continue
            incoming = dict(raw)
            if whv.event_core(incoming) is None:
                continue
            key = _chain_free_key(incoming)
            candidates = present.get(key) or []
            matched = next(
                (existing for existing in candidates
                 if same_event_tolerant(incoming, existing)),
                None,
            )
            if matched is not None:
                # Rule 1: the same event from two providers stays one record.
                # Record the corroboration, so the surviving row still says
                # which providers independently described it.
                _record_corroboration(matched, incoming)
                continue
            match_key = _zerion_match_key(incoming)
            existing = None
            if match_key is not None:
                for candidate in legacy.get(match_key) or []:
                    # An absent chain is compatible; a named one must agree.
                    chain = whv.asset_identity(candidate)[0]
                    if not chain or chain == whv.asset_identity(incoming)[0]:
                        existing = candidate
                        break
            if existing is not None:
                # Rule 2: enrich in place. The GMGN row stays the record; the
                # Zerion row contributes only what GMGN could not supply.
                existing["transaction_hash"] = whv.event_hash(incoming)
                existing["trade_timestamp"] = incoming.get("trade_timestamp")
                existing["trade_time_known"] = True
                if not existing.get("symbol") and incoming.get("symbol"):
                    existing["symbol"] = incoming["symbol"]
                if not existing.get("chain") and incoming.get("chain"):
                    existing["chain"] = incoming["chain"]
                _record_corroboration(existing, incoming)
                continue
            tag_provenance(incoming, provenance_of(incoming) or [SOURCE_ZERION])
            rows.append(incoming)
            if key is not None:
                present.setdefault(key, []).append(incoming)
            appended += 1

        if appended:
            # The stored history is kept in chronological order; adding rows
            # must not leave the bucket unsorted. Sorting never changes a count.
            rows.sort(key=lambda item: whv.event_ts(item) if isinstance(item, dict) else 0)
        # Deliberately no re-dedup of the GMGN rows: the stored buckets contain
        # rows the identity rule would collapse, and this function promises an
        # additive-or-neutral merge, never a smaller event count.
        merged[wallet] = rows

    return merged


# ---------------------------------------------------------------------------
# Quality-engine projection
# ---------------------------------------------------------------------------
def to_quality_row(row: Any) -> dict[str, Any] | None:
    """Project one history row onto the ``wallet_quality_engine`` row contract.

    ``wallet_quality_engine._ts`` reads ``trade_timestamp`` first and falls back
    to ``timestamp``, so a Zerion row's authoritative trade time is what the
    engine now sees, while the observation time stays on the row for auditing.

    GMGN-only fields are left absent rather than filled with a default. A
    missing key means "this provider does not report it"; a zero would mean
    "the value is zero", and ``is_open_or_close = 0`` in particular is read as
    a position open.
    """
    if not isinstance(row, dict):
        return None
    identity = whv.asset_identity(row)
    if identity is None or whv.event_ts(row) <= 0:
        return None

    trade_ts = row.get("trade_timestamp")
    has_trade_time = whv.has_trade_timestamp(row)
    # Seconds, normalized from milliseconds if the provider used them, so this
    # matches what the quality engine and the reconstruction both read.
    timestamp = whv.event_ts(row)

    projected = {
        "chain": identity[0],
        "address": identity[1],
        "symbol": str(row.get("symbol") or "").upper() or None,
        "side": whv.event_side(row),
        "amount_usd": whv.event_usd(row),
        "price_usd": whv.event_price(row),
        # trade time when known, observation time otherwise
        "timestamp": timestamp,
        # preserved exactly as the provider reported it
        "trade_timestamp": trade_ts if has_trade_time else None,
        "trade_time_known": has_trade_time,
        "transaction_hash": whv.event_hash(row) or None,
        "source": row.get("source"),
        "sources": provenance_of(row),
    }
    return projected


def quality_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project a whole wallet history, dropping rows with no usable identity."""
    projected = (to_quality_row(row) for row in rows or [])
    return [row for row in projected if row is not None]
