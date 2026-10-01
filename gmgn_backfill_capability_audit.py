#!/usr/bin/env python3
"""GMGN Historical Backfill Capability Audit.

Read-only audit of whether the *already integrated* GMGN layer can retrieve
wallet/trade history deep enough to satisfy the project's 90-day Historical
Wallet Discovery requirement.

Three tiers of evidence, never collapsed
    Every capability conclusion is labelled with how it is known:

    ``endpoint_capability``
        What the code shows about the CLI invocation itself -- which
        subcommands are called and which flags are passed. Read from the
        repository source, so it is exact, but it describes the *request* the
        project makes and not the provider's behaviour.

    ``code_capability``
        What the project's own code can actually do with the response --
        pagination handling, page caps, rate-limit policy, chain and wallet
        selection.

    ``demonstrated_capability``
        What local artifacts prove. Only this tier is evidence that something
        works.

    Where the repository contains no reliable evidence, the verdict is the
    literal string ``UNVERIFIED — repository evidence is insufficient``. No
    GMGN capability is asserted from documentation this repository does not
    contain.

Why UNVERIFIED is likely and must not be softened
    ``gmgn_depth_probe.py`` exists specifically because the project's GMGN
    depth is unknown, and the workflow that runs it deliberately does **not**
    commit its result artifact, so no probe measurement exists in the
    repository. The one committed backfill archive contains zero records. The
    probe has therefore never been recorded as run, and this audit cannot
    assert a provider-side historical window from the code.

What this module does not do
    No request is issued. No provider is contacted. No dataset, registry,
    archive or production module is written or modified. The only file this
    module writes is its own report. ``gmgn_layer``, ``gmgn_depth_probe`` and
    ``wallet_trade_backfill`` are read and parsed, never imported-and-called in
    a way that could reach the network, and never edited.

Determinism
    Everything except ``generated_at`` is a pure function of the repository
    files. ``payload_sha256`` is SHA-256 over the report minus ``generated_at``
    and minus the digest itself, matching the convention used by
    ``proven_wallet_registry.json`` and ``historical_depth_audit.json``.

Usage
    python3 gmgn_backfill_capability_audit.py            # write the report
    python3 gmgn_backfill_capability_audit.py --verify   # rebuild, no write
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent

GMGN_LAYER = HERE / "gmgn_layer.py"
DEPTH_PROBE = HERE / "gmgn_depth_probe.py"
BACKFILL = HERE / "wallet_trade_backfill.py"
PROBE_WORKFLOW = HERE / ".github/workflows/gmgn-depth-probe.yml"
BACKFILL_WORKFLOW = HERE / ".github/workflows/gmgn-wallet-backfill.yml"
ARCHIVE_DIR = HERE / "wallet_archive/raw/gmgn/activity"
HISTORY = HERE / "gmgn_wallet_history.json"

SCHEMA_VERSION = 1
MODE = "GMGN_BACKFILL_CAPABILITY_AUDIT_READ_ONLY"

#: The literal verdict used wherever the repository cannot support a claim.
UNVERIFIED = "UNVERIFIED \u2014 repository evidence is insufficient"

NON_DETERMINISTIC = ("generated_at",)

#: Project files this audit must leave byte-identical. Recorded so the claim is
#: checkable rather than asserted.
READ_ONLY_FILES = (
    "gmgn_layer.py",
    "gmgn_depth_probe.py",
    "wallet_trade_backfill.py",
    "gmgn_wallet_history.json",
    "wallet_history_validation.py",
    "historical_discovery.py",
    "proven_wallet_registry.json",
    "reverse_historical_discovery.json",
)

LIMITATIONS = {
    "no_provider_contact": (
        "No GMGN request was issued by this audit. Every endpoint-tier "
        "statement describes the request the repository's code constructs, "
        "which is not the same as what the provider returns."
    ),
    "no_committed_probe_result": (
        "gmgn-depth-probe.yml states the result artifact is deliberately not "
        "committed, and gmgn_depth_probe.json is absent from the repository. "
        "No measurement of provider-side depth exists locally."
    ),
    "empty_archive_is_not_a_measurement": (
        "The committed wallet_archive holds zero records. That is a failure of "
        "one scripted run, not evidence about how far back GMGN can page."
    ),
    "cli_surface_inferred_from_flag_usage": (
        "The gmgn-cli flag set is read from how this repository invokes it. "
        "Whether the CLI accepts additional flags -- a real cursor, a from/to "
        "window, a larger limit -- is not established here, because gmgn-cli is "
        "not installed in this environment and its help output cannot be read."
    ),
    "no_api_documentation_in_repository": (
        "No GMGN API reference, changelog or provider-declared retention window "
        "is committed. Any statement about a provider-imposed maximum age "
        "would therefore be fabricated."
    ),
    "rate_limit_policy_is_project_side_only": (
        "The repository's rate-limit handling is fully readable, but the "
        "provider's actual quota numbers are not documented in the repository."
    ),
}


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_digest(envelope: Mapping[str, Any]) -> str:
    """SHA-256 over every field that must not change between runs."""
    payload = {k: v for k, v in envelope.items() if k not in NON_DETERMINISTIC}
    determinism = dict(payload.get("determinism") or {})
    determinism.pop("payload_sha256", None)
    payload["determinism"] = determinism
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _cli_invocations(path: Path) -> list[dict[str, Any]]:
    """Every gmgn-cli argv list the file builds, as code not as text.

    Read from the AST rather than grepped, so a flag mentioned in a comment or
    docstring cannot be mistaken for one the code actually sends.
    """
    found: list[dict[str, Any]] = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.List):
            continue
        try:
            values = [ast.literal_eval(el) for el in node.elts]
        except (ValueError, SyntaxError):
            continue
        if not values or not all(isinstance(v, str) for v in values):
            continue
        if "gmgn-cli" not in values:
            continue
        # Argument lists are built as ["npx", ..., *args, flag, value]; capture
        # the literal span and the surrounding line so a reader can locate it.
        found.append({
            "file": path.name,
            "line": node.lineno,
            "argv_literal": values,
        })
    return found


def _string_constants(path: Path) -> list[str]:
    tree = _parse(path)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _module_assignments(path: Path) -> dict[str, Any]:
    """Module-level constant assignments, evaluated without executing the module."""
    out: dict[str, Any] = {}
    for node in _parse(path).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    continue
    return out


def audit_endpoints() -> dict[str, Any]:
    """Which GMGN subcommands and flags the project actually sends."""
    layer_calls = _cli_invocations(GMGN_LAYER)
    backfill_calls = _cli_invocations(BACKFILL)
    probe_calls = _cli_invocations(DEPTH_PROBE)
    constants = _module_assignments(GMGN_LAYER)

    return {
        "transport": {
            "mechanism": "npx --yes gmgn-cli <subcommand> ... --raw",
            "library_dependency": "none; subprocess only",
            "note": (
                "GMGN is reached only through the npm CLI wrapper. No direct "
                "HTTP call to a GMGN host exists in gmgn_layer.py; its only "
                "requests.* calls are CMC (line 69) and Telegram (line 710)."
            ),
        },
        "wallet_activity_or_history": {
            "endpoint": "gmgn portfolio activity",
            "wrapper": "gmgn_layer.portfolio_activity(chain, wallet, limit=200)",
            "flags_sent": ["--chain", "--wallet", "--limit", "--type buy", "--type sell", "--raw"],
            "time_range_flag_sent": False,
            "cursor_flag_sent": False,
            "offset_or_page_flag_sent": False,
            "evidence": "gmgn_layer.py:379-382",
            "classification": {
                "endpoint_capability": (
                    "A single unfiltered call. --limit bounds row count; no "
                    "from/to, cursor or offset flag is sent, so the provider is "
                    "asked for 'most recent rows' and nothing more."
                ),
                "code_capability": (
                    "One page per call. The layer cannot advance past that page."
                ),
                "demonstrated_capability": (
                    "gmgn_wallet_history.json holds 182,588 rows for 2,215 "
                    "wallets, but those came from the track smartmoney feed "
                    "accumulated over a 8.97-day window, not from "
                    "portfolio activity paging. Portfolio activity has never "
                    "demonstrated depth in local data."
                ),
            },
        },
        "smart_money_feed": {
            "endpoint": "gmgn track smartmoney",
            "wrapper": "gmgn_layer.run_gmgn(chain)",
            "flags_sent": ["--chain", "--limit", "200", "--raw"],
            "purpose": "recent smart-money signals, not wallet history",
            "evidence": "gmgn_layer.py:32-33",
            "classification": {
                "endpoint_capability": (
                    "No time filter and a fixed --limit 200. This is the feed "
                    "that populated gmgn_wallet_history.json; it is a forward "
                    "signal feed and is not a historical query."
                ),
                "code_capability": "200 rows per chain per run, most recent.",
                "demonstrated_capability": (
                    "182,588 rows over 2,215 wallets across an 8.9714-day "
                    "window, accumulated by repeated scheduled runs."
                ),
            },
        },
        "token_market_price": {
            "endpoint": "gmgn market kline",
            "wrapper": "gmgn_layer.token_kline(chain, address, start_ts, end_ts)",
            "flags_sent": ["--chain", "--address", "--resolution", "4h", "--from", "--to", "--raw"],
            "time_range_flag_sent": True,
            "note": (
                "The ONLY GMGN call in the project that passes an explicit "
                "time range. It is market data, not wallet activity, and is not "
                "a source of wallet trade history."
            ),
            "evidence": "gmgn_layer.py:392-395",
            "classification": {
                "endpoint_capability": (
                    "Accepts --from/--to in seconds, so the CLI can express an "
                    "arbitrary window for klines."
                ),
                "code_capability": (
                    "Callers pass a window bounded by an existing trade; see "
                    "wallet_history_validation's offline note that kline is only "
                    "consulted for peak confirmation."
                ),
                "demonstrated_capability": (
                    "Not demonstrated in any committed artifact; no kline "
                    "dataset is committed."
                ),
            },
        },
        "pagination": {
            "mechanism_in_production_code": "NONE",
            "evidence": (
                "gmgn_layer.portfolio_activity sends a single --limit with no "
                "cursor, offset or page parameter. The response parser reads "
                "obj['list'] or obj['data'] and ignores any pagination object."
            ),
            "mechanism_in_probe_code": "cursor-first, page-number fallback",
            "probe_evidence": "gmgn_depth_probe.py:185-191",
            "probe_detail": (
                "The probe tries --cursor when the previous response advertised "
                "one, else --page. It inspects pagination.cursor, next_cursor, "
                "nextCursor, page_token, next_page_token, is_last_page, "
                "has_more, total_pages, total, total_records and count."
            ),
            "critical_distinction": (
                "Cursor support exists ONLY in the probe. It is never used by "
                "gmgn_layer or wallet_trade_backfill, so the production "
                "integration has no pagination at all."
            ),
            "classification": {
                "endpoint_capability": UNVERIFIED,
                "code_capability": (
                    "The probe can follow a cursor IF the provider advertises "
                    "one. No committed artifact shows a provider response "
                    "containing a cursor, so cursor support is untested."
                ),
                "demonstrated_capability": (
                    "None. gmgn_depth_probe.json is absent and the workflow "
                    "explicitly does not commit it."
                ),
            },
        },
        "time_range_filtering": {
            "wallet_activity": "UNSUPPORTED by current code -- no from/to flag is sent",
            "token_kline": "SUPPORTED -- --from/--to are sent",
            "significance": (
                "This is the central gap. Even a working backfill script could "
                "not ask GMGN for 'trades in the last 90 days'; it can only ask "
                "for the most recent N rows and hope pagination reaches back."
            ),
        },
        "chain_selection": {
            "mechanism": "--chain flag per call",
            # Normalized to a list: gmgn_layer.CHAINS is a tuple, which JSON
            # writes as an array and reads back as a list. Leaving it a tuple
            # would make every --verify run compare list != tuple and fail.
            "supported_chains": list(constants.get("CHAINS") or ("sol", "bsc", "base", "eth")),
            "note": (
                "Looped per chain in wallet_trade_backfill.CHAINS and in "
                "gmgn_layer.CHAINS; identical four-chain set in both."
            ),
        },
        "wallet_selection": {
            "mechanism": "--wallet flag per call",
            "single_or_bulk": "one wallet per call",
            "enumeration_support": (
                "None. wallet_trade_backfill takes a single BACKFILL_WALLET env "
                "value; gmgn_depth_probe takes a single PROBE_WALLET. Neither "
                "enumerates wallets, and gmgn_depth_probe documents that as a "
                "deliberate safety property."
            ),
            "cost_implication": (
                "2,215 wallets x 4 chains = 8,860 separate CLI invocations to "
                "cover the committed population, each of which spawns npx."
            ),
        },
    }


def audit_limits() -> dict[str, Any]:
    """Caps, budgets and policies the current code imposes."""
    constants = _module_assignments(GMGN_LAYER)
    probe_constants = _module_assignments(DEPTH_PROBE)

    return {
        "max_page_size_requested": {
            "value": 200,
            "where": "gmgn_layer.portfolio_activity default limit=200; wallet_trade_backfill LIMIT env default 200",
            "note": (
                "This is what the project asks for, not a proven provider "
                "maximum. Whether GMGN would accept more is untested."
            ),
        },
        "max_pages_per_wallet": {
            "production": 1,
            "production_evidence": "gmgn_layer.portfolio_activity makes exactly one call and never loops.",
            "probe": probe_constants.get("MAX_PAGES", 10),
            "probe_note": "gmgn_depth_probe.MAX_PAGES is a hard self-imposed stop, default 10.",
            "backfill": 1,
            "backfill_evidence": "wallet_trade_backfill calls run_cli once per chain with no loop.",
        },
        "rows_per_wallet_ceiling": {
            "production": "200 rows per wallet per chain per run",
            "implied_by": "1 page x --limit 200",
        },
        "max_historical_timestamp": {
            "enforced_by_code": False,
            "note": (
                "No code path filters by age. There is no earliest-timestamp "
                "check anywhere in the GMGN integration."
            ),
            "provider_imposed": UNVERIFIED,
        },
        "provider_time_window": {
            "wallet_activity": UNVERIFIED,
            "reason": (
                "No GMGN documentation is committed and no live response has "
                "been recorded. The repository asserts only that the stored "
                "capture is 7.9-8.97 days wide, which describes collection, "
                "not provider retention."
            ),
        },
        "cursor_expiration": {
            "value": UNVERIFIED,
            "note": "No cursor has ever been observed in a committed artifact, so its lifetime is unknown.",
        },
        "rate_limits": {
            "provider_quota_numbers": UNVERIFIED,
            "project_policy": {
                "terminal_for_run": True,
                "policy": (
                    "gmgn_layer.detect_rate_limit treats a rate limit as "
                    "terminal for the entire run. Every later GMGN call "
                    "short-circuits to {} without touching the network, so the "
                    "run cannot extend a ban by retrying."
                ),
                "detection_shapes": [
                    "code=429 error=RATE_LIMIT_EXCEEDED",
                    "code=429 error=RATE_LIMIT_BANNED",
                    "HTTP 429 ... IP is temporarily banned ... repeated requests can extend the ban by 5s up to 5 minutes",
                    "Chinese-language plan limit (限频 / 频率)",
                ],
                "detection_rule": (
                    "Substring match on 'rate_limit', 'rate limit', '429', "
                    "限频 or 频率. Applied to stderr, stdout and the parsed "
                    "response body."
                ),
                "evidence": "gmgn_layer.py:253-309",
            },
            "activity_call_budget": {
                "value": constants.get("ACTIVITY_CALL_BUDGET", 300),
                "pacing_seconds": constants.get("ACTIVITY_CALL_PACING_SECONDS", 0.5),
                "evidence": "gmgn_layer.py:355-356",
                "note": (
                    "A project-side fan-out cap, not a provider limit. 300 calls "
                    "at 0.5s pacing; exhaustion silently returns [] for every "
                    "remaining wallet."
                ),
            },
            "backfill_has_no_rate_limit_handling": {
                "value": True,
                "evidence": "wallet_trade_backfill.run_cli checks only returncode; it never calls detect_rate_limit.",
                "consequence": (
                    "The backfill path has no rate-limit policy at all and, per "
                    "the provider's own quoted warning, retrying into a ban "
                    "extends it."
                ),
            },
        },
        "authentication": {
            "mechanism": "GMGN_API_KEY environment variable, exported into the subprocess",
            "layer": "gmgn_layer.py:14,325 (env['GMGN_API_KEY'] = GMGN_API_KEY)",
            "probe": "gmgn_depth_probe.py:115-117 refuses to run without a key",
            "backfill": (
                "wallet_trade_backfill.py does NOT check for a key and does "
                "NOT inject one into the subprocess env. The workflow supplies "
                "GMGN_API_KEY to the process, so the child inherits it, but the "
                "script itself has no guard and no preflight."
            ),
            "secret_handling": (
                "No credential value is read into any committed artifact by any "
                "of the three scripts."
            ),
            "note": (
                "This audit reads no credential and prints none. Whether the "
                "key is valid, scoped or metered is unverifiable from the "
                "repository."
            ),
        },
        "endpoint_specific_limitations": {
            "track_smartmoney": "Recent feed only. No time filter exists for it in the CLI usage here.",
            "portfolio_activity": "No time filter. Depth is reachable only via pagination, which production code does not use.",
            "market_kline": "Has --from/--to, but is market data, not wallet activity.",
        },
    }


def audit_wallet_archive() -> dict[str, Any]:
    """Why the committed archive holds one wallet and zero records."""
    files: list[dict[str, Any]] = []
    total_records = 0
    if ARCHIVE_DIR.is_dir():
        for path in sorted(ARCHIVE_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                files.append({"file": path.name, "readable": False})
                continue
            records = data.get("records")
            count = len(records) if isinstance(records, list) else 0
            total_records += count
            files.append({
                "file": path.name,
                "readable": True,
                "chain": data.get("chain"),
                "wallet": data.get("wallet"),
                "limit_requested": data.get("limit"),
                "records": count,
                "fetched_at": data.get("fetched_at"),
                "fetched_at_iso": (
                    datetime.fromtimestamp(int(data["fetched_at"]), timezone.utc).isoformat()
                    if isinstance(data.get("fetched_at"), (int, float)) else None
                ),
                "has_error_field": any(k in data for k in ("error", "errors", "status", "stderr")),
            })

    wallets = {f.get("wallet") for f in files if f.get("wallet")}

    return {
        "archive_dir": "wallet_archive/raw/gmgn/activity",
        "writer": "wallet_trade_backfill.py",
        "writer_invocation": ".github/workflows/gmgn-wallet-backfill.yml (workflow_dispatch only)",
        "files": files,
        "file_count": len(files),
        "unique_wallets": sorted(w for w in wallets if w),
        "total_records": total_records,
        "root_cause": {
            "summary": (
                "The archive is empty because the writer treats a failed "
                "request as an empty result and writes it anyway. "
                "wallet_trade_backfill.run_cli returns {} on any non-zero "
                "returncode, the caller derives rows = [] from that, and the "
                "file is written with records: [] unconditionally."
            ),
            "traced_path": [
                "wallet_trade_backfill.py:83-88 loops CHAINS and calls run_cli(chain).",
                "run_cli (line 34-38) runs npx gmgn-cli; if p.returncode != 0 it prints stderr and returns {}.",
                "It returns {} for every failure mode it survives: non-zero exit or unparsable stdout.",
                "A subprocess exception is different: line 34 is not wrapped, so it propagates and aborts the run before anything is written.",
                "Line 85 then does rows = obj.get('list') or obj.get('data') or [], which yields [] for {}.",
                "Lines 89-97 write the per-chain file with that empty list regardless of why it is empty.",
                "Lines 121-132 print a result summary; the raw stderr that explains the failure is printed to stdout only and is never persisted.",
            ],
            "why_the_cause_is_not_recoverable_from_the_artifact": (
                "The committed per-chain files record source, chain, wallet, "
                "fetched_at, limit and records. None of them records a return "
                "code, stderr, or any error field, so an empty file is "
                "indistinguishable from a wallet that genuinely had no "
                "activity in the window."
            ),
            "compounding_factors": [
                {
                    "factor": "subprocess exceptions are unhandled",
                    "evidence": "wallet_trade_backfill.py:34 calls subprocess.run outside any try/except; only the JSON parse loop has one.",
                    "consequence": (
                        "In an environment without npx this raises "
                        "FileNotFoundError and the script dies. npx is absent "
                        "in this audit environment (verified: 'npx: command "
                        "not found'), so the script cannot run here at all."
                    ),
                },
                {
                    "factor": "no API key preflight",
                    "evidence": "wallet_trade_backfill.py has no GMGN_API_KEY reference, unlike gmgn_depth_probe.py:115.",
                    "consequence": (
                        "An unkeyed or invalid key produces the same empty "
                        "records: [] as a rate-limited or empty wallet."
                    ),
                },
                {
                    "factor": "no rate-limit detection",
                    "evidence": "wallet_trade_backfill.run_cli never calls gmgn_layer.detect_rate_limit.",
                    "consequence": (
                        "A 429 is indistinguishable from an empty wallet in the "
                        "persisted artifact."
                    ),
                },
                {
                    "factor": "one page, one wallet, four chains",
                    "evidence": "CHAINS has 4 entries; run_cli is called once per chain; no pagination loop exists.",
                    "consequence": (
                        "The most this script could ever store is 4 x 200 = 800 "
                        "rows for a single wallet, i.e. roughly the most recent "
                        "few days. It could not produce 90 days even on full "
                        "success, which is the deeper structural limit."
                    ),
                },
            ],
            "what_is_not_the_cause": [
                "Not a data-format or normalization bug: normalize() is only reached for rows that were never fetched, and the summary confirms raw_rows=0 before normalization.",
                "Not an intentionally empty wallet: the workflow default wallet is a real address, but the artifact cannot prove either way because no error state was persisted.",
                "Not a threshold filter: LIMIT=200 bounds retrieval, not the recorded rows, and rows=0 was recorded upstream of the >=5000 USD qualification.",
            ],
            "conclusion_strength": (
                "The code path above is directly verifiable from the source and "
                "fully explains how an empty archive is produced. Which "
                "specific failure mode occurred on 2026-09-24 is NOT "
                "recoverable, because no error state was persisted."
            ),
        },
    }


def audit_capability() -> dict[str, Any]:
    """90/180/365 verdicts, each with its evidence tier kept separate."""
    def verdict(window_days: int) -> dict[str, Any]:
        return {
            "window_days": window_days,
            "endpoint_capability": UNVERIFIED,
            "endpoint_reason": (
                "The repository contains no GMGN API documentation, no recorded "
                "live response, and no provider-declared retention window. "
                "gmgn-cli is not installed here, so its flags cannot be "
                "introspected. Claiming a provider-side limit here would be "
                "fabrication."
            ),
            "code_capability": False,
            "code_reason": (
                "wallet_trade_backfill issues exactly one unfiltered "
                "--limit 200 call per chain and has no pagination, so it "
                "cannot reach further back than that single page. "
                "gmgn_layer.portfolio_activity is identical."
            ),
            "demonstrated_capability": False,
            "demonstrated_reason": (
                "The only committed GMGN-derived artifact with any depth is "
                "gmgn_wallet_history.json, which spans 8.9714 days from the "
                "track smartmoney feed, not from portfolio activity. "
                "portfolio activity has never produced a single committed row: "
                "the wallet_archive holds zero records."
            ),
            "verdict": "NO",
            "verdict_basis": (
                "NO rather than UNVERIFIED because the current code and the "
                "local data both affirmatively show the capability is not "
                "available today, independent of what the provider might allow."
            ),
        }

    return {
        "A_90_days": verdict(90),
        "B_180_days": verdict(180),
        "C_365_days": verdict(365),
        "why_no_is_safer_than_unverified": (
            "UNVERIFIED is reserved for what the provider might do. The "
            "question 'can this project obtain 90 days today' has a definite "
            "answer: no, because the code retrieves one page and the archive "
            "holds nothing. That is a fact about the repository."
        ),
        "what_would_change_the_verdict": (
            "A recorded gmgn_depth_probe run showing span_days >= 90 for one "
            "wallet would establish endpoint capability. Nothing in the "
            "repository does so today."
        ),
    }


def audit_demonstrated_depth() -> dict[str, Any]:
    """What local data actually proves about depth, from the prior audit."""
    primary: dict[str, Any] = {"present": False}
    if HISTORY.exists():
        # Read the committed prior audit rather than re-deriving the same numbers.
        prior = HERE / "historical_depth_audit.json"
        if prior.exists():
            data = json.loads(prior.read_text(encoding="utf-8"))
            primary = {
                "present": True,
                "artifact": HISTORY.name,
                "md5": file_md5(HISTORY),
                "source_of_record": prior.name,
                "archive_span_days": data["findings"]["primary_archive_span_days"],
                "widest_asset_span_days": data["findings"]["widest_asset_span_days"],
                "unique_wallets": data["sources"]["primary"]["unique_wallets"],
                "reconstructed_rows": data["sources"]["primary"]["reconstructed_rows"],
                "unique_assets": data["sources"]["primary"]["unique_assets"],
                "collected_via": "gmgn track smartmoney (forward feed), not portfolio activity",
            }
    return {
        "gmgn_wallet_history": primary,
        "wallet_archive": {
            "records": 0,
            "proves": "nothing about depth; it is an empty run artifact",
        },
        "gmgn_depth_probe_result": {
            "present": DEPTH_PROBE.with_name("gmgn_depth_probe.json").exists(),
            "note": (
                "Absent by design: gmgn-depth-probe.yml states the result is "
                "deliberately not committed and stays in run logs."
            ),
        },
        "maximum_demonstrated_days": 8.9714,
        "required_days": 90,
        "shortfall_days": 81.0286,
    }


def audit_codebase_maturity() -> dict[str, Any]:
    """Test coverage and entry points, recorded because it bears on risk."""
    tests = sorted(p.name for p in HERE.glob("test_*.py"))
    # This audit's own test file matches the substring "gmgn" but exercises the
    # audit, not GMGN production code. Counting it would silently flip the
    # coverage finding below, so it is excluded by exact name and reported
    # separately rather than quietly dropped.
    audit_self_tests = [t for t in tests if t == Path(__file__).with_name(
        "test_gmgn_backfill_capability_audit.py").name]
    gmgn_tests = [t for t in tests if "gmgn" in t and t not in audit_self_tests]
    workflows = sorted(p.name for p in (HERE / ".github/workflows").glob("*.yml")) \
        if (HERE / ".github/workflows").is_dir() else []
    gmgn_workflows = [w for w in workflows if "gmgn" in w]
    return {
        "gmgn_test_files": gmgn_tests,
        "gmgn_tests_exist": bool(gmgn_tests),
        "audit_self_test_files_excluded": audit_self_tests,
        "note": (
            "No test_gmgn*.py exists apart from this audit's own test, which "
            "covers the audit and not GMGN production code. gmgn_layer.py, "
            "gmgn_depth_probe.py and wallet_trade_backfill.py have no unit "
            "tests, so their behaviour is unverified by the suite. The audit "
            "below therefore reads them as source, not as tested behaviour."
        ),
        "gmgn_workflows": gmgn_workflows,
        "all_workflows": workflows,
        "entry_points": {
            "scheduled_smart_money": ".github/workflows/gmgn-smart-money.yml",
            "manual_wallet_backfill": ".github/workflows/gmgn-wallet-backfill.yml (workflow_dispatch)",
            "manual_depth_probe": ".github/workflows/gmgn-depth-probe.yml (workflow_dispatch)",
        },
        "import_requirements": {
            "gmgn_layer": "imports requests at module level; requests is NOT installed in this environment, so gmgn_layer cannot be imported here.",
            "verified": "ModuleNotFoundError: No module named 'requests' on import gmgn_layer.",
            "consequence": (
                "Existing control docs already record this as environmental. "
                "It also means gmgn_layer cannot be exercised offline in this "
                "sandbox, which is why this audit parses its AST instead of "
                "calling it."
            ),
        },
    }


def build_report() -> dict[str, Any]:
    """Assemble the audit. Pure function of the repository files."""
    present = [name for name in READ_ONLY_FILES if (HERE / name).exists()]

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "orders_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": {
            "no_provider_contact": True,
            "no_backfill_started": True,
            "no_provider_added": True,
            "no_dataset_or_registry_modified": True,
            "no_threshold_modified": True,
            "no_production_module_modified": True,
            "production_modules_modified": [],
            "threshold_untouched": "reverse_historical_discovery.MIN_ASSET_HISTORY_DAYS",
            "only_file_written_by_this_audit": "gmgn_backfill_capability_audit.json",
        },
        "evidence_tiers": {
            "endpoint_capability": "What the CLI invocation the code constructs can request. Read from source; exact about the request, silent about the response.",
            "code_capability": "What the project's own code can do with the response, including pagination, caps and policy.",
            "demonstrated_capability": "What a committed local artifact proves. The only tier that proves something works.",
            "unverified_literal": UNVERIFIED,
        },
        "endpoints": audit_endpoints(),
        "limits": audit_limits(),
        "wallet_archive": audit_wallet_archive(),
        "capability": audit_capability(),
        "demonstrated_depth": audit_demonstrated_depth(),
        "codebase_maturity": audit_codebase_maturity(),
        "requirements_for_a_real_90d_backfill": {
            "current_state": "NOT POSSIBLE with the current code path.",
            "blocking_gaps": [
                {
                    "gap": "no time-range request for wallet activity",
                    "detail": (
                        "portfolio activity is called with only --limit. "
                        "Adding a from/to window is only possible if the CLI "
                        "accepts one; the kline call shows the CLI does accept "
                        "--from/--to for that subcommand, which is suggestive "
                        "but not proof for portfolio activity."
                    ),
                    "verified": "The absence of the flag in code is verified.",
                    "cli_support": UNVERIFIED,
                },
                {
                    "gap": "no pagination in the backfill path",
                    "detail": (
                        "Even with more rows per page, one page is all the "
                        "current code retrieves. The probe's cursor handling is "
                        "the only pagination implementation in the repository "
                        "and it is not wired into any writer."
                    ),
                    "verified": "The absence of pagination in wallet_trade_backfill and gmgn_layer is verified.",
                    "provider_pagination_support": UNVERIFIED,
                },
                {
                    "gap": "wallet enumeration",
                    "detail": (
                        "One wallet per invocation. Covering 2,215 wallets x 4 "
                        "chains is 8,860 CLI spawns, against a project-side "
                        "budget of 300 activity calls per run."
                    ),
                    "verified": "Single-wallet invocation and the 300-call budget are verified in source.",
                },
                {
                    "gap": "failure is indistinguishable from empty",
                    "detail": (
                        "Must be fixed before any backfill is trusted: the "
                        "writer must persist returncode, stderr, whether a key "
                        "was present, and whether a rate limit was seen, so "
                        "that 'no data' can never again be written as if it "
                        "were a fact about the wallet."
                    ),
                    "verified": "Verified by tracing wallet_trade_backfill.run_cli.",
                },
                {
                    "gap": "no rate-limit policy in the backfill path",
                    "detail": (
                        "The provider states retrying during a ban extends it. "
                        "A backfill without a terminal-on-429 policy risks "
                        "escalating to a 5-minute IP ban."
                    ),
                    "verified": "Verified: wallet_trade_backfill never calls detect_rate_limit.",
                },
                {
                    "gap": "no tests for the GMGN code",
                    "detail": (
                        "No test_gmgn*.py covers the GMGN production code. A backfill writer with no "
                        "tests, no persisted error state and no pagination "
                        "should not be pointed at a metered plan."
                    ),
                    "verified": "Verified by listing test_*.py.",
                },
            ],
            "minimum_to_attempt": [
                "Run gmgn_depth_probe once for one wallet and RECORD its result in the repository, so endpoint capability stops being unverified.",
                "Persist provider error state in the backfill writer so empty is distinguishable from failed.",
                "Add pagination to the writer, reusing gmgn_depth_probe's cursor handling rather than writing new API logic.",
                "Add offline unit tests for the writer and probe request construction, no network required.",
                "Only then scale to more wallets, respecting the activity budget.",
            ],
        },
        "is_gmgn_worth_attempting_first": {
            "verdict": "YES, but only as a measured probe first -- not as an assumed backfill source.",
            "reasons_for": [
                "It is the only wallet-trade provider already integrated, with a committed 182,588-row archive proving the schema and normalization path work end to end.",
                "Its kline subcommand already accepts --from/--to, so the CLI is not obviously incapable of time-windowed queries.",
                "A one-wallet probe already exists, is read-only, and is bounded by MAX_PAGES. Running it is cheap and decisive.",
            ],
            "reasons_for_caution": [
                "No committed measurement of provider-side depth exists, so depth is UNVERIFIED rather than known.",
                "The one committed backfill attempt produced zero records and recorded no reason.",
                "The backfill path has no pagination, no rate-limit policy, no error persistence and no tests.",
                "Coverage cost is high: one CLI spawn per wallet per chain.",
            ],
            "recommended_first_action": (
                "Run the existing read-only gmgn_depth_probe for ONE wallet and "
                "commit its result, before writing any new backfill code. That "
                "single measurement converts the 90/180/365 verdicts from "
                "NO-by-omission into evidence-based answers."
            ),
        },
        "limitations": LIMITATIONS,
        "provenance": {
            "files_read": [
                "gmgn_layer.py",
                "gmgn_depth_probe.py",
                "wallet_trade_backfill.py",
                ".github/workflows/gmgn-depth-probe.yml",
                ".github/workflows/gmgn-wallet-backfill.yml",
                "historical_depth_audit.json",
                "wallet_archive/raw/gmgn/activity/*.json",
            ],
            "read_only_files_md5": {name: file_md5(HERE / name) for name in present},
            "gmgn_layer_imported": False,
            "gmgn_layer_import_note": (
                "Parsed via ast, never imported: gmgn_layer imports requests at "
                "module level and requests is absent in this environment."
            ),
        },
        "determinism": {
            "non_deterministic_fields": list(NON_DETERMINISTIC),
            "digest": "sha256 over the report minus " + ", ".join(NON_DETERMINISTIC),
            "payload_sha256": "",
            "note": (
                "generated_at is the only wall-clock field, so rebuilding from "
                "an unchanged repository reproduces payload_sha256 exactly."
            ),
        },
    }
    report["determinism"]["payload_sha256"] = payload_digest(report)
    return report


REPORT = HERE / "gmgn_backfill_capability_audit.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="rebuild and compare, do not write")
    args = parser.parse_args(argv)

    report = build_report()

    caps = report["capability"]
    archive = report["wallet_archive"]
    limits = report["limits"]
    print(f"wallet_archive files      : {archive['file_count']}")
    print(f"wallet_archive wallets    : {len(archive['unique_wallets'])}")
    print(f"wallet_archive records    : {archive['total_records']}")
    print(f"max pages per wallet (prod): {limits['max_pages_per_wallet']['production']}")
    print(f"activity call budget      : {limits['rate_limits']['activity_call_budget']['value']}")
    print(f"pagination in prod code   : {report['endpoints']['pagination']['mechanism_in_production_code']}")
    print(f"90d  capability           : {caps['A_90_days']['verdict']}")
    print(f"180d capability           : {caps['B_180_days']['verdict']}")
    print(f"365d capability           : {caps['C_365_days']['verdict']}")
    print(f"max demonstrated days     : {report['demonstrated_depth']['maximum_demonstrated_days']}")
    print(f"endpoint capability       : {UNVERIFIED}")
    print(f"orders_enabled            : {report['orders_enabled']}")
    print(f"payload_sha256            : {report['determinism']['payload_sha256']}")

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
        for section in ("endpoints", "limits", "wallet_archive", "capability",
                        "demonstrated_depth", "codebase_maturity"):
            if committed.get(section) != report.get(section):
                print(f"FAIL: section moved: {section}")
                return 1
        print(f"OK: deterministic reproduction confirmed ({got[:16]}...)")
        return 0

    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {REPORT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())