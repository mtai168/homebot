# HomeBox Telegram Bot

HomeBox 是一個給家庭或小團隊使用的 Telegram 庫存管理 bot。它用一份簡單的 JSON 檔保存物品、分類、存放位置與數量，讓使用者可以直接在 Telegram 裡記錄「買了什麼」、「用了什麼」、「還剩多少」。


## 功能

- 新增庫存：記錄購買或補充的物品與數量
- 扣除庫存：記錄使用、消耗或取出的物品與數量
- 多位置管理：同一個物品可以分散在不同位置
- 分類管理：用分類整理物品
- 低庫存告警：列出低於告警值的物品
- 交易紀錄：以 append-only JSONL 保存買入/使用紀錄
- 定期提醒：可透過 systemd timer 或 cron 發送低庫存提醒

## Telegram 指令

Telegram 選單使用 ASCII slash commands，中文文字指令也可以直接輸入。

| Slash command | 中文指令 | 用途 | 範例 |
| --- | --- | --- | --- |
| `/buy` | `買` | 增加庫存 | `買 衛生紙 10`、`買了 維他命 3` |
| `/use` | `用` | 減少庫存 | `用 衛生紙 5`、`取出 維他命 1` |
| `/view` | `查看` | 查看庫存 | `查看`、`查看 保健品` |
| `/alert` | `告警` | 查看低庫存物品 | `告警` |
| `/log` | `紀錄` | 查看買/用紀錄 | `紀錄`、`紀錄 用 30` |
| `/help` | `說明` | 顯示說明 | `?`、`help`、`說明` |

管理類文字指令：

```text
新增分類 名稱
重命名分類 舊 => 新
重命名位置 舊 => 新
重命名物品 舊 => 新
刪除物品 名稱
刪除分類 名稱
刪除位置 名稱
```

刪除物品、分類、位置等破壞性操作會透過 Telegram inline keyboard 二次確認。

## 資料檔

正式部署建議使用：

```text
/srv/homebox/data.json
/srv/homebox/transactions.jsonl
/srv/homebox/chat_ids.json
/etc/homebox/.env
/var/log/homebox
```

重要檔案：

- `data.json`：目前庫存、分類、位置與告警值
- `transactions.jsonl`：買入/使用紀錄
- `chat_ids.json`：可接收提醒的 Telegram chat id
- `.env`：Telegram bot token 等環境變數，不能 commit

## 環境變數

必要：

```dotenv
BOT_TOKEN=你的 Telegram bot token
```

可選：

```dotenv
HOMEBOX_DATA_DIR=/srv/homebox
HOMEBOX_ENV_FILE=/etc/homebox/.env
HOMEBOX_LOG_DIR=/var/log/homebox
```


## 本機開發

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

請勿把 `.env`、真實 token、`data.json`、`transactions.jsonl` 或 `chat_ids.json` commit 到 GitHub。

## systemd 部署

目前的服務設計：

- 使用者：`homebox`
- App 目錄：`/srv/homebox`
- Secret：`/etc/homebox/.env`
- Log：`/var/log/homebox`
- 主服務：`homebox-bot.service`
- 低庫存提醒：`homebox-reminder.timer`

常用指令：

```bash
systemctl status homebox-bot.service --no-pager
systemctl restart homebox-bot.service
systemctl list-timers homebox-reminder.timer --no-pager
journalctl -u homebox-bot.service -n 100 --no-pager
```

## 備份

HomeBox 需自行備份，比如開發環境已被 Borg 備份涵蓋：

- `/srv/homebox`
- `/etc/homebox`

`.venv` 和 pip cache 是可重建內容，不需要備份。

## 安全注意事項

- Telegram token 等同密碼，不要寫進 source code
- 不要讓 bot 以 root 身分執行
- 不要讓同一個 Telegram bot token 在兩台主機同時 long polling
- 若 token 曾出現在 log，應透過 BotFather rotate token

## 相關文件

- `SPEC-v1.0.0.md`：功能規格
- `DEPLOY.md`：舊版部署說明
