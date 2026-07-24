#!/usr/bin/env python3
"""
HomeBox - 家中物品庫存管理 Telegram Bot
"""

import json
import os
import re
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)

# ─── 設定 ───────────────────────────────────────────────
def load_env_file(path="/root/homebox/.env"):
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip(chr(34) + chr(39)))

load_env_file()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN must be set in environment or /root/homebox/.env")
DATA_FILE = Path("/root/homebox/data.json")
LOG_FILE = "/var/log/homebox/bot.log"
CHAT_IDS_FILE = Path("/root/homebox/chat_ids.json")
TXN_FILE = Path("/root/homebox/transactions.jsonl")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# 分類 emoji 對照
CATEGORY_EMOJI = {
    "沐浴類": "🛁",
    "食品": "🍖",
    "日用耗品": "🧻",
    "保健品": "💊",
    "藥品": "🏥",
}

# 位置 emoji 對照（依常見位置自動配對，未配對的用 📦）
LOCATION_EMOJI = {
    "主廁所下方儲物櫃": "🚿",
    "鞋櫃上方儲存櫃": "👟",
    "臥室洗手台下方凹洞": "🛏️",
    "客廳沙發": "🛋",
    "廚房": "🍳",
    "書房": "📚",
    "陽台": "☀️",
    "車庫": "🚗",
    "倉庫": "🏪",
    "冰箱": "🧊",
    "衣櫃": "👔",
    "抽屜": "🗄️",
    "架子": "📐",
    "櫃子": "🗃️",
}

def cat_emoji(cat):
    return CATEGORY_EMOJI.get(cat, "📦")

def loc_emoji(loc):
    return LOCATION_EMOJI.get(loc, "📦")
def _seed_data():
    """首次啟動（data.json 不存在）時的預置資料，避免空庫存報錯。"""
    now = datetime.now().isoformat()
    return {
        "items": [
            {
                "id": 1,
                "name": "空氣",
                "category": "日常用品",
                "alert_threshold": 0,
                "locations": [{"name": "儲藏室", "quantity": 1}],
                "total": 1,
                "created_at": now,
                "updated_at": now,
            }
        ],
        "categories": ["日常用品"],
        "locations_index": ["儲藏室"],
        "next_id": 2,
    }

def load_data():
    if not DATA_FILE.exists():
        default = _seed_data()
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps(default, ensure_ascii=False, indent=2))
    return json.loads(DATA_FILE.read_text())

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def load_chat_ids():
    if CHAT_IDS_FILE.exists():
        return json.loads(CHAT_IDS_FILE.read_text())
    return []

def save_chat_ids(ids):
    CHAT_IDS_FILE.write_text(json.dumps(ids))

def get_item_by_name(data, name):
    """用名稱搜尋物品：先精確比對，再模糊比對"""
    name = name.strip().lower()
    # 精確比對
    for item in data["items"]:
        if item["name"].lower() == name:
            return item
    # 模糊比對：搜尋詞是物品名的子字串（使用者輸入較短時）
    # 例如輸入「生髮水」可以找到「韓國生髮水」
    # 但輸入「韓國生髮水補充罐」不會匹配到「韓國生髮水」
    for item in data["items"]:
        if name in item["name"].lower():
            return item
    return None

def get_all_item_names(data):
    return [item["name"] for item in data["items"]]

def get_locations_for_item(item):
    return [loc["name"] for loc in item["locations"]]

def get_total_quantity(item):
    return sum(loc["quantity"] for loc in item["locations"])

def add_item(data, name, category, alert_threshold):
    item = {
        "id": data["next_id"],
        "name": name,
        "category": category,
        "alert_threshold": alert_threshold,
        "locations": [],
        "total": 0,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }
    data["items"].append(item)
    data["next_id"] += 1
    save_data(data)
    return item

TZ_TPE = timezone(timedelta(hours=8))  # 台灣時區 GMT+8

def parse_ts(ts_str):
    """解析交易時間字串為 aware datetime（含時區）。

    舊紀錄可能為 naive 本地時間（系統 +8），自動視為 TZ_TPE。
    """
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_TPE)
    return dt

def write_transaction(action, item_name, qty, location):
    """Append transaction record to TXN_FILE (append-only JSONL).

    儲存 UTC 時間（含時區標記），顯示時再轉 +8。
    """
    try:
        txn = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,  # "buy" or "use"
            "item": item_name,
            "qty": qty,
            "location": location or "未指定",
        }
        TXN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TXN_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(txn, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error(f"write_transaction failed: {e}")

def add_stock(item, location, quantity):
    item_name = item.get("name", "未命名")
    for loc in item["locations"]:
        if loc["name"] == location:
            loc["quantity"] += quantity
            item["updated_at"] = datetime.now().isoformat()
            write_transaction("buy", item_name, quantity, location)
            return
    item["locations"].append({"name": location, "quantity": quantity})
    item["updated_at"] = datetime.now().isoformat()
    write_transaction("buy", item_name, quantity, location)

def remove_stock(item, location, quantity):
    item_name = item.get("name", "未命名")
    for loc in item["locations"]:
        if loc["name"] == location:
            qty = min(loc["quantity"], quantity)  # 實際扣多少（防止負數）
            loc["quantity"] = max(0, loc["quantity"] - quantity)
            item["updated_at"] = datetime.now().isoformat()
            write_transaction("use", item_name, qty, location)
            return qty
    return None

def add_location_to_index(data, location):
    if location not in data["locations_index"]:
        data["locations_index"].append(location)
        # 按最近使用排序（新增的放前面）
        data["locations_index"].sort(key=lambda x: 0 if x == location else 1)

def is_alert_needed(total, threshold):
    """判斷是否需要警告（低於告警值才警告，不是 <=）"""
    return total < threshold

def get_alert_items(data):
    """取得所有需要警告的物品列表"""
    alert_items = []
    for item in data["items"]:
        total = get_total_quantity(item)
        if is_alert_needed(total, item["alert_threshold"]):
            alert_items.append((item, total))
    return alert_items

# ─── 對話狀態 ───────────────────────────────────────────
(
    STATE_BUY_CHOOSE_ITEM,      # 選物品（新物品流程）
    STATE_BUY_CHOOSE_CATEGORY,  # 選分類
    STATE_BUY_ASK_ALERT,        # 問告警值
    STATE_BUY_CHOOSE_LOCATION,  # 選位置
    STATE_USE_CHOOSE_ITEM,      # 選物品（用了流程）
    STATE_USE_CHOOSE_LOCATION,  # 選位置（用了流程）
) = range(6)

# ─── 輔助函式 ───────────────────────────────────────────
def make_keyboard(buttons, cols=2):
    """建立 inline keyboard，每列 cols 個按鈕"""
    keyboard = []
    row = []
    for i, (text, callback_data) in enumerate(buttons):
        row.append(InlineKeyboardButton(text, callback_data=callback_data))
        if len(row) == cols:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)

def get_locations_keyboard(data, item_name=None):
    """建立位置選單，最後一項是 99=新增位置"""
    buttons = []
    for i, loc in enumerate(data["locations_index"]):
        buttons.append((f"{i+1}: {loc}", f"loc_{i}"))
    buttons.append(("99: ➕ 新增位置", "loc_new"))
    return make_keyboard(buttons, cols=1)

# ─── Bot 指令 ───────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # 自動註冊 chat ID
    ids = load_chat_ids()
    if chat_id not in ids:
        ids.append(chat_id)
        save_chat_ids(ids)
        log.info(f"Registered chat_id: {chat_id}")

    await update.message.reply_text(
        "🏠 HomeBox — 家中物品庫存管理\n\n"
        "可用指令：\n"
        "買 [物品] [數量] — 增加庫存\n"
        "用 [物品] [數量] — 減少庫存\n"
        "查看 — 列出所有物品\n"
        "查看 [位置] — 列出某位置物品\n"
        "查看 [分類] — 列出某分類物品\n"
        "告警 — 列出低於告警值的物品\n"
        "修改告警 [物品] [數值] — 設定物品警告值\n"
        "紀錄 / 記錄 / 纪录 — 查看買/用歷史（支援 用 / 買 / 7 天 / 用 30 等）\n"
        "幫助 — 顯示說明\n\n"
        "也可以直接打：\n"
        "• 買 衛生紙 10\n"
        "• 用 衛生紙 5\n"
        "• 買 → 逐步選分類、物品、數量、位置\n"
        "• 用 → 逐步選分類、物品、數量、位置"
    )

def build_help_text():
    return (
        "🏠 HomeBox\n\n"
        "📦 庫存\n"
        "• 🟢 買　增加庫存\n"
        "　例：\"買 衛生紙 10\"\n\n"
        "• 🔴 用　減少庫存\n"
        "　例：\"用 維他命 1\"\n\n"
        "• 📋 查看　查看庫存\n"
        "　\"查看\"：全部\n"
        "　\"查看 分類/位置\"：指定範圍\n\n"
        "• ⚠️ 告警　查看低庫存\n\n"
        "• 📝 紀錄　查詢歷史\n"
        "　例：\n"
        "　\"紀錄\"\n"
        "　\"紀錄 用 30\"\n"
        "　\"紀錄 買 7\"\n\n"
        "────────────\n\n"
        "⚙️ 管理\n"
        "（僅支援文字輸入）\n\n"
        "📁 分類\n"
        "• 新增分類 名稱\n"
        "• 重命名分類 舊 => 新\n"
        "• 刪除分類 名稱（需無物品引用）\n\n"
        "📍 位置\n"
        "• 重命名位置 舊 => 新\n"
        "• 刪除位置 名稱（需無庫存引用）\n\n"
        "🏷️ 物品\n"
        "• 重命名物品 舊 => 新\n"
        "• 刪除物品 名稱（會再次確認）\n\n"
        "────────────\n\n"
        "💡 說明\n"
        "• \"?\" \"？\" \"help\" \"說明\"\n\n"
        "🚀 快速操作\n"
        "輸入 \"/\"\n"
        "或點擊輸入框右側 ≡\n"
        "即可選擇常用指令。"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = build_help_text()
    # 超過 4000 字分段（說明頁目前不長，但預留）
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000])
    else:
        await update.message.reply_text(text)

# ─── 語義解析 ───────────────────────────────────────────
def parse_buy_command(text):
    """解析「買 物品 數量」"""
    patterns = [
        r'(?:買|補充|加入|新增)\s+(.+?)\s+(\d+)',
        r'(?:買|補充|加入|新增)\s+(\d+)\s+(.+)',
    ]
    for pat in patterns:
        m = re.match(pat, text.strip())
        if m:
            g1, g2 = m.group(1).strip(), m.group(2).strip()
            if g1.isdigit():
                return g2, int(g1)
            elif g2.isdigit():
                return g1, int(g2)
    return None, None

def parse_history_command(text):
    """解析「紀錄」指令：紀錄/記錄/纪录 / 紀錄 用 / 紀錄 買 / 紀錄 7 / 紀錄 用 7"""
    m = re.match(r'^(?:紀錄|記錄|纪录)\s*(用|買)?\s*(\d+)?$', text.strip())
    if m:
        action = m.group(1)  # None / "用" / "買"
        days = int(m.group(2)) if m.group(2) else None
        return action, days
    return None, None


async def handle_history(update: Update, context: ContextTypes.DEFAULT_TYPE, data=None):
    """查詢交易歷史紀錄"""
    text = update.message.text.strip()
    action, days = parse_history_command(text)

    if not TXN_FILE.exists():
        await update.message.reply_text("無紀錄")
        return

    with open(TXN_FILE, "r", encoding="utf-8") as f:
        txns = [json.loads(line) for line in f if line.strip()]

    if action == "用":
        txns = [t for t in txns if t["action"] == "use"]
    elif action == "買":
        txns = [t for t in txns if t["action"] == "buy"]

    if days:
        # cutoff 用 +8 本地時間比較（aware datetime，避免與 parse_ts 回傳的 aware 時間比較崩潰）
        cutoff = datetime.now(TZ_TPE) - timedelta(days=days)
        txns = [t for t in txns if parse_ts(t["ts"]) >= cutoff]

    # 新到舊排序，取最近 50 筆
    txns.sort(key=lambda x: x["ts"], reverse=True)
    txns = txns[:50]

    if not txns:
        await update.message.reply_text("無紀錄")
        return

    # 按日期分組（新→舊），組內再分 買/用
    groups = {}  # date_str -> {"buy": [...], "use": [...]}
    for t in txns:
        ts = parse_ts(t["ts"]).astimezone(TZ_TPE)
        date_str = ts.strftime("%m-%d")
        entry = f"    {t['item']} x{t['qty']} @{t['location']}"
        groups.setdefault(date_str, {"buy": [], "use": []})
        groups[date_str]["buy" if t["action"] == "buy" else "use"].append(entry)

    SEP = "━━━━━"
    out_lines = []
    date_strs = sorted(groups.keys(), reverse=True)
    for di, date_str in enumerate(date_strs):
        if di > 0:
            out_lines.append("")  # 日期之間留一行空白
        out_lines.append(SEP)
        out_lines.append(f"📅 {date_str}")
        out_lines.append(SEP)
        for label, icon in (("買", "🛒"), ("用", "📤")):
            items = groups[date_str]["buy" if label == "買" else "use"]
            if not items:
                continue
            out_lines.append("")  # 小標題前空一行
            out_lines.append(f"  ［{icon} {label}］")
            out_lines.append("")  # 小標題後空一行
            out_lines.extend(items)
    out = "\n".join(out_lines)

    # 超過 4000 字分段發送
    if len(out) > 4000:
        chunks, current = [], ""
        for line in out.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = line
            else:
                current += "\n" + line if current else line
        if current:
            chunks.append(current)
        for chunk in chunks:
            await update.message.reply_text(chunk)
    else:
        await update.message.reply_text(out)




def parse_use_command(text):
    """解析「用/使用 物品 數量」"""
    patterns = [
        r'(?:用|用了|使用|消耗|拿走|取出)\s+(.+?)\s+(\d+)',
        r'(?:用|用了|使用|消耗|拿走|取出)\s+(\d+)\s+(.+)',
    ]
    for pat in patterns:
        m = re.match(pat, text.strip())
        if m:
            g1, g2 = m.group(1).strip(), m.group(2).strip()
            if g1.isdigit():
                return g2, int(g1)
            elif g2.isdigit():
                return g1, int(g2)
    return None, None


def parse_set_alert_command(text):
    """解析「修改告警 物品 數值」或「設定告警 物品 數值」"""
    patterns = [
        r'(?:修改告警|設定告警|告警設為)\s+(.+?)\s+(\d+)',
        r'(?:修改告警|設定告警|告警設為)\s+(\d+)\s+(.+)',
    ]
    for pat in patterns:
        m = re.match(pat, text.strip())
        if m:
            g1, g2 = m.group(1).strip(), m.group(2).strip()
            if g1.isdigit():
                return g2, int(g1)
            elif g2.isdigit():
                return g1, int(g2)
    return None, None

# ─── 管理指令處理 ──────────────────────────────────────
async def handle_add_category(update, context, data, name):
    """新增分類（批次 1）。name 可含空格。"""
    name = name.strip()
    if not name:
        await update.message.reply_text("❌ 請提供分類名稱，例如：新增分類 日用品")
        return
    if name in data["categories"]:
        await update.message.reply_text(f"❌ 分類「{name}」已存在")
        return
    data["categories"].append(name)
    save_data(data)
    await update.message.reply_text(f"✅ 已新增分類「{name}」（目前共 {len(data['categories'])} 個）")


# ─── 輔助：精確比對 ────────────────────────────────────
def get_item_by_exact_name(data, name):
    """管理指令用純精確比對，避免「刪除 衛生紙」誤刪「捲筒衛生紙」。"""
    name = name.strip().lower()
    for item in data["items"]:
        if item["name"].lower() == name:
            return item
    return None

def recalc_total(item):
    item["total"] = sum(l["quantity"] for l in item["locations"])

def unique_locations_index(data):
    """刪除物品/位置後，清掉已無任何物品引用的位置，保持索引乾淨。"""
    used = set()
    for it in data["items"]:
        for l in it["locations"]:
            used.add(l["name"])
    data["locations_index"] = [loc for loc in data["locations_index"] if loc in used]


# ─── 管理指令：重命名 ─────────────────────────────────
def _split_rename(text):
    """重命名格式 舊 => 新；回傳 (old, new) 或 None（格式錯誤）。"""
    if "=>" not in text:
        return None
    old, new = text.split("=>", 1)
    old, new = old.strip(), new.strip()
    if not old or not new:
        return None
    return old, new

async def handle_rename_category(update, context, data, text):
    parts = _split_rename(text)
    if not parts:
        await update.message.reply_text("❌ 請使用：重命名分類 舊 => 新")
        return
    old, new = parts
    if old == new:
        await update.message.reply_text("⚠️ 新名稱與舊名稱相同，未做變更")
        return
    if old not in data["categories"]:
        await update.message.reply_text(f"❌ 找不到分類「{old}」")
        return
    if new in data["categories"]:
        await update.message.reply_text(f"❌ 「{new}」已存在，無法重命名")
        return
    n = 0
    for it in data["items"]:
        if it["category"] == old:
            it["category"] = new
            it["updated_at"] = datetime.now().isoformat()
            n += 1
    data["categories"][data["categories"].index(old)] = new
    save_data(data)
    await update.message.reply_text(f"✅ 已將分類「{old}」改名為「{new}」（影響 {n} 個物品）")

async def handle_rename_location(update, context, data, text):
    parts = _split_rename(text)
    if not parts:
        await update.message.reply_text("❌ 請使用：重命名位置 舊 => 新")
        return
    old, new = parts
    if old == new:
        await update.message.reply_text("⚠️ 新名稱與舊名稱相同，未做變更")
        return
    if old not in data["locations_index"]:
        await update.message.reply_text(f"❌ 找不到位置「{old}」")
        return
    if new in data["locations_index"]:
        await update.message.reply_text(f"❌ 「{new}」已存在，無法重命名")
        return
    n = 0
    for it in data["items"]:
        for l in it["locations"]:
            if l["name"] == old:
                l["name"] = new
                n += 1
        it["updated_at"] = datetime.now().isoformat()
    data["locations_index"][data["locations_index"].index(old)] = new
    save_data(data)
    await update.message.reply_text(f"✅ 已將位置「{old}」改名為「{new}」（影響 {n} 個物品）")

async def handle_rename_item(update, context, data, text):
    parts = _split_rename(text)
    if not parts:
        await update.message.reply_text("❌ 請使用：重命名物品 舊 => 新")
        return
    old, new = parts
    if old == new:
        await update.message.reply_text("⚠️ 新名稱與舊名稱相同，未做變更")
        return
    item = get_item_by_exact_name(data, old)
    if not item:
        await update.message.reply_text(f"❌ 找不到物品「{old}」")
        return
    if get_item_by_exact_name(data, new):
        await update.message.reply_text(f"❌ 「{new}」已存在，無法重命名")
        return
    item["name"] = new
    item["updated_at"] = datetime.now().isoformat()
    save_data(data)
    await update.message.reply_text(f"✅ 已將物品「{old}」改名為「{new}」（歷史紀錄保留舊名）")


# ─── 管理指令：刪除（含 inline 二次確認）───────────────
async def handle_delete_item_request(update, context, data, name):
    item = get_item_by_exact_name(data, name)
    if not item:
        await update.message.reply_text(f"❌ 找不到物品「{name}」")
        return
    context.user_data["pending_delete"] = ("item", item["id"])
    buttons = [("✅ 確定刪除", "confirm_del_item"), ("❌ 取消", "cancel_del_item")]
    await update.message.reply_text(
        f"⚠️ 「{item['name']}」目前有 {item['total']} 個，確定刪除？",
        reply_markup=make_keyboard(buttons, cols=2)
    )

async def handle_delete_category_request(update, context, data, name):
    name = name.strip()
    if name not in data["categories"]:
        await update.message.reply_text(f"❌ 找不到分類「{name}」")
        return
    refs = [it for it in data["items"] if it.get("category") == name]
    if refs:
        await update.message.reply_text(f"⚠️ 仍有 {len(refs)} 個物品屬於「{name}」，請先改其分類或刪除它們")
        return
    context.user_data["pending_delete"] = ("category", name)
    buttons = [("✅ 確定移除", "confirm_del_category"), ("❌ 取消", "cancel_del_category")]
    await update.message.reply_text(
        f"⚠️ 確定移除分類「{name}」？",
        reply_markup=make_keyboard(buttons, cols=2)
    )

async def handle_delete_location_request(update, context, data, name):
    name = name.strip()
    if name not in data["locations_index"]:
        await update.message.reply_text(f"❌ 找不到位置「{name}」")
        return
    refs = [it for it in data["items"] if any(l["name"] == name and l["quantity"] > 0 for l in it["locations"])]
    if refs:
        await update.message.reply_text(f"⚠️ 位置「{name}」仍有 {len(refs)} 個物品有庫存，請先歸零或移走")
        return
    context.user_data["pending_delete"] = ("location", name)
    buttons = [("✅ 確定移除", "confirm_del_location"), ("❌ 取消", "cancel_del_location")]
    await update.message.reply_text(
        f"⚠️ 確定移除位置「{name}」？",
        reply_markup=make_keyboard(buttons, cols=2)
    )


# ─── 主訊息處理 ────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    data = load_data()

    # 紀錄指令（放在最前面，避免被買/用解析誤吞）；同時接受異體字 記錄/纪录
    HISTORY_PREFIXES = ("紀錄", "記錄", "纪录")
    if text.startswith(HISTORY_PREFIXES):
        await handle_history(update, context, data)
        return

    # 管理指令（非對話態才可用的文字指令，插在最前避免與買/用誤吞）
    m_admin = re.match(r'^(新增分類|加分類)\s+(.+)$', text)
    if m_admin:
        await handle_add_category(update, context, data, m_admin.group(2).strip())
        return

    m_rename = re.match(r'^(重命名分類|改分類名|重命名位置|改位置名|重命名物品|改物品名)\s+(.+)$', text)
    if m_rename:
        verb, rest = m_rename.group(1), m_rename.group(2).strip()
        if verb in ("重命名分類", "改分類名"):
            await handle_rename_category(update, context, data, rest)
        elif verb in ("重命名位置", "改位置名"):
            await handle_rename_location(update, context, data, rest)
        else:
            await handle_rename_item(update, context, data, rest)
        return

    m_del = re.match(r'^(刪除物品|移除物品|刪除分類|移除分類|刪除位置|移除位置)\s+(.+)$', text)
    if m_del:
        verb, name = m_del.group(1), m_del.group(2).strip()
        if verb in ("刪除物品", "移除物品"):
            await handle_delete_item_request(update, context, data, name)
        elif verb in ("刪除分類", "移除分類"):
            await handle_delete_category_request(update, context, data, name)
        else:
            await handle_delete_location_request(update, context, data, name)
        return

    # 單獨「買」或「用」→ 跳出分類清單
    if text in ("買", "用"):
        await show_category_picker(update, context, data, text)
        return

    # 嘗試解析為「買/補充」
    item_name, quantity = parse_buy_command(text)
    if item_name is not None and quantity is not None:
        if quantity <= 0:
            await update.message.reply_text("❌ 數量必須是正整數")
            return
        await handle_buy(update, context, data, item_name, quantity)
        return

    # 嘗試解析為「用/使用」
    item_name, quantity = parse_use_command(text)
    if item_name is not None and quantity is not None:
        if quantity <= 0:
            await update.message.reply_text("❌ 數量必須是正整數")
            return
        await handle_use(update, context, data, item_name, quantity)
        return

    # 查看指令
    if text.startswith("查看") or text == "查看":
        filter_text = text.replace("查看", "").strip()
        if not filter_text:
            # 顯示查看選單
            buttons = [
                ("📋 所有物品", "view_all"),
                ("📂 分類", "view_cat"),
                ("📍 位置", "view_loc"),
            ]
            await update.message.reply_text(
                "🔍 查看物品",
                reply_markup=make_keyboard(buttons, cols=1)
            )
            return
        else:
            await handle_list(update, context, data, filter_text)
            return

    # 告警
    if text == "告警":
        await handle_alert(update, context, data)
        return

    # 修改告警
    item_name, new_threshold = parse_set_alert_command(text)
    if item_name is not None and new_threshold is not None:
        await handle_set_alert(update, context, data, item_name, new_threshold)
        return

    # 幫助 / 說明
    if text in ("幫助", "help", "/help", "?", "？", "說明"):
        await cmd_help(update, context)
        return

    # 無法識別
    await update.message.reply_text(
        "❓ 我不理解這個指令。\n"
        "試試看：\n"
        "• 買 衛生紙 10\n"
        "• 用 衛生紙 5\n"
        "• 查看\n"
        "• 告警\n"
        "• 打 ? 或 help 看完整指令說明"
    )

# ─── 分類/物品選擇流程 ─────────────────────────────────
async def show_category_picker(update, context, data, mode):
    """顯示分類清單，讓使用者選分類"""
    # mode: "買" 或 "用"
    categories = data.get("categories", [])
    if not categories:
        await update.message.reply_text("❌ 沒有任何分類，請先新增分類")
        return

    context.user_data["mode"] = mode  # 記錄是買還是用

    buttons = [(f"📂 {cat}", f"pickcat_{i}") for i, cat in enumerate(categories)]
    # ponytail: 只有「買」模式才顯示新增物品按鈕
    if mode == "買":
        buttons.append(("🆕 新增物品（不分類）", "pickcat_new"))

    emoji = "🛒" if mode == "買" else "📤"
    await update.message.reply_text(
        f"{emoji} 選擇分類：",
        reply_markup=make_keyboard(buttons, cols=1)
    )


async def show_item_picker(update, context, data, category_idx):
    """顯示該分類下的物品清單"""
    mode = context.user_data.get("mode", "買")
    categories = data.get("categories", [])

    if category_idx == -1:
        # 新增物品模式
        context.user_data["pick_new_item"] = True
        await update.message.reply_text(
            "🆕 請輸入新物品名稱："
        )
        context.user_data["pick_awaiting_name"] = True
        return

    cat_name = categories[category_idx]
    items_in_cat = [i for i in data["items"] if i.get("category") == cat_name]

    if not items_in_cat:
        await update.message.reply_text(
            f"📂 {cat_name} 沒有任何物品\n"
            f"請先新增物品"
        )
        return

    context.user_data["pick_category"] = cat_name

    buttons = []
    for i, item in enumerate(items_in_cat):
        total = get_total_quantity(item)
        label = f"{item['name']}（現有 {total}）"
        if mode == "用" and total == 0:
            label += " ⚠️無庫存"
        buttons.append((label, f"pickitem_{i}"))
    # ponytail: 只有「買」模式才顯示新增物品按鈕
    if mode == "買":
        buttons.append(("🆕 新增物品", "pickcat_new"))

    emoji = "🛒" if mode == "買" else "📤"
    await update.message.reply_text(
        f"{emoji} {cat_name} 選擇物品：",
        reply_markup=make_keyboard(buttons, cols=1)
    )


async def handle_pick_item(update, context, data, item_idx):
    """使用者選了物品後，問數量"""
    mode = context.user_data.get("mode", "買")
    cat_name = context.user_data.get("pick_category")
    items_in_cat = [i for i in data["items"] if i.get("category") == cat_name]
    item = items_in_cat[item_idx]

    # ponytail: 「用」模式檢查庫存
    if mode == "用":
        total = get_total_quantity(item)
        if total == 0:
            await update.message.reply_text(f"❌ {item['name']} 目前沒有任何庫存，請先補充")
            context.user_data.clear()
            return

    context.user_data["pick_item_id"] = item["id"]
    context.user_data["pick_item_name"] = item["name"]

    await update.message.reply_text(
        f"📦 {item['name']}（現有 {get_total_quantity(item)}）\n"
        f"請輸入數量："
    )
    context.user_data["pick_awaiting_quantity"] = True


async def handle_pick_quantity(update, context, data, quantity):
    """使用者輸入數量後，根據 mode 走買或用流程"""
    mode = context.user_data.get("mode", "買")
    item_id = context.user_data.get("pick_item_id")
    item_name = context.user_data.get("pick_item_name")

    import logging
    logging.info(f"handle_pick_quantity: mode={mode}, item_id={item_id}, item_name={item_name}, quantity={quantity}, user_data={context.user_data}")

    if mode == "買":
        context.user_data.pop("pick_awaiting_quantity", None)
        is_new = context.user_data.get("pick_new_item", False)

        if is_new:
            # 新物品 → 先問告警值，再建立物品選位置
            if "buy_alert_threshold" not in context.user_data:
                context.user_data["buy_awaiting_alert"] = True
                context.user_data["buy_quantity"] = quantity
                await update.message.reply_text(
                    f"🆕 {context.user_data.get('buy_new_item_name', item_name)} | 數量：{quantity}\n"
                    f"請輸入告警值（低於此數量時提醒，預設 3）："
                )
                return
            alert = context.user_data.pop("buy_alert_threshold", 3)
            item_name = context.user_data["buy_new_item_name"]
            category = context.user_data.get("buy_category", "未分類")
            item = add_item(data, item_name, category, alert)
            item_id = item["id"]
            context.user_data["buy_item_id"] = item_id
            context.user_data["buy_quantity"] = quantity
            context.user_data["buy_new_item"] = True
            context.user_data.pop("buy_awaiting_alert", None)
            save_data(data)
            # 繼續往下走選位置
        else:
            # 既有物品
            item_id = context.user_data.get("pick_item_id")
            context.user_data["buy_item_id"] = item_id
            context.user_data["buy_quantity"] = quantity
            context.user_data["buy_new_item"] = False
            item = next(i for i in data["items"] if i["id"] == item_id)

        all_locations = data.get("locations_index", [])
        item_name_display = item["name"]

        if not all_locations:
            await update.message.reply_text(
                f"📦 {item_name_display} | 新增 {quantity}\n"
                f"請輸入存放位置："
            )
            context.user_data["buy_awaiting_new_location"] = True
            return

        buttons = []
        for i, loc in enumerate(all_locations):
            qty_in_loc = next((l["quantity"] for l in item["locations"] if l["name"] == loc), 0)
            buttons.append((f"{i+1}: {loc}（現有 {qty_in_loc}）", f"loc_{i}"))
        buttons.append(("99: ➕ 新增位置", "loc_new"))

        await update.message.reply_text(
            f"📦 {item_name_display} | 新增 {quantity}\n"
            f"要放在哪裡？",
            reply_markup=make_keyboard(buttons, cols=1)
        )

    elif mode == "用":
        # 用流程：檢查庫存，選位置
        item = next(i for i in data["items"] if i["id"] == item_id)
        total = get_total_quantity(item)

        if total == 0:
            await update.message.reply_text(
                f"❌ {item_name} 目前沒有任何庫存\n"
                f"請先補充庫存再使用"
            )
            context.user_data.clear()
            return
        if total < quantity:
            await update.message.reply_text(
                f"⚠️ {item_name} 目前只有 {total}，不夠用 {quantity}\n"
                f"請先買一些再用，或確認正確使用數量"
            )
            context.user_data.clear()
            return

        context.user_data["use_item_id"] = item_id
        context.user_data["use_quantity"] = quantity
        context.user_data.pop("pick_awaiting_quantity", None)

        available_locs = [l["name"] for l in item["locations"] if l["quantity"] > 0]
        if not available_locs:
            await update.message.reply_text(f"❌ {item_name} 沒有任何位置的庫存")
            context.user_data.clear()
            return

        buttons = []
        for i, loc in enumerate(available_locs):
            loc_qty = next(l["quantity"] for l in item["locations"] if l["name"] == loc)
            buttons.append((f"{i+1}: {loc}（現有 {loc_qty}）", f"useloc_{i}"))

        await update.message.reply_text(
            f"📤 {item_name}（現有 {total}）\n"
            f"要用 {quantity}，從哪個位置扣？",
            reply_markup=make_keyboard(buttons, cols=1)
        )


async def handle_buy_other_location(query, context, data):
    """買完一個位置後，問是否其他位置也要新增同樣物品"""
    item_id = context.user_data.get("buy_item_id")
    item = next((i for i in data["items"] if i["id"] == item_id), None)
    if not item:
        context.user_data.clear()
        return

    total = get_total_quantity(item)
    buttons = [
        ("📍 是，繼續新增位置", "buy_more_loc"),
        ("✅ 完成", "buy_done"),
    ]
    await query.edit_message_text(
        f"✅ {item['name']} 已新增\n"
        f"總數：{total}\n\n"
        f"要繼續新增到其他位置嗎？",
        reply_markup=make_keyboard(buttons, cols=1)
    )


# ─── 買了/補充流程 ─────────────────────────────────────
async def handle_buy(update, context, data, item_name, quantity):
    item = get_item_by_name(data, item_name)

    if item:
        # 既有物品 → 選位置
        context.user_data["buy_item_id"] = item["id"]
        context.user_data["buy_quantity"] = quantity
        context.user_data["buy_new_item"] = False

        locations = get_locations_for_item(item)
        all_locations = data["locations_index"]

        if not all_locations:
            # 沒有位置過 → 直接問新位置
            await update.message.reply_text(
                f"📦 {item_name}（現有 {get_total_quantity(item)}）\n"
                f"新增 {quantity}，請輸入存放位置："
            )
            context.user_data["buy_awaiting_new_location"] = True
            return

        # 顯示位置選單
        buttons = []
        for i, loc in enumerate(all_locations):
            qty_in_loc = next((l["quantity"] for l in item["locations"] if l["name"] == loc), 0)
            buttons.append((f"{i+1}: {loc}（現有 {qty_in_loc}）", f"loc_{i}"))
        buttons.append(("99: ➕ 新增位置", "loc_new"))

        await update.message.reply_text(
            f"📦 {item_name}（現有 {get_total_quantity(item)}）\n"
            f"新增 {quantity}，要放在哪裡？",
            reply_markup=make_keyboard(buttons, cols=1)
        )
    else:
        # 新物品 → 選分類
        context.user_data["buy_new_item_name"] = item_name
        context.user_data["buy_quantity"] = quantity
        context.user_data["buy_new_item"] = True

        buttons = [(f"{i+1}: {cat}", f"cat_{i}") for i, cat in enumerate(data["categories"])]
        await update.message.reply_text(
            f"🆕 新物品：{item_name}\n"
            f"數量：{quantity}\n"
            "請選擇分類：",
            reply_markup=make_keyboard(buttons, cols=1)
        )

# ─── 用了流程 ──────────────────────────────────────────
async def handle_use(update, context, data, item_name, quantity):
    item = get_item_by_name(data, item_name)

    if not item:
        await update.message.reply_text(f"❌ 找不到物品「{item_name}」")
        return

    total = get_total_quantity(item)
    if total == 0:
        await update.message.reply_text(
            f"❌ {item_name} 目前沒有任何庫存\n"
            f"請先補充庫存再使用"
        )
        return
    if total < quantity:
        await update.message.reply_text(
            f"⚠️ {item_name} 目前只有 {total}，不夠用 {quantity}\n"
            f"請先買一些再用，或確認正確使用數量"
        )
        return

    context.user_data["use_item_id"] = item["id"]
    context.user_data["use_quantity"] = quantity

    # ponytail: 列出所有有庫存的位置，讓使用者選
    available_locs = []
    for loc in item["locations"]:
        if loc["quantity"] > 0:
            available_locs.append(loc["name"])

    if not available_locs:
        await update.message.reply_text(f"❌ {item_name} 沒有任何位置的庫存")
        return

    buttons = []
    for i, loc_name in enumerate(available_locs):
        qty = next(l["quantity"] for l in item["locations"] if l["name"] == loc_name)
        buttons.append((f"{i+1}: {loc_name}（現有 {qty}）", f"useloc_{i}"))

    await update.message.reply_text(
        f"📦 {item_name}（總共 {total}）\n"
        f"要用 {quantity}，從哪個位置扣？",
        reply_markup=make_keyboard(buttons, cols=1)
    )

# ─── 查看功能 ───────────────────────────────────────────
async def show_items_list(update, context, data, items, header, grouped=False):
    """顯示物品列表（共用函式）"""
    if not items:
        text = f"{header}\n（沒有物品）"
    elif grouped:
        # 分組顯示：依分類分隔
        groups = {}
        for item in items:
            cat = item.get("category", "未分類")
            if cat not in groups:
                groups[cat] = []
            groups[cat].append(item)
        
        lines = [header + "\n"]
        # 按照 data["categories"] 順序顯示
        for cat in data.get("categories", []) + ["未分類"]:
            if cat in groups:
                lines.append(f"\n{cat_emoji(cat)} {cat}\n")
                for item in groups[cat]:
                    total = get_total_quantity(item)
                    alert = " ⚠️" if is_alert_needed(total, item["alert_threshold"]) else ""
                    locs = ", ".join(f"{loc_emoji(l['name'])}{l['name']}({l['quantity']})" for l in item["locations"] if l["quantity"] > 0)
                    lines.append(f"  • {item['name']} | {total}{alert}")
                    if locs:
                        lines.append(f"    📍 {locs}")
        text = "\n".join(lines).strip()
    else:
        # 現有平坦格式
        lines = [f"{header}\n"]
        for item in items:
            total = get_total_quantity(item)
            alert = " ⚠️" if is_alert_needed(total, item["alert_threshold"]) else ""
            cat_em = cat_emoji(item["category"])
            locs = ", ".join(f"{loc_emoji(l['name'])}{l['name']}({l['quantity']})" for l in item["locations"] if l["quantity"] > 0)
            lines.append(f"{cat_em} {item['name']} | 共{total}{alert}")
            if locs:
                lines.append(f"  📍 {locs}")
        text = "\n".join(lines)

    # 如果訊息太長，分段發送
    if len(text) > 4000:
        chunks = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = line
            else:
                current += "\n" + line if current else line
        if current:
            chunks.append(current)
        for chunk in chunks:
            await update.effective_message.reply_text(chunk)
    else:
        await update.effective_message.reply_text(text)

async def handle_list(update, context, data, filter_text):
    items = data["items"]

    if filter_text:
        # 檢查是否是位置
        location_items = [it for it in items if any(l["name"] == filter_text for l in it["locations"])]
        # 檢查是否是分類
        category_items = [it for it in items if it["category"] == filter_text]

        if location_items:
            items = location_items
            header = f"📍 位置：{filter_text}"
        elif category_items:
            items = category_items
            header = f"📂 分類：{filter_text}"
        else:
            await update.message.reply_text(f"❌ 找不到位置或分類「{filter_text}」")
            return
        await show_items_list(update, context, data, items, header)
    else:
        header = "📋 所有物品"
        # 全部物品使用分組顯示
        await show_items_list(update, context, data, items, header, grouped=True)
        return
async def handle_alert(update, context, data):
    alert_items = get_alert_items(data)

    if not alert_items:
        await update.message.reply_text("✅ 所有物品庫存充足")
        return

    # 分組顯示
    groups = {}
    for item, total in alert_items:
        cat = item.get("category", "未分類")
        if cat not in groups:
            groups[cat] = []
        groups[cat].append((item, total))

    lines = ["⚠️ 以下物品庫存偏低：\n"]
    for cat in data.get("categories", []) + ["未分類"]:
        if cat in groups:
            lines.append(f"\n{cat_emoji(cat)} {cat}\n")
            for item, total in groups[cat]:
                lines.append(f"  • {item['name']}：{total}（告警值 {item['alert_threshold']}）")

    await update.message.reply_text("\n".join(lines))

async def handle_set_alert(update: Update, context, data, item_name, new_threshold):
    """修改物品的告警值"""
    item = get_item_by_name(data, item_name)
    if not item:
        await update.message.reply_text(f"❌ 找不到物品「{item_name}」")
        return
    
    if new_threshold < 0:
        await update.message.reply_text("❌ 告警值必須是非負整數")
        return
    
    old_threshold = item["alert_threshold"]
    item["alert_threshold"] = new_threshold
    item["updated_at"] = datetime.now().isoformat()
    save_data(data)
    
    total = get_total_quantity(item)
    emoji = "⚠️" if is_alert_needed(total, new_threshold) else "✅"
    await update.message.reply_text(
        f"✅ {item['name']} 告警值已修改\n"
        f"由 {old_threshold} → {new_threshold}\n"
        f"目前庫存：{total} {emoji}"
    )

# ─── Callback 處理 ─────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = load_data()
    cb = query.data

    # 查看選單
    if cb == "view_all":
        await show_items_list(update, context, data, data["items"], "📋 所有物品", grouped=True)
        return

    if cb == "view_cat":
        buttons = [(f"{cat_emoji(cat)} {cat}", f"viewcat_{cat}") for cat in data["categories"]]
        buttons.append(("🔙 返回", "view_back"))
        await query.edit_message_text(
            "📂 選擇分類：",
            reply_markup=make_keyboard(buttons, cols=1)
        )
        return

    if cb.startswith("viewcat_"):
        cat = cb.replace("viewcat_", "")
        items = [it for it in data["items"] if it["category"] == cat]
        await show_items_list(update, context, data, items, f"{cat_emoji(cat)} 分類：{cat}")
        return

    if cb == "view_loc":
        if not data["locations_index"]:
            await query.edit_message_text("📍 目前沒有任何位置")
            return
        buttons = [(f"{loc_emoji(loc)} {loc}", f"viewloc_{loc}") for loc in data["locations_index"]]
        buttons.append(("🔙 返回", "view_back"))
        await query.edit_message_text(
            "📍 選擇位置：",
            reply_markup=make_keyboard(buttons, cols=1)
        )
        return

    if cb.startswith("viewloc_"):
        loc = cb.replace("viewloc_", "")
        items = [it for it in data["items"] if any(l["name"] == loc for l in it["locations"])]
        await show_items_list(update, context, data, items, f"{loc_emoji(loc)} 位置：{loc}")
        return

    if cb == "view_back":
        buttons = [
            ("📋 所有物品", "view_all"),
            ("🗂️ 分類", "view_cat"),
            ("📍 位置", "view_loc"),
        ]
        await query.edit_message_text(
            "🔍 查看物品",
            reply_markup=make_keyboard(buttons, cols=1)
        )
        return

    # 管理指令二次確認（刪除物品/分類/位置）
    if cb in ("confirm_del_item", "cancel_del_item", "confirm_del_category", "cancel_del_category", "confirm_del_location", "cancel_del_location"):
        pending = context.user_data.get("pending_delete")
        if not pending:
            await query.edit_message_text("❌ 找不到待確認的動作，可能已過期")
            return
        kind, target = pending
        if cb.startswith("cancel_"):
            context.user_data.pop("pending_delete", None)
            await query.edit_message_text("❌ 已取消刪除")
            return
        # 確認：重新校驗目標仍存在（防並發/過期）
        if kind == "item":
            item = next((it for it in data["items"] if it["id"] == target), None)
            if not item:
                context.user_data.pop("pending_delete", None)
                await query.edit_message_text("❌ 物品已不存在")
                return
            data["items"].remove(item)
            unique_locations_index(data)
            save_data(data)
            context.user_data.pop("pending_delete", None)
            await query.edit_message_text(f"✅ 已刪除物品「{item['name']}」")
        elif kind == "category":
            if target not in data["categories"]:
                context.user_data.pop("pending_delete", None)
                await query.edit_message_text("❌ 分類已不存在")
                return
            refs = [it for it in data["items"] if it.get("category") == target]
            if refs:
                context.user_data.pop("pending_delete", None)
                await query.edit_message_text(f"⚠️ 仍有 {len(refs)} 個物品屬於「{target}」，無法刪除")
                return
            data["categories"].remove(target)
            save_data(data)
            context.user_data.pop("pending_delete", None)
            await query.edit_message_text(f"✅ 已移除分類「{target}」")
        elif kind == "location":
            if target not in data["locations_index"]:
                context.user_data.pop("pending_delete", None)
                await query.edit_message_text("❌ 位置已不存在")
                return
            refs = [it for it in data["items"] if any(l["name"] == target and l["quantity"] > 0 for l in it["locations"])]
            if refs:
                context.user_data.pop("pending_delete", None)
                await query.edit_message_text(f"⚠️ 位置「{target}」仍有 {len(refs)} 個物品有庫存，無法刪除")
                return
            for it in data["items"]:
                it["locations"] = [l for l in it["locations"] if l["name"] != target]
                recalc_total(it)
                it["updated_at"] = datetime.now().isoformat()
            data["locations_index"].remove(target)
            save_data(data)
            context.user_data.pop("pending_delete", None)
            await query.edit_message_text(f"✅ 已移除位置「{target}」")
        return

    # 選分類（新物品）
    if cb.startswith("cat_"):
        cat_idx = int(cb.split("_")[1])
        category = data["categories"][cat_idx]
        context.user_data["buy_category"] = category
        item_name = context.user_data["buy_new_item_name"]
        quantity = context.user_data["buy_quantity"]

        await query.edit_message_text(
            f"🆕 {item_name} | {quantity} | {category}\n"
            f"請輸入低於多少要告警（純數字）："
        )
        context.user_data["buy_awaiting_alert"] = True
        return

    # 選位置（買了）
    if cb.startswith("loc_"):
        if cb == "loc_new":
            await query.edit_message_text("請輸入新位置名稱：")
            context.user_data["buy_awaiting_new_location"] = True
            return

        loc_idx = int(cb.split("_")[1])
        location = data["locations_index"][loc_idx]

        # ponytail: 如果是從「繼續新增位置」來的，buy_quantity 已被清除，需先問數量
        quantity = context.user_data.get("buy_quantity")
        if quantity is None:
            # 選位置後等數量
            context.user_data["buy_selected_location"] = location
            context.user_data["buy_awaiting_loc_quantity"] = True
            await query.edit_message_text(
                f"📦 {location} 要放多少？\n"
                f"（輸入數量）"
            )
            return

        if context.user_data.get("buy_new_item"):
            # 新物品 → 先建立物品
            item_name = context.user_data["buy_new_item_name"]
            category = context.user_data["buy_category"]
            alert = context.user_data.get("buy_alert_threshold", 3)
            item = add_item(data, item_name, category, alert)
            add_stock(item, location, quantity)
            add_location_to_index(data, location)
            save_data(data)
            total = get_total_quantity(item)
            await query.edit_message_text(
                f"✅ 已新增 {item_name} {quantity} → {location}\n"
                f"分類：{category} | 總數：{total} | 告警值：{alert}"
            )
        else:
            # 既有物品
            item_id = context.user_data["buy_item_id"]
            item = next(i for i in data["items"] if i["id"] == item_id)
            add_stock(item, location, quantity)
            add_location_to_index(data, location)
            save_data(data)
            total = get_total_quantity(item)
            await query.edit_message_text(
                f"✅ {item['name']} +{quantity} → {location}\n"
                f"總數：{total}"
            )

        # 買完問是否其他位置也要新增
        await handle_buy_other_location(query, context, data)
        return

    # 新物品選分類（從分類清單新增物品後）
    if cb.startswith("buycat_"):
        cat_idx = int(cb.split("_")[1])
        category = data["categories"][cat_idx]
        context.user_data["buy_category"] = category
        item_name = context.user_data["buy_new_item_name"]
        await query.edit_message_text(
            f"🆕 {item_name} | 分類：{category}\n請輸入數量："
        )
        context.user_data["pick_awaiting_quantity"] = True
        return

    # 分類選擇（買/用）
    if cb.startswith("pickcat_"):
        if cb == "pickcat_new":
            await show_item_picker(query, context, data, -1)
            return
        cat_idx = int(cb.split("_")[1])
        await show_item_picker(query, context, data, cat_idx)
        return

    # 物品選擇（買/用）
    if cb.startswith("pickitem_"):
        item_idx = int(cb.split("_")[1])
        await handle_pick_item(query, context, data, item_idx)
        return

    # 買：繼續新增位置
    if cb == "buy_more_loc":
        item_id = context.user_data.get("buy_item_id")
        item = next((i for i in data["items"] if i["id"] == item_id), None)
        if not item:
            context.user_data.clear()
            return
        all_locations = data.get("locations_index", [])
        buttons = []
        for i, loc in enumerate(all_locations):
            qty_in_loc = next((l["quantity"] for l in item["locations"] if l["name"] == loc), 0)
            buttons.append((f"{i+1}: {loc}（現有 {qty_in_loc}）", f"loc_{i}"))
        buttons.append(("99: ➕ 新增位置", "loc_new"))
        buttons.append(("✅ 完成", "buy_done"))
        await query.edit_message_text(
            f"📦 {item['name']} 還要放哪裡？\n"
            f"（請選位置，選完後輸入數量）",
            reply_markup=make_keyboard(buttons, cols=1)
        )
        # ponytail: 清除 buy_quantity，選位置後重新問數量
        context.user_data.pop("buy_quantity", None)
        return

    # 買：完成
    if cb == "buy_done":
        item_id = context.user_data.get("buy_item_id")
        item = next((i for i in data["items"] if i["id"] == item_id), None)
        total = get_total_quantity(item) if item else 0
        name = item["name"] if item else ""
        await query.edit_message_text(f"✅ {name} 新增完成，總數：{total}")
        context.user_data.clear()
        return

    # 取消補扣（必須在 useloc_ 之前）
    if cb == "useloc_cancel":
        item_id = context.user_data.get("use_item_id")
        item = next((i for i in data["items"] if i["id"] == item_id), None)
        total = get_total_quantity(item) if item else 0
        await query.edit_message_text(f"✋ 已取消，{item['name'] if item else ''} 總數：{total}")
        context.user_data.clear()
        return

    # 選位置（用了）— 第二輪（補扣）
    if cb.startswith("useloc2_"):
        loc_idx = int(cb.split("_")[1])
        item_id = context.user_data["use_item_id"]
        quantity = context.user_data["use_quantity"]
        item = next(i for i in data["items"] if i["id"] == item_id)

        remaining_locs = [l for l in item["locations"] if l["quantity"] > 0]
        location = remaining_locs[loc_idx]["name"]
        loc_qty = remaining_locs[loc_idx]["quantity"]

        if loc_qty >= quantity:
            remove_stock(item, location, quantity)
            save_data(data)
            total = get_total_quantity(item)
            remaining = loc_qty - quantity
            msg = f"✅ {item['name']} -{quantity} ← {location}\n該位置剩餘：{remaining} | 總數：{total}"
            if is_alert_needed(total, item["alert_threshold"]):
                msg += f"\n⚠️ 已低於告警值（{item['alert_threshold']}）"
            await query.edit_message_text(msg)
            context.user_data.clear()
        else:
            # 還是不夠 → 繼續問
            shortfall = quantity - loc_qty
            remove_stock(item, location, loc_qty)
            save_data(data)
            total = get_total_quantity(item)

            remaining_locs2 = [l for l in item["locations"] if l["quantity"] > 0]
            if remaining_locs2 and shortfall > 0:
                context.user_data["use_quantity"] = shortfall
                buttons = []
                for i, loc in enumerate(remaining_locs2):
                    buttons.append(
                        (f"{i+1}: {loc['name']}（現有 {loc['quantity']}）", f"useloc2_{i}")
                    )
                buttons.append(("✋ 夠了，不再扣", "useloc_cancel"))
                await query.edit_message_text(
                    f"⚠️ {location} 只有 {loc_qty}，已扣完\n"
                    f"還差 {shortfall}，要從哪裡繼續扣？\n"
                    f"（總數剩餘：{total}）",
                    reply_markup=make_keyboard(buttons, cols=1)
                )
            else:
                await query.edit_message_text(
                    f"⚠️ {location} 只有 {loc_qty}，已扣完\n"
                    f"沒有其他位置的庫存了\n"
                    f"實際扣了 {loc_qty}（原定 {quantity}）| 總數：{total}"
                )
                context.user_data.clear()
        return

    # 選位置（用了）— 第一輪
    if cb.startswith("useloc_"):
        loc_idx = int(cb.split("_")[1])
        item_id = context.user_data["use_item_id"]
        quantity = context.user_data["use_quantity"]
        item = next(i for i in data["items"] if i["id"] == item_id)

        # ponytail: 重新取得有庫存的位置（順序跟按鈕一致）
        available_locs = [l["name"] for l in item["locations"] if l["quantity"] > 0]
        location = available_locs[loc_idx]
        loc_qty = next(l["quantity"] for l in item["locations"] if l["name"] == location)

        if loc_qty >= quantity:
            # 該位置夠扣 → 直接扣
            remove_stock(item, location, quantity)
            save_data(data)
            total = get_total_quantity(item)
            remaining = loc_qty - quantity
            msg = f"✅ {item['name']} -{quantity} ← {location}\n該位置剩餘：{remaining} | 總數：{total}"
            if is_alert_needed(total, item["alert_threshold"]):
                msg += f"\n⚠️ 已低於告警值（{item['alert_threshold']}）"
            await query.edit_message_text(msg)
            context.user_data.clear()
        else:
            # 該位置不夠扣 → 先扣完，再問剩下要從哪裡扣
            shortfall = quantity - loc_qty
            remove_stock(item, location, loc_qty)  # 扣完該位置
            save_data(data)
            total = get_total_quantity(item)

            # 重新取得剩餘有庫存的位置（排除已扣完的）
            remaining_locs = [l for l in item["locations"] if l["quantity"] > 0]
            if remaining_locs and shortfall > 0:
                # 還有其他位置有庫存 → 問使用者
                context.user_data["use_quantity"] = shortfall
                context.user_data["use_item_id"] = item["id"]

                buttons = []
                for i, loc in enumerate(remaining_locs):
                    buttons.append(
                        (f"{i+1}: {loc['name']}（現有 {loc['quantity']}）", f"useloc2_{i}")
                    )
                buttons.append(("✋ 夠了，不再扣", "useloc_cancel"))

                await query.edit_message_text(
                    f"⚠️ {location} 只有 {loc_qty}，已扣完\n"
                    f"還差 {shortfall}，要從哪裡繼續扣？\n"
                    f"（總數剩餘：{total}）",
                    reply_markup=make_keyboard(buttons, cols=1)
                )
            else:
                # 沒有其他位置有庫存 → 扣多少算多少
                await query.edit_message_text(
                    f"⚠️ {location} 只有 {loc_qty}，已扣完\n"
                    f"沒有其他位置的庫存了\n"
                    f"實際扣了 {loc_qty}（原定 {quantity}）| 總數：{total}"
                )
                context.user_data.clear()
        return

# ─── 處理純文字輸入（新位置、告警值等）─────────────────
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    data = load_data()

    # 等待數量（選位置後輸入數量 — 繼續新增位置用）
    if context.user_data.get("buy_awaiting_loc_quantity"):
        context.user_data.pop("buy_awaiting_loc_quantity")
        try:
            quantity = int(text)
            if quantity <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ 請輸入正整數：")
            context.user_data["buy_awaiting_loc_quantity"] = True
            return
        location = context.user_data.pop("buy_selected_location")
        item_id = context.user_data.get("buy_item_id")
        item = next((i for i in data["items"] if i["id"] == item_id), None)
        if not item:
            context.user_data.clear()
            await update.message.reply_text("❌ 找不到物品")
            return
        add_stock(item, location, quantity)
        add_location_to_index(data, location)
        save_data(data)
        total = get_total_quantity(item)
        # 問是否繼續新增到其他位置
        buttons = [
            ("📍 是，繼續新增位置", "buy_more_loc"),
            ("✅ 完成", "buy_done"),
        ]
        await update.message.reply_text(
            f"✅ {item['name']} +{quantity} → {location}\n"
            f"總數：{total}\n\n"
            f"要繼續新增到其他位置嗎？",
            reply_markup=make_keyboard(buttons, cols=1)
        )
        return

    # 等待新物品名稱（從分類清單選「新增物品」後）
    if context.user_data.get("pick_awaiting_name"):
        context.user_data.pop("pick_awaiting_name")
        item_name = text.strip()
        # 檢查是否已存在
        existing = get_item_by_name(data, item_name)
        if existing:
            # 已存在 → 直接進入數量
            context.user_data["pick_item_id"] = existing["id"]
            context.user_data["pick_item_name"] = existing["name"]
            context.user_data["pick_new_item"] = False
        else:
            # 新物品 → 先問分類
            context.user_data["buy_new_item_name"] = item_name
            context.user_data["pick_new_item"] = True
            categories = data.get("categories", [])
            if categories:
                buttons = [(f"{i+1}: {cat}", f"buycat_{i}") for i, cat in enumerate(categories)]
                await update.message.reply_text(
                    f"🆕 新物品：{item_name}\n選擇分類：",
                    reply_markup=make_keyboard(buttons, cols=1)
                )
                return
            else:
                # 沒分類 → 直接問數量
                context.user_data["buy_category"] = "未分類"
                context.user_data["pick_new_item"] = True
                await update.message.reply_text(
                    f"🆕 {item_name}\n請輸入數量："
                )
                context.user_data["pick_awaiting_quantity"] = True
                return
        await update.message.reply_text(
            f"📦 {item_name}（現有 {get_total_quantity(existing) if existing else 0}）\n請輸入數量："
        )
        context.user_data["pick_awaiting_quantity"] = True
        return

    # 等待數量（從物品清單選了物品後）
    if context.user_data.get("pick_awaiting_quantity"):
        context.user_data.pop("pick_awaiting_quantity")
        try:
            quantity = int(text)
            if quantity <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ 請輸入正整數：")
            context.user_data["pick_awaiting_quantity"] = True
            return
        await handle_pick_quantity(update, context, data, quantity)
        return

    # 等待新位置
    if context.user_data.get("buy_awaiting_new_location"):
        location = text
        quantity = context.user_data["buy_quantity"]
        add_location_to_index(data, location)

        if context.user_data.get("buy_new_item"):
            item_name = context.user_data["buy_new_item_name"]
            category = context.user_data["buy_category"]
            alert = context.user_data.get("buy_alert_threshold", 3)
            item = add_item(data, item_name, category, alert)
            add_stock(item, location, quantity)
            save_data(data)
            total = get_total_quantity(item)
            await update.message.reply_text(
                f"✅ 已新增 {item_name} {quantity} → {location}\n"
                f"分類：{category} | 總數：{total} | 告警值：{alert}"
            )
        else:
            item_id = context.user_data["buy_item_id"]
            item = next(i for i in data["items"] if i["id"] == item_id)
            add_stock(item, location, quantity)
            save_data(data)
            total = get_total_quantity(item)
            await update.message.reply_text(
                f"✅ {item['name']} +{quantity} → {location}\n"
                f"總數：{total}"
            )

        context.user_data["buy_awaiting_new_location"] = False
        context.user_data.clear()
        return

    # 等待告警值
    if context.user_data.get("buy_awaiting_alert"):
        try:
            alert = int(text)
            if alert < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ 請輸入正整數：")
            return

        context.user_data["buy_alert_threshold"] = alert
        context.user_data["buy_awaiting_alert"] = False

        # 建立物品
        item_name = context.user_data["buy_new_item_name"]
        quantity = context.user_data["buy_quantity"]
        category = context.user_data.get("buy_category", "未分類")
        item = add_item(data, item_name, category, alert)
        save_data(data)

        context.user_data["buy_item_id"] = item["id"]
        context.user_data["buy_new_item"] = False  # ponytail: 物品已建立，選位置時不再重複建立

        # 顯示位置選單
        if not data["locations_index"]:
            await update.message.reply_text(
                f"🆕 {item_name} +{quantity} | {category} | 告警值：{alert}\n"
                f"請輸入存放位置："
            )
            context.user_data["buy_awaiting_new_location"] = True
            return

        buttons = []
        for i, loc in enumerate(data["locations_index"]):
            buttons.append((f"{i+1}: {loc}", f"loc_{i}"))
        buttons.append(("99: ➕ 新增位置", "loc_new"))

        await update.message.reply_text(
            f"🆕 {item_name} +{quantity} | {category} | 告警值：{alert}\n"
            f"要放在哪裡？",
            reply_markup=make_keyboard(buttons, cols=1)
        )
        return

    # 無法識別 → 交給主處理
    await handle_message(update, context)

# ─── 指令定義（說明頁與 Telegram /指令 註冊共用，防漂移）──────────
# Telegram BotCommand 只收 ASCII，故 menu 用 slug；中文走文字解析層。
COMMANDS = [
    # (ascii_slug, 中文文字指令, 說明, 格式範例)
    ("buy",  "買",   "增加庫存",        "買 衛生紙 10 / 買了 維他命 3"),
    ("use",  "用",   "減少庫存",        "用 衛生紙 5 / 取出 維他命 1"),
    ("view", "查看", "列出物品（無參=分類分組；加分類/位置名=過濾）", "查看 / 查看 保健品"),
    ("alert", "告警", "列出低於告警值的物品", "告警"),
    ("log",  "紀錄", "紀錄：買/用歷史（可加 用/買/天數，如 紀錄 用 30，異體字 記錄/纪录）", "紀錄 / 紀錄 用 30"),
    ("help", "說明", "顯示本說明頁",    "? / help"),
]

async def register_commands(application):
    """啟動時註冊 Telegram ≡ 選單指令（ASCII slug）。失敗只 log 不阻擋。"""
    try:
        from telegram import BotCommand
        await application.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, _, desc, _ in COMMANDS]
        )
        log.info("Registered Telegram commands menu")
    except Exception as e:
        log.warning(f"set_my_commands failed (non-fatal): {e}")


# ─── 主程式 ─────────────────────────────────────────────
def main():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).post_init(register_commands).build()

    # 處理 callback（要在 MessageHandler 之前）
    app.add_handler(CallbackQueryHandler(handle_callback))

    # 文字訊息
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    # 指令：/start /help 直接對應；ASCII slug 導向文字解析層（等同打中文）
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    async def _slug_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # /buy /use /view /alert /log 轉成對應中文文字指令再走主流程
        slug = update.message.text.strip().lstrip("/").split()[0].lower()
        text_map = {c[0]: c[1] for c in COMMANDS}
        if slug not in text_map:
            await cmd_help(update, context)
            return
        # PTB 的 update.message.text 是唯讀 property，不能改；
        # 用一個最小 shim 攜帶中文指令，避免 AttributeError 被 dispatcher 吞掉。
        zh = text_map[slug]
        class _Msg:
            def __init__(self, text, src):
                self.text = text
                self._src = src
            async def reply_text(self, *a, **k):
                await self._src.reply_text(*a, **k)
        shim = type("Update", (), {"message": _Msg(zh, update.message)})()
        await handle_message(shim, context)

    for cmd, _, _, _ in COMMANDS:
        if cmd not in ("start", "help"):
            app.add_handler(CommandHandler(cmd, _slug_to_text))

    log.info("HomeBox bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
