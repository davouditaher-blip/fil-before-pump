"""Leakage-safe historical proxy backtest.

Uses wallet_history.json only as a holder-delta proxy. It does NOT claim those
balance changes are confirmed buys/sells. The report therefore labels the
result as PROXY and refuses to manufacture 30/90/180-day results when the
dataset does not contain enough history.
"""
import json, math, os
from collections import defaultdict, deque
from pathlib import Path

from fil_engine.historical import load_json_rows, normalize_holder_snapshots

INITIAL=100.0
WINDOWS=(30,90,180)
ENTRY_USD=float(os.environ.get("FIL_ENTRY_MIN_USD","5000"))
TP=float(os.environ.get("FIL_TP","0.20"))
SL=float(os.environ.get("FIL_SL","0.08"))
HORIZON=int(os.environ.get("FIL_HORIZON_HOURS","24"))*3600
FEE=float(os.environ.get("FIL_FEE","0.001"))
SLIPPAGE=float(os.environ.get("FIL_SLIPPAGE","0.0005"))

def pct(a,b):
    return (b/a-1.0)*100.0 if a else None

def max_future(points, i, end_ts):
    best=points[i][1]
    low=points[i][1]
    for ts,p in points[i+1:]:
        if ts>end_ts: break
        best=max(best,p); low=min(low,p)
    return best,low

def simulate(signals, prices, start_ts, end_ts):
    # Build symbol price arrays. Only prices at/before the signal are used for
    # the entry; all later prices are outcomes, never features.
    by_symbol=defaultdict(list)
    for p in prices:
        if start_ts<=p.timestamp<=end_ts:
            by_symbol[p.symbol].append((p.timestamp,p.price_usd))
    for s in by_symbol: by_symbol[s].sort()

    # Deduplicate the same token into one signal per 30 minutes, preserving
    # the number of unique wallets as a confluence feature.
    buckets={}
    for x in signals:
        if x.side!="buy" or x.amount_usd and x.amount_usd<ENTRY_USD:
            continue
        if not (start_ts<=x.timestamp<=end_ts): continue
        key=(x.symbol,x.timestamp//1800)
        row=buckets.setdefault(key,{"ts":x.timestamp,"symbol":x.symbol,"wallets":set(),"price":x.price_usd})
        row["wallets"].add(x.wallet)
        if x.timestamp<row["ts"]:
            row["ts"]=x.timestamp; row["price"]=x.price_usd

    balance=INITIAL; peak=INITIAL; max_dd=0.0
    trades=[]; used_symbols=set()
    for row in sorted(buckets.values(),key=lambda x:x["ts"]):
        symbol=row["symbol"]
        if symbol in used_symbols: continue
        series=by_symbol.get(symbol,[])
        if not series: continue
        future=[(ts,p) for ts,p in series if ts>=row["ts"]]
        if not future: continue
        entry_ts, entry=future[0]
        if entry<=0: continue
        # Require the entry to be observable at signal time.
        if entry_ts>row["ts"]+6*3600: continue
        exit_price=None; exit_ts=None; reason="horizon"
        for ts,p in future[1:]:
            r=p/entry-1.0
            if r>=TP: exit_price=p; exit_ts=ts; reason="tp"; break
            if r<=-SL: exit_price=p; exit_ts=ts; reason="sl"; break
            if ts-entry_ts>=HORIZON:
                exit_price=p; exit_ts=ts; break
        if exit_price is None:
            continue
        net=(exit_price*(1-SLIPPAGE))/(entry*(1+SLIPPAGE))-1
        net-=2*FEE
        pnl=balance*net
        balance+=pnl
        peak=max(peak,balance)
        max_dd=max(max_dd,(peak-balance)/peak*100 if peak else 0)
        trades.append({"symbol":symbol,"signal_ts":row["ts"],"entry_ts":entry_ts,
                       "exit_ts":exit_ts,"entry":entry,"exit":exit_price,
                       "return_pct":net*100,"pnl":pnl,"reason":reason,
                       "wallet_count":len(row["wallets"])})
        used_symbols.add(symbol)
    wins=sum(t["pnl"]>0 for t in trades)
    losses=sum(t["pnl"]<0 for t in trades)
    gp=sum(t["pnl"] for t in trades if t["pnl"]>0)
    gl=-sum(t["pnl"] for t in trades if t["pnl"]<0)
    return {
        "initial_balance":INITIAL,"final_balance":round(balance,6),
        "roi_pct":round((balance/INITIAL-1)*100,4),
        "trades":len(trades),"wins":wins,"losses":losses,
        "win_rate_pct":round(wins/len(trades)*100,4) if trades else 0,
        "gross_profit":round(gp,6),"gross_loss":round(gl,6),
        "profit_factor":round(gp/gl,4) if gl else (math.inf if gp else 0),
        "max_drawdown_pct":round(max_dd,4),
        "parameters":{"entry_min_usd":ENTRY_USD,"tp_pct":TP*100,"sl_pct":SL*100,
                      "horizon_hours":HORIZON/3600,"fee_rate":FEE,"slippage_rate":SLIPPAGE},
        "sample_trades":trades[:100],
    }

def main():
    path=os.environ.get("FIL_WALLET_HISTORY","wallet_history.json")
    rows=load_json_rows(path)
    signals,prices=normalize_holder_snapshots(rows)
    if not signals or not prices:
        raise SystemExit("No usable historical holder/price rows found.")
    timestamps=[p.timestamp for p in prices]
    first,last=min(timestamps),max(timestamps)
    span_days=(last-first)/86400
    report={"schema_version":"2.1","mode":"HOLDER_DELTA_PROXY",
            "warning":"Balance changes are proxies, not confirmed buys/sells.",
            "rows":len(rows),"signals":len(signals),"price_points":len(prices),
            "wallets":len({s.wallet for s in signals}),
            "symbols":len({s.symbol for s in signals}),
            "first_timestamp":first,"last_timestamp":last,"history_days":round(span_days,3),
            "windows":{}}
    for days in WINDOWS:
        if span_days<days:
            report["windows"][f"{days}d"]={"status":"INSUFFICIENT_HISTORY",
                "required_days":days,"available_days":round(span_days,3)}
            continue
        report["windows"][f"{days}d"]={"status":"OK",
            **simulate(signals,prices,last-days*86400,last)}
    out=Path(os.environ.get("FIL_BACKTEST_OUT","fil_v2_backtest_report.json"))
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    print(json.dumps({k:v for k,v in report.items() if k!="windows"},ensure_ascii=False,indent=2))
    print(json.dumps(report["windows"],ensure_ascii=False,indent=2))

if __name__=="__main__": main()
