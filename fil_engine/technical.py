def score_technical(features:dict)->tuple[float,list[str]]:
    score=50.0; ev=[]
    if features.get("higher_low") is True: score+=15; ev.append("higher low")
    if features.get("compression") is True: score+=10; ev.append("compression")
    if features.get("breakout_ready") is True: score+=15; ev.append("breakout context")
    if features.get("overextended") is True: score-=15; ev.append("overextended")
    return round(max(0,min(100,score)),2),ev
