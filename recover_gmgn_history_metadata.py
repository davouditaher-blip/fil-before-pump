#!/usr/bin/env python3
"""One-time recovery of GMGN event metadata from trusted committed archive snapshots."""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import wallet_history_validation as whv

RECOVERY_COMMITS = [
    # Merge commit before the recent scanner runs, followed by progressively newer
    # committed archives. These snapshots let us restore fields lost by old
    # last-row-wins deduplication while retaining every newer event.
    "c7c578a51788203a9f4e700e0f1bb927645eec53",
    "1fdd9792c37b68826781c9eaebf650c27023e00c",
    "29f6b41bdfb873c2332529febdf324e3fc51722b",
    "985757038fa88268c6a300829f3672f8c4eb487c",
]


def _load_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Archive root is not an object: {label}")
    for wallet, rows in data.items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Invalid history rows for wallet {wallet!r}: {label}")
    return data


def _load_current(path: Path) -> dict[str, Any]:
    return _load_json_bytes(path.read_bytes(), str(path))


def _archive_from_commit(commit: str) -> dict[str, Any]:
    if subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode != 0:
        subprocess.run(
            ["git", "fetch", "origin", commit, "--depth=1"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    compressed = subprocess.check_output(
        ["git", "show", f"{commit}:gmgn_wallet_history.json.gz"]
    )
    raw = gzip.decompress(compressed)
    return _load_json_bytes(raw, f"{commit}:gmgn_wallet_history.json.gz")


def _identity_count(history: dict[str, Any]) -> int:
    return len({
        whv.history_event_identity(wallet, row)
        for wallet, rows in history.items()
        for row in rows
    })


def _trade_timestamp_count(history: dict[str, Any]) -> int:
    return sum(
        1 for rows in history.values() for row in rows
        if row.get("trade_timestamp") not in (None, "", 0, "0")
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    current_path = Path("gmgn_wallet_history.json")
    done_path = Path("gmgn_history_recovery_done.json")
    if done_path.exists():
        try:
            done = json.loads(done_path.read_text(encoding="utf-8"))
        except Exception:
            done = {}
        # Version 2 changes event identity to include asset+side alongside the
        # transaction hash. Re-run once against the trusted snapshots so any
        # distinct asset legs collapsed by the old tx-only key can be restored.
        if done.get("status") == "complete" and int(done.get("recovery_version", 0)) >= 2:
            print(json.dumps({"recovery": "already_complete", "recovery_version": 2, "done_file": str(done_path)}))
            return 0

    current = _load_current(current_path)
    rows_before = sum(len(rows) for rows in current.values())
    identities_before = _identity_count(current)
    trade_timestamps_before = _trade_timestamp_count(current)

    # Apply newest historical snapshots first and the current archive last.
    # merge_history_record keeps earliest non-empty static fields, unions tags,
    # preserves peaks/first-hit timestamps, and lets current dynamic fields update.
    for commit in reversed(RECOVERY_COMMITS):
        snapshot = _archive_from_commit(commit)
        current = whv.merge_history_data(snapshot, current)
        del snapshot

    rows_after = sum(len(rows) for rows in current.values())
    identities_after = _identity_count(current)
    trade_timestamps_after = _trade_timestamp_count(current)
    whv.atomic_write_history(current_path, current)

    report = {
        "status": "complete",
        "recovery_version": 2,
        "event_identity_scheme": "wallet+chain+transaction+asset+side (fallback: asset+time+side+amount+price)",
        "recovery_commits": RECOVERY_COMMITS,
        "rows_before": rows_before,
        "rows_after": rows_after,
        "identities_before": identities_before,
        "identities_after": identities_after,
        "trade_timestamp_rows_before": trade_timestamps_before,
        "trade_timestamp_rows_after": trade_timestamps_after,
        "recovered_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "note": "Idempotent one-time metadata recovery; archive snapshots were merged by stable event identity."
    }
    _atomic_json(done_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
