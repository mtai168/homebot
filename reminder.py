#!/usr/bin/env python3
"""
HomeBox — 定期提醒 script
每週一、三 15:00 發送告警提醒
由 cron 呼叫
"""

import json
import logging
import os
from pathlib import Path
from datetime import datetime

import requests

ENV_FILE = Path(os.getenv("HOMEBOX_ENV_FILE", os.path.join(os.getenv("HOMEBOX_DATA_DIR", "/root/homebox"), ".env")))

def load_env_file(path=ENV_FILE):
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip(chr(34) + chr(39)))

load_env_file()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(f"BOT_TOKEN must be set in environment or {ENV_FILE}")
APP_DIR = Path(os.getenv("HOMEBOX_DATA_DIR", "/root/homebox"))
LOG_DIR = Path(os.getenv("HOMEBOX_LOG_DIR", "/var/log/homebox"))
DATA_FILE = APP_DIR / "data.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = str(LOG_DIR / "reminder.log")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# 這裡填入你的 Telegram chat ID
# 第一次跟 bot 互動後，bot 會印出 chat ID
CHAT_IDS_FILE = APP_DIR / "chat_ids.json"


def load_chat_ids():
    if CHAT_IDS_FILE.exists():
        return json.loads(CHAT_IDS_FILE.read_text())
    return []


def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=30)
    return r.status_code == 200


def main():
    if not DATA_FILE.exists():
        log.warning("data.json not found")
        return

    data = json.loads(DATA_FILE.read_text())
    chat_ids = load_chat_ids()

    if not chat_ids:
        log.warning("No chat IDs registered")
        return

    alert_items = []
    for item in data["items"]:
        total = sum(l["quantity"] for l in item["locations"])
        if total < item["alert_threshold"]:
            alert_items.append((item, total))

    if not alert_items:
        log.info("No alert items, skipping")
        return

    lines = ["⚠️ HomeBox 庫存告警\n"]
    for item, total in alert_items:
        lines.append(f"• {item['name']}：{total}（告警值 {item['alert_threshold']}）")
    lines.append("\n記得補貨喔！")

    msg = "\n".join(lines)

    for chat_id in chat_ids:
        try:
            if send_telegram(chat_id, msg):
                log.info(f"Sent alert to {chat_id}")
            else:
                log.error(f"Failed to send to {chat_id}")
        except Exception as e:
            log.error(f"Error sending to {chat_id}: {e}")


if __name__ == "__main__":
    main()
