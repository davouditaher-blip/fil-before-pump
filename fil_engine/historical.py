"""Historical wallet/holder adapters and leakage-safe signal reconstruction.

Holder deltas are marked as proxies unless the source explicitly labels buy/sell.
"""
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from .models import WalletEvent

@dataclass(frozen=True)
class PricePoint:
    timestamp: int
    price_usd: float
    symbol: str
    source: str = ""

@dataclass(frozen=True)
class HistoricalSignal:
    timestamp: int
    wallet: str
    symbol: str
    side: str
    amount_usd: float
    price_usd: float
    source: str
    confidence: float = 0.0

def _num(row: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value=row.get(key)
        try:
            if value is not None and str(value)!="":
                return float(value)
        except (TypeError,ValueError):
            pass
    return None

def normalize_explicit_events(rows: Iterable[Dict[str, Any]]) -> List[WalletEvent]:
    out=[]
    for row in rows:
        side=str(row.get("side") or row.get("event_type") or "").lower()
        if side not in {"buy","sell"}:
            continue
        ts=_num(row,"timestamp","time","trade_timestamp")
        amount=_num(row,"amount_usd","usd_value","cost_usd","buy_cost_usd")
        if not ts or amount is None or amount<=0:
            continue
        wallet=str(row.get("wallet") or row.get("maker") or row.get("address") or "")
        symbol=str(row.get("symbol") or row.get("base_token_symbol") or row.get("token") or "").upper()
        if not wallet or not symbol:
            continue
        out.append(WalletEvent(wallet,symbol,int(ts),side,amount,
            str(row.get("tx_hash") or ""),str(row.get("token_address") or row.get("mint") or ""),
            str(row.get("source") or "provider"),str(row.get("chain") or ""),
            _num(row,"token_amount","amount_tokens"),_num(row,"price_usd","price"),1.0))
    return sorted(out,key=lambda x:(x.timestamp,x.wallet,x.token,x.tx_hash))

def normalize_holder_snapshots(rows: Iterable[Dict[str, Any]], min_relative_change_pct: float=1.0) -> Tuple[List[HistoricalSignal],List[PricePoint]]:
    grouped={}
    for row in rows:
        wallet=str(row.get("wallet") or row.get("address") or "")
        symbol=str(row.get("symbol") or row.get("token") or "").upper()
        ts=_num(row,"timestamp","time")
        if not wallet or not symbol or not ts:
            continue
        grouped.setdefault((wallet.lower(),symbol),[]).append(row)
    signals=[]; prices={}
    for (wallet,symbol),series in grouped.items():
        series.sort(key=lambda r:int(_num(r,"timestamp","time") or 0))
        prev=None
        for row in series:
            ts=int(_num(row,"timestamp","time") or 0)
            price=_num(row,"price_usd","price")
            if price and price>0:
                prices[(symbol,ts)]=PricePoint(ts,price,symbol,"wallet_history")
            if prev is not None and price and price>0:
                old_balance=_num(prev,"balance","balance_raw","token_balance")
                new_balance=_num(row,"balance","balance_raw","token_balance")
                delta_balance=(new_balance-old_balance) if old_balance is not None and new_balance is not None else None
                old_pct=_num(prev,"percentage","holder_percentage")
                new_pct=_num(row,"percentage","holder_percentage")
                delta_pct=(new_pct-old_pct) if old_pct is not None and new_pct is not None else None
                relative=abs(delta_balance/old_balance)*100.0 if old_balance and delta_balance is not None else None
                increase=((delta_balance is not None and relative is not None and delta_balance>0 and relative>=min_relative_change_pct) or (delta_pct is not None and delta_pct>=0.005))
                decrease=((delta_balance is not None and relative is not None and delta_balance<0 and relative>=min_relative_change_pct) or (delta_pct is not None and delta_pct<=-0.005))
                if increase or decrease:
                    side="buy" if increase else "sell"
                    notional=abs(delta_balance)*price if delta_balance is not None else 0.0
                    confidence=0.55 if delta_balance is not None else 0.35
                    if delta_pct is not None: confidence+=0.10
                    signals.append(HistoricalSignal(ts,wallet,symbol,side,notional,price,"holder_delta_proxy",min(confidence,0.85)))
            prev=row
    return sorted(signals,key=lambda x:(x.timestamp,x.wallet,x.symbol)),sorted(prices.values(),key=lambda x:(x.symbol,x.timestamp))

def load_json_rows(path: str) -> List[Dict[str, Any]]:
    import json
    with open(path,"r",encoding="utf-8") as handle:
        obj=json.load(handle)
    if isinstance(obj,list): return obj
    if not isinstance(obj,dict): return []
    for key in ("events","records","rows","data"):
        if isinstance(obj.get(key),list): return obj[key]
    rows=[]
    if obj and all(isinstance(v,list) for v in obj.values()):
        for wallet,series in obj.items():
            for row in series:
                if isinstance(row,dict):
                    item=dict(row); item.setdefault("wallet",wallet); rows.append(item)
    return rows

def freeze_before(rows: Iterable[Any], signal_ts: int) -> List[Any]:
    return [row for row in rows if int(getattr(row,"timestamp",0))<=signal_ts]
