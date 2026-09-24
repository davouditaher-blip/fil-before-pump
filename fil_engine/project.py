from typing import Dict, Any

def score_project(features: Dict[str,Any]) -> tuple[float,list[str]]:
    """Project-led score. Missing fields are neutral, never fabricated."""
    weights={"users_growth":15,"tvl_growth":15,"fees_growth":10,"holders_growth":10,"dex_activity":10,"liquidity_growth":10,"dev_activity":10,"ecosystem_growth":10,"valuation_room":10}
    score=0.0; evidence=[]
    for k,w in weights.items():
        v=features.get(k)
        if isinstance(v,(int,float)):
            score += max(0,min(1,float(v)))*w
            if v>0.6: evidence.append(f"{k} positive")
    return round(min(100,score),2),evidence

def cohort_label(outcome_pct:float, horizon_hours:int, threshold:float=30.0)->str:
    return "successful" if outcome_pct>=threshold else "failed_or_non_move"
