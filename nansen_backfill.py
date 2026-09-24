import json
import os
import time
from pathlib import Path

import requests

API = "https://api.nansen.ai/api/v1/profiler/address/historical-balances"

key = os.environ.get("NANSEN_API_KEY", "").strip()
wallet = os.environ.get("NANSEN_WALLET", "").strip()
chain = os.environ.get("NANSEN_CHAIN", "ethereum").strip()
date_from = os.environ.get("NANSEN_FROM", "").strip()
date_to = os.environ.get("NANSEN_TO", "").strip()

if not key:
    raise SystemExit("NANSEN_API_KEY is missing")
if not wallet or not date_from or not date_to:
    raise SystemExit("NANSEN backfill parameters are missing")

out = Path("wallet_archive/raw/nansen/historical_balances") / chain
out.mkdir(parents=True, exist_ok=True)
path = out / f"{wallet.lower()}.json"

checkpoint_path = out / f"{wallet.lower()}.partial.json"

all_rows = []
page = 1
per_page = 100

# Resume from a previous interrupted run without losing already-fetched pages.
if checkpoint_path.exists():
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
        if (
            checkpoint.get("wallet", "").lower() == wallet.lower()
            and checkpoint.get("chain") == chain
            and (checkpoint.get("coverage") or {}).get("from") == date_from
            and (checkpoint.get("coverage") or {}).get("to") == date_to
        ):
            saved_rows = checkpoint.get("records", [])
            if isinstance(saved_rows, list):
                all_rows = saved_rows
                page = int(checkpoint.get("next_page", 1))
                print(
                    f"RESUME page={page} saved_rows={len(all_rows)} "
                    f"checkpoint={checkpoint_path}"
                )
    except Exception as exc:
        print(f"CHECKPOINT_READ_WARNING={exc}")

while True:
    body = {
        "address": wallet,
        "chain": chain,
        "date": {"from": date_from, "to": date_to},
        "pagination": {"page": page, "per_page": per_page},
    }
    r = requests.post(
        API,
        headers={
            "apiKey": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=60,
    )
    print(f"page={page} http={r.status_code}")

    if r.status_code < 200 or r.status_code >= 300:
        print(r.text[:2000])
        print(
            f"PARTIAL_ARCHIVE={checkpoint_path} "
            f"RECORDS_SAVED={len(all_rows)} NEXT_PAGE={page}"
        )
        r.raise_for_status()

    obj = r.json()
    data = obj.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("Nansen response data is not a list")

    all_rows.extend(data)
    pagination = obj.get("pagination") or {}
    print(f"page={page} rows={len(data)} total_rows={len(all_rows)}")

    # Checkpoint immediately after every successful page.
    checkpoint_payload = {
        "schema_version": 1,
        "source": "nansen",
        "endpoint": "profiler/address/historical-balances",
        "wallet": wallet,
        "chain": chain,
        "coverage": {"from": date_from, "to": date_to},
        "fetched_at": int(time.time()),
        "next_page": page + 1,
        "records": all_rows,
    }
    checkpoint_path.write_text(
        json.dumps(checkpoint_payload, ensure_ascii=False, indent=2) + "\\n"
    )

    if pagination.get("is_last_page") is True or not data:
        break

    page += 1
    if page > 100:
        raise RuntimeError("Pagination safety limit reached")
    time.sleep(0.25)

payload = {
    "schema_version": 1,
    "source": "nansen",
    "endpoint": "profiler/address/historical-balances",
    "wallet": wallet,
    "chain": chain,
    "coverage": {"from": date_from, "to": date_to},
    "fetched_at": int(time.time()),
    "records": all_rows,
}

path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n")
checkpoint_path.unlink(missing_ok=True)

print(f"ARCHIVE={path}")
print(f"RECORDS={len(all_rows)}")
