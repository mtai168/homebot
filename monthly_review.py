#!/usr/bin/env python3
"""Monthly review reminder — sends TODO-pending items via Telegram."""

import json, subprocess, pathlib, os, sys

# Read memory for TODO items
MEMORY_FILE = pathlib.Path("/root/.hermes/memory/agents/default/MEMORY.md")
TODO_FILE = pathlib.Path("/root/.hermes/memory/agents/default/TODO.md")

def load_env_file(path="/root/homebox/.env"):
    if not pathlib.Path(path).exists():
        return
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip(chr(34) + chr(39)))

load_env_file()
BOT_TOKEN = os.getenv("MONTHLY_REVIEW_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("MONTHLY_REVIEW_BOT_TOKEN or BOT_TOKEN must be set in environment or /root/homebox/.env")

def get_chat_id():
    """Get chat ID from bot updates."""
    import urllib.request
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        for result in data.get("result", []):
            chat = result.get("message", {}).get("chat", {})
            if chat.get("type") in ("group", "supergroup"):
                return chat["id"]
    except Exception as e:
        print(f"Error getting chat ID: {e}")
    return None

def send_message(chat_id, text):
    import urllib.request, urllib.parse
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def collect_todos():
    """Collect pending/in-progress items from memory."""
    items = []

    # Check memory file for TODO later items
    if MEMORY_FILE.exists():
        content = MEMORY_FILE.read_text()
        # Find TODO later section
        in_todo = False
        for line in content.split("\n"):
            if "TODO later" in line:
                in_todo = True
                continue
            if in_todo:
                if line.startswith("§") or line.startswith("##"):
                    break
                if line.strip().startswith("-") or line.strip().startswith("*"):
                    items.append(line.strip().lstrip("-* ").strip())

    return items

def main():
    todos = collect_todos()

    if not todos:
        text = "📋 **每月 Review**\n\n目前沒有待處理的 TODO 項目！🎉"
    else:
        text = "📋 **每月 Review — 待處理項目**\n\n"
        for i, item in enumerate(todos, 1):
            text += f"{i}. {item}\n"
        text += f"\n共 {len(todos)} 項待處理。"

    chat_id = get_chat_id()
    if chat_id:
        result = send_message(chat_id, text)
        print(f"Sent to chat {chat_id}: {result.get('ok', False)}")
    else:
        print("No group chat ID found. Send a message to the bot first.")
        sys.exit(1)

if __name__ == "__main__":
    main()
