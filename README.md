# 我想和你见一面

> [English version below](#english)

一个轻量的自托管预约系统。访客从你的真实飞书日历中选择一个空闲时段，描述想聊的内容，后台自动创建日程并发送飞书通知——无需反复协调时间。

在线体验：[xbchen.top/appointment](http://xbchen.top/appointment)

---

## 功能特性

- **实时空闲查询** — 每次打开页面同步飞书日历 freebusy 数据，只展示真实可用的时段
- **异步预约流水线** — 前端提交后立即返回；后台 worker 处理飞书 API 调用、AI 标题生成和消息通知
- **AI 日程标题** — 由 Claude 将自由文本的会议说明提炼为简洁的日程标题
- **模糊姓名匹配** — 支持缩写（`lxy`）、拼音（`lixinyu`）、倒序（`xinyuli`）、英文名（`Leo`）、花名（`小企鹅`）；三层匹配策略：
  1. 飞书通讯录直接搜索
  2. 别名表（英文名 + 花名，从个人说明书文档自动同步 + 人工维护）
  3. Claude 兜底：携带完整团队名单 + 邮箱前缀推断
- **英文名自动同步** — 每次打开页面自动搜索飞书中的「个人说明书」文档，从标题解析英文名 / 花名写入别名表
- **IM 消息通知** — 预约确认后向日历所有者发送飞书私信
- **三步预约流程** — 填写信息 → 选择时段 → 确认；简洁的移动端界面，无第三方依赖
- **防重复预约** — SQLite 原子锁防止并发场景下的时间段冲突

## 架构

```
浏览器                     Flask（5001 端口）              后台 Worker
──────                     ──────────────────             ─────────────
打开页面  ──/api/slots──▶  同步忙闲时段               ─┐
                           后台刷新团队名单（异步）      │  每 30 秒：
                                                        │  处理待完成的预约
提交预约  ──/api/book───▶  写入 SQLite                 │    → 模糊姓名匹配
          ◀─ ok ─────────  立即返回                    │    → 生成 AI 标题
                                                        │    → 创建飞书日程
                                                     ───┘    → 发送 IM 通知
```

## 技术栈

| 层级 | 选型 |
|---|---|
| 后端 | Python 3 + Flask |
| 数据库 | SQLite（WAL 模式） |
| 前端 | 原生 HTML/CSS/JS（单文件，无构建步骤） |
| 日历 | 飞书日历 API，通过 [lark-cli](https://github.com/larksuite/lark-cli) 调用 |
| AI | Claude（`claude -p`）——日程标题生成 + 模糊姓名解析 |
| 反向代理 | Nginx |
| 进程管理 | systemd |

## 目录结构

```
appointment/
├── app.py                  # Flask 后端 + 后台 worker
├── static/
│   ├── index.html          # 完整前端（HTML + CSS + JS）
│   └── quotes.md           # 成功卡片翻面文案
├── scripts/
│   └── git-sync.sh         # GitHub 自动拉取脚本（cron）
├── deploy/
│   └── appointment.service # systemd 单元文件
└── title_style_guide.md    # AI 生成日程标题的风格指南
```

## 数据库结构

SQLite 数据库（`appointments.db`，不提交 git）：

| 表 | 用途 |
|---|---|
| `bookings` | 预约记录（待处理 + 已处理） |
| `busy_slots` | 飞书日历忙闲缓存 |
| `cache_meta` | 最后同步时间戳 |
| `member_aliases` | 英文名 / 花名 → 中文名 映射 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/appointment/api/slots` | 同步飞书忙闲数据，后台刷新团队名单，返回缓存时段 |
| `POST` | `/appointment/api/book` | 写入预约记录并立即返回 |
| `POST` | `/appointment/api/lookup-user` | （历史接口）返回 `{found: false}`，姓名解析已移至后台 worker |

## 模糊姓名匹配说明

后台处理预约时，按以下顺序解析提交的姓名：

1. **直接搜索** — 用原始输入在飞书通讯录中搜索
2. **别名表** — 对 `member_aliases` 做大小写不敏感查询；数据来源：
   - 自动同步：每次打开页面搜索「个人说明书」文档，从标题解析英文名 / 花名
   - 手动维护：直接写入数据库处理特殊情况（如 `Leo Li → 李鑫宇`）
3. **Claude 兜底** — 以上均无匹配时，Claude 结合完整团队名单（中文名 + 邮箱前缀）推断最可能的匹配

解析出的显示名用于日程标题、日程描述和 IM 通知。

手动添加别名：

```sql
INSERT OR REPLACE INTO member_aliases (alias, member_name) VALUES ('Tom', '张某某');
```

## 部署

### 前置条件

- Python 3.10+
- [lark-cli](https://github.com/larksuite/lark-cli)，需完成 **bot** 身份和 **user** 身份的认证
- Claude CLI（`claude`）在 PATH 中
- Nginx（生产环境）

### 安装运行

```bash
git clone https://github.com/aidenxbchen/Feishu-appointment.git
cd Feishu-appointment

pip install flask

# 修改 app.py 顶部常量：
#   OWNER_OPEN_ID  — 日历所有者的飞书 open_id
#   LARK           — lark-cli 路径
#   CLAUDE         — claude 路径

python app.py  # 默认端口 5001
```

### Nginx 配置

```nginx
location /appointment {
    proxy_pass http://127.0.0.1:5001;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 60s;
}
```

### systemd 服务

```ini
[Unit]
Description=Appointment Booking App
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/appointment/app.py
WorkingDirectory=/path/to/appointment
Restart=always
RestartSec=10
User=admin

[Install]
WantedBy=multi-user.target
```

## 配置项

`app.py` 顶部常量：

| 变量 | 说明 |
|---|---|
| `OWNER_OPEN_ID` | 日历所有者的飞书 open_id |
| `LARK` | lark-cli 路径 |
| `CLAUDE` | claude 路径 |
| `WORKER_TICK` | 后台 worker 轮询间隔（秒，默认 30） |
| `MAX_RETRY` | 失败预约最大重试次数（默认 3） |

## 许可证

MIT

---

<a name="english"></a>

# 我想和你见一面 *(I'd Love to Meet You)*

A lightweight self-hosted appointment booking app. Visitors pick an open time slot from your real Feishu calendar, describe what they'd like to discuss, and a calendar event is automatically created in the background — no scheduling back-and-forth needed.

Live: [xbchen.top/appointment](http://xbchen.top/appointment)

## Features

- **Real-time availability** — syncs Feishu Calendar freebusy on every page load; visitors only see genuinely open slots
- **Async booking pipeline** — frontend returns instantly after submit; a background worker handles Feishu API calls, AI title generation, and IM notification
- **AI-generated event titles** — Claude turns a freeform meeting description into a concise calendar title
- **Fuzzy name matching** — handles initials (`lxy`), pinyin (`lixinyu`), reversed order (`xinyuli`), English names (`Leo`), and nicknames (`小企鹅`); three-layer lookup:
  1. Direct Feishu contact search
  2. Alias table (English names + nicknames, auto-synced from personal manuals + manually maintained)
  3. Claude + team roster + email-prefix context as fallback
- **English name auto-sync** — on each page load, searches Feishu for `个人说明书` documents and extracts English names/nicknames from document titles into the alias table
- **IM notification** — sends a Feishu direct message to the calendar owner after each confirmed booking
- **3-step flow** — describe → pick date/time → confirm; clean mobile-friendly UI, no external dependencies
- **Double-booking protection** — atomic SQLite lock prevents race conditions on the same slot

## Architecture

```
Browser                     Flask (port 5001)              Background Worker
──────                      ─────────────────              ─────────────────
page load   ──/api/slots──▶ sync busy slots          ─┐
                            refresh team roster (bg)   │  every 30s:
                                                       │  process pending bookings
submit      ──/api/book───▶ write to SQLite            │    → fuzzy name match
            ◀─ ok ──────── return immediately          │    → generate AI title
                                                       │    → create Feishu event
                                                    ───┘    → send IM notification
```

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python 3 + Flask |
| Database | SQLite (WAL mode) |
| Frontend | Vanilla HTML/CSS/JS (single file, zero build step) |
| Calendar | Feishu Calendar API via [lark-cli](https://github.com/larksuite/lark-cli) |
| AI | Claude (`claude -p`) — event title generation + fuzzy name resolution |
| Reverse proxy | Nginx |
| Process manager | systemd |

## Project Structure

```
appointment/
├── app.py                  # Flask backend + background worker
├── static/
│   ├── index.html          # Entire frontend (HTML + CSS + JS)
│   └── quotes.md           # Quotes shown on the success card flip
├── scripts/
│   └── git-sync.sh         # Cron script for auto-pull from GitHub
├── deploy/
│   └── appointment.service # systemd unit file
└── title_style_guide.md    # Style guide for AI-generated event titles
```

## Database Schema

SQLite database (`appointments.db`, not committed to git):

| Table | Purpose |
|---|---|
| `bookings` | Pending and processed appointment records |
| `busy_slots` | Cached freebusy intervals from Feishu |
| `cache_meta` | Last sync timestamp |
| `member_aliases` | English names and nicknames → Chinese name mapping |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/appointment/api/slots` | Sync busy slots from Feishu; refresh team roster in background; return cached intervals |
| `POST` | `/appointment/api/book` | Write booking to DB and return immediately |
| `POST` | `/appointment/api/lookup-user` | (Legacy stub) Returns `{found: false}` — lookup now handled by background worker |

## Fuzzy Name Matching

When processing a booking, the system resolves the submitted name in three steps:

1. **Direct search** — query Feishu Contacts with the raw input
2. **Alias table** — case-insensitive lookup in `member_aliases`; populated by:
   - Auto-sync: on each page load, searches for `个人说明书` wiki docs and parses English names / nicknames from document titles
   - Manual entries: added directly to the DB for edge cases (e.g. `Leo Li → 李鑫宇`)
3. **Claude fallback** — if neither above matches, Claude receives the input name alongside the full team roster (Chinese names + email prefixes) and infers the most likely match

The resolved display name is used for the calendar event title, event description, and IM notification.

To add a manual alias:

```sql
INSERT OR REPLACE INTO member_aliases (alias, member_name) VALUES ('Tom', '张某某');
```

## Setup

### Prerequisites

- Python 3.10+
- [lark-cli](https://github.com/larksuite/lark-cli) authenticated with a **bot** identity and a **user** identity
- Claude CLI (`claude`) in PATH
- Nginx (for production)

### Install & Run

```bash
git clone https://github.com/aidenxbchen/Feishu-appointment.git
cd Feishu-appointment

pip install flask

# Set constants in app.py:
#   OWNER_OPEN_ID  — your Feishu open_id
#   LARK           — path to lark-cli binary
#   CLAUDE         — path to claude binary

python app.py  # default port 5001
```

### Nginx

```nginx
location /appointment {
    proxy_pass http://127.0.0.1:5001;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 60s;
}
```

### systemd

```ini
[Unit]
Description=Appointment Booking App
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/appointment/app.py
WorkingDirectory=/path/to/appointment
Restart=always
RestartSec=10
User=admin

[Install]
WantedBy=multi-user.target
```

## Configuration

Constants at the top of `app.py`:

| Variable | Description |
|---|---|
| `OWNER_OPEN_ID` | Feishu `open_id` of the calendar owner |
| `LARK` | Path to `lark-cli` binary |
| `CLAUDE` | Path to `claude` CLI binary |
| `WORKER_TICK` | Seconds between background worker sweeps (default: 30) |
| `MAX_RETRY` | Max retry attempts for failed bookings (default: 3) |

## License

MIT
