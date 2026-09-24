from .models import Candidate

def score_candidate(c: Candidate)->dict:
    total=c.score()
    reasons=list(c.evidence)
    if c.wallet_score>=60: reasons.append("strong wallet intelligence")
    if c.project_score>=60: reasons.append("project leading indicators")
    if c.volume_score>=55: reasons.append("volume expansion/context")
    if c.safety_blocked: reasons.append("SAFETY BLOCK")
    return {"symbol":c.symbol,"timestamp":c.timestamp,"score":total,"class":c.class_name,
            "safety_blocked":c.safety_blocked,"evidence":reasons,"components":{
              "wallet":c.wallet_score,"project":c.project_score,"volume":c.volume_score,
              "market":c.market_score,"safety":c.safety_score,"technical":c.technical_score}}
