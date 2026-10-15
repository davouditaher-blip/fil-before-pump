"""Safe paginated GMGN activity backfill; research-only, never executes trades."""
import json, os, subprocess, time
from pathlib import Path
CHAINS=("sol","bsc","base","eth")
WALLET=os.environ.get("BACKFILL_WALLET","0x7bf30399dba8051c2bf191afe2cd07ea4e064625").strip()
try: LIMIT=int(os.environ.get("GMGN_ACTIVITY_LIMIT","100"))
except ValueError: LIMIT=100
if not 1 <= LIMIT <= 200: raise SystemExit("GMGN_ACTIVITY_LIMIT must be between 1 and 200")
MAX_PAGES=100
OUT=Path("wallet_archive/raw/gmgn/activity")

class BackfillError(RuntimeError):
    def __init__(self,message,returncode=None,stderr=""):
        super().__init__(message); self.returncode=returncode; self.stderr=str(stderr or "")[-3000:]

def atomic_json_write(path,payload):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\\n",encoding="utf-8"); os.replace(tmp,path)

def parse_output(stdout):
    for line in reversed(str(stdout or "").splitlines()):
        try: obj=json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(obj,dict): return obj
    raise BackfillError("GMGN CLI returned no parseable JSON object")

def extract_rows_cursor(obj):
    data=None
    for key in ("activities","list","data"):
        if key in obj and obj.get(key) is not None: data=obj.get(key); break
    if isinstance(data,dict):
        nested=data; data=next((nested[k] for k in ("activities","list","data") if isinstance(nested.get(k),list)),None)
    if not isinstance(data,list): raise BackfillError("GMGN response has no activity list; refusing to record empty history")
    pg=obj.get("pagination") if isinstance(obj.get("pagination"),dict) else {}
    cursor=obj.get("next") or obj.get("next_cursor") or obj.get("nextCursor") or obj.get("next_page_token") or pg.get("next") or pg.get("next_cursor") or pg.get("nextCursor") or pg.get("cursor")
    more=obj.get("has_more",pg.get("has_more")); last=obj.get("is_last_page",pg.get("is_last_page"))
    return data,(str(cursor) if cursor else None),more,last

def run_cli(chain,wallet,limit,cursor=None):
    cmd=["npx","--yes","gmgn-cli","portfolio","activity","--chain",chain,"--wallet",wallet,"--limit",str(limit),"--type","buy","--type","sell","--raw"]
    if cursor: cmd.extend(["--cursor",cursor])
    try: p=subprocess.run(cmd,env=os.environ.copy(),capture_output=True,text=True,timeout=120)
    except Exception as exc: raise BackfillError("GMGN CLI invocation failed: "+str(exc)) from exc
    if p.returncode: raise BackfillError("GMGN CLI exited with status "+str(p.returncode),p.returncode,p.stderr)
    return parse_output(p.stdout)

def identity(row):
    tx=row.get("tx_hash") or row.get("transaction_hash") or row.get("transactionHash") or row.get("id")
    if tx: return ("tx",str(tx).lower())
    token=row.get("token") if isinstance(row.get("token"),dict) else {}
    return ("fallback",str(row.get("timestamp") or row.get("trade_timestamp") or ""),str(row.get("side") or row.get("event_type") or row.get("type") or "").lower(),str(row.get("token_address") or row.get("base_address") or row.get("address") or token.get("address") or "").lower(),str(row.get("cost_usd") or row.get("buy_cost_usd") or row.get("amount_usd") or ""))

def merge_rows(*groups):
    merged={}
    for group in groups:
        for row in group or []:
            if not isinstance(row,dict): continue
            key=identity(row)
            if key in merged: merged[key].update(row)
            else: merged[key]=dict(row)
    return sorted(merged.values(),key=lambda r:int(r.get("timestamp") or r.get("trade_timestamp") or 0))

def fetch_chain(chain,wallet,limit,out_dir=OUT,runner=run_cli):
    out_dir=Path(out_dir); out_dir.mkdir(parents=True,exist_ok=True); stem=f"{chain}_{wallet.lower()}"
    archive=out_dir/f"{stem}.json"; checkpoint=out_dir/f"{stem}.partial.json"; status=out_dir/f"{stem}.status.json"
    old_rows=[]
    if archive.exists():
        try:
            old=json.loads(archive.read_text(encoding="utf-8"))
            if old.get("wallet","").lower()==wallet.lower() and old.get("chain")==chain and isinstance(old.get("records"),list): old_rows=old["records"]
        except Exception as exc: raise BackfillError("Existing archive unreadable; refusing overwrite: "+str(exc))
    rows=[]; cursor=None
    if checkpoint.exists():
        try:
            saved=json.loads(checkpoint.read_text(encoding="utf-8"))
            if saved.get("wallet","").lower()==wallet.lower() and saved.get("chain")==chain and isinstance(saved.get("records"),list): rows=saved["records"]; cursor=saved.get("next_cursor")
        except Exception as exc: raise BackfillError("Checkpoint unreadable; refusing discard: "+str(exc))
    seen={cursor} if cursor else set(); pages=0
    while True:
        if pages>=MAX_PAGES: raise BackfillError(f"Pagination safety limit reached ({MAX_PAGES})")
        obj=runner(chain,wallet,limit,cursor); page,next_cursor,more,last=extract_rows_cursor(obj); pages+=1; rows=merge_rows(rows,page)
        atomic_json_write(checkpoint,{"schema_version":2,"source":"gmgn","endpoint":"portfolio/activity","wallet":wallet,"chain":chain,"fetched_at":int(time.time()),"pages_fetched_this_run":pages,"next_cursor":next_cursor,"records":rows})
        print(f"{chain}: page={pages} page_rows={len(page)} accumulated={len(rows)}")
        if next_cursor:
            if next_cursor in seen: raise BackfillError("Repeated GMGN cursor; stopping infinite pagination")
            seen.add(next_cursor); cursor=next_cursor; time.sleep(.25); continue
        if last is True or more is False or len(page)<limit: break
        if len(page)>=limit: raise BackfillError("Full GMGN page has no next cursor; completeness unverified. Checkpoint kept; completed archive unchanged.")
        break
    combined=merge_rows(old_rows,rows)
    atomic_json_write(archive,{"schema_version":2,"source":"gmgn","endpoint":"portfolio/activity","wallet":wallet,"chain":chain,"fetched_at":int(time.time()),"pages_fetched_this_run":pages,"previous_records":len(old_rows),"new_records":len(rows),"total_records":len(combined),"records":combined})
    checkpoint.unlink(missing_ok=True)
    atomic_json_write(status,{"schema_version":1,"source":"gmgn","endpoint":"portfolio/activity","wallet":wallet,"chain":chain,"status":"complete","pages_fetched":pages,"records_fetched":len(rows),"archive_records":len(combined),"fetched_at":int(time.time()),"error":None})
    return {"chain":chain,"pages":pages,"fetched":len(rows),"archive":len(combined)}

def main():
    if not WALLET: raise SystemExit("BACKFILL_WALLET is required")
    OUT.mkdir(parents=True,exist_ok=True); results=[]; errors=[]
    for chain in CHAINS:
        try: results.append(fetch_chain(chain,WALLET,LIMIT))
        except Exception as exc:
            atomic_json_write(OUT/f"{chain}_{WALLET.lower()}.status.json",{"schema_version":1,"source":"gmgn","endpoint":"portfolio/activity","wallet":WALLET,"chain":chain,"status":"failed_or_incomplete","fetched_at":int(time.time()),"error":str(exc),"returncode":getattr(exc,"returncode",None),"stderr":getattr(exc,"stderr","")})
            print(f"ERROR chain={chain}: {exc}")
            if getattr(exc,"stderr",""): print("STDERR: "+exc.stderr)
            errors.append(chain)
    print("=== BACKFILL RESULT ==="); print("WALLET="+WALLET); print("CHAINS_COMPLETE="+str(len(results))); print("CHAINS_FAILED_OR_INCOMPLETE="+(",".join(errors) if errors else "none")); print(json.dumps(results,ensure_ascii=False))
    if errors: raise SystemExit(1)

if __name__=="__main__": main()