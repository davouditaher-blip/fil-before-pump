import json
import os
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GITHUB_TOKEN = os.environ.get("GITHUB_PAT") or os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "davouditaher-blip/fil-before-pump")
WORKFLOW_FILE = "fil-before-pump.yml"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH_API = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"

# Telegram-trigger queue; GitHub Actions polls it and consumes requests.
PENDING_SCAN_REQUESTS = []

RANKS = {
    "top100": "🥇 رتبه 1–100",
    "101_200": "🥈 رتبه 101–200",
    "201_300": "🥉 رتبه 201–300",
    "all": "🌐 همه 1–300",
}
FILTERS = {
    "smart": "🐋 Smart Money",
    "wallet": "👛 ولت و accumulation",
    "whale": "🐳 نهنگ",
    "volume": "💰 حجم 24–48h",
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
        [{"text": "🚀 اجرای فیل کامل", "callback_data": cb("run", rank, "all")}],
        [
            {"text": ("✅ " if rank == "top100" else "") + RANKS["top100"], "callback_data": cb("rank", "top100", selected)},
            {"text": ("✅ " if rank == "101_200" else "") + RANKS["101_200"], "callback_data": cb("rank", "101_200", selected)},
        ],
        [
            {"text": ("✅ " if rank == "201_300" else "") + RANKS["201_300"], "callback_data": cb("rank", "201_300", selected)},
            {"text": ("✅ " if rank == "all" else "") + RANKS["all"], "callback_data": cb("rank", "all", selected)},
        ],
        [{"text": "🎯 انتخاب لایه‌های بررسی", "callback_data": cb("noop", rank, selected)}],
    ]

    for key in ("smart", "wallet", "whale", "volume"):
        new_filters = [x for x in filters if x != key] if key in filters else filters + [key]
        rows.append([{
            "text": ("✅ " if key in filters else "▫️ ") + FILTERS[key],
            "callback_data": cb("toggle", rank, ",".join(new_filters) or "all"),
        }])

    rows.append([
        {"text": "🧹 پاک کردن انتخاب‌ها", "callback_data": cb("clear", "all", "all")},
        {"text": "▶️ اجرای انتخاب فعلی", "callback_data": cb("run", rank, selected)},
    ])
    return {"inline_keyboard": rows}

def send_menu(chat_id, rank="all", filters=None, message_id=None):
    filters = filters or []
    active = [FILTERS[x] for x in filters if x in FILTERS]
    body = (
        "🐋 فیل قبل از پامپ\n\n"
        f"رتبه: {RANKS.get(rank, RANKS['all'])}\n"
        "اولویت: 🐋 Smart Money → 👛 accumulation/ولت مشترک → سابقه ولت → 🐳 نهنگ → 💰 حجم\n"
        "تکنیکال: فعلاً بدون اثر در انتخاب کاندید\n"
        f"لایه‌های انتخابی: {' + '.join(active) if active else 'فیل کامل (همه لایه‌های اصلی)'}"
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
    """
    Start the scanner immediately from Telegram by dispatching the GitHub
    Actions workflow. The old in-memory queue was never consumed by Actions,
    so Telegram requests could appear accepted while no scan actually ran.
    """
    if not GITHUB_TOKEN.strip():
        raise RuntimeError("GITHUB_PAT is not configured on Render")

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "ref": "main",
        "inputs": {
            "rank_range": rank or "all",
            "filter": filters or "all",
        },
    }
    r = requests.post(GH_API, headers=headers, json=payload, timeout=20)
    # GitHub's current REST API returns 200 when run details are returned;
    # some GitHub Enterprise/API variants return 204 with no body.
    if r.status_code not in (200, 201, 204):
        try:
            data = r.json()
            detail = data.get("message", r.text)
            errors = data.get("errors")
            if errors:
                detail = f"{detail}; errors={errors}"
        except Exception:
            detail = r.text
        raise RuntimeError(f"GitHub Actions dispatch failed ({r.status_code}): {str(detail)[:300]}")

    # Keep Telegram independent of whether GitHub returns run details.
    if r.status_code == 200:
        try:
            result = r.json()
            print("GitHub Actions dispatch accepted:", result.get("workflow_run_id", "run-id-not-returned"))
        except Exception:
            print("GitHub Actions dispatch accepted with HTTP 200.")
    else:
        print(f"GitHub Actions dispatch accepted with HTTP {r.status_code}.")
    return True

def process(update):
    if "message" in update:
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip().lower()
        if text in ("/start", "/fil", "start", "menu", "منو"):
            send_menu(chat_id)
        elif text:
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
            text = "⏳ اسکن فیل کامل شروع شد. نتیجه پس از پایان اجرای GitHub Actions در همین چت ارسال می‌شود."
        except Exception as exc:
            print("Scanner dispatch error:", exc)
            text = f"❌ اجرای اسکن شروع نشد: {exc}"
        tg("editMessageText", data={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": json.dumps(menu(rank, filters), ensure_ascii=False),
        })

def github_token_check():
    """Safe diagnostic: never returns or logs the GitHub token."""
    if not GITHUB_TOKEN.strip():
        return {"ok": False, "stage": "environment", "status": None, "message": "GITHUB_TOKEN is empty"}
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        user = requests.get("https://api.github.com/user", headers=headers, timeout=15)
        if user.status_code != 200:
            return {"ok": False, "stage": "authentication", "status": user.status_code,
                    "message": user.json().get("message", "GitHub authentication failed")}
        repo = requests.get(f"https://api.github.com/repos/{REPO}", headers=headers, timeout=15)
        if repo.status_code != 200:
            return {"ok": False, "stage": "repository_access", "status": repo.status_code,
                    "message": repo.json().get("message", "Repository access failed")}
        workflow = requests.get(
            f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}",
            headers=headers, timeout=15,
        )
        if workflow.status_code != 200:
            return {"ok": False, "stage": "workflow_access", "status": workflow.status_code,
                    "message": workflow.json().get("message", "Workflow access failed")}
        return {"ok": True, "stage": "complete", "status": 200,
                "message": "GitHub authentication, repository access, and workflow access are OK"}
    except Exception as exc:
        return {"ok": False, "stage": "network", "status": None, "message": str(exc)[:200]}

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
        if self.path == "/scan-request":
            # Only GitHub Actions holding the existing Telegram bot secret may
            # consume the queue. The token is never returned or logged.
            if self.headers.get("X-Scan-Token", "") != BOT_TOKEN:
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"error":"unauthorized"}')
                return
            request = PENDING_SCAN_REQUESTS.pop(0) if PENDING_SCAN_REQUESTS else None
            body = json.dumps({"pending": bool(request), "request": request}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/github-check":
            result = github_token_check()
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
