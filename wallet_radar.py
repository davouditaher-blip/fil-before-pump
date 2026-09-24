import json, os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from gmgn_layer import GMGN_API_KEY, run_gmgn, run_gmgn_cli, portfolio_activity, analyze_wallet_activity
OUT=Path('wallet_radar.json'); THRESHOLD=5000.0; CHAINS=('sol','bsc','base','eth')

# Radar is observational: it must never be used as an execution signal by itself.
STATUS_HOLDING='holding'; STATUS_TRIMMED='trimmed'; STATUS_EXITED='exited'; STATUS_UNKNOWN='unknown'
def num(*v):
    for x in v:
        try:
            if x is not None and str(x)!='': return float(x)
        except: pass
    return 0.0
def sym(r): return str(r.get('symbol') or r.get('base_token_symbol') or ((r.get('base_token') or {}).get('symbol')) or '?').upper()
def holdings(chain,wallet):
    o=run_gmgn_cli(['portfolio','holdings','--chain',chain,'--wallet',wallet,'--limit','100'],timeout=75); d=o.get('list') or o.get('data') or []; return d if isinstance(d,list) else []
def main():
    if not GMGN_API_KEY: raise SystemExit('GMGN_API_KEY is not configured.')
    now=int(datetime.now(timezone.utc).timestamp()); qualified={}; trades=[]
    for c in CHAINS: trades.extend(run_gmgn(c))
    for t in trades:
        if str(t.get('side') or '').lower()!='buy': continue
        amount=num(t.get('amount_usd'),t.get('usd_value'),t.get('amount')); w=t.get('maker'); c=t.get('chain') or ''
        if not w or amount<THRESHOLD: continue
        q=qualified.setdefault(f'{c}:{w}',{'wallet':w,'chain':c,'qualified_buy_count':0,'qualified_buy_usd':0.0,'assets':{}}); q['qualified_buy_count']+=1; q['qualified_buy_usd']+=amount
        s=sym(t); a=q['assets'].setdefault(s,{'buy_usd':0.0,'buys':0}); a['buy_usd']+=amount; a['buys']+=1
    radar=json.loads(OUT.read_text()) if OUT.exists() else {}; report=[]
    for key,q in qualified.items():
        w,c=q['wallet'],q['chain']
        try:
            act=portfolio_activity(c,w,limit=500) or []
        except Exception as e:
            print(f'portfolio warning {c}:{w[:8]}: {e}')
            act=[]
        try:
            p=analyze_wallet_activity(c,w,act,{},now) or {}
        except Exception as e:
            print(f'analysis warning {c}:{w[:8]}: {e}')
            p={}
        try:
            cur=holdings(c,w)
        except Exception as e:
            print(f'holdings warning {c}:{w[:8]}: {e}')
            cur=[]
        held=[{'symbol':sym(x),'value_usd':num(x.get('value_usd'),x.get('usd_value'),x.get('amount_usd')),'pnl_usd':num(x.get('pnl_usd'),x.get('profit_usd'),x.get('unrealized_pnl'))} for x in cur if sym(x)!='?']
        by=defaultdict(list)
        for a in act:
            s=sym(a); side=str(a.get('side') or a.get('type') or '').lower(); ts=int(num(a.get('timestamp'),a.get('block_timestamp'),a.get('time')))
            if s!='?' and ts and side in ('buy','sell'): by[s].append((side,num(a.get('amount_usd'),a.get('usd_value'),a.get('amount'))))
        hs={x['symbol'] for x in held}; states={}
        for s,ev in by.items():
            b=sum(v for side,v in ev if side=='buy'); z=sum(v for side,v in ev if side=='sell'); states[s]='holding' if s in hs else ('exited' if z>=b*.9 and z>0 else ('trimmed' if z>0 else 'holding_unknown_balance'))
        row={'wallet':w,'chain':c,'threshold_usd':THRESHOLD,'qualified_buy_count':q['qualified_buy_count'],'qualified_buy_usd':round(q['qualified_buy_usd'],2),'assets':q['assets'],'performance':{'observed_opportunities':p.get('observed_opportunities',0),'unknown_opportunities':p.get('unknown_opportunities',0),'successful_pre_pump_entries':p.get('successful_pre_pump_entries',0),'pre_pump_win_rate':p.get('pre_pump_win_rate')},'current_holdings':held[:100],'position_states':states,'last_scan':now}; radar[key]=row; report.append(row)
    OUT.write_text(json.dumps(radar,indent=2,ensure_ascii=False), encoding='utf-8')
    # Telegram output is intentionally disabled here: the integrated scanner
    # owns the 30-minute report so Radar does not create duplicate messages.
    lines=['🐋 WALLET RADAR — خریدهای >= $5K','Read-only | $5K معیار کشف است، نه اثبات کیفیت.']
    for w in sorted(report,key=lambda x:x['qualified_buy_usd'],reverse=True)[:30]:
        p=w['performance']; wr=p['pre_pump_win_rate']; wt=f'{wr:.0f}%' if isinstance(wr,(int,float)) else 'N/A'; short=w['wallet'][:8]+'â¦'+w['wallet'][-6:]; lines.append(f"\nð {short} | {w['chain']} | buys {w['qualified_buy_count']} | ${w['qualified_buy_usd']:,.0f} | pre-pump {p['successful_pre_pump_entries']}/{p['observed_opportunities']} ({wt})"); lines += [f"   {s}: {st}" for s,st in list(w['position_states'].items())[:8]]; heldnames=[x['symbol'] for x in w['current_holdings']]; lines.append('   ð¢ holdings: '+', '.join(heldnames[:10])) if heldnames else None
    msg='\n'.join(lines)
    print(msg)
if __name__=='__main__': main()
