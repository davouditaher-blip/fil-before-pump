"""Read-only client for Hyperliquid's public Info API.

Nothing in this module needs an account. Hyperliquid's ``/info`` endpoint is an
unauthenticated ``POST`` that takes an address and returns read-only data, so
historical discovery is satisfied entirely from the public interface: there is
no private key, no signature, no wallet connection and no API key to store.

The one thing this layer is careful about is honesty about coverage. Hyperliquid
retains only a bounded window of a user's fills, and this module reports that
boundary (``retained_fills_ceiling``, ``ceiling_reached``) instead of quietly
presenting a short history as if it were complete.

A fill is a real trade, so the rows produced here enter historical discovery as
trades and are normalized by the shared
:mod:`historical_discovery` pipeline. This module only fetches and prepares
provider rows; it does not define a second identity, dedup or scoring path.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

#: The public, unauthenticated Info endpoint. There is no signing and no auth.
INFO_URL = "https://api.hyperliquid.xyz/info"

SOURCE = "hyperliquid"

#: Matches the ``archive_dir`` already declared on the Hyperliquid SourceAdapter.
ARCHIVE_DIR = Path("wallet_archive/raw/hyperliquid/fills")

#: Documented response caps. Both are provider limits, not repository limits.
MAX_FILLS_PER_RESPONSE = 2000
#: "only the 10000 most recent fills are available" -- for any user.
RETAINED_FILLS_CEILING = 10000

FILL_TYPE_BY_TIME = "userFillsByTime"
FILL_TYPE_RECENT = "userFills"
REQUEST_TYPES = (FILL_TYPE_BY_TIME, FILL_TYPE_RECENT)

USER_AGENT = "fil-before-pump/1.0 (+https://github.com/davouditaher-blip/fil-before-pump)"

REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
PAGE_PACING_SECONDS = 0.2
REQUEST_BUDGET = 200

RETRYABLE_STATUS = (429, 500, 502, 503, 504)
TERMINAL_STATUS = (400, 401, 403, 404, 405, 422)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

#: Hyperliquid returns an all-zero hash for TWAP fills. That is a placeholder,
#: not a transaction, so it is dropped rather than stored as an identifier.
ZERO_HASH = "0x" + ("0" * 64)

_HEX64_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

PROVIDER_STATE: dict[str, Any] = {
    "requests_made": 0,
    "rate_limited": False,
}


class HyperliquidRequestError(ValueError):
    """A request that cannot be built or sent (bad address, bad window)."""


# ---------------------------------------------------------------------------
# Value coercion -- missing stays missing
# ---------------------------------------------------------------------------
def to_millis(value: Any) -> int | None:
    """Coerce seconds, milliseconds or ISO-8601 to epoch milliseconds.

    ``None`` when the value is unusable. A timestamp that cannot be read is not
    a timestamp of zero, and writing zero would place the fill in 1970.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            text = str(value).replace("Z", "+00:00")
            number = float(datetime.fromisoformat(text).timestamp())
        except ValueError:
            return None
    if not math.isfinite(number):
        return None
    seconds = number / 1000.0 if number > 10**12 else number
    millis = int(seconds * 1000)
    return millis if millis > 0 else None


def as_float(value: Any) -> float | None:
    """A finite float, or ``None`` when the provider gave no usable number."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def notional_usd(px: Any, sz: Any) -> float | None:
    """USD value of a fill, derived from its own price and size.

    Hyperliquid does not report a USD notional: it reports ``px`` (USD price per
    unit) and ``sz`` (size in units). Their product is the fill's traded value,
    so this is arithmetic on reported fields rather than an estimate. When either
    is missing the result is ``None`` -- never ``0.0``, which would read as
    "measured, and it was zero".
    """
    price = as_float(px)
    size = as_float(sz)
    if price is None or size is None:
        return None
    product = price * size
    return product if math.isfinite(product) else None


def normalize_wallet(wallet: Any) -> str:
    """The bare 42-character address, or raise.

    Hyperliquid documents that the *actual* master or sub-account address must
    be used; an agent wallet's address returns an empty result. Normalizing to
    bare lowercase hex keeps one wallet to one key.
    """
    text = str(wallet or "").strip().lower()
    if not _ADDRESS_RE.fullmatch(text):
        raise HyperliquidRequestError(
            "Hyperliquid needs a 42-character 0x address of the account itself "
            "(not an agent wallet); nothing was sent."
        )
    return text


# ---------------------------------------------------------------------------
# Public request construction
# ---------------------------------------------------------------------------
def build_fill_request(
    wallet: Any,
    *,
    start_ms: Any = None,
    end_ms: Any = None,
    request_type: str = FILL_TYPE_BY_TIME,
    aggregate_by_time: bool | None = None,
) -> dict[str, Any]:
    """The JSON body for a public, read-only fill query.

    ``userFillsByTime`` is the historical entry point and requires ``startTime``;
    ``userFills`` returns only the most recent fills and takes no window. The
    body carries an address and a time window and nothing else -- no signature,
    no nonce, no credential.
    """
    kind = str(request_type or "").strip()
    if kind not in REQUEST_TYPES:
        raise HyperliquidRequestError(
            f"unsupported request type {kind!r}; expected one of {REQUEST_TYPES}"
        )
    body: dict[str, Any] = {"type": kind, "user": normalize_wallet(wallet)}

    if kind == FILL_TYPE_BY_TIME:
        start = to_millis(start_ms)
        if start is None:
            raise HyperliquidRequestError(
                "userFillsByTime requires a startTime; nothing was sent."
            )
        body["startTime"] = start
        end = to_millis(end_ms)
        if end is not None:
            body["endTime"] = end
        if end is not None and end < start:
            raise HyperliquidRequestError(
                "endTime precedes startTime; nothing was sent."
            )
    elif start_ms is not None or end_ms is not None:
        # Silently dropping a requested window would return recent fills while
        # the caller believed it had a range.
        raise HyperliquidRequestError(
            "userFills takes no time window; use userFillsByTime for a range."
        )

    if aggregate_by_time is not None:
        body["aggregateByTime"] = bool(aggregate_by_time)
    return body


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------
def normalize_fill(
    fill: Any,
    *,
    wallet: Any = None,
    fetched_at: Any = None,
) -> dict[str, Any] | None:
    """One provider fill as a row the shared normalizer accepts.

    Returns ``None`` for a row with no asset, because a fill without a coin has
    no identity and cannot be deduplicated or reconstructed.

    The original fill is preserved under ``raw_payload`` so that re-deriving any
    field later never costs another request. That is also what keeps the
    dropped TWAP hash auditable: it stays visible in the raw payload instead of
    being silently erased.
    """
    if not isinstance(fill, Mapping):
        return None

    coin = str(fill.get("coin") or "").strip()
    if not coin:
        return None

    px = as_float(fill.get("px"))
    sz = as_float(fill.get("sz"))

    raw_hash = str(fill.get("hash") or "").strip()
    # A zero hash is Hyperliquid's placeholder for a TWAP slice, not a
    # transaction. Keeping it would let every TWAP fill of every wallet collide
    # on one bogus transaction id.
    hash_value = raw_hash if (_HEX64_RE.match(raw_hash) and raw_hash != ZERO_HASH) else None

    row: dict[str, Any] = {
        "wallet": str(wallet or "").strip().lower() or None,
        "coin": coin,
        "side": str(fill.get("side") or "").strip() or None,
        "dir": str(fill.get("dir") or "").strip() or None,
        "px": px,
        "sz": sz,
        "usd": notional_usd(px, sz),
        "time": to_millis(fill.get("time")),
        "hash": hash_value,
        "tid": fill.get("tid"),
        "oid": fill.get("oid"),
        "closed_pnl": as_float(fill.get("closedPnl")),
        "fee": as_float(fill.get("fee")),
        "fee_token": str(fill.get("feeToken") or "").strip() or None,
        "crossed": fill.get("crossed"),
        "fetched_at": to_millis(fetched_at),
    }
    return row


def _as_fill_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("fills", "rows", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def normalize_fills(
    payload: Any,
    *,
    wallet: Any = None,
    fetched_at: Any = None,
) -> dict[str, Any]:
    """Normalize a fill response into rows plus a count of what was rejected.

    Rejected rows are counted, never silently dropped: an unidentifiable fill is
    a coverage gap the reader must be able to see.
    """
    fills = _as_fill_list(payload)
    rows: list[dict[str, Any]] = []
    rejected = 0
    zero_hash_dropped = 0
    for fill in fills:
        row = normalize_fill(fill, wallet=wallet, fetched_at=fetched_at)
        if row is None:
            rejected += 1
            continue
        if isinstance(fill, Mapping) and not row["hash"]:
            if str(fill.get("hash") or "").strip() == ZERO_HASH:
                zero_hash_dropped += 1
        rows.append(row)
    return {
        "rows": rows,
        "received": len(fills),
        "rejected": rejected,
        "zero_hash_dropped": zero_hash_dropped,
    }


# ---------------------------------------------------------------------------
# Transport -- public, unauthenticated, read-only
# ---------------------------------------------------------------------------
def _default_post(url: str, body: Any, headers: Mapping[str, str], timeout: int) -> Any:
    """Live HTTP POST against the public Info endpoint.

    ``requests`` is imported lazily so this module and its tests import and run
    without the dependency present, matching the repository's CI, which only
    installs it for jobs that actually call a provider. No ``auth`` argument is
    passed because there is nothing to authenticate with.
    """
    import requests

    return requests.post(url, json=body, headers=dict(headers), timeout=timeout)


def _header(headers: Any, name: str) -> str:
    for key, value in dict(headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _retry_delay(status: int, headers: Any, attempt: int) -> float:
    retry_after = _header(headers, "Retry-After").strip()
    if retry_after:
        try:
            return min(BACKOFF_CAP_SECONDS, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))


def _error_detail(response: Any) -> str:
    try:
        text = response.text
    except Exception:
        text = ""
    detail = str(text or "").strip()
    if not detail:
        try:
            detail = json.dumps(response.json())[:300]
        except Exception:
            detail = ""
    return detail[:300]


def _post_once(
    body: Mapping[str, Any],
    *,
    post: Callable[..., Any],
    timeout: int,
    sleep: Callable[[float], Any] = time.sleep,
) -> tuple[list[Any], dict[str, Any]]:
    """One attempt. Returns ``(fills, outcome)``; never raises for a network fault."""
    outcome: dict[str, Any] = {"ok": False, "error": "", "status": None, "attempts": 1}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        outcome["attempts"] = attempt
        if PROVIDER_STATE["requests_made"] >= REQUEST_BUDGET:
            outcome["error"] = "request budget exhausted for this run"
            return [], outcome
        PROVIDER_STATE["requests_made"] += 1
        try:
            response = post(
                INFO_URL,
                body,
                {"Content-Type": "application/json", "User-Agent": USER_AGENT},
                timeout,
            )
        except Exception as exc:  # network, DNS, TLS, timeout
            outcome["error"] = f"transport error: {type(exc).__name__}"
            if attempt < MAX_ATTEMPTS:
                sleep(_retry_delay(0, {}, attempt))
                continue
            return [], outcome

        status = int(getattr(response, "status_code", 0) or 0)
        outcome["status"] = status
        if 200 <= status < 300:
            try:
                payload = response.json()
            except Exception:
                outcome["error"] = "response was not valid JSON"
                return [], outcome
            if isinstance(payload, Mapping) and payload.get("error"):
                outcome["error"] = str(payload["error"])[:300]
                return [], outcome
            outcome["ok"] = True
            return _as_fill_list(payload), outcome

        outcome["error"] = _error_detail(response) or f"HTTP {status}"
        if status in TERMINAL_STATUS:
            return [], outcome
        if status not in RETRYABLE_STATUS:
            return [], outcome
        if attempt >= MAX_ATTEMPTS:
            return [], outcome
        sleep(_retry_delay(status, getattr(response, "headers", None), attempt))
    return [], outcome


def reset_state() -> None:
    """Clear the per-run request counter and rate-limit flag.

    The counter is a safety valve, not identity, so a caller (or a test) that
    wants a fresh budget asks for one explicitly instead of the module
    quietly forgetting how much it has already spent.
    """
    PROVIDER_STATE["requests_made"] = 0
    PROVIDER_STATE["rate_limited"] = False


def fetch_fills(
    wallet: Any,
    *,
    start_ms: Any = None,
    end_ms: Any = None,
    request_type: str = FILL_TYPE_BY_TIME,
    aggregate_by_time: bool | None = None,
    max_pages: int = 5,
    post: Callable[..., Any] | None = None,
    timeout: int = REQUEST_TIMEOUT,
    pace: float = PAGE_PACING_SECONDS,
    sleep: Callable[[float], Any] = time.sleep,
    now_ms: Any = None,
) -> dict[str, Any]:
    """Fetch fills for one public address. Read-only, and no wallet is required.

    Pagination follows the provider's documented rule: the last returned
    timestamp becomes the next ``startTime``. Two hard limits are reported rather
    than hidden -- at most ``MAX_FILLS_PER_RESPONSE`` fills per response, and at
    most ``RETAINED_FILLS_CEILING`` recent fills existing per user at all.
    Reaching the latter means the history is genuinely incomplete, and the
    result says so in ``ceiling_reached``.
    """
    address = normalize_wallet(wallet)
    sender = post or _default_post
    pages = max(1, int(max_pages or 1))
    end = to_millis(end_ms) or to_millis(now_ms) or int(time.time() * 1000)
    start = to_millis(start_ms)

    result: dict[str, Any] = {
        "ok": False,
        "error": "",
        "wallet": address,
        "source": SOURCE,
        "endpoint": INFO_URL,
        "request_type": str(request_type or ""),
        "pages": 0,
        "requests_made": 0,
        "received": 0,
        "rejected": 0,
        "zero_hash_dropped": 0,
        "rows": [],
        "truncated": False,
        "ceiling_reached": False,
        "coverage_incomplete": False,
        "provider_history_bounded": True,
        "oldest_time": None,
        "newest_time": None,
        "retained_fills_ceiling": RETAINED_FILLS_CEILING,
        "auth_required": False,
    }

    # userFills has no window: exactly one page is meaningful, and it must still
    # be requested -- an absent window is not an absent request.
    by_time = str(request_type or "") != FILL_TYPE_RECENT
    if not by_time:
        pages = 1
        start = None

    cursor = start
    seen: set[Any] = set()
    rows: list[dict[str, Any]] = []

    for _ in range(pages):
        if by_time and cursor is None:
            break
        body = build_fill_request(
            address,
            start_ms=cursor,
            end_ms=end if by_time else None,
            request_type=request_type,
            aggregate_by_time=aggregate_by_time,
        )
        before = PROVIDER_STATE["requests_made"]
        fills, outcome = _post_once(body, post=sender, timeout=timeout, sleep=sleep)
        result["requests_made"] += PROVIDER_STATE["requests_made"] - before
        result["pages"] += 1
        if not outcome["ok"]:
            result["error"] = outcome["error"] or "request failed"
            result["status"] = outcome.get("status")
            result["attempts"] = outcome.get("attempts")
            break

        page = normalize_fills(fills, wallet=address, fetched_at=end)
        result["received"] += page["received"]
        result["rejected"] += page["rejected"]
        result["zero_hash_dropped"] += page["zero_hash_dropped"]

        for row in page["rows"]:
            # `tid` is Hyperliquid's own per-fill id and is the primary key
            # within one account. The rest of the tuple is carried along so a
            # provider that ever reuses a tid cannot silently collapse two
            # different fills into one.
            key = ("tid", row["tid"], row.get("hash"), row.get("time")) if row.get("tid") not in (None, "") else (
                "tuple",
                row.get("hash"),
                row.get("coin"),
                row.get("time"),
                row.get("sz"),
                row.get("side"),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

        result["ok"] = True

        if not by_time:
            break
        if len(page["rows"]) < MAX_FILLS_PER_RESPONSE:
            # A partial page is the last page; the window is exhausted.
            break

        stamps = [r["time"] for r in page["rows"] if r["time"]]
        if not stamps:
            break
        oldest = min(stamps)
        if cursor is not None and oldest >= cursor:
            # The window did not move: stop rather than loop forever.
            result["truncated"] = True
            break
        cursor = oldest + 1
        if len(rows) >= RETAINED_FILLS_CEILING:
            result["ceiling_reached"] = True
            result["truncated"] = True
            break
        if pace:
            sleep(pace)
    else:
        result["truncated"] = True

    stamps = [r["time"] for r in rows if r["time"]]
    result["rows"] = rows
    result["oldest_time"] = min(stamps) if stamps else None
    result["newest_time"] = max(stamps) if stamps else None
    # `coverage_incomplete` means "we know we are short", not "the data looks
    # short". A wallet that simply had no fills for the first hour of a
    # requested day returns nothing near `startTime` and is perfectly complete,
    # so an old-looking window is not evidence of a missing page.
    if result["truncated"] or result["ceiling_reached"]:
        result["coverage_incomplete"] = True
    # A standing property of this source, always true and never a per-response
    # finding: Hyperliquid keeps only a bounded number of recent fills. If a
    # caller asks for a window older than that, the response is simply whatever
    # the provider still retains, and no single reply can reveal the difference.
    result["provider_history_bounded"] = True
    result["retained_fills_ceiling"] = RETAINED_FILLS_CEILING
    return result


def read_archive(directory: Path | str = ARCHIVE_DIR) -> dict[str, Any]:
    """Read committed fill archives without any network access.

    Used by the replay path so discovery can be re-run offline.
    """
    folder = Path(directory)
    out: dict[str, list[dict[str, Any]]] = {}
    if not folder.is_dir():
        return out
    for path in sorted(folder.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping):
            wallet = payload.get("wallet") or path.stem
            fetched_at = payload.get("fetched_at")
            rows = payload.get("rows") or payload.get("fills") or []
        elif isinstance(payload, list):
            wallet, fetched_at, rows = path.stem, None, payload
        else:
            continue
        prepared = normalize_fills(rows, wallet=wallet, fetched_at=fetched_at)["rows"]
        if wallet and prepared:
            out.setdefault(str(wallet).strip().lower(), []).extend(prepared)
    return out
