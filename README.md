# 我想和你见一面

A lightweight self-hosted appointment booking app. Visitors pick an open time slot from your real Feishu calendar, describe what they'd like to discuss, and a calendar event is automatically created in the background — no scheduling back-and-forth needed.

Live: [xbchen.top/appointment](http://xbchen.top/appointment)

---

## Features

- **Real-time availability** — syncs Feishu Calendar freebusy on every page load; visitors only see genuinely open slots
- **Async booking pipeline** — frontend returns instantly after submit; a background worker handles Feishu API calls, AI title generation, and IM notification
- **AI-generated event titles** — Claude turns a freeform meeting description into a concise calendar title
- **Fuzzy name matching** — handles initials (`lxy`), pinyin (`lixinyu`), reversed order (`xinyuli`), English names (`Leo`), and nicknames (`小企鹅`); uses a three-layer lookup:
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
