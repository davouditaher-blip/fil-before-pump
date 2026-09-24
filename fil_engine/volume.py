def pct_change(now, old):
    if old in (None,0) or now is None: return None
    return (float(now)/float(old)-1)*100

def score_volume(v:dict)->tuple[float,list[str]]:
    score=0; ev=[]
    for key,w in (("1d",35),("2d",25),("3d",15),("7d",15),("14d",10)):
        x=v.get(key)
        if isinstance(x,(int,float)):
            if x>0: score += min(100,max(0,x))/100*w
            if x>=10: ev.append(f"{key} volume +{x:.1f}%")
    return round(min(100,score),2),ev
