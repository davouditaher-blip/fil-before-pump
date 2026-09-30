"""Zerion Wallet Transactions API as an ADDITIONAL historical wallet source.

This module is provider/integration only. It fetches decoded transaction history
for a wallet, follows Zerion's opaque cursor pagination, retries transient
failures, and emits records in the same shape the repository's wallet history
already uses. It is strictly read-only:

* it never places, signs or simulates an order;
* it never writes to ``gmgn_wallet_history.json`` unless a caller explicitly
  hands it a history mapping to merge into;
* it never rewrites, reorders away or deletes an existing GMGN row.

Zerion is an *addition* to the existing GMGN history, not a replacement. GMGN
stays the primary collector; this provider supplies independent decoded history
(wider chain coverage, per-transfer USD value and price, canonical contract
addresses) that the offline reconstruction in ``wallet_history_validation`` can
consume with no other change.

Endpoint
--------
``GET https://api.zerion.io/v1/wallets/{address}/transactions/``
with HTTP Basic auth where the username is the API key and the password is
empty, i.e. ``Authorization: Basic base64("<ZERION_API_KEY>:")``.

Environment
-----------
``ZERION_API_KEY`` is the only credential this module reads. It is never
printed, logged, stored in an artifact, or placed in ``PROVIDER_STATE``.

Honesty rules this normalizer obeys
------------------------------------
Zerion reports real on-chain facts (transfer direction, quantity, historical
price, USD value, contract address, block time). It does not report a
current-price / entry-price ratio, so this normalizer deliberately emits **no**
``price_change``, ``peak_multiple`` or ``first_*_timestamp`` keys rather than
inventing them. No module in this repository indexes those keys on a history
row (all use ``.get``), so their absence is safe. Post-entry peak reconstruction
for Zerion-sourced rows therefore comes from the wallet's own later trades, via
``whv.reconstruct_asset`` -- the same honest, observation-backed path GMGN rows
already use, and a window with no forward observation stays UNKNOWN rather than
being recorded as 0%.

Likewise a transaction whose ``status`` is not ``confirmed``, one Zerion flags
as spam, a ``self`` transfer, and a native-asset transfer (no contract address,
so no token identity) are all dropped, each with a reason, instead of being
mangled into a plausible-looking trade.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import quote, urlencode, urlsplit

import wallet_history_validation as whv

ZERION_API_KEY = os.environ.get("ZERION_API_KEY", "")
API_HOST = "api.zerion.io"
API_BASE = f"https://{API_HOST}"
TRANSACTIONS_PATH = "/v1/wallets/{address}/transactions/"
SOURCE = "zerion"
ARCHIVE_DIR = Path("wallet_archive/raw/zerion/transactions")

# The chains this project already tracks, in the repository's short names. GMGN
# calls them sol/bsc/base/eth; Zerion names them solana/binance-smart-chain/
# base/ethereum. Both spellings are accepted as input and the normalized record
# always carries the short name, so a Zerion row and a GMGN row of the same
# token land on the same ``whv.asset_identity`` key.
SUPPORTED_CHAINS = ("sol", "bsc", "base", "eth")

CHAIN_ALIASES = {
    "eth": "ethereum",
    "ethereum": "ethereum",
    "mainnet": "ethereum",
    "base": "base",
    "bsc": "binance-smart-chain",
    "bnb": "binance-smart-chain",
    "binance": "binance-smart-chain",
    "binance-smart-chain": "binance-smart-chain",
    "sol": "solana",
    "solana": "solana",
}

REPO_CHAIN_NAMES = {
    "ethereum": "eth",
    "base": "base",
    "binance-smart-chain": "bsc",
    "solana": "sol",
}

CHAIN_FAMILY = {
    "ethereum": "evm",
    "base": "evm",
    "binance-smart-chain": "evm",
    "solana": "solana",
}

# Documented ``filter[operation_types]`` enum. A value outside this set is
# rejected locally instead of being sent and answered with a 400.
OPERATION_TYPES = (
    "approve", "bid", "burn", "claim", "delegate", "deploy", "deposit",
    "execute", "mint", "receive", "revoke", "revoke_delegation", "send",
    "trade", "withdraw",
)

# Trade filtering defaults to ``trade`` only. A swap is the operation this
# project's entry/exit reconstruction is defined against; approvals, claims
# and plain transfers are not entries and would otherwise dilute the history.
DEFAULT_OPERATION_TYPES = ("trade",)

MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 100
PAGE_SIZE = 100
MAX_PAGES = 50
MAX_URL_LENGTH = 2000

REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
PAGE_PACING_SECONDS = 0.2
# Run-scoped request budget. This is a metered plan, and a wallet fan-out
# (candidates x wallets) is unbounded, exactly as with GMGN's ACTIVITY_CALL_BUDGET.
REQUEST_BUDGET = 1500

# Zerion's own guidance: retry 429/500/503, never retry 400/401/422 (they
# return the same answer however many times they are sent).
RETRYABLE_STATUS = (429, 500, 503)
# 402 is the status the live API actually returns for a request it will not
# serve for want of a credential (observed against api.zerion.io), alongside the
# documented 401. Both are credential problems, not transient ones, so neither
# is retried and neither is mistaken for rate limiting.
TERMINAL_STATUS = (400, 401, 402, 403, 404, 422)

# Explicit User-Agent. Measured against the live API: a request carrying the
# default `Python-urllib/x.y` agent is refused at the Cloudflare edge with
# "Error 1010: The site owner has blocked access based on your browser's
# signature" (HTTP 403) before it ever reaches Zerion. A descriptive agent is
# accepted. `python-requests` happens to be allowed today, but depending on a
# library's default agent to stay allowed is a hidden dependency, so the header
# is set explicitly.
USER_AGENT = "fil-before-pump/1.0 (+https://github.com/davouditaher-blip/fil-before-pump)"

# Run-scoped provider state. Deliberately holds no credential: the key lives in
# ZERION_API_KEY and is never copied in here, so provider_state() is always
# safe to print into a report or an artifact.
PROVIDER_STATE: dict[str, Any] = {
    "source": SOURCE,
    "endpoint": TRANSACTIONS_PATH,
    "configured": bool(ZERION_API_KEY),
    "requests_made": 0,
    "rate_limited": False,
    "reason": None,
    "detail": None,
    "at": None,
}

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------
def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_iso_seconds(value: Any) -> int:
    """Parse an ISO-8601 timestamp into epoch seconds; 0 when unparseable."""
    text = str(value or "").strip()
    if not text:
        return 0
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def to_millis(value: Any) -> int | None:
    """Normalise a date-range bound to the 13-digit milliseconds Zerion demands.

    Accepts epoch seconds, epoch milliseconds, an ISO-8601 string or a ``YYYY-MM-DD``
    date. Zerion rejects anything that is not exactly 13 digits with a 400, so a
    bad bound is refused here rather than being spent as a quota-consuming error.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = int(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            number = int(text)
        else:
            seconds = parse_iso_seconds(text)
            if seconds <= 0:
                return None
            number = seconds * 1000
    # 1e12 is the practical second/millisecond boundary (1e12 ms is year 33658).
    if number <= 10**12:
        number *= 1000
    if not 1_000_000_000_000 <= number <= 9_999_999_999_999:
        return None
    return number


def zerion_chain(name: Any) -> str:
    """Canonical Zerion chain id for a project short name or alias."""
    return CHAIN_ALIASES.get(str(name or "").strip().lower(), "")


def repo_chain(chain_id: Any) -> str:
    """Repository short chain name for a Zerion chain id."""
    key = str(chain_id or "").strip().lower()
    return REPO_CHAIN_NAMES.get(key, key)


def address_family(address: Any) -> str:
    """``evm``, ``solana`` or ``""`` for an unrecognised address.

    Used to refuse an impossible chain filter before it costs a request: Zerion
    answers an EVM chain filter for a Solana address with a 400.
    """
    text = str(address or "").strip()
    if not text:
        return ""
    if ":" in text:
        prefix, _, remainder = text.partition(":")
        if prefix.strip().lower() in set(CHAIN_ALIASES):
            return address_family(remainder.strip())
    if _EVM_ADDRESS.fullmatch(text):
        return "evm"
    if _BASE58_ADDRESS.fullmatch(text):
        return "solana"
    return ""


def provider_state() -> dict[str, Any]:
    """Snapshot of the run-scoped provider state. Never contains the API key."""
    return dict(PROVIDER_STATE)


def is_rate_limited() -> bool:
    return bool(PROVIDER_STATE.get("rate_limited"))


def _mark_rate_limited(reason: str, detail: str = "") -> None:
    PROVIDER_STATE["rate_limited"] = True
    PROVIDER_STATE["reason"] = str(reason)
    PROVIDER_STATE["detail"] = str(detail)[-400:]
    PROVIDER_STATE["at"] = datetime.now(timezone.utc).isoformat()
    print(f"ZERION RATE LIMIT — stopping Zerion requests for this run: {reason} {detail[-160:]}")


def _clear_rate_limit() -> None:
    if PROVIDER_STATE.get("rate_limited"):
        PROVIDER_STATE["rate_limited"] = False
        PROVIDER_STATE["reason"] = None
        PROVIDER_STATE["detail"] = None
        PROVIDER_STATE["at"] = None


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------
def _clean_chains(chains: Iterable[Any] | None, address: str = "") -> list[str]:
    if chains is None:
        return []
    if isinstance(chains, str):
        candidates = [part for part in chains.replace(";", ",").split(",")]
    else:
        candidates = list(chains)
    resolved: list[str] = []
    for candidate in candidates:
        if candidate is None or str(candidate).strip() == "":
            continue
        chain_id = zerion_chain(candidate)
        if not chain_id:
            raise ValueError(
                f"unsupported Zerion chain: {str(candidate).strip()!r} "
                f"(supported: {', '.join(sorted(set(CHAIN_ALIASES)))})"
            )
        if chain_id not in resolved:
            resolved.append(chain_id)
    family = address_family(address)
    if family:
        for chain_id in resolved:
            if CHAIN_FAMILY.get(chain_id) != family:
                raise ValueError(
                    f"chain {chain_id} is not compatible with a {family} wallet address"
                )
    return resolved


def _clean_operation_types(operation_types: Sequence[Any] | None) -> list[str]:
    if operation_types is None:
        operation_types = DEFAULT_OPERATION_TYPES
    if isinstance(operation_types, str):
        candidates = [part for part in operation_types.replace(";", ",").split(",")]
    else:
        candidates = list(operation_types)
    resolved: list[str] = []
    for candidate in candidates:
        name = str(candidate or "").strip().lower()
        if not name:
            continue
        if name not in OPERATION_TYPES:
            raise ValueError(
                f"unsupported Zerion operation type: {name!r} "
                f"(supported: {', '.join(OPERATION_TYPES)})"
            )
        if name not in resolved:
            resolved.append(name)
    return resolved


def transactions_url(address: Any) -> str:
    """The wallet-transactions URL for an address, with the path segment escaped."""
    text = str(address or "").strip()
    if not text:
        raise ValueError("wallet address is required")
    return API_BASE + TRANSACTIONS_PATH.format(address=quote(text, safe=""))


def build_params(
    address: Any = "",
    chains: Iterable[Any] | None = None,
    operation_types: Sequence[Any] | None = DEFAULT_OPERATION_TYPES,
    start: Any = None,
    end: Any = None,
    include_trash: bool = False,
    page_size: int = PAGE_SIZE,
    currency: str = "usd",
    asset_types: Sequence[Any] | None = None,
    search_query: str | None = None,
) -> dict[str, str]:
    """Build the first-page query string.

    ``start``/``end`` accept seconds, milliseconds or an ISO string and are sent
    as the 13-digit ``filter[min_mined_at]`` / ``filter[max_mined_at]`` bounds the
    endpoint requires. ``operation_types`` is the trade filter and defaults to
    ``trade``. Spam is excluded unless ``include_trash`` is set.
    """
    try:
        size = int(page_size)
    except (TypeError, ValueError):
        raise ValueError(f"page_size must be an integer, got {page_size!r}") from None
    if not MIN_PAGE_SIZE <= size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between {MIN_PAGE_SIZE} and {MAX_PAGE_SIZE}")

    params: dict[str, str] = {"page[size]": str(size)}

    currency_name = str(currency or "usd").strip().lower()
    if currency_name:
        params["currency"] = currency_name

    resolved_chains = _clean_chains(chains, str(address or ""))
    if resolved_chains:
        params["filter[chain_ids]"] = ",".join(resolved_chains)

    resolved_ops = _clean_operation_types(operation_types)
    if resolved_ops:
        params["filter[operation_types]"] = ",".join(resolved_ops)

    if asset_types:
        names = [str(item or "").strip().lower() for item in asset_types]
        names = [name for name in names if name]
        if names:
            params["filter[asset_types]"] = ",".join(dict.fromkeys(names))

    if start is not None and start != "":
        millis = to_millis(start)
        if millis is None:
            raise ValueError(f"unusable history start date: {start!r}")
        params["filter[min_mined_at]"] = str(millis)
    if end is not None and end != "":
        millis = to_millis(end)
        if millis is None:
            raise ValueError(f"unusable history end date: {end!r}")
        params["filter[max_mined_at]"] = str(millis)
    if (
        "filter[min_mined_at]" in params
        and "filter[max_mined_at]" in params
        and int(params["filter[min_mined_at]"]) > int(params["filter[max_mined_at]"])
    ):
        raise ValueError("history start date is after the end date")

    # Zerion's own default is no_filter; opt into dropping spam explicitly
    # rather than inheriting it silently, so the archive records the decision.
    params["filter[trash]"] = "no_filter" if include_trash else "only_non_trash"

    query = str(search_query or "").strip()
    if query:
        params["filter[search_query]"] = query

    return params


def _is_trusted_url(url: Any) -> bool:
    """Only follow a cursor that stays on the Zerion production host.

    ``links.next`` is followed verbatim, as the API requires, and the request
    carries Basic-auth credentials. A cursor pointing anywhere else must never
    receive the API key, so a non-Zerion host is refused.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc.lower() == API_HOST


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _default_get(url, params, headers, auth, timeout):
    """Live HTTP GET.

    ``requests`` is imported lazily so this module and its tests import and run
    without the dependency present, matching the repo's CI which only installs it
    for the jobs that actually call a provider.
    """
    import requests

    return requests.get(url, params=params, headers=headers, auth=auth, timeout=timeout)


def _header(headers: Any, name: str) -> str:
    for key, value in dict(headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _error_detail(response: Any, status: int) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts = [
                str(item.get("detail") or item.get("title") or "")
                for item in errors
                if isinstance(item, dict)
            ]
            joined = "; ".join(part for part in parts if part)
            if joined:
                return joined[:300]
    return str(getattr(response, "text", "") or "")[:300]


def _quota_exhausted(headers: Any) -> str:
    """Day/month quota exhaustion, where waiting cannot possibly help.

    Deliberately excludes the per-second limit: that one resets within a second
    and is a normal, retryable 429, so treating it as terminal would abandon a
    page that one short sleep would have returned.
    """
    for header, label in (
        ("RateLimit-Org-Day-Remaining", "daily"),
        ("RateLimit-Org-Month-Remaining", "monthly"),
    ):
        raw = _header(headers, header).strip()
        if raw in ("0", "0.0"):
            return f"{label} quota exhausted"
    return ""


def _retry_delay(status: int, headers: Any, attempt: int) -> float:
    retry_after = _header(headers, "Retry-After")
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), BACKOFF_CAP_SECONDS * 2))
        except ValueError:
            pass
    if status == 429:
        reset = _header(headers, "RateLimit-Org-Second-Reset")
        if reset:
            try:
                return max(0.0, min(float(reset) + 0.1, BACKOFF_CAP_SECONDS * 2))
            except ValueError:
                pass
    return min(BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)), BACKOFF_CAP_SECONDS)


def _respect_second_limit(headers: Any, sleep: Callable[[float], None]) -> None:
    """Wait out the per-second window when the provider says it is spent.

    The per-second limit resets within a second, so this is a pacing decision,
    not a rate-limit verdict: paging straight through a spent window is what
    turns a healthy run into a 429.
    """
    remaining = _header(headers, "RateLimit-Org-Second-Remaining").strip()
    if remaining not in ("0", "0.0"):
        return
    reset = _header(headers, "RateLimit-Org-Second-Reset").strip()
    try:
        wait = float(reset)
    except ValueError:
        return
    if wait > 0:
        sleep(min(wait, BACKOFF_CAP_SECONDS))


def request_headers() -> dict[str, str]:
    """Headers for every Zerion request. Carries no credential."""
    return {"Accept": "application/json", "User-Agent": USER_AGENT}


def fetch_page(
    url: str,
    params: dict[str, str] | None = None,
    api_key: str | None = None,
    http_get: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    """GET one Zerion page with bounded exponential backoff.

    Behaviour differs deliberately from ``gmgn_layer.run_gmgn_cli``, which treats
    a rate limit as terminal for the run because GMGN states that retrying during
    a ban *extends* it. Zerion documents the opposite: retry 429 with exponential
    backoff, honouring ``Retry-After`` and ``RateLimit-Org-Second-Reset``. So a
    transient 429/500/503 is retried, and only an exhausted daily/monthly quota
    (where no wait can help) stops the run.

    Returns ``{"ok", "status", "payload", "headers", "error", "attempts"}`` plus
    ``terminal`` and ``reason`` when the provider will not serve the request. A
    missing API key short-circuits before any request is issued, so an unkeyed
    run cannot quietly look like an empty wallet.
    """
    key = ZERION_API_KEY if api_key is None else str(api_key)
    getter = http_get or _default_get
    total = max(1, int(attempts))
    result: dict[str, Any] = {
        "ok": False, "status": 0, "payload": {}, "headers": {},
        "error": None, "attempts": 0, "terminal": False, "reason": None,
    }
    if not str(key).strip():
        result["error"] = "missing_api_key"
        return result
    if not _is_trusted_url(url):
        result["error"] = "untrusted_url"
        return result

    delay = BACKOFF_BASE_SECONDS
    for attempt in range(1, total + 1):
        result["attempts"] = attempt
        PROVIDER_STATE["requests_made"] = int(PROVIDER_STATE.get("requests_made", 0)) + 1
        try:
            response = getter(
                url, params, request_headers(), (str(key), ""), REQUEST_TIMEOUT
            )
        except Exception as exc:
            result["status"] = 0
            result["error"] = f"transport_error: {type(exc).__name__}: {exc}"
            if attempt < total:
                sleep(delay)
                delay = min(delay * 2, BACKOFF_CAP_SECONDS)
                continue
            return result

        status = int(getattr(response, "status_code", 0) or 0)
        headers = dict(getattr(response, "headers", {}) or {})
        result["status"] = status
        result["headers"] = headers

        if 200 <= status < 300:
            try:
                payload = response.json()
            except Exception as exc:
                result["error"] = f"invalid_json: {type(exc).__name__}"
                if attempt < total:
                    sleep(delay)
                    delay = min(delay * 2, BACKOFF_CAP_SECONDS)
                    continue
                return result
            if not isinstance(payload, dict):
                result["error"] = "unexpected_payload"
                return result
            result.update(ok=True, payload=payload, error=None)
            _clear_rate_limit()
            return result

        detail = _error_detail(response, status)
        if status in RETRYABLE_STATUS:
            exhausted = _quota_exhausted(headers)
            if exhausted:
                _mark_rate_limited("QUOTA_EXHAUSTED", f"{status} {exhausted}")
                result["error"] = f"http_{status}: {exhausted}"
                return result
            if attempt < total:
                wait = _retry_delay(status, headers, attempt)
                sleep(wait)
                delay = min(delay * 2, BACKOFF_CAP_SECONDS)
                continue
        elif status in TERMINAL_STATUS:
            # A terminal status is reported as a distinct reason, because the
            # consumer must be able to tell "this wallet has no history" apart
            # from "this request will never be served". A 401/402 is a
            # credential fault, not a statement about the wallet, and retrying
            # would only repeat it.
            result["error"] = f"http_{status}: {detail}"
            result["terminal"] = True
            result["reason"] = (
                "CREDENTIAL_REJECTED" if status in (401, 402, 403)
                else "REQUEST_NOT_SERVABLE"
            )
            return result
        result["error"] = f"http_{status}: {detail}"
        return result

    result["error"] = result["error"] or "retries_exhausted"
    return result


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def _chain_id_of(resource: dict) -> str:
    relationships = resource.get("relationships") or {}
    chain = (relationships.get("chain") or {}) if isinstance(relationships, dict) else {}
    data = (chain.get("data") or {}) if isinstance(chain, dict) else {}
    if not isinstance(data, dict):
        return ""
    return str(data.get("id") or "").strip().lower()


def _implementation_address(info: dict, chain_id: str) -> str:
    """Contract address of the transferred fungible on the transaction's chain.

    A fungible is multi-chain, so the implementation matching this transaction's
    chain is the correct one; another chain's address would be a different token
    identity. Native assets report a null address and yield ``""``.
    """
    implementations = [x for x in (info.get("implementations") or []) if isinstance(x, dict)]
    if chain_id:
        for impl in implementations:
            if str(impl.get("chain_id") or "").strip().lower() == chain_id:
                address = str(impl.get("address") or "").strip()
                if address:
                    return address
    for impl in implementations:
        address = str(impl.get("address") or "").strip()
        if address:
            return address
    return ""


def normalize_transaction(resource: Any, now_ts: int | None = None) -> list[dict[str, Any]]:
    """One Zerion transaction -> wallet-history rows, one per fungible transfer.

    A swap moves two tokens, so it yields a buy row for what the wallet received
    and a sell row for what it sent. That is the same wallet-centric ``side``
    semantics the GMGN collector uses, which is what the entry/exit
    reconstruction is defined against.
    """
    if not isinstance(resource, dict):
        return []
    attributes = resource.get("attributes")
    if not isinstance(attributes, dict):
        return []

    status = str(attributes.get("status") or "").strip().lower()
    if status and status != "confirmed":
        # failed / pending: a reverted trade is not a trade.
        return []

    flags = attributes.get("flags")
    if isinstance(flags, dict) and flags.get("is_trash"):
        # Zerion's spam classification; a spam token is not wallet intelligence.
        return []

    mined_at = parse_iso_seconds(attributes.get("mined_at"))
    if mined_at <= 0:
        return []

    observed = int(now_ts if now_ts is not None else time.time())
    operation_type = str(attributes.get("operation_type") or "").strip().lower()
    chain_id = _chain_id_of(resource)
    chain = repo_chain(chain_id)
    transaction_hash = str(attributes.get("hash") or resource.get("id") or "")

    rows: list[dict[str, Any]] = []
    for transfer in attributes.get("transfers") or []:
        if not isinstance(transfer, dict):
            continue
        direction = str(transfer.get("direction") or "").strip().lower()
        if direction == "in":
            side = "buy"
        elif direction == "out":
            side = "sell"
        else:
            # 'self' moves a token between the wallet's own accounts.
            continue

        info = transfer.get("fungible_info")
        info = info if isinstance(info, dict) else {}
        address = _implementation_address(info, chain_id)
        if not address:
            # Native asset (ETH/SOL/BNB): no contract address means no token
            # identity, and whv.asset_identity would drop the row anyway.
            continue

        quantity = transfer.get("quantity")
        quantity = quantity if isinstance(quantity, dict) else {}
        token_amount = _num(quantity.get("float")) or _num(quantity.get("numeric"))

        price = _num(transfer.get("price"))
        value = _num(transfer.get("value"))
        # Zerion may report value without price (or the reverse). Reconstruct
        # the missing half from the other two real figures rather than
        # defaulting either to zero, which would read as a free trade.
        if price <= 0 and value > 0 and token_amount > 0:
            price = value / token_amount
        if value <= 0 and price > 0 and token_amount > 0:
            value = price * token_amount

        rows.append({
            "source": SOURCE,
            # Observation time vs event time, matching the GMGN row contract:
            # wallet_history_validation.event_ts prefers trade_timestamp, and
            # substituting the ingest time would shift every entry forward.
            "timestamp": observed,
            "trade_timestamp": mined_at,
            "transaction_hash": transaction_hash,
            "chain": chain,
            "address": address,
            "symbol": str(info.get("symbol") or "").upper(),
            "side": side,
            "amount_usd": round(value, 8),
            "price_usd": round(price, 12),
            "token_amount": token_amount,
            "operation_type": operation_type,
            "status": status or "confirmed",
            # Zerion does not report open/close. Left explicitly unknown rather
            # than guessed, because a wrong "open" would invent an entry.
            "is_open_or_close": None,
            "maker_tags": [],
        })
    return rows


def normalize_rows(
    resources: Iterable[Any], now_ts: int | None = None
) -> list[dict[str, Any]]:
    """Normalize a list of transactions, dropping cross-page repeats.

    The identity rule is byte-for-byte ``whv._dedupe``'s, so a transaction that
    appears on two pages cannot be counted twice by the consumer.
    """
    rows: list[dict[str, Any]] = []
    for resource in resources or []:
        rows.extend(normalize_transaction(resource, now_ts=now_ts))
    return _sort_unique(rows)


# The event-identity helpers live in wallet_history_validation so the merge here
# and the reconstruction the consumer performs cannot drift apart. They are
# re-exported because the provider's own tests and callers refer to them here.
event_core = whv.event_core
event_hash = whv.event_hash
same_event = whv.same_event


def event_identity(row: Any) -> tuple | None:
    """The strict event key: asset identity + time + side + size + hash.

    The hash stays in this key on purpose: two genuinely different trades in the
    same second, same token, same size and same price are two real events, and
    the hash is the only thing that tells them apart. Dropping it would quietly
    delete a second entry.

    This key is *too strict* to compare a hashless GMGN row against a hashed
    Zerion row, which is exactly why :func:`whv.same_event` exists. Use this for
    two rows that both carry a hash, and ``same_event`` when matching across
    providers.
    """
    core = event_core(row)
    if core is None:
        return None
    return core + (event_hash(row),)


def _sort_unique(rows: Iterable[dict]) -> list[dict]:
    """Chronological, de-duplicated copy of ``rows`` (input left untouched)."""
    seen: set[tuple] = set()
    kept: list[tuple[int, dict]] = []
    for index, row in enumerate(rows or []):
        identity = event_identity(row)
        if identity is None:
            continue
        if identity in seen:
            continue
        seen.add(identity)
        kept.append((index, row))
    kept.sort(key=lambda pair: (whv.event_ts(pair[1]), pair[0]))
    return [row for _, row in kept]


# ---------------------------------------------------------------------------
# Public fetch
# ---------------------------------------------------------------------------
def fetch_wallet_transactions(
    address: Any,
    chains: Iterable[Any] | str | None = None,
    start: Any = None,
    end: Any = None,
    operation_types: Sequence[Any] | None = DEFAULT_OPERATION_TYPES,
    include_trash: bool = False,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    currency: str = "usd",
    search_query: str | None = None,
    api_key: str | None = None,
    http_get: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
    request_budget: int | None = REQUEST_BUDGET,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Fetch one wallet's decoded history with cursor pagination.

    Pagination follows ``links.next`` verbatim and never constructs a cursor by
    hand, because the token is opaque. The first page carries the filter query
    string; every later page is the provider's own URL with no extra parameters.

    The result distinguishes UNAVAILABLE from EMPTY, which is the distinction the
    wallet gate cares about: ``ok=False`` with an ``error`` means the provider
    could not answer, and must never be read as "this wallet has no history".
    """
    observed = int(now_ts if now_ts is not None else time.time())
    result: dict[str, Any] = {
        "ok": False,
        "source": SOURCE,
        "endpoint": TRANSACTIONS_PATH,
        "wallet": str(address or ""),
        "rows": [],
        "transactions": 0,
        "pages": 0,
        "http_status": None,
        "truncated": False,
        "error": None,
        "terminal": False,
        "reason": None,
        "page_trace": [],
        "coverage": {"from": start, "to": end},
        "fetched_at": observed,
    }

    try:
        url = transactions_url(address)
        params = build_params(
            address=address, chains=chains, operation_types=operation_types,
            start=start, end=end, include_trash=include_trash,
            page_size=page_size, currency=currency, search_query=search_query,
        )
    except ValueError as exc:
        result["error"] = f"invalid_request: {exc}"
        return result

    encoded = urlencode(params)
    if len(url) + 1 + len(encoded) > MAX_URL_LENGTH:
        result["error"] = (
            "invalid_request: request URL exceeds Zerion's 2000-character limit; "
            "narrow the chain or operation filter"
        )
        return result

    if is_rate_limited():
        result["error"] = f"rate_limited: {PROVIDER_STATE.get('reason')}"
        return result

    limit = max(1, int(max_pages))
    next_url: str | None = url
    next_params: dict[str, str] | None = params
    seen_cursors: set[str] = set()
    raw_transactions: list[Any] = []
    rows: list[dict[str, Any]] = []
    spent = 0

    while next_url:
        if result["pages"] >= limit:
            result["truncated"] = True
            result["error"] = "max_pages_reached"
            break
        if request_budget is not None and spent >= int(request_budget):
            result["error"] = "request_budget_exhausted"
            break
        if is_rate_limited():
            result["error"] = f"rate_limited: {PROVIDER_STATE.get('reason')}"
            break

        page = fetch_page(
            next_url, next_params, api_key=api_key, http_get=http_get, sleep=sleep
        )
        spent += 1
        result["http_status"] = page["status"]
        if not page["ok"]:
            result["error"] = page["error"]
            result["terminal"] = bool(page.get("terminal"))
            result["reason"] = page.get("reason")
            break

        result["pages"] += 1
        payload = page["payload"] or {}
        data = payload.get("data")
        data = data if isinstance(data, list) else []
        result["transactions"] += len(data)
        raw_transactions.extend(data)
        before = len(rows)
        for resource in data:
            rows.extend(normalize_transaction(resource, now_ts=observed))

        links = payload.get("links")
        links = links if isinstance(links, dict) else {}
        following = str(links.get("next") or "").strip()
        # Per-page tally, so a caller can prove that an opaque cursor actually
        # advanced rather than replaying the first page.
        result["page_trace"].append({
            "page": result["pages"],
            "transactions": len(data),
            "normalized": len(rows) - before,
            "has_next": bool(following and data),
        })
        if not following or not data:
            break
        if not _is_trusted_url(following):
            result["truncated"] = True
            result["error"] = "untrusted_next_cursor"
            break
        if following in seen_cursors or following == next_url:
            result["error"] = "cursor_loop_detected"
            break
        seen_cursors.add(following)
        next_url, next_params = following, None
        _respect_second_limit(page["headers"], sleep)
        if PAGE_PACING_SECONDS:
            sleep(PAGE_PACING_SECONDS)

    result["rows"] = _sort_unique(rows)
    # Kept for the archive. Without it a normalizer change could not be
    # re-applied to an old fetch without spending quota a second time.
    result["raw_transactions"] = raw_transactions
    result["ok"] = result["error"] is None
    return result


# ---------------------------------------------------------------------------
# Additive merge into existing history
# ---------------------------------------------------------------------------
def merge_into_history(wallet: Any, rows: Iterable[dict], history: dict | None) -> dict[str, Any]:
    """Add Zerion rows to a wallet-history mapping, additively.

    The promise this makes, and the reason Zerion can never damage the existing
    GMGN history:

    * no existing row is modified, removed or replaced;
    * a row both providers already agree on is written once, so merging is not
      double-counting;
    * a row only one provider has is kept exactly as it arrived;
    * an empty or failed fetch adds nothing and deletes nothing.

    Rows are returned chronologically, using a stable sort, because the
    reconstruction reads them in time order. That is the same ordering
    ``gmgn_layer.update_history`` already maintains.

    Duplicate detection uses :func:`same_event` rather than a strict key,
    because a large share of stored GMGN rows carry no transaction hash. A strict
    key would treat a Zerion row and a hashless GMGN row describing one on-chain
    event as two records, and the resulting double count would inflate every
    derived PROVEN/quality figure.
    """
    summary: dict[str, Any] = {
        "ok": False, "wallet": "", "history_key": "", "added": 0,
        "skipped": 0, "total": 0, "gmgn_rows": 0, "error": None,
    }
    canonical = whv.wallet_identity(wallet)
    if not canonical:
        summary["error"] = "wallet address is required"
        return summary
    summary["wallet"] = canonical
    if history is None or not isinstance(history, dict):
        summary["error"] = "history must be a mapping"
        return summary

    # Reuse the stored key when this wallet already exists under another casing,
    # so a checksummed address cannot fork into a second half-history.
    key = canonical
    for existing in history:
        if whv.wallet_identity(existing) == canonical:
            key = existing
            break

    bucket = history.get(key)
    if not isinstance(bucket, list):
        bucket = []
    summary["gmgn_rows"] = len(bucket)
    summary["history_key"] = key

    # Existing rows are indexed by the hash-free event core, so an incoming row
    # is only ever compared against rows that could be the same event. A strict
    # identity key cannot see that a hashless GMGN row and a hashed Zerion row
    # are one on-chain event, and a hash-free key cannot tell two same-second
    # trades of the same size apart; `same_event` consults the hash when both
    # sides have one and falls back to the remaining fields only when at least
    # one side does not, which is the case GMGN's hashless rows create.
    index: dict[tuple, list[dict[str, Any]]] = {}
    for existing_row in bucket:
        if not isinstance(existing_row, dict):
            continue
        core = event_core(existing_row)
        if core is not None:
            index.setdefault(core, []).append(existing_row)
    added = 0
    skipped = 0
    for row in rows or []:
        core = event_core(row)
        if core is None:
            skipped += 1
            continue
        candidates = index.get(core)
        if candidates and any(same_event(row, other) for other in candidates):
            skipped += 1
            continue
        if candidates is None:
            candidates = index[core] = []
        candidates.append(dict(row))
        bucket.append(dict(row))
        added += 1

    if added:
        # Defensive key: a real history file is provider data and may hold a
        # non-dict row. Sorting must never be the thing that breaks a merge.
        bucket.sort(key=lambda item: whv.event_ts(item) if isinstance(item, dict) else 0)
    history[key] = bucket

    summary["ok"] = True
    summary["added"] = added
    summary["skipped"] = skipped
    summary["total"] = len(bucket)
    return summary


# ---------------------------------------------------------------------------
# Archive + CLI
# ---------------------------------------------------------------------------
def archive_path(wallet: Any, fetched_at: Any, directory: Path | str = ARCHIVE_DIR) -> Path:
    """Deterministic, non-colliding archive path for one fetch.

    ``<wallet>_<fetched_at>.json`` keeps every snapshot of a wallet instead of
    overwriting the previous one, which is what a bare ``<wallet>.json`` did.
    The stamp comes from the fetch result rather than the wall clock, so a
    replayed run writes the same name and a genuinely new run does not. If two
    fetches genuinely land in the same second, a counter suffix keeps the
    earlier file: no archive is ever replaced.
    """
    folder = Path(directory)
    safe = whv.wallet_identity(wallet) or "unknown"
    try:
        stamp = int(fetched_at)
    except (TypeError, ValueError):
        stamp = 0
    path = folder / f"{safe}_{stamp}.json"
    index = 2
    while path.exists():
        path = folder / f"{safe}_{stamp}_{index}.json"
        index += 1
    return path


def write_archive(result: dict, directory: Path | str = ARCHIVE_DIR) -> Path:
    """Persist a fetch under ``wallet_archive/raw/zerion/transactions``.

    The raw provider payload and the normalized rows are both kept, following
    the layout the GMGN and Nansen archives already use, so a later change to
    the normalizer can be re-derived from the raw response without another quota
    spend. The raw transactions were previously collected by the fetch and then
    dropped, which made that re-derivation impossible. The API key is never part
    of the payload.

    Each fetch writes its own file (see :func:`archive_path`), so backfilling a
    wallet twice accumulates history instead of silently replacing it.
    """
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    raw = result.get("raw_transactions")
    payload = {
        "schema_version": 2,
        "source": SOURCE,
        "endpoint": result.get("endpoint"),
        "wallet": result.get("wallet"),
        "fetched_at": result.get("fetched_at"),
        "coverage": result.get("coverage"),
        "ok": result.get("ok"),
        "error": result.get("error"),
        "truncated": result.get("truncated"),
        "summary": {
            "pages": result.get("pages"),
            "transactions": result.get("transactions"),
            "normalized_rows": len(result.get("rows") or []),
            "raw_transactions": len(raw) if isinstance(raw, list) else 0,
        },
        "records": result.get("rows") or [],
        # The undecoded provider payload, so a normalizer change can be
        # re-applied without spending quota again.
        "raw_transactions": raw if isinstance(raw, list) else [],
    }
    path = archive_path(result.get("wallet"), result.get("fetched_at"), folder)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    """Backfill one wallet from the environment. Read-only; never trades.

    ``ZERION_BACKFILL_WALLET`` (required), ``ZERION_BACKFILL_CHAINS`` (comma
    separated, default all supported), ``ZERION_BACKFILL_FROM`` /
    ``ZERION_BACKFILL_TO`` (ISO or epoch, optional).
    """
    wallet = os.environ.get("ZERION_BACKFILL_WALLET", "").strip()
    if not wallet:
        print("ZERION_BACKFILL_WALLET is required")
        return 2
    chains_raw = os.environ.get("ZERION_BACKFILL_CHAINS", "").strip()
    chains = [part for part in chains_raw.replace(";", ",").split(",") if part.strip()] or None
    result = fetch_wallet_transactions(
        wallet,
        chains=chains,
        start=os.environ.get("ZERION_BACKFILL_FROM") or None,
        end=os.environ.get("ZERION_BACKFILL_TO") or None,
    )
    path = write_archive(result)
    print("=== ZERION BACKFILL RESULT ===")
    print(f"WALLET={result['wallet']}")
    print(f"OK={result['ok']}")
    print(f"PAGES={result['pages']}")
    print(f"TRANSACTIONS={result['transactions']}")
    print(f"NORMALIZED_ROWS={len(result['rows'])}")
    print(f"TRUNCATED={result['truncated']}")
    if result["error"]:
        print(f"ERROR={result['error']}")
    print(f"ARCHIVE={path}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
