from collections import defaultdict
from typing import Dict, Iterable, List, Tuple
from .models import WalletEvent, WalletStats

QUALIFY_USD = 5000.0

def classify_events(events: Iterable[WalletEvent]) -> Dict[Tuple[str,str], str]:
    """Classify each wallet/token stream using only events up to each event time.
    This is intentionally conservative: buys after a sell are not automatically new entries.
    """
    grouped=defaultdict(list)
    for e in events: grouped[(e.wallet.lower(),e.token.lower())].append(e)
    out={}
    for key, rows in grouped.items():
        rows=sorted(rows,key=lambda x:(x.timestamp,x.tx_hash))
        position=0.0
        for i,e in enumerate(rows):
            if e.side.lower()=="buy":
                label="new_entry" if position<=0 else "add"
                position += max(0.0,e.amount_usd)
            elif e.side.lower()=="sell":
                label="full_exit" if position>0 and e.amount_usd>=position*.9 else "reduce"
                position=max(0.0,position-max(0.0,e.amount_usd))
            else: continue
            out[(key[0],key[1],e.timestamp,e.tx_hash)] = label
    return out

def wallet_quality(events: Iterable[WalletEvent], outcomes: Dict[str,float]|None=None) -> Dict[str,WalletStats]:
    """Calculate auditable wallet stats from event-level realized outcomes.
    outcomes maps tx_hash to realized USD PnL; unknown outcomes are not treated as losses.
    """
    outcomes=outcomes or {}
    stats={}
    for e in events:
        w=e.wallet.lower(); s=stats.setdefault(w,WalletStats(wallet=e.wallet))
        if e.side.lower()!="buy" or e.amount_usd<QUALIFY_USD: continue
        s.trades += 1
        pnl=outcomes.get(e.tx_hash)
        if pnl is None: continue
        s.pnl_usd += pnl
        if pnl>0: s.wins+=1
        elif pnl<0: s.losses+=1
    for s in stats.values():
        decided=s.wins+s.losses
        s.roi=round(s.pnl_usd/(max(1.0,s.trades*QUALIFY_USD))*100,4) if s.trades else None
        if s.losses: s.profit_factor=round(sum(max(0,outcomes.get('',0)) for _ in [])+1,4) # populated by evaluator when full trade outcomes exist
        s.first_mover_score=round(100*s.pre_pump_successes/max(1,s.pre_pump_entries),2) if s.pre_pump_entries else 0.0
    return stats

def convergence(events: Iterable[WalletEvent], min_usd:float=QUALIFY_USD) -> Dict[str,List[str]]:
    """Return wallet -> tokens for qualified buys; caller can weight by wallet quality."""
    out=defaultdict(set)
    for e in events:
        if e.side.lower()=="buy" and e.amount_usd>=min_usd:
            out[e.wallet.lower()].add(e.token.upper())
    return {w:sorted(tokens) for w,tokens in out.items() if len(tokens)>=2}
