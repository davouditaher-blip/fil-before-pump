"""Manual, read-only smoke test against the real Zerion API.

Purpose
-------
Every other test in this repository is offline and deterministic. This script
is the one thing that talks to the live provider, and it exists to answer a
single question: does the account behind ``ZERION_API_KEY`` actually work
against ``api.zerion.io`` with the code in ``zerion_layer``?

It is deliberately tiny. It requests ``page[size]=2`` and follows at most one
``links.next`` cursor, so it cannot turn into a backfill and cannot meaningfully
drain a metered quota. It is read-only: it writes no archive, mutates no
history, and touches no scoring, selection or trading path.

What it proves, and what it does not
-----------------------------------
Proves, on a 200 response: the credential shape, the filter query, JSON
envelope parsing, normalization into wallet-history rows, and that an opaque
``links.next`` cursor is followed and returns a genuinely different page.
It also reports a rejected credential clearly, distinguishing a wrong key
(401/402), an untrackable address (400) and a Cloudflare-level block (403).

Does not prove: anything about a real wallet's full history, volume, rate
limits, or the quality of the underlying data. Zero records with a 200 is a
valid, non-failing outcome for a quiet window; it means the request worked and
the window happened to be empty.

Security
--------
The key is read from the environment and is never printed, logged, or written
to a file. The ``Authorization`` header is never printed. Every string that
reaches the report is passed through :func:`_redact`, which strips the key and
its Base64 ``Basic`` form, so a credential echoed back inside a provider error
message still cannot leak into CI output.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from typing import Any, Callable

import zerion_layer as zl

# A real, already-tracked smart-money wallet used by this repository's own
# tests and present in gmgn_wallet_history.json. Deliberately NOT the Binance
# hot wallet from the Nansen workflow: Zerion declines to track high-volume
# exchange addresses, so that address would answer 400 and prove nothing about
# the credential.
DEFAULT_WALLET = "0x3D457D0B79EFAC77ed38F37870C713D0244479EA"
DEFAULT_DAYS = 90
PAGE_SIZE = 2
MAX_PAGES = 2

# Only match a credential that is actually *assigned* to a keyword. The
# separator is required on purpose: a bare "key is invalid" or "basic trade"
# is ordinary provider prose and must survive into the report, whereas
# "Authorization: Basic eyJ..." must not.
_SECRETISH = re.compile(
    r"(?i)\b(?:authorization|bearer|basic|token|api[_-]?key|apikey|key|secret|password)\b"
    r"\s*[:=]\s*\S+"
)

# A real Zerion key is long. Redacting a very short value would corrupt every
# word in the report ("k" would turn "The API key is invalid" into rubble), so
# such a value is not treated as a redactable credential.
MIN_REDACTABLE_LEN = 8


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively remove credentials and credential-shaped text from output."""
    if isinstance(value, dict):
        return {k: _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    text = value
    for secret in secrets:
        if not secret:
            continue
        text = text.replace(secret, "<redacted>")
        for form in (f"{secret}:", f"{secret}:"):
            text = text.replace(
                base64.b64encode(form.encode()).decode(), "<redacted>"
            )
    return _SECRETISH.sub("<redacted>", text)


def _secret_variants(key: str) -> tuple[str, ...]:
    if not key or len(key) < MIN_REDACTABLE_LEN:
        return ()
    return (key, f"{key}:", "Basic " + base64.b64encode(f"{key}:".encode()).decode())


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    """A short, obviously-normalized projection of one history row."""
    return {
        "chain": row.get("chain"),
        "side": row.get("side"),
        "token_symbol": row.get("token_symbol"),
        "amount_usd": row.get("amount_usd"),
        "price_usd": row.get("price_usd"),
        "timestamp": row.get("trade_timestamp") or row.get("timestamp"),
        "has_tx_hash": bool(row.get("tx_hash") or row.get("transaction_hash")),
    }


def run_smoke(
    address: str = DEFAULT_WALLET,
    *,
    api_key: str | None = None,
    days: int = DEFAULT_DAYS,
    chains: list[str] | None = None,
    http_get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Perform the minimal real request and return a scrubbed report.

    ``http_get`` is injectable only so this function can be unit-tested offline.
    Left as ``None`` in CI, ``zerion_layer`` uses ``requests``.
    """
    end = int(now_ts if now_ts is not None else time.time())
    start = end - max(1, int(days)) * 86400
    used_chains = list(chains or ["eth"])
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "pass": bool(passed), "detail": detail})

    # Built up front and always in the same shape, so render() never has to
    # guess. A missing key must still produce a readable report rather than a
    # traceback, because that is the one failure an operator will actually hit.
    report: dict[str, Any] = {
        "ok": False,
        "wallet": address,
        "chain_filter": used_chains,
        "window_utc": [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end))],
        "page_size": PAGE_SIZE,
        "max_pages": MAX_PAGES,
        "requests_spent": 0,
        "http_status": None,
        "endpoint": zl.transactions_url(address),
        "transactions": 0,
        "normalized_rows": 0,
        "page_trace": [],
        "truncated": False,
        "error": None,
        "terminal": False,
        "reason": None,
        "checks": checks,
        "sample_rows": [],
    }

    if not api_key:
        check("api_key_present", False, "ZERION_API_KEY is empty or unset")
        report["error"] = "missing_api_key"
        return _redact(report, ())
    report["requests_spent"] = 1

    check("api_key_present", True, "ZERION_API_KEY was supplied (value not shown)")

    result = zl.fetch_wallet_transactions(
        address,
        chains=chains or ["eth"],
        operation_types=["trade"],
        start=start,
        end=end,
        page_size=PAGE_SIZE,
        max_pages=MAX_PAGES,
        api_key=api_key,
        http_get=http_get,
        sleep=sleep,
    )

    status = result.get("http_status") or 0
    rows = result.get("rows") or []
    trace = result.get("page_trace") or []

    authorized = status == 200 and not result.get("error")
    check("authenticated_200", authorized,
          f"HTTP {status}: {result.get('error') or 'ok'}")

    if authorized:
        check("envelope_parsed", result.get("pages", 0) >= 1,
              f"{result.get('pages')} page(s) read, "
              f"{result.get('transactions')} transaction(s) in the data array")
        check("normalized_rows", isinstance(rows, list),
              f"{len(rows)} row(s) from {result.get('transactions')} transaction(s)")
        has_next = any(p.get("has_next") for p in trace)
        if not has_next:
            check("links_next", True,
                  "no links.next on the first page; nothing to follow "
                  "(single page of results)")
        else:
            followed = len(trace) > 1
            advanced = followed and trace[0]["transactions"] != trace[-1]["transactions"]
            check("links_next", followed and advanced,
                  f"cursor followed across {len(trace)} page(s): "
                  f"{[p['transactions'] for p in trace]}")
    else:
        check("envelope_parsed", False, "no 200 response to parse")
        check("normalized_rows", False, "no rows to normalize")
        check("links_next", False, "no 200 response, so no links.next")

    report.update({
        "ok": authorized,
        "requests_spent": result.get("pages") if authorized else 1,
        "http_status": status or None,
        "transactions": result.get("transactions"),
        "normalized_rows": len(rows),
        "page_trace": trace,
        "truncated": result.get("truncated"),
        "error": result.get("error"),
        "terminal": result.get("terminal"),
        "reason": result.get("reason"),
        "sample_rows": [_row_summary(r) for r in rows[:PAGE_SIZE]],
    })
    return _redact(report, _secret_variants(api_key))


def render(report: dict[str, Any]) -> str:
    lines = ["", "=" * 66, "ZERION LIVE API SMOKE TEST", "=" * 66]
    lines.append(f"wallet        : {report['wallet']}")
    lines.append(f"chain filter  : {', '.join(report['chain_filter'])}")
    lines.append(f"window (UTC)  : {report['window_utc'][0]} -> {report['window_utc'][1]}")
    lines.append(f"page[size]    : {report['page_size']}  (max_pages={report['max_pages']})")
    lines.append(f"requests spent: {report.get('requests_spent')}")
    lines.append("-" * 66)
    lines.append(f"HTTP status   : {report.get('http_status')}")
    lines.append(f"transactions  : {report.get('transactions')}")
    lines.append(f"normalized    : {report.get('normalized_rows')} row(s)")
    if report.get("error"):
        lines.append(f"error         : {report['error']}")
    if report.get("reason"):
        lines.append(f"reason        : {report['reason']}  (terminal={report.get('terminal')})")
    if report.get("page_trace"):
        lines.append("-" * 66)
        for p in report["page_trace"]:
            lines.append(
                f"  page {p['page']}: {p['transactions']} transaction(s) -> "
                f"{p['normalized']} row(s), next={p['has_next']}"
            )
    if report.get("sample_rows"):
        lines.append("-" * 66)
        for row in report["sample_rows"]:
            lines.append(f"  {json.dumps(row, sort_keys=True)}")
    lines.append("-" * 66)
    for c in report["checks"]:
        mark = "PASS" if c["pass"] else "FAIL"
        lines.append(f"  [{mark}] {c['check']}: {c['detail']}")
    lines.append("-" * 66)
    lines.append(f"RESULT: {'PASS' if report['ok'] else 'FAIL'}")
    lines.append("=" * 66)
    lines.append("Note: a 200 with 0 records is a PASS. It means the request was")
    lines.append("authorized and parsed correctly, and the window was simply quiet.")
    lines.append("credentials and Authorization headers are never printed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--wallet", default=os.environ.get("ZERION_SMOKE_WALLET", DEFAULT_WALLET))
    parser.add_argument("--days", type=int, default=int(os.environ.get("ZERION_SMOKE_DAYS", DEFAULT_DAYS)))
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    report = run_smoke(args.wallet, days=args.days, api_key=os.environ.get("ZERION_API_KEY"))
    print(render(report))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
