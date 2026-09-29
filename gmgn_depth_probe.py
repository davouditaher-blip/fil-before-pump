"""ONE-WALLET GMGN historical-depth probe. Manual, bounded, read-only.

Purpose
-------
The project cannot honestly state how far back GMGN wallet activity goes, because
the only evidence in the repository is a 7.9-day ``gmgn_wallet_history.json``
collection window and one earlier backfill archive that came back empty. Rather
than assume a depth (6 months, 1 year, 4 years), this probe measures it for a
single wallet and records exactly what the provider did.

What it answers
---------------
- Does ``--limit`` return the *full* history, or only a recent slice?
- Is there real pagination, and is it page-number or cursor based?
- Does the provider eventually stop returning older records, and where?
- Are transaction hashes / event ids present (needed for dedupe and audit)?
- Do repeated requests return duplicates?
- What errors and rate-limit behaviour occur?

Safety
------
- EXACTLY one wallet, from ``PROBE_WALLET``. It never enumerates wallets.
- Read-only: never writes history, never places an order, never enables
  execution. It writes only its own result file.
- ``MAX_PAGES`` is a hard stop, so pagination cannot loop forever. The probe
  also stops on a non-advancing page, an empty page, an explicit provider
  boundary flag, or a repeated page signature.
- Never prints the API key.

Result: ``gmgn_depth_probe.json`` (git-ignored by the operator; contains no
credentials -- only wallet, chain, counts and timestamps).
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHAINS = tuple(
    c.strip()
    for c in os.environ.get("PROBE_CHAINS", "sol,bsc,base,eth").split(",")
    if c.strip()
)
WALLET = os.environ.get("PROBE_WALLET", "").strip()
# Never assume 200 is "the whole history": it is only the first slice.
LIMIT = int(os.environ.get("PROBE_LIMIT", "200"))
# Hard safety stop. A probe must never become an unbounded backfill.
MAX_PAGES = int(os.environ.get("PROBE_MAX_PAGES", "10"))
PAGE_SLEEP = float(os.environ.get("PROBE_PAGE_SLEEP", "0.4"))
RESULT_PATH = Path(os.environ.get("PROBE_RESULT", "gmgn_depth_probe.json"))
CLI_TIMEOUT = int(os.environ.get("PROBE_CLI_TIMEOUT", "120"))

GMGN_API_KEY = os.environ.get("GMGN_API_KEY", "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    return int(_num(value, float(default)))


def _event_ts(row: dict[str, Any]) -> int:
    """Event time in seconds, preferring the trade time over the observation time."""
    raw = row.get("trade_timestamp")
    if raw in (None, "", 0, "0"):
        raw = row.get("timestamp") or row.get("time")
    value = _int(raw)
    return value // 1000 if value > 10**12 else value


def _side(row: dict[str, Any]) -> str:
    return str(row.get("side") or row.get("event_type") or row.get("type") or "").strip().lower()


def _tx_id(row: dict[str, Any]) -> str:
    return str(
        row.get("transaction_hash")
        or row.get("tx_hash")
        or row.get("txHash")
        or row.get("id")
        or ""
    )


def _token_address(row: dict[str, Any]) -> str:
    token = row.get("token") if isinstance(row.get("token"), dict) else {}
    return str(
        token.get("address")
        or row.get("base_address")
        or row.get("token_address")
        or row.get("address")
        or ""
    )


def run_cli(args: list[str]) -> tuple[dict[str, Any], str | None]:
    """Run gmgn-cli and return (parsed_json, error). Never raises."""
    import subprocess

    if not GMGN_API_KEY:
        return {}, "GMGN_API_KEY is not configured"
    env = os.environ.copy()
    env["GMGN_API_KEY"] = GMGN_API_KEY
    cmd = ["npx", "--yes", "gmgn-cli", *args, "--raw"]
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=CLI_TIMEOUT
        )
    except FileNotFoundError:
        return {}, "npx/gmgn-cli is not available in this environment"
    except subprocess.TimeoutExpired:
        return {}, f"CLI timeout after {CLI_TIMEOUT}s"
    if proc.returncode != 0:
        return {}, (proc.stderr or "non-zero exit")[-500:]
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj, None
        except json.JSONDecodeError:
            continue
    return {}, "no parsable JSON on stdout"


def probe_chain(chain: str) -> dict[str, Any]:
    """Probe one chain for one wallet, honouring every stop condition."""
    result: dict[str, Any] = {
        "chain": chain,
        "wallet": WALLET,
        "started_at": _now_iso(),
        "finished_at": None,
        "requests": 0,
        "pages": 0,
        "rows": 0,
        "rows_per_page": [],
        "earliest_event": None,
        "latest_event": None,
        "span_days": None,
        "buys": 0,
        "sells": 0,
        "other_sides": {},
        "rows_with_event_id": 0,
        "rows_with_timestamp": 0,
        "pagination_detected": False,
        "pagination_type": None,
        "pagination_exhausted": False,
        "provider_boundary": None,
        "duplicate_rows_across_pages": 0,
        "stop_reason": None,
        "errors": [],
    }

    # page -> list of seen tx ids, to detect overlap and non-advancing pages.
    seen_ids: set[str] = set()
    all_ids: list[str] = []
    stamps: list[int] = []
    previous_signature: tuple | None = None
    cursor: str | None = None
    raw_pagination_sample: Any = None

    for page in range(1, MAX_PAGES + 1):
        args = [
            "portfolio", "activity",
            "--chain", chain,
            "--wallet", WALLET,
            "--limit", str(LIMIT),
            "--type", "buy",
            "--type", "sell",
        ]
        if page > 1:
            # Only used if the provider actually advertises a cursor; recorded
            # verbatim so its real behaviour is auditable.
            if cursor:
                args += ["--cursor", cursor]
            else:
                args += ["--page", str(page)]

        obj, error = run_cli(args)
        result["requests"] += 1
        if error:
            result["errors"].append({"page": page, "error": error})
            result["stop_reason"] = "request_error"
            break

        rows = obj.get("list") or obj.get("data") or []
        if not isinstance(rows, list):
            rows = []
        result["pages"] += 1
        result["rows_per_page"].append(len(rows))
        result["rows"] += len(rows)

        if not rows:
            result["stop_reason"] = "empty_page"
            break

        # Full event identity, used to detect duplicates and non-advancing pages.
        page_ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = _tx_id(row)
            page_ids.append(row_id)
            all_ids.append(row_id)
            if row_id:
                seen_ids.add(row_id)
                result["rows_with_event_id"] += 1
            stamp = _event_ts(row)
            if stamp > 0:
                result["rows_with_timestamp"] += 1
                stamps.append(stamp)
            side = _side(row)
            if side == "buy":
                result["buys"] += 1
            elif side == "sell":
                result["sells"] += 1
            else:
                result["other_sides"][side or "(empty)"] = (
                    result["other_sides"].get(side or "(empty)", 0) + 1
                )

        unique_page = {i for i in page_ids if i}
        result["duplicate_rows_across_pages"] += len(page_ids) - len(unique_page) if page > 1 else 0

        signature = (
            len(rows),
            min(stamps) if stamps else 0,
            max(stamps) if stamps else 0,
        )
        if previous_signature is not None and signature == previous_signature:
            # The provider returned the same page again: pagination is not
            # advancing, so stop instead of looping.
            result["stop_reason"] = "pagination_not_advancing"
            break
        previous_signature = signature

        pagination = obj.get("pagination") or {}
        if raw_pagination_sample is None and pagination:
            raw_pagination_sample = pagination
        if isinstance(pagination, dict) and pagination:
            result["pagination_detected"] = True
            for field in ("cursor", "next_cursor", "nextCursor", "page_token", "next_page_token"):
                if pagination.get(field):
                    result["pagination_type"] = f"cursor:{field}"
                    cursor = str(pagination[field])
                    break
            else:
                if pagination.get("is_last_page") is True or pagination.get("has_more") is False:
                    result["pagination_type"] = "is_last_page/has_more"
                elif "page" in pagination or "total_pages" in pagination:
                    result["pagination_type"] = "page_number"
            if pagination.get("is_last_page") is True or pagination.get("has_more") is False:
                result["pagination_exhausted"] = True
                result["stop_reason"] = "provider_reports_last_page"
                break
            total_pages = pagination.get("total_pages")
            if total_pages is not None and page >= _int(total_pages, 0) > 0:
                result["pagination_exhausted"] = True
                result["stop_reason"] = "reached_total_pages"
                break

        # A provider boundary can also be signalled by a total record count.
        for field in ("total", "total_records", "count"):
            if isinstance(pagination, dict) and pagination.get(field) is not None:
                if _int(pagination[field]) and result["rows"] >= _int(pagination[field]):
                    result["provider_boundary"] = f"{field}={pagination[field]}"
                    result["pagination_exhausted"] = True
                    result["stop_reason"] = "reached_reported_total"
                    break
        if result["stop_reason"] == "reached_reported_total":
            break

        time.sleep(PAGE_SLEEP)
    else:
        result["stop_reason"] = "max_pages_reached"

    if stamps:
        earliest, latest = min(stamps), max(stamps)
        result["earliest_event"] = datetime.fromtimestamp(earliest, timezone.utc).isoformat()
        result["latest_event"] = datetime.fromtimestamp(latest, timezone.utc).isoformat()
        result["span_days"] = round((latest - earliest) / 86400.0, 3)
    else:
        result["stop_reason"] = result["stop_reason"] or "no_rows_returned"

    result["unique_event_ids"] = len([i for i in seen_ids if i])
    result["raw_pagination_sample"] = raw_pagination_sample
    result["finished_at"] = _now_iso()
    return result


def main() -> int:
    if not WALLET:
        print("PROBE_WALLET is required (exactly one wallet).")
        print("Example: PROBE_WALLET=0x3d457d0b79efac77ed38f37870c713d0244479ea")
        return 2

    started = _now_iso()
    # Keyed by chain so the per-chain results stay addressable in the artifact.
    chains: dict[str, dict[str, Any]] = {chain: probe_chain(chain) for chain in CHAINS}

    aggregate = {
        "probe": "gmgn_wallet_historical_depth",
        "wallet": WALLET,
        "chains_probed": list(CHAINS),
        "requested_limit_per_page": LIMIT,
        "max_pages": MAX_PAGES,
        "started_at": started,
        "finished_at": _now_iso(),
        "read_only": True,
        "orders_enabled": False,
        "writes_history": False,
        "totals": {
            "requests": sum(c["requests"] for c in chains.values()),
            "pages": sum(c["pages"] for c in chains.values()),
            "rows": sum(c["rows"] for c in chains.values()),
            "buys": sum(c["buys"] for c in chains.values()),
            "sells": sum(c["sells"] for c in chains.values()),
        },
        "max_span_days": max(
            (c["span_days"] for c in chains.values() if c["span_days"] is not None),
            default=None,
        ),
        "earliest_event_any_chain": min(
            (c["earliest_event"] for c in chains.values() if c["earliest_event"]),
            default=None,
        ),
        "latest_event_any_chain": max(
            (c["latest_event"] for c in chains.values() if c["latest_event"]),
            default=None,
        ),
        "pagination_detected_any_chain": any(
            c["pagination_detected"] for c in chains.values()
        ),
        "pagination_type_any_chain": sorted(
            {c["pagination_type"] for c in chains.values() if c["pagination_type"]}
        ),
        "side_coverage": dict(
            Counter(
                side
                for c in chains.values()
                for side, n in c["other_sides"].items()
                for _ in range(n)
            )
        ),
        "by_chain": chains,
    }

    RESULT_PATH.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(aggregate["totals"], indent=2))
    print(f"max_span_days={aggregate['max_span_days']}")
    print(f"earliest_event_any_chain={aggregate['earliest_event_any_chain']}")
    print(f"pagination_detected={aggregate['pagination_detected_any_chain']} "
          f"types={aggregate['pagination_type_any_chain']}")
    for chain, data in chains.items():
        print(f"  {chain:5} pages={data['pages']} rows={data['rows']} "
              f"buys={data['buys']} sells={data['sells']} "
              f"ids={data['rows_with_event_id']} stop={data['stop_reason']}")
        for err in data["errors"]:
            print(f"        error p{err['page']}: {err['error'][:200]}")
    print(f"RESULT={RESULT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
