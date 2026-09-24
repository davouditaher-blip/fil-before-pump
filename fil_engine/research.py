import json, time
from pathlib import Path
REGISTRY=Path("research/strategy_registry.json")

def load_registry():
    if not REGISTRY.exists(): return {"schema_version":1,"strategies":[]}
    return json.loads(REGISTRY.read_text())

def record_version(version:str, change:str, rationale:str, backtest:dict|None=None, oos:dict|None=None, paper:dict|None=None, status="candidate"):
    obj=load_registry(); obj["strategies"].append({"version":version,"created_at":int(time.time()),"change":change,"rationale":rationale,"backtest":backtest,"oos":oos,"paper":paper,"status":status})
    REGISTRY.parent.mkdir(parents=True,exist_ok=True); REGISTRY.write_text(json.dumps(obj,indent=2,ensure_ascii=False)); return obj
