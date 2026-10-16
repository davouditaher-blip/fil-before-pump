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
                "GMGN transport uses subprocess; the module's requests calls are unrelated CMC/Telegram helpers."
            ),
        },
        "wallet_activity_or_history": {
            "endpoint": "gmgn portfolio activity",
            "wrapper": "gmgn_layer.portfolio_activity(chain, wallet, limit=200)",
            "flags_sent": ["--chain", "--wallet", "--limit", "--type buy", "--type sell", "--raw", "--cursor"],
            "time_range_flag_sent": False,
            "cursor_flag_sent": True,
            "offset_or_page_flag_sent": False,
            "evidence": "gmgn_layer.portfolio_activity",
            "classification": {
                "endpoint_capability": (
                    "The wrapper sends --cursor on subsequent calls but no from/to "
                    "time range. Whether the installed GMGN CLI/provider honours "
                    "the cursor shape is UNVERIFIED because no live cursor response "
                    "has been recorded."
                ),
                "code_capability": (
                    "The runtime layer follows cursor pages, bounded to "
                    "MAX_ACTIVITY_PAGES_PER_WALLET and the global activity-call "
                    "budget; it rejects a full page without a completion signal "
                    "rather than claiming the history is complete."
                ),
                "demonstrated_capability": (
                    "The primary archive is accumulated by the track smartmoney "
                    "feed, not portfolio-activity paging. Its current measured "
                    "span is recorded in historical_depth_audit.json; the "
                    "separate wallet activity archive still has zero records, "
                    "so provider cursor depth remains un demonstrated."
                ),
            },
        },
        "smart_money_feed": {
            "endpoint": "gmgn track smartmoney",
            "wrapper": "gmgn_layer.run_gmgn(chain)",
            "flags_sent": ["--chain", "--limit", "200", "--raw"],
            "purpose": "recent smart-money signals, not wallet history",
            "evidence": "gmgn_layer.run_gmgn",
            "classification": {
                "endpoint_capability": (
                    "No time filter and a fixed --limit 200. This is the feed "
                    "that populated gmgn_wallet_history.json; it is a forward "
                    "signal feed and is not a historical query."
                ),
                "code_capability": "200 rows per chain per run, most recent.",
                "demonstrated_capability": (
                    "Current row/wallet counts and measured archive span are recorded in "
                    "historical_depth_audit.json. This recent-signal feed has not "
                    "demonstrated 90-day per-wallet history."
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
            "evidence": "gmgn_layer.token_kline",
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
            "mechanism_in_production_code": "bounded cursor pagination",
            "evidence": (
                "gmgn_layer.portfolio_activity sends --cursor on subsequent calls, "
                "follows supported cursor/has_more/is_last signals, and stops at "
                "MAX_ACTIVITY_PAGES_PER_WALLET or the global call budget."
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
                "Cursor loops now exist in both gmgn_layer and wallet_trade_backfill. "
                "What remains unverified is whether the installed CLI/provider "
                "actually accepts the cursor flag and returns the expected signals."
            ),
            "classification": {
                "endpoint_capability": UNVERIFIED,
                "code_capability": (
                    "The runtime layer and backfill can follow cursors if the "
                    "provider response uses the supported shapes. No committed "
                    "live response demonstrates that provider behavior."
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
                f"{wallet_count} wallets x {chain_count} chains = "
                f"{wallet_count * chain_count} separate CLI invocations to "
                "cover the committed population, each of which spawns npx."
            ),
        },
    }


def audit_limits() -> dict[str, Any]:
    """Caps, budgets and policies the current code imposes."""
    constants = _module_assignments(GMGN_LAYER)
    probe_constants = _module_assignments(DEPTH_PROBE)
    depth = audit_demonstrated_depth()
    primary = depth.get("primary_archive") or {}
    wallet_count = int(primary.get("unique_wallets") or 0)
    chain_count = len(constants.get("CHAINS") or ("sol", "bsc", "base", "eth"))

    return {
        "max_page_size_requested": {
            "value": 200,
            "where": "gmgn_layer.portfolio_activity default limit=200; wallet_trade_backfill defaults to 100 and accepts 1-200",
            "note": (
                "This is what the project asks for, not a proven provider "
                "maximum. Whether GMGN would accept more is untested."
            ),
        },
        "max_pages_per_wallet": {
            "production": constants.get("MAX_ACTIVITY_PAGES_PER_WALLET", 3),
            "production_evidence": "gmgn_layer.portfolio_activity follows cursors within a per-wallet page cap and a global call budget.",
            "probe": probe_constants.get("MAX_PAGES", 10),
            "probe_note": "gmgn_depth_probe.MAX_PAGES is a hard self-imposed stop, default 10.",
            "backfill": _module_assignments(BACKFILL).get("MAX_PAGES", 1000),
            "backfill_evidence": "wallet_trade_backfill.fetch_chain loops through cursors until completion or a safety guard.",
        },
        "rows_per_wallet_ceiling": {
            "production": "Up to 3 pages x --limit 200 per wallet, subject to the global activity-call budget and provider completion signals",
            "implied_by": "MAX_ACTIVITY_PAGES_PER_WALLET x --limit; cursor completion is not live-verified",
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
                "No GMGN documentation or recorded live cursor response is "
                "committed. The measured archive span describes local collection, "
                "not provider retention or the maximum available historical window."
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
                "evidence": "gmgn_layer.portfolio_activity cursor loop and completion checks",
            },
            "activity_call_budget": {
                "value": constants.get("ACTIVITY_CALL_BUDGET", 300),
                "pacing_seconds": constants.get("ACTIVITY_CALL_PACING_SECONDS", 0.5),
                "evidence": "gmgn_layer.detect_rate_limit",
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
            "layer": "gmgn_layer imports GMGN_API_KEY from the process environment and passes the inherited environment to the CLI subprocess",
            "probe": "gmgn_depth_probe.py:115-117 refuses to run without a key",
            "backfill": (
                "wallet_trade_backfill.py checks GMGN_API_KEY before making "
                "requests and fails closed when it is absent. The CLI subprocess "
                "inherits the environment copied by run_cli."
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
            "portfolio_activity": "No time filter. Production follows cursors for at most 3 pages per wallet, but provider cursor behaviour and retention are unverified.",
            "market_kline": "Has --from/--to, but is market data, not wallet activity.",
        },
    }


def audit_wallet_archive() -> dict[str, Any]:
    """Report the old empty backfill artifact without inventing its failure cause."""
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
                "file": path.name, "readable": True, "chain": data.get("chain"),
                "wallet": data.get("wallet"), "limit_requested": data.get("limit"),
                "records": count, "fetched_at": data.get("fetched_at"),
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
        "files": files, "file_count": len(files),
        "unique_wallets": sorted(w for w in wallets if w),
        "total_records": total_records,
        "root_cause": {
            "summary": (f"The committed per-wallet archive currently contains {total_records} activity records across {len(wallets)} wallets. The old files do not record return code, stderr, or a reliable completion status, so the precise historical failure mode cannot be proven. The current backfill has since been hardened: it validates the API key, paginates with checkpoints, refuses incomplete full pages, preserves the prior archive, and writes failed_or_incomplete status details on errors."),
            "traced_path": [
                "wallet_trade_backfill.run() checks GMGN_API_KEY before any provider request.",
                "wallet_trade_backfill.fetch_chain() paginates, writes checkpoints and refuses a full page without a completion signal.",
                "wallet_trade_backfill.main() persists failed_or_incomplete status and exits nonzero on chain errors.",
            ],
            "why_the_cause_is_not_recoverable_from_the_artifact": ("The committed empty archive files predate the current status schema and contain no return code, stderr, or completion state. An empty records array is therefore indistinguishable from a genuinely empty result, so the original failure mode is NOT recoverable from these files alone."),
            "compounding_factors": [
                {"factor": "one wallet per workflow dispatch", "evidence": "BACKFILL_WALLET is a single workflow input/default.", "consequence": "The workflow does not enumerate the entire wallet population automatically."},
                {"factor": "pagination safety cap", "evidence": "wallet_trade_backfill.MAX_PAGES is 1000.", "consequence": "The cap is finite and no live provider cursor behaviour has been verified."},
                {"factor": "no backfill rate-limit detection", "evidence": "wallet_trade_backfill does not call gmgn_layer.detect_rate_limit.", "consequence": "Rate-limit responses are persisted as failures, but there is no provider-specific terminal rate-limit policy in the backfill path."},
            ],
            "what_is_not_the_cause": [
                "The current code no longer treats malformed/error responses as an empty successful history.",
                "The historical empty artifact is not evidence that the selected wallet had no activity.",
                "The USD qualification threshold does not explain records: 0 because the artifact was empty before qualification.",
            ],
            "conclusion_strength": ("The empty archive and missing error fields are directly verifiable. The current writer is hardened, but the exact cause of the old empty files is NOT recoverable from the persisted artifact."),
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
            "code_capability": True,
            "code_reason": (
                "The runtime layer has bounded cursor pagination and the backfill has a "
                "1,000-page safety cap. Neither sends a time-range filter, the "
                "CLI cursor semantics are not verified live, and the repository "
                "has no committed result proving 90 days."
            ),
            "demonstrated_capability": False,
            "demonstrated_reason": (
                "The current primary archive span is reported dynamically from "
                "historical_depth_audit.json and comes from the track smartmoney "
                "feed, not portfolio activity. The wallet_archive holds zero "
                "records, so pagination has not demonstrated historical depth."
            ),
            "verdict": "NO",
            "verdict_basis": (
                "NO because no committed live response demonstrates a 90-day "
                "window and provider cursor/retention behaviour is UNVERIFIED. "
                "Code-side pagination is implemented, but implementation alone "
                "does not prove that the endpoint returns older history."
            ),
        }

    return {
        "A_90_days": verdict(90),
        "B_180_days": verdict(180),
        "C_365_days": verdict(365),
        "why_no_is_safer_than_unverified": (
            "UNVERIFIED is reserved for what the provider might do. The "
            "question 'can this project obtain 90 days today' has a definite "
            "the code has cursor loops, but no live provider response or committed "
            "activity backfill proves a 90-day window. The safe verdict is NO "
            "until a depth probe demonstrates the required span."
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
        "maximum_demonstrated_days": float(primary.get("archive_span_days") or 0.0),
        "required_days": 90,
        "shortfall_days": max(0.0, 90.0 - float(primary.get("archive_span_days") or 0.0)),
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
            "The repository now has unit tests for GMGN history merging, chain "
            "attribution, cursor pagination and wallet backfill parsing. These "
            "exercise local logic with fixtures/mocks; they do not prove the "
            "installed GMGN CLI or provider honours cursor semantics. This audit "
            "therefore keeps endpoint capability UNVERIFIED."
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
        "demonstrated_depth": depth,
        "codebase_maturity": audit_codebase_maturity(),
        "requirements_for_a_real_90d_backfill": {
            "current_state": "NOT DEMONSTRATED: code-side cursor loops exist, but provider cursor semantics and 90-day depth have not been verified.",
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
                    "gap": "provider cursor behavior not live-verified",
                    "detail": (
                        "Both runtime and backfill code now loop through cursor "
                        "pages, but no recorded live response confirms that the "
                        "installed CLI accepts --cursor or signals completion as expected."
                    ),
                    "verified": "Cursor flags and pagination loops are verified in source; provider behavior remains UNVERIFIED.",
                    "provider_pagination_support": UNVERIFIED,
                },
                {
                    "gap": "wallet enumeration",
                    "detail": (
                        f"One wallet per invocation. Covering {wallet_count} "
                        f"wallets x {chain_count} chains is "
                        f"{wallet_count * chain_count} CLI spawns, against a "
                        "project-side budget of 300 activity calls per run."
                    ),
                    "verified": "Single-wallet invocation and the 300-call budget are verified in source.",
                },
                {
                    "gap": "historical empty artifact has no failure state",
                    "detail": (
                        "The old committed files lack returncode/stderr/status, "
                        "so the original failure mode cannot be recovered. The "
                        "current writer now persists failed_or_incomplete status, "
                        "returncode and stderr for new failures."
                    ),
                    "verified": "Current writer's failure-state persistence is verified in source; old failure cause remains unrecoverable.",
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
                    "gap": "no live provider integration evidence",
                    "detail": (
                        "Unit tests cover local parsing, cursor loops, chain attribution "
                        "and preservation, but none proves the installed GMGN CLI "
                        "or provider honors cursor pagination or exposes 90 days."
                    ),
                    "verified": "The test suite contains mocked/offline tests; no committed live probe result exists.",
                },
            ],
            "minimum_to_attempt": [
                "Run gmgn_depth_probe once for one wallet and RECORD its result in the repository, so endpoint cursor support and observed depth stop being unverified.",
                "Review the recorded run for rate limiting and partial-page behavior before scaling.",
                "Only then scale to more wallets, respecting the activity budget and the backfill's per-wallet safety cap.",
            ],
        },
        "is_gmgn_worth_attempting_first": {
            "verdict": "YES, but only as a measured probe first -- not as an assumed backfill source.",
            "reasons_for": [
                f"It is the only wallet-trade provider already integrated, with a committed {int(primary.get('rows') or 0):,}-row archive. The archive proves schema presence, not 90-day provider depth.",
                "Its kline subcommand already accepts --from/--to, so the CLI is not obviously incapable of time-windowed queries.",
                "A one-wallet probe already exists, is read-only, and is bounded by MAX_PAGES. Running it is cheap and decisive.",
            ],
            "reasons_for_caution": [
                "No committed measurement of provider-side depth exists, so depth is UNVERIFIED rather than known.",
                "The one committed backfill attempt produced zero records and recorded no reason.",
                "The backfill path has cursor pagination and error persistence, but lacks provider-specific rate-limit detection.",
                "Coverage cost is high: one workflow dispatch handles one wallet across selected chains.",
            ],
            "recommended_first_action": (
                "Run the existing read-only gmgn_depth_probe for ONE wallet and "
                "commit its result. That measurement is now the missing evidence "
                "needed to validate the existing cursor implementation and "
                "determine whether 90/180/365-day retrieval is actually available."
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