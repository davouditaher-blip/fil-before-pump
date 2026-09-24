"""CLI adapter for the V2 engine. It is deliberately read-only."""
import json, os, time
from pathlib import Path
from fil_engine.models import Candidate, WalletEvent
from fil_engine.wallet import classify_events, convergence, wallet_quality
from fil_engine.scoring import score_candidate


def load_events(path="wallet_events.json"):
    p=Path(path)
    if not p.exists(): return []
    obj=json.loads(p.read_text())
    rows=obj if isinstance(obj,list) else obj.get("events",obj.get("records",[]))
    out=[]
    for r in rows:
        try:
            out.append(WalletEvent(str(r.get("wallet") or r.get("address") or ""),str(r.get("symbol") or r.get("token") or ""),int(r.get("timestamp") or r.get("time") or 0),str(r.get("side") or r.get("type") or ""),float(r.get("amount_usd") or r.get("usd_value") or 0),str(r.get("tx_hash") or ""),str(r.get("token_address") or ""),str(r.get("source") or ""),str(r.get("chain") or "")))
        except (TypeError,ValueError): continue
    return [e for e in out if e.wallet and e.token and e.timestamp]

def main():
    events=load_events(os.environ.get("FIL_WALLET_EVENTS","wallet_events.json"))
    stats=wallet_quality(events)
    conv=convergence(events)
    # Discovery report: no future PnL is inferred here.
    ranked=[]
    for wallet,tokens in conv.items():
        st=stats.get(wallet)
        ws=min(100.0,20+len(tokens)*10+(st.wins*5 if st else 0))
        for token in tokens:
            ranked.append(score_candidate(Candidate(token,int(time.time()),wallet_score=ws,evidence=[f"shared qualified wallet: {wallet[:8]}…",f"wallet assets: {len(tokens)}"])))
    ranked.sort(key=lambda x:x["score"],reverse=True)
    print(json.dumps({"schema_version":"2.0","generated_at":int(time.time()),"events":len(events),"convergence_wallets":len(conv),"candidates":ranked[:100]},ensure_ascii=False,indent=2))
if __name__=="__main__": main()
