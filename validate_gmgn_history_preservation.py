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


def _stable(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value).strip()


def row_identity(wallet: str, row: dict[str, Any]) -> tuple:
    wallet_key = whv.wallet_identity(wallet)
    chain = _stable(row.get("chain")).lower()
    tx = _stable(row.get("transaction_hash") or row.get("tx_hash") or row.get("transactionHash") or row.get("id"))
    if tx:
        return (wallet_key, chain, "tx", tx.lower())
    address = _stable(row.get("address") or row.get("base_address"))
    if chain in ("eth", "ethereum", "bsc", "bnb", "base", "eth-mainnet", "bsc-mainnet", "base-mainnet"):
        address = address.lower()
    timestamp = _stable(row.get("trade_timestamp") or row.get("timestamp"))
    side = _stable(row.get("side") or row.get("type")).lower()
    amount = _stable(row.get("amount_usd") or row.get("usd") or row.get("usd_value") or row.get("value_usd"))
    price = _stable(row.get("price_usd") or row.get("price") or row.get("priceUsd"))
    return (wallet_key, chain, "fallback", address, timestamp, side, amount, price)


def compare_histories(baseline: dict[str, list[dict[str, Any]]], current: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    before = {
        row_identity(wallet, row)
        for wallet, rows in baseline.items()
        for row in rows
    }
    after = {
        row_identity(wallet, row)
        for wallet, rows in current.items()
        for row in rows
    }
    missing = before - after
    return {
        "baseline_wallets": len(baseline),
        "current_wallets": len(current),
        "baseline_rows": sum(len(rows) for rows in baseline.values()),
        "current_rows": sum(len(rows) for rows in current.values()),
        "baseline_unique_identities": len(before),
        "current_unique_identities": len(after),
        "missing_identity_kinds": len(missing),
        "missing_row_instances": len(missing),
        "preserved": not missing,
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
        raise SystemExit("GMGN history preservation check FAILED; refusing to compress or commit a shortened archive")
    print("GMGN history preservation check PASSED")


if __name__ == "__main__":
    main()
