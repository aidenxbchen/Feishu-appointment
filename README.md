# Feishu Appointment Booking

A lightweight self-hosted web app that lets anyone book a meeting with you directly — no back-and-forth needed. Visitors pick an available time slot from your real Feishu (Lark) calendar, describe what they want to discuss, and a calendar event is automatically created and sent to both parties.

Live demo: [xbchen.top/appointment](http://xbchen.top/appointment)

---

## Features

- **Real-time availability** — reads your Feishu Calendar freebusy data so visitors only see genuinely open slots
- **AI-generated event titles** — uses Claude to turn a freeform meeting description into a concise calendar title
- **Auto-invite** — looks up the visitor by name in Feishu Contacts and adds them as an attendee
- **3-step flow** — fill in info → pick date/time → confirm; clean mobile-friendly UI with no external dependencies
- **Weekday filter** — automatically hides weekends; shows the next 14 days

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python 3 + Flask |
| Frontend | Vanilla HTML/CSS/JS (single file, zero build step) |
| Calendar | Feishu Calendar API via [lark-cli](https://github.com/larksuite/lark-cli) |
| AI | Claude (local CLI, `claude -p`) |
| Reverse proxy | Nginx |
| Process manager | systemd |

## Project Structure

```
appointment/
├── app.py              # Flask backend — 3 API endpoints
└── static/
    └── index.html      # Entire frontend (HTML + CSS + JS)
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/appointment/api/slots` | Returns busy time intervals for the next 14 days |
| `POST` | `/appointment/api/lookup-user` | Finds a Feishu user by name and returns their `open_id` |
| `POST` | `/appointment/api/book` | Creates a Feishu Calendar event and optionally invites the attendee |

## Setup

### Prerequisites

- Python 3.10+
- [lark-cli](https://github.com/larksuite/lark-cli) authenticated with both a **bot** identity (for freebusy) and a **user** identity (for creating events)
- Claude CLI (`claude`) available in PATH
- Nginx (for production)

### Install & Run

```bash
# Clone
git clone https://github.com/aidenxbchen/Feishu-appointment.git
cd Feishu-appointment

# Install dependencies
pip install flask

# Set calendar owner open_id in app.py → OWNER_OPEN_ID

# Run (default port 5001)
python app.py
```

### Nginx Config

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location /appointment {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }
}
```

### systemd Service

```ini
[Unit]
Description=Appointment Booking App
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/admin/appointment/app.py
Restart=always
User=admin

[Install]
WantedBy=multi-user.target
```

## Configuration

Edit the constants at the top of `app.py`:

| Variable | Description |
|---|---|
| `LARK` | Path to `lark-cli` binary |
| `CLAUDE` | Path to `claude` CLI binary |
| `OWNER_OPEN_ID` | Feishu `open_id` of the calendar owner |

## License

MIT
