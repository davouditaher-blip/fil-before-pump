BLOCKING={"honeypot","mint_authority","freeze_authority","rug_risk","liquidity_critical","malicious_dev"}
def assess_safety(features:dict)->tuple[float,bool,list[str]]:
    evidence=[]; blocked=False; score=100.0
    for key in BLOCKING:
        v=features.get(key)
        if v is True or (isinstance(v,(int,float)) and v>=0.9): blocked=True; score=0; evidence.append(f"blocked: {key}")
    for key in ("holder_concentration","insider_concentration","bundle_risk","unlock_risk"):
        v=features.get(key)
        if isinstance(v,(int,float)):
            score-=max(0,min(1,v))*15
            if v>=.7: evidence.append(f"elevated {key}")
    return round(max(0,score),2),blocked,evidence
