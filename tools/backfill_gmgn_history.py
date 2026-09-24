"""Backfill historical GMGN activity for a bounded set of qualified wallets.

This is research-only. It stores provider-labelled buys/sells and preserves
provider outcome fields when present. It never executes trades.
"""
import json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

INPUT=Path("gmgn_wallet_history.json")
OUT=Path(os.environ.get("GMGN_BACKFILL_OUT","gmgn_backfill_history.json"))
TOP_WALLETS=int(os.environ.get("GMGN_BACKFILL_WALLETS","25"))
LIMIT=int(os.environ.get("GMGN_BACKFILL_LIMIT","1000"))
WORKERS=int(os.environ.get("GMGN_BACKFILL_WORKERS","4"))
CHAINS=("sol","bsc","base","eth")

def load_wallets():
    obj=json.loads(INPUT.read_text())
    scores={}
    for wallet,rows in obj.items():
        if not isinstance(rows,list): continue
        total=sum(float(r.get("amount_usd") or 0) for r in rows
                  if str(r.get("side") or "").lower()=="buy")
        qualified=sum(1 for r in rows if str(r.get("side") or "").lower()=="buy"
                     and float(r.get("amount_usd") or 0)>=5000)
        if qualified:
            scores[wallet]={"wallet":wallet,"buy_usd":total,
                            "qualified_buys":qualified}
    return sorted(scores.values(),key=lambda x:(x["qualified_buys"],x["buy_usd"]),reverse=True)[:TOP_WALLETS]

def fetch(chain,wallet):
    env=os.environ.copy()
    cmd=["npx","--yes","gmgn-cli","portfolio","activity",
         "--chain",chain,"--wallet",wallet,"--limit",str(LIMIT),
         "--type","buy","--type","sell","--raw"]
    try:
        p=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=120)
        if p.returncode!=0:
            return chain,wallet,[],p.stderr[-500:]
        for line in reversed(p.stdout.strip().splitlines()):
            try:
                obj=json.loads(line)
                if isinstance(obj,dict):
                    rows=obj.get("list") or obj.get("data") or []
                    return chain,wallet,rows if isinstance(rows,list) else [],""
            except json.JSONDecodeError:
                pass
        return chain,wallet,[],"no json response"
    except Exception as e:
        return chain,wallet,[],str(e)

def normalize(chain,wallet,row):
    side=str(row.get("side") or row.get("type") or row.get("event_type") or "").lower()
    if side not in {"buy","sell"}: return None
    ts=int(float(row.get("timestamp") or row.get("trade_timestamp") or row.get("time") or 0))
    address=row.get("base_address") or row.get("token_address") or row.get("address")
    token=row.get("base_token") or {}
    symbol=str(row.get("base_token_symbol") or token.get("symbol") or row.get("symbol") or "?").upper()
    amount=float(row.get("amount_usd") or row.get("usd_value") or row.get("cost_usd") or row.get("amount") or 0)
    if not ts or not address or not symbol: return None
    return {
        "wallet":wallet,"chain":chain,"timestamp":ts,"trade_timestamp":int(float(row.get("trade_timestamp") or ts)),
        "side":side,"address":address,"symbol":symbol,"amount_usd":amount,
        "price_usd":float(row.get("price_usd") or row.get("price") or row.get("execution_price") or 0),
        "transaction_hash":row.get("transaction_hash") or row.get("tx_hash") or row.get("id") or "",
        "is_open_or_close":row.get("is_open_or_close"),
        "price_change":row.get("price_change"),
        "peak_multiple":row.get("peak_multiple") or row.get("max_multiple"),
        "first_2x_timestamp":row.get("first_2x_timestamp") or 0,
        "maker_tags":(row.get("maker_info") or {}).get("tags") or row.get("maker_tags") or [],
    }

def main():
    wallets=load_wallets()
    tasks=[(chain,w["wallet"]) for w in wallets for chain in CHAINS]
    all_rows=[]; errors=[]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures=[ex.submit(fetch,c,w) for c,w in tasks]
        for f in as_completed(futures):
            chain,wallet,rows,error=f.result()
            if error: errors.append({"chain":chain,"wallet":wallet,"error":error})
            for row in rows:
                n=normalize(chain,wallet,row)
                if n: all_rows.append(n)
    dedupe={}
    for row in all_rows:
        key=(row["wallet"],row["chain"],row["transaction_hash"] or
             (row["trade_timestamp"],row["address"],row["side"],round(row["amount_usd"],8)))
        dedupe[key]=row
    rows=sorted(dedupe.values(),key=lambda x:(x["trade_timestamp"],x["wallet"]))
    qualified=[r for r in rows if r["side"]=="buy" and r["amount_usd"]>=5000]
    ts=[r["trade_timestamp"] for r in rows if r["trade_timestamp"]]
    report={"schema_version":"2.2","wallets_requested":len(wallets),
            "wallets":[w["wallet"] for w in wallets],
            "records":len(rows),"qualified_buys":len(qualified),
            "first_timestamp":min(ts) if ts else None,"last_timestamp":max(ts) if ts else None,
            "history_days":(max(ts)-min(ts))/86400 if len(ts)>1 else 0,
            "errors":errors[:50],"records_data":rows}
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in {"records_data","errors"}},ensure_ascii=False,indent=2))
    print("errors=",len(errors))

if __name__=="__main__":
    main()
