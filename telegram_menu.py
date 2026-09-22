import json
import os
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
WORKFLOW_FILE = "fil-before-pump.yml"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH_API = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"

RANKS = {
    "top100":"🥇 رتبه 1–100",
    "101_200":"🥈 رتبه 101–200",
    "201_300":"🥉 رتبه 201–300",
    "all":"🌐 همه ارزها (1–300)"
}
FILTERS = {
    "smart":"🐋 Smart Money",
    "common":"🔁 ولت‌های مشترک",
    "history":"🧠 سابقه ولت",
    "status":"📍 وضعیت ولت",
    "whale":"🐳 نهنگ",
    "volume":"📊 حجم"
}

def api(method, **kwargs):
    r = requests.post(f"{API}/{method}", timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()

def menu(rank="all", filters=None):
    filters = filters or []
    filt = ",".join(filters) or "all"
    def cb(action, r=rank, f=filt):
        return f"{action}|{r}|{f}"[:64]

    rows = [
        [{"text":"🔥 فیل کامل (1–300)","callback_data":cb("run","all","all")}],
        [
            {"text":("✅ " if rank=="top100" else "")+RANKS["top100"],"callback_data":cb("rank","top100",filt)},
            {"text":("✅ " if rank=="101_200" else "")+RANKS["101_200"],"callback_data":cb("rank","101_200",filt)}
        ],
        [
            {"text":("✅ " if rank=="201_300" else "")+RANKS["201_300"],"callback_data":cb("rank","201_300",filt)},
            {"text":("✅ " if rank=="all" else "")+RANKS["all"],"callback_data":cb("rank","all",filt)}
        ]
    ]
    for key in ("smart","common","history","status","whale","volume"):
        newfilters = [x for x in filters if x != key] if key in filters else filters+[key]
        rows.append([{"text":("✅ " if key in filters else "")+FILTERS[key],
                      "callback_data":cb("toggle",rank,",".join(newfilters) or "all")}])
    rows.append([{"text":"🧹 پاک کردن فیلترها","callback_data":"clear|all|all"}])
    rows.append([{"text":"🚀 اجرای بررسی","callback_data":cb("run")}])
    return {"inline_keyboard":rows}

def send_menu(chat_id, rank="all", filters=None, edit_message_id=None):
    active=[FILTERS[x] for x in (filters or []) if x in FILTERS]
    text="🐋 فیل قبل از پامپ\n\nرتبه: "+RANKS.get(rank,RANKS["all"])+"\nاولویت: Smart Money → ولت مشترک → سابقه ولت → وضعیت خروج → نهنگ → حجم\nفیلترها: "+(" + ".join(active) if active else "همه سیگنال‌ها")
    payload={"chat_id":chat_id,"text":text,"reply_markup":json.dumps(menu(rank,filters),ensure_ascii=False)}
    if edit_message_id:
        payload["message_id"]=edit_message_id
        api("editMessageText",data=payload)
    else:
        api("sendMessage",data=payload)

def dispatch(rank, filters):
    headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {GITHUB_TOKEN}","X-GitHub-Api-Version":"2022-11-28"}
    data={"ref":"main","inputs":{"rank_range":rank,"filter":filters or "all"}}
    r=requests.post(GH_API,headers=headers,json=data,timeout=30)
    if r.status_code not in (200,201,204):
        raise RuntimeError(f"GitHub workflow dispatch failed: HTTP {r.status_code} {r.text[:500]}")

def process_update(update):
    if "message" in update:
        msg=update["message"]
        chat_id=str(msg["chat"]["id"])
        text=(msg.get("text") or "").strip().lower()
        if text in ("/start","start","menu","منو") or text:
            send_menu(chat_id)
        return
    q=update.get("callback_query")
    if not q:
        return
    parts=q.get("data","").split("|",2)
    if len(parts)!=3:
        return
    action,rank,filt=parts
    filters=[] if filt in ("","all") else [x for x in filt.split(",") if x in FILTERS]
    chat_id=str(q["message"]["chat"]["id"])
    message_id=q["message"]["message_id"]
    api("answerCallbackQuery",data={"callback_query_id":q["id"]})
    if action in ("rank","toggle","settings"):
        send_menu(chat_id,rank,filters,message_id)
    elif action=="clear":
        send_menu(chat_id,"all",[],message_id)
    elif action=="run":
        dispatch(rank,",".join(filters) if filters else "all")
        api("editMessageText",data={"chat_id":chat_id,"message_id":message_id,
            "text":"⏳ بررسی انتخابی ارسال شد. نتیجه بعد از اجرای اسکن در همین چت می‌آید.",
            "reply_markup":json.dumps(menu(rank,filters),ensure_ascii=False)})

OFFSET_FILE = "telegram_update_offset.txt"

def load_offset():
    try:
        return int(open(OFFSET_FILE, "r", encoding="utf-8").read().strip())
    except Exception:
        return 0

def save_offset(offset):
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        f.write(str(offset))

def main():
    offset=load_offset()
    while True:
        r=requests.get(f"{API}/getUpdates",
                       params={"timeout":20,"offset":offset,
                               "allowed_updates":json.dumps(["message","callback_query"])},
                       timeout=30)
        r.raise_for_status()
        updates=r.json().get("result",[])
        if not updates:
            break
        for update in updates:
            next_offset=int(update["update_id"])+1
            try:
                process_update(update)
            except Exception as e:
                print(f"Update handling warning: {e}")
            offset=max(offset,next_offset)
            save_offset(offset)

if __name__=="__main__":
    main()
