# HomeBox Bot — 部署說明

> 這份說明給**要自己架設一份 HomeBox** 的人看。HomeBox 是一個純 Telegram 機器人，用來管理家裡的庫存（買/用/查看/告警/歷史紀錄）。
> 不需要寫程式，照下面步驟複製一份到你的機器即可。

---

## 1. 你需要準備什麼

- 一台 24 小時開機的機器（VPS、舊電腦、樹莓派都行），裝好 **Linux**（Ubuntu / Debian 為例）
- 一個 **Telegram 帳號**，以及一個 **Telegram Bot Token**（下面教怎麼拿）
- 基本的終端機操作能力（能複製貼上指令）

---

## 2. 取得 Telegram Bot Token

1. 在 Telegram 搜尋 `@BotFather`，開始對話
2. 傳送 `/newbot`，照提示取一個 bot 名字（例如 `Maurice的庫存 bot`）
3. BotFather 會給你一組 token，長這樣：
   ```
   123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   ```
   **把這串複製起來**，下一步要用。

---

## 3. 安裝 Python 與套件

在機器上開終端機，依序執行：

```bash
# 安裝 Python 3 與 pip（若已經有可跳過）
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

# 建立一個專用資料夾與虛擬環境
mkdir -p /root/homebox
cd /root/homebox
python3 -m venv venv
source venv/bin/activate

# 安裝 Telegram 套件
pip install python-telegram-bot==22.6
```

> 之後如果要重啟 bot，記得先 `source venv/bin/activate` 再跑。

---

## 4. 放入程式檔案

把下面這幾個檔案放到 `/root/homebox/`：

| 檔案 | 作用 |
|------|------|
| `bot.py` | 機器人主程式 |
| `reminder.py` | 每週提醒低庫存的腳本 |
| `data.seed.json` | 首次啟動的預置資料（空氣 / 日常用品 / 儲藏室） |

你可以從原始來源複製這些檔案，或用 `git clone`（若你有仓库）。

**設定你的 Token**：
複製 `.env.example` 成 `.env`，再把你在步驟 2 拿到的 token 填到 `.env`：
```bash
cp .env.example .env
nano .env
```

`.env` 範例：
```dotenv
BOT_TOKEN=你的token貼這裡
```

程式會從環境變數或 `/root/homebox/.env` 讀取 `BOT_TOKEN`。不要把 token 寫進 `bot.py`、`reminder.py` 或其他 source code。

> ⚠️ Token 等同密碼，不要公開給別人，也不要 commit 到公開倉庫。

---

## 5. 首次啟動（產生資料庫）

第一次啟動前，先手動複製預置檔（**只做一次，之後不要覆蓋**）：

```bash
cd /root/homebox
# 若 data.json 不存在才複製（保護既有資料）
[ -f data.json ] || cp data.seed.json data.json
```

然後測試跑一次：
```bash
source venv/bin/activate
python3 bot.py
```
終端機出現 `Application started` 就成功了。這時去 Telegram 對你的 bot 傳 `?` 或 `help`，應該會看到指令說明。

> 測試完按 `Ctrl+C` 停止，我們下一步把它設成背景常駐。

---

## 6. 設成系統服務（開機自動跑）

建立 systemd 服務檔：

```bash
sudo tee /etc/systemd/system/homebox-bot.service > /dev/null <<'EOF'
[Unit]
Description=HomeBox Telegram Bot
After=network.target

[Service]
Type=simple
ExecStart=/root/homebox/venv/bin/python3 /root/homebox/bot.py
WorkingDirectory=/root/homebox
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

啟用並啟動：
```bash
sudo systemctl daemon-reload
sudo systemctl enable homebox-bot
sudo systemctl start homebox-bot
```

檢查狀態（看到 `active (running)` 就對了）：
```bash
systemctl status homebox-bot
```

之後機器重開機會自動啟動，程式崩潰也會自動重啟。

---

## 7. 設定每週低庫存提醒（選用）

如果你想每週一、三收到「哪些東西快沒了」的提醒，加一行排程：

```bash
crontab -e
```
在檔案最後加這一行（每週一、三 15:00 執行）：
```cron
0 15 * * 1,3 /root/homebox/venv/bin/python3 /root/homebox/reminder.py >> /var/log/homebox/cron.log 2>&1
```

> 若 `/var/log/homebox/` 資料夾不存在，先建：
> ```bash
> sudo mkdir -p /var/log/homebox
> ```

---

## 8. 常用管理指令

在 Telegram 對 bot 傳送（打 `/` 或點輸入框旁 ≡ 可喚出選單）：

| 你想做 | 傳送 |
|--------|------|
| 增加庫存 | `買 衛生紙 10` |
| 減少庫存 | `用 衛生紙 5` |
| 看所有物品 | `查看` |
| 看低庫存 | `告警` |
| 看買/用紀錄 | `紀錄` 或 `紀錄 用 30`（近30天用） |
| 完整說明 | `?` 或 `help` |
| 新增分類 | `新增分類 日用品` |
| 改名字（物品/分類/位置） | `重命名物品 舊名 => 新名` |
| 刪除物品 | `刪除物品 名稱`（會要你按鈕確認） |
| 刪除分類 | `刪除分類 名稱`（無物品引用才能刪） |
| 刪除位置 | `刪除位置 名稱`（該位置無庫存才能刪） |

> 分隔符是 `=>`（兩個字元：等於+大於），不是箭頭 →。

---

## 9. 備份與還原

你的所有庫存資料在 `/root/homebox/data.json`。要備份就複製這個檔案：
```bash
cp /root/homebox/data.json /root/homebox/data.json.bak
```
要還原就把 `.bak` 複製回去並重啟服務。

---

## 10. 常見問題

**Q: bot 沒反應？**
- 檢查服務：`systemctl status homebox-bot`
- 看日誌：`journalctl -u homebox-bot -n 50`
- 最常見原因：token 打錯、或 venv 路徑不對

**Q: 為什麼 ≡ 選單顯示的是英文（buy/use/view...）？**
- Telegram 規定選單指令只能英文，所以選單用 `buy` 等。但傳送中文（`買`）一樣能用，只是選單上顯示英文 slug。

**Q: 我想從頭清空重來？**
- 停止服務 → 刪除 `data.json` → 重新 `cp data.seed.json data.json` → 啟動服務。

**Q: 多人共用？**
- 只要大家都傳訊息給同一個 bot，資料就是共用的（一份庫存）。誰先傳 `/start` 就會被記錄為可接收提醒的人。
