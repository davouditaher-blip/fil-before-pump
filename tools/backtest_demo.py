"""Minimal reproducible backtest runner; adapters can feed real historical signals later."""
import json
from fil_engine.backtest import Trade,run,as_dict
trades=[]
# Intentionally empty by default: never fabricate historical performance.
print(json.dumps({"status":"ready","message":"No fabricated trades. Supply timestamped historical signals/prices to run a valid backtest.","report":as_dict(run(trades,100.0))},indent=2))
