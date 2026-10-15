"""Read-only inventory of the currently committed GMGN archive vs its historical audit."""
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "gmgn_wallet_history.json"
COMPRESSED = HERE / "gmgn_wallet_history.json.gz"
DEPTH_AUDIT = HERE / "historical_depth_audit.json"


def as_ts(value):
    try:
        value = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return value // 1000 if value > 10**12 else value


def main():
    if RAW.exists():
        raw = RAW.read_bytes()
        source_path = RAW
    elif COMPRESSED.exists():
        raw = gzip.decompress(COMPRESSED.read_bytes())
        source_path = COMPRESSED
    else:
        raise SystemExit("No raw or compressed GMGN archive exists")

    history = json.loads(raw)
    if not isinstance(history, dict):
        raise SystemExit("GMGN archive root is not a wallet mapping")

    rows = []
    assets = set()
    chains = {}
    asset_times = defaultdict(list)
    chain_field_presence = Counter()
    chain_field_values = {name: Counter() for name in ("chain", "network", "chain_name", "chain_id", "chainId", "blockchain")}
    missing_chain_keys = Counter()
    rows_missing_chain = 0
    rows_missing_contract = 0
    trade_timestamps = 0
    priced_rows = 0
    for wallet, records in history.items():
        if not isinstance(records, list):
            continue
        for row in records:
            if not isinstance(row, dict):
                continue
            rows.append(row)
            chain = str(row.get("chain") or "").strip().lower()
            address = str(row.get("address") or row.get("base_address") or "").strip()
            if address:
                assets.add((chain, address))
            else:
                rows_missing_contract += 1
            chains[chain or "(none)"] = chains.get(chain or "(none)", 0) + 1
            if not chain:
                rows_missing_chain += 1
                missing_chain_keys.update(row.keys())
            for field in chain_field_values:
                value = row.get(field)
                if value not in (None, "", 0, "0"):
                    chain_field_presence[field] += 1
                    chain_field_values[field][str(value)] += 1
            ts_for_asset = as_ts(row.get("trade_timestamp") or row.get("timestamp"))
            if address and ts_for_asset > 0:
                asset_times[(chain, address)].append(ts_for_asset)
            if row.get("trade_timestamp") not in (None, "", 0, "0"):
                trade_timestamps += 1
            if row.get("price_usd") not in (None, "", 0, "0") or row.get("price") not in (None, "", 0, "0"):
                priced_rows += 1

    times = [as_ts(row.get("trade_timestamp") or row.get("timestamp")) for row in rows]
    times = sorted(ts for ts in times if ts > 0)
    md5 = hashlib.md5(raw).hexdigest()
    sha256 = hashlib.sha256(raw).hexdigest()
    expected = {}
    if DEPTH_AUDIT.exists():
        audit = json.loads(DEPTH_AUDIT.read_text(encoding="utf-8"))
        expected = audit.get("provenance") or {}
    expected_md5 = expected.get("primary_archive_md5")
    expected_bytes = expected.get("primary_archive_bytes")
    span_days = (times[-1] - times[0]) / 86400 if len(times) > 1 else 0
    asset_spans = [
        (max(values) - min(values)) / 86400
        for values in asset_times.values() if len(values) >= 2
    ]
    asset_spans.sort()
    result = {
        "mode": "READ_ONLY_CURRENT_GMGN_ARCHIVE_AUDIT",
        "archive_path": source_path.name,
        "compressed_file_bytes": COMPRESSED.stat().st_size if COMPRESSED.exists() else None,
        "decompressed_bytes": len(raw),
        "decompressed_md5": md5,
        "decompressed_sha256": sha256,
        "wallet_count": len(history),
        "row_count": len(rows),
        "unique_chain_contract_assets": len(assets),
        "rows_with_trade_timestamp": trade_timestamps,
        "rows_with_price": priced_rows,
        "earliest_event_utc": datetime.fromtimestamp(times[0], timezone.utc).isoformat() if times else None,
        "latest_event_utc": datetime.fromtimestamp(times[-1], timezone.utc).isoformat() if times else None,
        "archive_span_days": round(span_days, 6),
        "rows_by_chain": dict(sorted(chains.items())),
        "rows_missing_chain": rows_missing_chain,
        "rows_missing_contract_address": rows_missing_contract,
        "candidate_chain_field_presence": dict(chain_field_presence),
        "candidate_chain_field_top_values": {
            field: dict(counter.most_common(10)) for field, counter in chain_field_values.items()
        },
        "common_keys_on_rows_missing_chain": dict(missing_chain_keys.most_common(30)),
        "assets_with_at_least_two_timestamps": len(asset_spans),
        "max_single_asset_history_days": round(asset_spans[-1], 6) if asset_spans else 0,
        "assets_with_at_least_90_days": sum(span >= 90 for span in asset_spans),
        "assets_with_at_least_180_days": sum(span >= 180 for span in asset_spans),
        "assets_with_at_least_365_days": sum(span >= 365 for span in asset_spans),
        "historical_depth_audit_primary_archive_md5": expected_md5,
        "historical_depth_audit_primary_archive_bytes": expected_bytes,
        "historical_depth_audit_matches_current_archive": (
            md5 == expected_md5 and len(raw) == expected_bytes
            if expected_md5 is not None and expected_bytes is not None else None
        ),
        "orders_enabled": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
