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

all_rows = []
page = 1
per_page = 100

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
        r.raise_for_status()

    obj = r.json()
    data = obj.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("Nansen response data is not a list")

    all_rows.extend(data)
    pagination = obj.get("pagination") or {}
    print(f"page={page} rows={len(data)} total_rows={len(all_rows)}")

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

path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
print(f"ARCHIVE={path}")
print(f"RECORDS={len(all_rows)}")
