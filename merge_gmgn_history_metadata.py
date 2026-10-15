#!/usr/bin/env python3
"""Merge a pre-run GMGN history snapshot back into the current archive losslessly."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import wallet_history_validation as whv


def load_history(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"History root must be an object: {path}")
    for wallet, rows in data.items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Invalid history rows for wallet {wallet!r} in {path}")
    return data


def identity_count(history: dict[str, Any]) -> int:
    return len({
        whv.history_event_identity(wallet, row)
        for wallet, rows in history.items()
        for row in rows
    })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", default="gmgn_wallet_history.json")
    parser.add_argument("--baseline", action="append", required=True,
                        help="A trusted snapshot to merge; repeat for multiple snapshots.")
    args = parser.parse_args(argv)

    current_path = Path(args.current)
    current = load_history(current_path)
    before_rows = sum(len(rows) for rows in current.values())
    before_identities = identity_count(current)

    # Baselines are applied oldest-first, then the current file. Static historical
    # fields come from the earliest non-empty observation; dynamic fields can update.
    histories = [load_history(Path(path)) for path in args.baseline]
    merged = whv.merge_history_data(*histories, current)
    after_rows = sum(len(rows) for rows in merged.values())
    after_identities = identity_count(merged)
    whv.atomic_write_history(current_path, merged)

    result = {
        "current_file": str(current_path),
        "baseline_files": args.baseline,
        "rows_before": before_rows,
        "rows_after": after_rows,
        "identities_before": before_identities,
        "identities_after": after_identities,
        "restored_identity_count": max(0, after_identities - before_identities),
        "metadata_merge_completed": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
