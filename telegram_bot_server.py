import json
import os
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ.get("GITHUB_REPOSITORY", "davouditaher-blip/fil-before-pump")
WORKFLOW_FILE = "fil-before-pump.yml"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH_API = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"

RANKS = {
    "top100": "🥇 رتبه 1–100",
    "101_200": "🥈 رتبه 101–200",
    "201_300": "🥉 رتبه 201–300",
    "301_400": "🏅 رتبه 301–400",
    "401_500": "🎖️ رتبه 401–500",
    "501_1000": "📌 رتبه 501–1000",
    "all": "🌐 همه ارزها",
}
FILTERS = {
    "volume": "📊 حجم",
    "smart": "🐋 Smart Money",
    "whale": "🐳 نهنگ",
    "wallet": "👛 ولت‌ها",
    "technical": "📈 تکنیکال",
}

def tg(method, **kwargs):
    r = requests.post(f"{API}/{method}", timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()

def menu(rank="all", filters=None):
    filters = filters or []
    selected = ",".join(filters) or "all"

    def cb(action, r=rank, f=selected):
        return f"{action}|{r}|{f}"[:64]

    rows = [
        [
            {"text": "⚡ اسکن لحظه‌ای", "callback_data": cb("run")},
            {"text": "🔥 فیل کامل", "callback_data": cb("run", "all", "all")},
        ],
        [
            {"text": ("✅ " if rank == "top100" else "") + RANKS["top100"], "callback_data": cb("rank", "top100")},
            {"text": ("✅ " if rank == "101_200" else "") + RANKS["101_200"], "callback_data": cb("rank", "101_200")},
        ],
        [
            {"text": ("✅ " if rank == "201_300" else "") + RANKS["201_300"], "callback_data": cb("rank", "201_300")},
            {"text": ("✅ " if rank == "301_400" else "") + RANKS["301_400"], "callback_data": cb("rank", "301_400")},
        ],
        [
            {"text": ("✅ " if rank == "401_500" else "") + RANKS["401_500"], "callback_data": cb("rank", "401_500")},
            {"text": ("✅ " if rank == "501_1000" else "") + RANKS["501_1000"], "callback_data": cb("rank", "501_1000")},
        ],
        [
            {"text": ("✅ " if rank == "all" else "") + RANKS["all"], "callback_data": cb("rank", "all")},
            {"text": "⚙️ تنظیمات", "callback_data": cb("settings")},
        ],
    ]

    for key, label in FILTERS.items():
        new_filters = [x for x in filters if x != key] if key in filters else filters + [key]
        rows.append([
            {
                "text": ("✅ " if key in filters else "") + label,
                "callback_data": cb("toggle", rank, ",".join(new_filters) or "all"),
            }
        ])

    rows.append([{"text": "🧹 پاک کردن فیلترها", "callback_data": "clear|all|all"}])
    rows.append([{"text": "🔥 اجرای بررسی", "callback_data": cb("run")}])
    return {"inline_keyboard": rows}

def send_menu(chat_id, rank="all", filters=None, message_id=None):
    filters = filters or []
    active = [FILTERS[x] for x in filters if x in FILTERS]
    body = (
        "🐋 فیل قبل از پامپ\n\n"
        f"رتبه: {RANKS.get(rank, RANKS['all'])}\n"
        f"فیلترها: {' + '.join(active) if active else 'همه سیگنال‌ها'}"
    )
    data = {
        "chat_id": chat_id,
        "text": body,
        "reply_markup": json.dumps(menu(rank, filters), ensure_ascii=False),
    }
    if message_id:
        data["message_id"] = message_id
        tg("editMessageText", data=data)
    else:
        tg("sendMessage", data=data)

def dispatch(rank, filters):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "ref": "main",
        "inputs": {"rank_range": rank, "filter": filters or "all"},
    }
    r = requests.post(GH_API, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"GitHub workflow dispatch failed: HTTP {r.status_code} {r.text[:500]}")

def process(update):
    if "message" in update:
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip().lower()
        if text in ("/start", "start", "menu", "منو") or text:
            send_menu(chat_id)
        return

    q = update.get("callback_query")
    if not q:
        return

    parts = q.get("data", "").split("|", 2)
    if len(parts) != 3:
        return

    action, rank, filt = parts
    filters = [] if filt in ("", "all") else [x for x in filt.split(",") if x in FILTERS]
    chat_id = str(q["message"]["chat"]["id"])
    message_id = q["message"]["message_id"]

    tg("answerCallbackQuery", data={"callback_query_id": q["id"]})

    if action in ("rank", "toggle", "settings"):
        send_menu(chat_id, rank, filters, message_id)
    elif action == "clear":
        send_menu(chat_id, "all", [], message_id)
    elif action == "run":
        try:
            dispatch(rank, ",".join(filters) if filters else "all")
            text = "⏳ بررسی انتخابی ارسال شد. نتیجه بعد از اجرای اسکن در همین چت می‌آید."
        except Exception as exc:
            text = f"❌ خطا در اجرای اسکن: {exc}"
        tg(
            "editMessageText",
            data={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": json.dumps(menu(rank, filters), ensure_ascii=False),
            },
        )

def set_webhook():
    external = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not external:
        print("WEBHOOK_URL/RENDER_EXTERNAL_URL is not set; webhook was not registered.")
        return
    url = external.rstrip("/") + "/telegram/webhook"
    result = tg("setWebhook", data={"url": url, "drop_pending_updates": False})
    print("Telegram webhook:", result)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"Fil Before Pump Telegram Bot is running."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/telegram/webhook":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length)
            update = json.loads(payload.decode("utf-8"))
            process(update)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as exc:
            print("Webhook error:", exc)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    def log_message(self, fmt, *args):
        print(fmt % args)

if __name__ == "__main__":
    set_webhook()
    port = int(os.environ.get("PORT", "10000"))
    print(f"Telegram bot server listening on :{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
