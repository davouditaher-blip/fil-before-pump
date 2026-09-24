from dataclasses import dataclass, asdict
from typing import Iterable, List, Dict

@dataclass
class Trade:
    entry_ts:int
    exit_ts:int
    entry:float
    exit:float
    capital:float
    fee_rate:float=.001
    slippage_rate:float=.0005
    @property
    def gross_pnl(self): return self.capital*((self.exit*(1-self.slippage_rate))/(self.entry*(1+self.slippage_rate))-1)
    @property
    def fees(self): return self.capital*self.fee_rate*2
    @property
    def net_pnl(self): return self.gross_pnl-self.fees

@dataclass
class BacktestReport:
    initial_balance:float
    final_balance:float
    roi:float
    win_rate:float
    wins:int
    losses:int
    gross_profit:float
    gross_loss:float
    fees:float
    max_drawdown:float
    profit_factor:float
    trades:int

def run(trades:Iterable[Trade], initial_balance:float=100.0)->BacktestReport:
    balance=initial_balance; peak=balance; max_dd=0.0; wins=losses=0; gp=gl=fees=0.0
    for t in sorted(trades,key=lambda x:x.entry_ts):
        pnl=t.net_pnl; balance+=pnl; fees+=t.fees
        if pnl>0: wins+=1; gp+=pnl
        elif pnl<0: losses+=1; gl+=-pnl
        peak=max(peak,balance); max_dd=max(max_dd,(peak-balance)/peak*100 if peak else 0)
    n=wins+losses
    return BacktestReport(initial_balance,balance,(balance/initial_balance-1)*100 if initial_balance else 0,
        wins/n*100 if n else 0,wins,losses,gp,gl,fees,max_dd,gp/gl if gl else (float('inf') if gp else 0),n)

def as_dict(report:BacktestReport)->dict: return asdict(report)
