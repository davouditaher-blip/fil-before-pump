"""GMGN historical trade/outcome analytics.

Outcome fields are used only after the historical entry timestamp when scoring
past performance. They must never be fed into the entry-time feature vector.
"""
from collections import defaultdict
from typing import Any, Dict, Iterable, List

def _num(x):
    try:
        return float(x) if x is not None and str(x)!="" else None
    except (TypeError,ValueError):
        return None

def qualified_buys(history: Dict[str,list], min_usd: float=5000.0) -> List[dict]:
    rows=[]
    for wallet,items in history.items():
        if not isinstance(items,list): continue
        for r in items:
            if str(r.get("side") or "").lower()!="buy": continue
            amount=_num(r.get("amount_usd"))
            ts=int(_num(r.get("trade_timestamp") or r.get("timestamp")) or 0)
            if amount is None or amount<min_usd or not ts: continue
            row=dict(r); row["wallet"]=wallet; row["timestamp"]=ts; row["amount_usd"]=amount
            rows.append(row)
    return sorted(rows,key=lambda x:(x["timestamp"],x["wallet"]))

def outcome(row: dict) -> dict:
    peak=_num(row.get("peak_multiple"))
    if peak is None or peak<=0:
        peak=_num(row.get("price_change"))
    return {
        "known": peak is not None and peak>0,
        "peak_multiple": peak,
        "hit_1_2x": bool(peak is not None and peak>=1.2),
        "hit_1_5x": bool(peak is not None and peak>=1.5),
        "hit_2x": bool(peak is not None and peak>=2.0),
        "first_2x_timestamp": int(_num(row.get("first_2x_timestamp")) or 0),
    }

def wallet_stats(history: Dict[str,list], min_usd: float=5000.0) -> Dict[str,dict]:
    rows=qualified_buys(history,min_usd)
    by=defaultdict(list)
    for r in rows: by[r["wallet"]].append(r)
    out={}
    for wallet,items in by.items():
        outs=[outcome(r) for r in items]
        known=[x for x in outs if x["known"]]
        out[wallet]={
            "qualified_buys":len(items),
            "known_outcomes":len(known),
            "hit_1_2x":sum(x["hit_1_2x"] for x in known),
            "hit_1_5x":sum(x["hit_1_5x"] for x in known),
            "hit_2x":sum(x["hit_2x"] for x in known),
            "win_rate_2x":round(sum(x["hit_2x"] for x in known)/len(known)*100,2) if known else None,
            "symbols":sorted({str(r.get("symbol") or "").upper() for r in items if r.get("symbol")}),
        }
    return out
