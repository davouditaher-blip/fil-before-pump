#!/usr/bin/env python3
"""Generate the deterministic PROVEN_WALLET_REGISTRY artifact.

Builds ``proven_wallet_registry.json`` from the already-committed
``gmgn_wallet_history.json`` using the existing registry interfaces:

    historical_discovery.normalize_rows -> proven_wallet_registry.build_registry

No network, no provider logic, no new classifier, no new threshold. The
PROVEN criteria are whatever ``wallet_history_validation.reconstruct_wallet``
already applies, and this script never inspects or restates them.

Usage
    python3 build_proven_wallet_registry.py            # write the artifact
    python3 build_proven_wallet_registry.py --verify   # rebuild, compare, do not write

Determinism
    ``generated_at`` is a wall-clock stamp and is the ONLY field that is
    expected to differ between runs. Everything else is a pure function of the
    input archive, so ``payload_sha256`` -- a SHA-256 over the envelope with
    ``generated_at`` and the digest itself removed -- is stable. ``--verify``
    rebuilds from the archive and fails if that digest moves, which is what
    makes the committed artifact auditable rather than merely plausible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import historical_discovery as hd
import proven_wallet_registry as pwr
import wallet_history_validation as whv

HERE = Path(__file__).resolve().parent
SOURCE_ARCHIVE = HERE / "gmgn_wallet_history.json"
ARTIFACT = HERE / "proven_wallet_registry.json"

#: Fields excluded from the determinism digest, with the reason each is
#: excluded. Everything not listed here must be byte-stable across runs.
NON_DETERMINISTIC = ("generated_at",)


def file_md5(path: Path) -> str:
    """MD5 of a file, read in chunks so a 100 MB archive stays off the heap."""
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_digest(registry: dict[str, Any]) -> str:
    """SHA-256 over everything that must not change between runs.

    Two exclusions, and both are required. ``generated_at`` is the wall-clock
    stamp. ``determinism.payload_sha256`` is the digest itself, and it is
    *nested* -- popping only the top level would leave the real digest (or the
    placeholder) inside the hashed bytes, so the stored value could never match
    a recomputation of the envelope it ships in.
    """
    payload = {
        k: v for k, v in registry.items() if k not in NON_DETERMINISTIC
    }
    determinism = dict(payload.get("determinism") or {})
    determinism.pop("payload_sha256", None)
    payload["determinism"] = determinism
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build() -> dict[str, Any]:
    """Build the registry envelope from the committed GMGN archive.

    The archive rows are raw collector output, not the unified observation shape
    the registry documents as its input, so they go through the existing shared
    normalizer first. That is not a reclassification: the resulting PROVEN set is
    identical either way, but the registry's own provenance fields only become
    truthful afterwards (``sources`` would otherwise be empty and
    ``historical_trades`` zero for every wallet).
    """
    before = file_md5(SOURCE_ARCHIVE)
    history = json.loads(SOURCE_ARCHIVE.read_text(encoding="utf-8"))

    unified: dict[str, list[dict[str, Any]]] = {}
    skipped: list[str] = []
    for wallet, rows in history.items():
        key = hd.wallet_key(wallet)
        if not key:
            skipped.append(str(wallet))
            continue
        unified[key] = hd.normalize_rows(rows, hd.SOURCE_GMGN, wallet=key, fetched_at=None)

    # current_wallets=None, not []: no live feed was consulted, so active_now
    # stays None ("not known") for every wallet instead of falsely claiming
    # every wallet has left the feed.
    registry = pwr.build_registry(unified, current_wallets=None)

    registry["source"] = {
        "artifact": SOURCE_ARCHIVE.name,
        "md5": before,
        "bytes": SOURCE_ARCHIVE.stat().st_size,
        "provider": hd.SOURCE_GMGN,
        "normalized_by": "historical_discovery.normalize_rows",
        "wallets_in_source": len(history),
        "rows_in_source": sum(len(v) for v in history.values()),
        "wallets_skipped_unkeyed": len(skipped),
    }
    registry["proven_criteria"] = {
        "source": "wallet_history_validation.reconstruct_wallet",
        "min_observed_entries": whv.MIN_OBSERVED_ENTRIES,
        "min_successful_entries": whv.MIN_SUCCESSFUL_ENTRIES,
        "min_win_rate_pct": whv.MIN_WIN_RATE_PCT,
        "target_multiple": whv.TARGET_MULTIPLE,
        "states": list(whv.CLASSES),
    }
    registry["determinism"] = {
        "non_deterministic_fields": list(NON_DETERMINISTIC),
        "digest": "sha256 over the envelope minus " + ", ".join(NON_DETERMINISTIC),
        "payload_sha256": "",
        "note": (
            "generated_at is the only wall-clock field. payload_sha256 covers "
            "every other field, including the source archive md5 and counts, so "
            "regenerating from an unchanged archive reproduces it exactly."
        ),
    }
    # Computed last, over the finished envelope. The placeholder is popped by
    # payload_digest, so the digest covers this whole block including the note
    # and the source md5, but never covers itself. Computing it any earlier
    # would hash an envelope that does not yet contain the digest block, and
    # the stored value would then fail its own verification on reload.
    registry["determinism"]["payload_sha256"] = payload_digest(registry)

    after = file_md5(SOURCE_ARCHIVE)
    if after != before:
        raise SystemExit(
            "source archive changed during the build: "
            f"{before} -> {after}; refusing to write a registry derived from "
            "a moving input"
        )
    return registry


def report(registry: dict[str, Any]) -> None:
    """Print the counts a reviewer needs to check the artifact by hand."""
    from collections import Counter

    counts = Counter(e["proven_status"] for e in registry["wallets"].values())
    print(f"wallets considered : {registry['wallet_count']}")
    print(f"proven_count       : {registry['proven_count']}")
    print(f"orders_enabled     : {registry['orders_enabled']}")
    print(f"current_feed_observed: {registry['current_feed_observed']}")
    for state, count in counts.most_common():
        print(f"  {state:<34} {count:>6}")
    problems = pwr.validate_registry(registry)
    print(f"validate_registry  : {'clean' if not problems else problems[:5]}")
    print(f"payload_sha256     : {registry['determinism']['payload_sha256']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="rebuild and compare against the committed artifact without writing",
    )
    args = parser.parse_args(argv)

    registry = build()
    report(registry)

    if args.verify:
        committed = pwr.load_registry(ARTIFACT)
        if not committed:
            print(f"FAIL: no committed artifact at {ARTIFACT.name}")
            return 1
        new_digest = registry["determinism"]["payload_sha256"]
        old_digest = (committed.get("determinism") or {}).get("payload_sha256")
        if new_digest != old_digest:
            print(f"FAIL: payload digest moved {old_digest} -> {new_digest}")
            return 1
        if committed.get("proven_count") != registry["proven_count"]:
            print(f"FAIL: proven_count {committed['proven_count']} -> {registry['proven_count']}")
            return 1
        if committed.get("wallet_count") != registry["wallet_count"]:
            print(f"FAIL: wallet_count {committed['wallet_count']} -> {registry['wallet_count']}")
            return 1
        if (committed.get("source") or {}).get("md5") != registry["source"]["md5"]:
            print("FAIL: source archive md5 changed")
            return 1
        print(f"OK: deterministic reproduction confirmed ({new_digest[:16]}...)")
        return 0

    pwr.save_registry(registry, ARTIFACT)
    print(f"wrote {ARTIFACT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
