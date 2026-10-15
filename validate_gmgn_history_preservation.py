"""Fail-closed check that a GMGN history update did not delete prior records."""
import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import wallet_history_validation as whv


def load_history(path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Cannot parse history JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"History root must be a wallet-to-rows object: {path}")
    result = {}
    for wallet, rows in value.items():
        if not isinstance(wallet, str) or not isinstance(rows, list):
            raise ValueError(f"History entry must map a wallet string to a list: {path}")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"History rows must all be objects for wallet {wallet}: {path}")
        result[wallet] = rows
    return result


def row_identity(wallet: str, row: dict[str, Any]) -> tuple:
    return whv.history_event_identity(wallet, row)


_STATIC_FIELDS = (
    "timestamp", "trade_timestamp", "transaction_hash", "tx_hash", "transactionHash", "id",
    "chain", "address", "base_address", "side", "type", "symbol", "amount_usd", "usd",
    "usd_value", "value_usd", "price_usd", "price", "priceUsd", "is_open_or_close",
)


def _nonempty(value):
    return value not in (None, "", [], {})


def _tag_set(value):
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    return set()


def _metadata_losses(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    losses = []
    for key in _STATIC_FIELDS:
        old_value = old.get(key)
        new_value = new.get(key)
        if _nonempty(old_value) and not _nonempty(new_value):
            losses.append(key)
        elif _nonempty(old_value) and _nonempty(new_value):
            old_stable = whv._identity_value(old_value)
            new_stable = whv._identity_value(new_value)
            if key in ("transaction_hash", "tx_hash", "transactionHash"):
                old_stable, new_stable = old_stable.lower(), new_stable.lower()
            if old_stable != new_stable:
                losses.append(key)
    old_peak = whv._num(old.get("peak_multiple"), 0.0)
    new_peak = whv._num(new.get("peak_multiple"), 0.0)
    if old_peak > new_peak:
        losses.append("peak_multiple")
    for key in ("first_1_2x_timestamp", "first_1_5x_timestamp", "first_2x_timestamp"):
        old_ts = whv._timestamp_value(old.get(key))
        new_ts = whv._timestamp_value(new.get(key))
        if old_ts and (not new_ts or new_ts > old_ts):
            losses.append(key)
    old_tags = _tag_set(old.get("maker_tags"))
    new_tags = _tag_set(new.get("maker_tags"))
    if old_tags and not old_tags.issubset(new_tags):
        losses.append("maker_tags")
    old_info = old.get("maker_info")
    new_info = new.get("maker_info")
    if isinstance(old_info, dict) and old_info:
        if not isinstance(new_info, dict):
            losses.append("maker_info")
        else:
            if any(key not in new_info or new_info[key] != value for key, value in old_info.items()):
                losses.append("maker_info")
    return sorted(set(losses))


def _identity_rows(history: dict[str, list[dict[str, Any]]]) -> dict[tuple, dict[str, Any]]:
    merged_history = whv.merge_history_data(history)
    result = {}
    for wallet, rows in merged_history.items():
        for row in rows:
            result[row_identity(wallet, row)] = row
    return result


def compare_histories(baseline: dict[str, list[dict[str, Any]]], current: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    before_rows = _identity_rows(baseline)
    after_rows = _identity_rows(current)
    before = set(before_rows)
    after = set(after_rows)
    missing = before - after
    metadata_losses = Counter()
    examples = []
    for identity in before & after:
        lost = _metadata_losses(before_rows[identity], after_rows[identity])
        for field in lost:
            metadata_losses[field] += 1
        if lost and len(examples) < 20:
            examples.append({"identity": list(identity), "lost_fields": lost})
    metadata_preserved = not metadata_losses
    return {
        "baseline_wallets": len(baseline),
        "current_wallets": len(current),
        "baseline_rows": sum(len(rows) for rows in baseline.values()),
        "current_rows": sum(len(rows) for rows in current.values()),
        "baseline_unique_identities": len(before),
        "current_unique_identities": len(after),
        "missing_identity_kinds": len(missing),
        "missing_row_instances": len(missing),
        "missing_metadata_fields": sum(metadata_losses.values()),
        "metadata_loss_counts_by_field": dict(sorted(metadata_losses.items())),
        "metadata_loss_examples": examples,
        "metadata_preserved": metadata_preserved,
        "preserved": not missing and metadata_preserved,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=Path("gmgn_wallet_history.baseline.json"))
    parser.add_argument("--current", type=Path, default=Path("gmgn_wallet_history.json"))
    args = parser.parse_args(argv)
    baseline = load_history(args.baseline)
    current = load_history(args.current)
    result = compare_histories(baseline, current)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["preserved"]:
        raise SystemExit("GMGN history preservation check FAILED; refusing to compress or commit a shortened or metadata-damaged archive")
    print("GMGN history preservation check PASSED")


if __name__ == "__main__":
    main()
