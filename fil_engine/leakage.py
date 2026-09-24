def visible_at(event_ts:int, row_ts:int)->bool:
    """Historical feature is usable only when its timestamp is <= signal time."""
    return row_ts <= event_ts

def freeze(rows, signal_ts:int):
    return [r for r in rows if int(r.get("timestamp",0)) <= signal_ts]
