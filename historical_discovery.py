"""Source-neutral historical wallet discovery.

This module is the *first half* of the historical discovery pipeline:

    source adapters
      -> normalized historical observations
      -> identity normalization
      -> additive deduplication with provenance preserved
      -> trade reconstruction
      -> candidate wallet generation

Everything downstream of this file consumes the observations it produces. No
source is scored here, and nothing here is imported by the scanner, the
confluence engine, the risk engine, trade readiness, or paper trading. Zerion,
Nansen and Hyperliquid all enter through the same adapters, so adding a fourth
provider cannot turn into a fourth scoring system.

Why an observation rather than a new history format
---------------------------------------------------
``wallet_history_validation`` already owns the event identity this repository
trusts, and its accessors are provider-neutral by construction. An observation
produced here therefore *reuses those field names* (``chain``, ``address``,
``side``, ``amount_usd``, ``price_usd``, ``timestamp``, ``trade_timestamp``,
``transaction_hash``) and is simultaneously a valid history row. The extra
provenance keys are simply ignored by those accessors. That is deliberate: a
parallel "discovery row" format would have needed its own dedup rule, and the
two rules would eventually disagree about whether one on-chain event is one
event or two -- which is exactly the bug class this pipeline exists to remove.

Discovery is not conviction
---------------------------
Historical activity, a historical candidate, a qualified historical wallet and
a proven wallet are four different statements, and this module keeps them apart:

``HISTORICAL_ACTIVITY``        the wallet is on the record at all
``HISTORICAL_CANDIDATE``       enough activity to be worth evaluating
``QUALIFIED_HISTORICAL_WALLET`` enough *sized* activity to be measured
``PROVEN``                     the existing forward-only criteria, unchanged

Only the last one is a judgement about wallet quality, and it is never computed
here. ``PROVEN`` is copied from ``whv.reconstruct_wallet`` so the discovery
engine cannot mint a proven wallet out of volume. Volume is not skill.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import wallet_history_validation as whv
import wallet_quality_engine as wqe
import zerion_history as zh

# ---------------------------------------------------------------------------
# Source names
# ---------------------------------------------------------------------------
SOURCE_GMGN = "gmgn"
SOURCE_ZERION = "zerion"
SOURCE_NANSEN = "nansen"
SOURCE_HYPERLIQUID = "hyperliquid"

KNOWN_SOURCES = (SOURCE_GMGN, SOURCE_ZERION, SOURCE_NANSEN, SOURCE_HYPERLIQUID)

#: Observation kinds. A source that cannot prove a row was a *trade* says so
#: rather than guessing, because a balance snapshot counted as a trade would
#: create a historical entry that never happened.
KIND_TRADE = "TRADE"
KIND_BALANCE_SNAPSHOT = "BALANCE_SNAPSHOT"
KIND_UNKNOWN = "UNKNOWN"

#: Discovery tiers, weakest first. See the module docstring.
TIER_NONE = "NO_HISTORICAL_ACTIVITY"
TIER_ACTIVITY = "HISTORICAL_ACTIVITY"
TIER_CANDIDATE = "HISTORICAL_CANDIDATE"
TIER_QUALIFIED = "QUALIFIED_HISTORICAL_WALLET"
TIER_PROVEN = whv.PROVEN

DISCOVERY_TIERS = (TIER_ACTIVITY, TIER_CANDIDATE, TIER_QUALIFIED, TIER_PROVEN)

#: The qualified-trade floor is the *existing* quality-engine threshold, not a
#: new number. Discovery decides who deserves to be evaluated; it does not get
#: to decide what "qualified" means.
QUALIFIED_TRADE_USD = wqe.THRESHOLD_USD

#: A candidate needs enough history to be worth reconstructing at all. This is a
#: discovery-volume floor, deliberately far below the quality engine's own
#: ``>= 3 qualified buys`` requirement, so it cannot be mistaken for evidence.
MIN_CANDIDATE_TRADES = 3

#: The repository's chain vocabulary. Providers disagree: Nansen says
#: ``ethereum``, Zerion says ``ethereum``, GMGN says ``eth``. Left untranslated
#: those become three different ``asset_identity`` keys for one token, so the
#: same position is reconstructed as three separate entries. An unknown chain is
#: kept verbatim and marked unknown rather than guessed at.
CHAIN_ALIASES: dict[str, str] = {
    "ethereum": "eth", "eth": "eth", "mainnet": "eth",
    "eth-mainnet": "eth", "ethereum-mainnet": "eth", "erc20": "eth", "eth-erc20": "eth",
    "binance-smart-chain": "bsc", "bsc": "bsc", "bnb": "bsc", "bnb-chain": "bsc",
    "bsc-mainnet": "bsc", "binance": "bsc", "bnb-smart-chain": "bsc", "bepe20": "bsc",
    "base": "base", "base-mainnet": "base", "base-erc20": "base",
    "solana": "sol", "sol": "sol", "solana-mainnet": "sol", "spl": "sol",
    "hyperevm": "hyperliquid", "hyperliquid": "hyperliquid", "hyper-evm": "hyperliquid",
    "hyperevm-mainnet": "hyperliquid",
}


def canonical_chain(value: Any) -> str:
    """Map a provider chain name onto the repository's short name.

    Returns ``""`` for an absent chain. That is an absence, not a contradiction,
    and the existing identity rules already tolerate it: 73% of stored GMGN rows
    carry no chain at all, so inventing one would attribute history that the
    data does not support.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return CHAIN_ALIASES.get(text, text)

# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceAdapter:
    """How one provider names its fields, and what its timestamps mean.

    Each source is described once, here. Adding a provider is one entry in
    ``ADAPTERS`` plus its own reader, not a new pipeline and not a new identity
    rule.
    """

    source: str
    address: tuple[str, ...] = ()
    chain: tuple[str, ...] = ()
    symbol: tuple[str, ...] = ()
    side: tuple[str, ...] = ()
    amount_usd: tuple[str, ...] = ()
    price_usd: tuple[str, ...] = ()
    quantity: tuple[str, ...] = ()
    tx_hash: tuple[str, ...] = ()
    #: Fields that are a real on-chain trade time.
    trade_time: tuple[str, ...] = ()
    #: Fields that are when the row was seen, or a snapshot instant.
    observed_time: tuple[str, ...] = ()
    #: Provider-side record identifier, when the provider has one.
    record_id: tuple[str, ...] = ()
    #: Provider direction vocabulary -> the wallet-centric buy/sell convention.
    side_map: Mapping[str, str] = field(default_factory=dict)
    #: Whether a row from this source is a trade at all.
    kind: str = KIND_TRADE
    #: Archive directories this source writes to, relative to the repo root.
    archive_dir: str = ""

    def field(self, row: Mapping[str, Any], names: tuple[str, ...]) -> Any:
        for name in names:
            if name in row:
                value = row[name]
                if value not in (None, "", [], {}):
                    return value
        return None


_GMGN = SourceAdapter(
    source=SOURCE_GMGN,
    address=("address", "base_address", "token_address"),
    chain=("chain",),
    symbol=("symbol", "token_symbol"),
    side=("side", "type"),
    amount_usd=("amount_usd", "usd", "value"),
    price_usd=("price_usd", "price"),
    quantity=("token_amount", "quantity"),
    tx_hash=("transaction_hash", "tx_hash"),
    trade_time=("trade_timestamp",),
    observed_time=("timestamp",),
    archive_dir="wallet_archive/raw/gmgn/activity",
)

# GMGN omits the chain on most stored rows. That is an absence, not a
# contradiction, and the existing identity rules already handle it.
_ZERION = SourceAdapter(
    source=SOURCE_ZERION,
    address=("address", "base_address"),
    chain=("chain",),
    symbol=("symbol", "token_symbol"),
    side=("side", "type", "direction"),
    amount_usd=("amount_usd", "usd", "value"),
    price_usd=("price_usd", "price"),
    quantity=("token_amount", "quantity"),
    tx_hash=("transaction_hash", "tx_hash"),
    trade_time=("trade_timestamp",),
    observed_time=("timestamp",),
    archive_dir=str(zh.ARCHIVE_DIR),
)

# Nansen's archived artifact is a *profiler historical-balances* response: a
# series of balance snapshots, not a trade ledger. It carries no side, no entry
# price and no transaction hash, and its timestamp is the snapshot instant. So
# it is normalized as a snapshot with the trade fields left unknown. Treating it
# as a trade would invent entries.
_NANSEN = SourceAdapter(
    source=SOURCE_NANSEN,
    address=("token_address", "address", "contract_address", "mint"),
    chain=("chain", "chain_id", "blockchain", "network"),
    symbol=("token_symbol", "symbol", "name"),
    side=(),
    amount_usd=("balance_value", "value_usd", "usd_value", "value"),
    price_usd=("token_price", "price_usd", "price", "price_usd_last"),
    quantity=("balance", "balance_raw", "token_balance", "quantity"),
    tx_hash=(),
    trade_time=(),
    observed_time=("timestamp", "date", "last_seen"),
    kind=KIND_BALANCE_SNAPSHOT,
    archive_dir="wallet_archive/raw/nansen/historical_balances",
)

# Hyperliquid is declared here so the schema is fixed and testable before any
# integration exists. There is no Hyperliquid client in this repository: this
# adapter only describes row shapes, and it invents no endpoint or credential.
# A perpetual trader is a real trader, so the row kind is a trade; what is
# genuinely unknown today is the *chain* identity, which stays empty rather than
# being guessed at.
_HYPERLIQUID = SourceAdapter(
    source=SOURCE_HYPERLIQUID,
    address=("coin", "asset", "symbol"),
    chain=(),
    symbol=("coin", "asset"),
    side=("side", "dir", "direction"),
    amount_usd=("usd", "notional_usd", "closed_pnl_usd", "value"),
    price_usd=("px", "price_usd", "price", "entry_px"),
    quantity=("sz", "size", "quantity"),
    tx_hash=("hash", "tx_hash", "transaction_hash"),
    trade_time=("time", "timestamp", "trade_timestamp"),
    observed_time=("fetched_at",),
    record_id=("tid", "id"),
    # Hyperliquid reports A/B (ask/bid) and verbal open/close forms.
    side_map={
        "a": "sell", "b": "buy", "ask": "sell", "bid": "buy",
        "buy": "buy", "sell": "sell",
        "open long": "buy", "close long": "sell",
        "open short": "sell", "close short": "buy",
        "long": "buy", "short": "sell",
    },
    archive_dir="wallet_archive/raw/hyperliquid/fills",
)

ADAPTERS: dict[str, SourceAdapter] = {
    adapter.source: adapter
    for adapter in (_GMGN, _ZERION, _NANSEN, _HYPERLIQUID)
}


def adapter_for(source: Any) -> SourceAdapter:
    """Look up a source adapter, falling back to a tolerant generic reader.

    An unknown source is not an error: it is normalized through the same union
    of aliases the shared accessors use, and it is *labelled* as unrecognised so
    a new provider cannot quietly claim a known source's name.
    """
    key = str(source or "").strip().lower()
    if key in ADAPTERS:
        return ADAPTERS[key]
    return SourceAdapter(source=key or "unknown")


def _as_seconds(value: Any) -> int:
    """Coerce a provider timestamp to epoch seconds; 0 when unusable."""
    if value is None or value == "":
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        # ISO-8601, the shape Nansen and several APIs use.
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return number // 1000 if number > 10**12 else number


def _as_float(value: Any) -> float | None:
    """A float, or ``None`` when the provider supplied no usable number.

    A missing value stays missing. Turning it into ``0.0`` would let a row pass
    a size comparison it never earned.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _map_side(adapter: SourceAdapter, raw: Any) -> str | None:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if adapter.side_map:
        return adapter.side_map.get(text, text if text in ("buy", "sell") else None)
    return text if text in ("buy", "sell") else None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_observation(
    row: Any,
    source: str,
    *,
    wallet: Any = None,
    fetched_at: Any = None,
    record_id: Any = None,
    raw_payload: Any = None,
) -> dict[str, Any] | None:
    """Turn one provider row into a source-neutral historical observation.

    The result is simultaneously a valid ``wallet_history_validation`` history
    row, because it reuses that module's field names. Provenance is added
    alongside, never in place of, the values themselves.

    Returns ``None`` for a row with no usable wallet or no usable asset
    identity. A row that cannot be identified cannot be deduplicated, so
    accepting it would let every re-ingest of it add another copy.
    """
    if not isinstance(row, Mapping):
        return None
    adapter = adapter_for(source)
    wallet_key = whv.wallet_identity(wallet if wallet not in (None, "") else row.get("wallet"))
    if not wallet_key:
        return None

    address = adapter.field(row, adapter.address)
    address = str(address).strip() if address else ""
    if not address:
        return None

    chain = adapter.field(row, adapter.chain)
    chain = canonical_chain(chain)

    symbol = adapter.field(row, adapter.symbol)
    side = _map_side(adapter, adapter.field(row, adapter.side))
    amount_usd = _as_float(adapter.field(row, adapter.amount_usd))
    price_usd = _as_float(adapter.field(row, adapter.price_usd))
    quantity = _as_float(adapter.field(row, adapter.quantity))

    trade_raw = adapter.field(row, adapter.trade_time)
    observed_raw = adapter.field(row, adapter.observed_time)
    trade_ts = _as_seconds(trade_raw)
    observed_ts = _as_seconds(observed_raw) or trade_ts
    # A source that declares no authoritative trade field never has one. That is
    # the difference between "no trade time" and "no trade". Coerced with
    # bool(): the candidate field lists are tuples, and leaking one here would
    # make a truthiness check elsewhere silently pass on an empty list.
    trade_time_known = bool(trade_ts) and adapter.kind == KIND_TRADE and bool(adapter.trade_time)

    tx_hash = adapter.field(row, adapter.tx_hash)
    tx_hash = str(tx_hash).strip() if tx_hash else None

    if adapter.kind != KIND_TRADE:
        kind = adapter.kind
    elif side and trade_time_known:
        kind = KIND_TRADE
    else:
        kind = KIND_UNKNOWN

    observation: dict[str, Any] = {
        # identity
        "wallet": wallet_key,
        "chain": chain,
        "address": address,
        "symbol": str(symbol).upper() if symbol else None,
        # economics
        "side": side,
        "amount_usd": amount_usd,
        "price_usd": price_usd,
        "quantity": quantity,
        # time
        "timestamp": observed_ts,
        "trade_timestamp": trade_ts if trade_time_known else None,
        "trade_time_known": trade_time_known,
        "transaction_hash": tx_hash,
        # provenance
        "source": adapter.source,
        "sources": [adapter.source],
        "source_record_id": (
            str(record_id) if record_id not in (None, "") else
            (str(adapter.field(row, adapter.record_id)) if adapter.record_id and adapter.field(row, adapter.record_id) else None)
        ),
        "fetched_at": _as_seconds(fetched_at) or None,
        "kind": kind,
        "confidence": _confidence(chain, tx_hash, side, trade_time_known, amount_usd),
        # the source-specific payload, kept verbatim. Re-deriving a field later
        # must never require spending quota on the provider again.
        "raw": raw_payload if raw_payload is not None else dict(row),
    }
    return observation


def _confidence(
    chain: str,
    tx_hash: str,
    side: str | None,
    trade_time_known: bool,
    amount_usd: float | None,
) -> dict[str, Any]:
    """How much this observation can be trusted, and why.

    Recorded per observation rather than inferred later, so a reader can see
    that a missing chain or an unproven side is a known limitation instead of a
    silent gap. Nothing here changes any threshold.
    """
    reasons: list[str] = []
    if not chain:
        reasons.append("chain_unknown")
    if not tx_hash:
        reasons.append("no_transaction_hash")
    if not side:
        reasons.append("side_unknown")
    if not trade_time_known:
        reasons.append("no_trade_timestamp")
    if amount_usd is None:
        reasons.append("no_usd_value")
    if not reasons:
        return {"identity": "exact", "trade_time": True, "value": True, "reasons": []}
    identity = "exact" if (chain and tx_hash) else "partial" if chain else "unknown"
    return {
        "identity": identity,
        "trade_time": bool(trade_time_known),
        "value": amount_usd is not None,
        "reasons": reasons,
    }


def normalize_rows(
    rows: Iterable[Any],
    source: str,
    *,
    wallet: Any = None,
    fetched_at: Any = None,
) -> list[dict[str, Any]]:
    """Normalize a batch of provider rows, dropping only unusable ones."""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        observation = normalize_observation(row, source, wallet=wallet, fetched_at=fetched_at)
        if observation is not None:
            out.append(observation)
    return out


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------


def read_gmgn_history(path: Path | str = whv.HISTORY_FILE) -> dict[str, list[dict[str, Any]]]:
    """Read stored GMGN history and normalize it as observations."""
    stored = whv.load_history(path)
    return {
        wallet: normalize_rows(rows, SOURCE_GMGN, wallet=wallet)
        for wallet, rows in stored.items()
    }


def read_zerion_archive(directory: Path | str = zh.ARCHIVE_DIR) -> dict[str, list[dict[str, Any]]]:
    """Read archived Zerion snapshots and normalize them as observations.

    ``load_zerion_history`` already combines overlapping snapshots without
    duplicating a row, so the archive is read once, not per file.
    """
    loaded = zh.load_zerion_history(directory)
    return {
        wallet: normalize_rows(rows, SOURCE_ZERION, wallet=wallet)
        for wallet, rows in loaded.items()
    }


def _json_paths(target: Path) -> list[Path]:
    """Every JSON file at ``target``, whether it is a file or a directory tree.

    Readers take either form on purpose. A backfill is normally read as a whole
    archive, but a single freshly written artifact is a legitimate thing to
    normalize, and refusing it would push callers back into hand-rolled parsing.
    """
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    return sorted(p for p in target.rglob("*.json") if p.is_file())


def read_nansen_archive(
    directory: Path | str = _NANSEN.archive_dir,
) -> dict[str, list[dict[str, Any]]]:
    """Read archived Nansen profiler artifacts and normalize them as snapshots.

    The Nansen response rows are stored verbatim by ``nansen_backfill`` and are
    not reinterpreted anywhere in this repository, so this reader stays tolerant:
    it takes whatever field names the payload actually uses, keeps the raw row,
    and reports how many records it could not identify rather than dropping them
    silently.
    """
    folder = Path(directory)
    history: dict[str, list[dict[str, Any]]] = {}
    for path in _json_paths(folder):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        records = payload.get("records")
        if not isinstance(records, list):
            continue
        # A Nansen chain may live one directory down; the payload is preferred
        # and the path is only a fallback.
        wallet = whv.wallet_identity(payload.get("wallet")) or whv.wallet_identity(path.stem)
        chain = canonical_chain(payload.get("chain"))
        fetched_at = payload.get("fetched_at")
        observations: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            row = dict(record)
            if chain and not row.get("chain"):
                row["chain"] = chain
            observation = normalize_observation(
                row, SOURCE_NANSEN, wallet=wallet, fetched_at=fetched_at, raw_payload=record,
            )
            if observation is not None:
                observations.append(observation)
        if wallet and observations:
            history.setdefault(wallet, []).extend(observations)
    return history


def read_hyperliquid_rows(
    rows: Any = None,
    *,
    wallet: Any = None,
    fetched_at: Any = None,
) -> list[dict[str, Any]]:
    """Normalize historical perpetual-trader rows.

    Accepts an iterable of rows, a path to a JSON file, or an already-parsed
    mapping, because there is no Hyperliquid client here yet and the row contract
    should be usable from a fixture today and from a real fetch later.

    There is no Hyperliquid client in this repository. This function exists so
    the row contract is fixed and testable now, and so a future integration is a
    reader plus a fetch, not a new discovery or scoring path.
    """
    if isinstance(rows, (str, Path)):
        for path in _json_paths(Path(rows)):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping):
                rows = payload.get("fills") or payload.get("rows") or payload.get("data") or []
                break
        else:
            return []
    elif isinstance(rows, Mapping):
        rows = rows.get("fills") or rows.get("rows") or rows.get("data") or []
    return normalize_rows(rows, SOURCE_HYPERLIQUID, wallet=wallet, fetched_at=fetched_at)


READERS: dict[str, Callable[..., Any]] = {
    SOURCE_GMGN: read_gmgn_history,
    SOURCE_ZERION: read_zerion_archive,
    SOURCE_NANSEN: read_nansen_archive,
}


# ---------------------------------------------------------------------------
# Identity + additive dedup with provenance
# ---------------------------------------------------------------------------


def wallet_key(wallet: Any) -> str:
    return whv.wallet_identity(wallet)


def _bucket_key(row: Mapping[str, Any]) -> tuple | None:
    """Chain-free lookup key, delegated so it cannot drift from the merge rule."""
    return zh._chain_free_key(row)


def merge_sources(
    base: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    incoming: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Fold any number of sources together without inflating or losing events.

    Additive-or-neutral, like ``zerion_history.merge_sources`` but for an
    arbitrary source set:

    * the base is accepted **verbatim**. Its rows are never matched against each
      other, because a stored dataset may legitimately hold two rows the tolerant
      matcher would call the same event, and re-deduplicating it would quietly
      shrink committed history. Only *incoming* rows are tested for a match;
    * a row that matches an accepted row is *corroborated*, never appended, so
      two providers describing one on-chain event produce one event;
    * a row that is genuinely new is appended and the bucket is re-sorted, so
      chronological order survives;
    * no accepted row is ever modified except to record which sources saw it.

    The result is therefore always at least as large as the base: a provider
    conflict can add evidence, never remove a row. Re-running the same ingestion
    is a no-op, which is what makes a repeatable backfill safe to schedule.
    """
    merged: dict[str, list[dict[str, Any]]] = {}
    wallets: list[str] = []
    for mapping in (base, incoming):
        for wallet in mapping or {}:
            key = wallet_key(wallet)
            if key and key not in wallets:
                wallets.append(key)

    for wallet in wallets:
        rows: list[dict[str, Any]] = []
        index: dict[tuple, list[dict[str, Any]]] = {}

        def accept(row: Mapping[str, Any]) -> None:
            """Add a row without testing it against what is already here."""
            rows.append(row)
            key = _bucket_key(row)
            if key is not None:
                index.setdefault(key, []).append(row)

        for mapping in (base, incoming):
            is_base = mapping is base
            for original_wallet, source_rows in (mapping or {}).items():
                if wallet_key(original_wallet) != wallet:
                    continue
                for raw in source_rows or []:
                    observation = _as_observation(raw)
                    if observation is None:
                        continue
                    if is_base:
                        # Stored history is taken as-is.
                        accept(observation)
                        continue
                    candidates = index.get(_bucket_key(observation)) or []
                    matched = next(
                        (existing for existing in candidates
                         if zh.same_event_tolerant(observation, existing)),
                        None,
                    )
                    if matched is not None:
                        zh._record_corroboration(matched, observation)
                        continue
                    accept(observation)

        if rows:
            rows.sort(key=lambda item: whv.event_ts(item))
        merged[wallet] = rows
    return merged


def _as_observation(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Accept either a stored history row or an already-normalized observation."""
    if not isinstance(row, Mapping):
        return None
    if "sources" in row and "confidence" in row:
        row = dict(row)
    else:
        sources = row.get("source")
        row = normalize_observation(
            row,
            str(sources) if sources else "unknown",
            wallet=row.get("wallet"),
            fetched_at=row.get("fetched_at"),
        )
        if row is None:
            return None
    return row


def observation_provenance(row: Any) -> dict[str, Any]:
    """The provenance of one observation, safe to embed in a registry entry."""
    if not isinstance(row, Mapping):
        return {"sources": [], "source_record_ids": [], "corroborations": []}
    record_id = row.get("source_record_id")
    return {
        "sources": zh.provenance_of(row),
        "source_record_ids": [str(record_id)] if record_id else [],
        "corroborations": zh._corroborations(dict(row)),
        "confidence": row.get("confidence") if isinstance(row.get("confidence"), dict) else None,
        "trade_time_known": bool(row.get("trade_time_known")),
    }


# ---------------------------------------------------------------------------
# Discovery tiers + candidates
# ---------------------------------------------------------------------------


def evaluate_wallet(
    wallet: Any,
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Evaluate one wallet's historical record without ever calling it proven.

    The reconstruction and the PROVEN verdict are delegated to
    ``wallet_history_validation``. This function only adds the weaker, purely
    descriptive discovery tiers below ``QUALIFIED_HISTORICAL_WALLET``, so a
    wallet that trades a great deal is never presented as a good trader.
    """
    key = wallet_key(wallet)
    clean = [dict(r) for r in (rows or []) if isinstance(r, Mapping)]
    if not key:
        return {
            "wallet": wallet, "discovery_tier": TIER_NONE, "proven": False,
            "observations": 0, "trades": 0, "qualified_trades": 0,
            "sources": [], "chains": [], "assets_traded": 0,
            "proven_status": whv.NO_HISTORY,
            "reason": "no canonical wallet identity",
        }

    trades = [r for r in clean if r.get("kind") == KIND_TRADE and whv.event_side(r) in ("buy", "sell")]
    snapshots = [r for r in clean if r.get("kind") == KIND_BALANCE_SNAPSHOT]
    qualified = [r for r in trades if (whv.event_usd(r) or 0.0) >= QUALIFIED_TRADE_USD]

    # The one and only PROVEN source. Unchanged criteria, unchanged threshold.
    profile = whv.reconstruct_wallet(key, clean)
    proven = profile["history_class"] == whv.PROVEN

    if proven:
        tier = TIER_PROVEN
        reason = "existing forward-only PROVEN criteria met"
    elif len(qualified) >= 1:
        tier = TIER_QUALIFIED
        reason = f"{len(qualified)} trade(s) at or above ${QUALIFIED_TRADE_USD:,.0f}"
    elif len(trades) >= MIN_CANDIDATE_TRADES:
        tier = TIER_CANDIDATE
        reason = f"{len(trades)} trade(s) with no qualified size yet"
    elif clean:
        tier = TIER_ACTIVITY
        reason = (
            f"{len(snapshots)} balance snapshot(s), no reconstructable trade"
            if not trades else f"{len(trades)} trade(s), below the candidate floor"
        )
    else:
        tier = TIER_NONE
        reason = "no usable observation"

    sources = sorted({s for r in clean for s in zh.provenance_of(r)})
    chains = sorted({str(r.get("chain") or "").lower() for r in clean if r.get("chain")})
    assets = {
        (str(r.get("chain") or "").lower(), str(r.get("address") or ""))
        for r in clean if r.get("address")
    }

    return {
        "wallet": key,
        "discovery_tier": tier,
        "proven": proven,
        "reason": reason,
        "observations": len(clean),
        "trades": len(trades),
        "balance_snapshots": len(snapshots),
        "qualified_trades": len(qualified),
        "sources": sources,
        "chains": chains,
        "assets_traded": len(assets),
        "last_seen": max((whv.event_ts(r) for r in clean), default=0) or None,
        "multi_source": len(sources) > 1,
        "corroborated_events": sum(1 for r in clean if zh.has_corroboration(r)),
        # ``proven_status`` rather than whv's ``history_class``: the registry
        # contract names this field, and two names for one verdict is how the
        # two drift apart later. The *value* is whv's, verbatim.
        "proven_status": profile["history_class"],
        "pre_pump_win_rate": profile["pre_pump_win_rate"],
        "reconstructed_entries": profile["opportunities"],
    }


def discover(
    history: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> dict[str, dict[str, Any]]:
    """Evaluate every wallet in a unified historical record."""
    return {
        wallet_key(wallet): evaluate_wallet(wallet, rows)
        for wallet, rows in (history or {}).items()
    }


def discover_candidates(
    history: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Wallets worth historical evaluation, strongest evidence first.

    This is the discovery engine's output. It is a *candidate* list, and every
    entry still has to survive the existing forward-only evaluation before it can
    be anything else. A candidate is a request to look, never a verdict.
    """
    records = discover(history)
    candidates = [
        record for record in records.values()
        if record["discovery_tier"] in (TIER_CANDIDATE, TIER_QUALIFIED, TIER_PROVEN)
    ]

    def rank(record: Mapping[str, Any]) -> tuple:
        tier_rank = {
            TIER_PROVEN: 0, TIER_QUALIFIED: 1, TIER_CANDIDATE: 2,
            TIER_ACTIVITY: 3, TIER_NONE: 4,
        }
        return (
            tier_rank.get(record["discovery_tier"], 9),
            -record["qualified_trades"],
            -record["trades"],
            -(record["pre_pump_win_rate"] or 0.0),
            record["wallet"],
        )

    candidates.sort(key=rank)
    return candidates


def discovery_summary(
    history: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> dict[str, Any]:
    """Aggregate counts per tier, for an operator reading one line."""
    records = discover(history)
    tiers = {tier: 0 for tier in (TIER_NONE,) + DISCOVERY_TIERS}
    sources: dict[str, int] = {}
    for record in records.values():
        tiers[record["discovery_tier"]] = tiers.get(record["discovery_tier"], 0) + 1
        for source in record["sources"]:
            sources[source] = sources.get(source, 0) + 1
    return {
        "mode": "HISTORICAL_DISCOVERY_READ_ONLY",
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wallets": len(records),
        "wallets_by_tier": tiers,
        "wallets_by_source": sources,
        "candidates": len(discover_candidates(history)),
        "multi_source_wallets": sum(1 for r in records.values() if r["multi_source"]),
        "criteria": {
            "qualified_trade_usd": QUALIFIED_TRADE_USD,
            "min_candidate_trades": MIN_CANDIDATE_TRADES,
            "proven_delegated_to": "wallet_history_validation.reconstruct_wallet",
            "proven_criteria": {
                "min_observed_entries": whv.MIN_OBSERVED_ENTRIES,
                "min_successful_entries": whv.MIN_SUCCESSFUL_ENTRIES,
                "min_win_rate_pct": whv.MIN_WIN_RATE_PCT,
                "target_multiple": whv.TARGET_MULTIPLE,
            },
            "note": (
                "Historical activity and a high trade count never imply PROVEN. "
                "Only the existing forward-only criteria can set that flag."
            ),
        },
    }
