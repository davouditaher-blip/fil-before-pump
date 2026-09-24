from fil_engine.models import Candidate, WalletEvent
from fil_engine.wallet import classify_events, convergence
from fil_engine.backtest import Trade, run
from fil_engine.leakage import visible_at

def test_wallet_entry_add_exit():
    es=[WalletEvent("w","x",1,"buy",6000,"a"),WalletEvent("w","x",2,"buy",2000,"b"),WalletEvent("w","x",3,"sell",1000,"c"),WalletEvent("w","x",4,"sell",7000,"d")]
    c=classify_events(es)
    assert c[("w","x",1,"a")] == "new_entry"
    assert c[("w","x",2,"b")] == "add"
    assert c[("w","x",3,"c")] == "reduce"
    assert c[("w","x",4,"d")] == "full_exit"

def test_convergence_requires_two_tokens():
    es=[WalletEvent("w","a",1,"buy",6000),WalletEvent("w","b",2,"buy",7000),WalletEvent("q","a",1,"buy",6000)]
    assert convergence(es)=={"w":["A","B"]}

def test_wallet_dominant_score():
    c=Candidate("X",10,wallet_score=100,technical_score=0)
    assert c.score()==40.0

def test_safety_blocks():
    c=Candidate("X",10,wallet_score=100,safety_blocked=True)
    assert c.score()==0

def test_no_future_visibility():
    assert visible_at(100,100)
    assert not visible_at(100,101)

def test_backtest_includes_costs():
    r=run([Trade(1,2,100,110,100)])
    assert r.trades==1 and r.final_balance<110
