#!/usr/bin/env python3
"""
Appointment booking web app — integrates with Feishu Calendar.
Serves at /appointment on the configured domain.

v1.1 changes:
- SQLite local cache for busy slots (no more blocking Feishu API on page load)
- Async booking pipeline: write to DB first, background worker handles Feishu + notification
- Atomic double-booking check via threading lock
- IM notification to owner after each confirmed booking
- run_lark hardened against TimeoutExpired
"""
import subprocess
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='static', static_url_path='/appointment/static')

LARK = '/home/admin/.hermes/node/bin/lark-cli'
CLAUDE = '/home/admin/.local/bin/claude'
OWNER_OPEN_ID = 'ou_5f427cfac9103e9e0b2428236d377586'
STYLE_GUIDE = os.path.join(os.path.dirname(__file__), 'title_style_guide.md')
DB_PATH = os.path.join(os.path.dirname(__file__), 'appointments.db')

BUSY_SYNC_INTERVAL = 300   # seconds between Feishu free/busy syncs
WORKER_TICK = 30           # seconds between pending-booking sweeps
MAX_RETRY = 3

_booking_lock = threading.Lock()


# ── Database ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS bookings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                content       TEXT    NOT NULL,
                start_time    TEXT    NOT NULL,
                end_time      TEXT    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'pending',
                attendee_open_id TEXT,
                event_id      TEXT,
                retry_count   INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                processed_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS busy_slots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
                end_time   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cache_meta (
                key        TEXT PRIMARY KEY,
                value      TEXT
            );
        ''')


# ── Feishu helpers ─────────────────────────────────────────────────────────────

def run_lark(args, identity='user', timeout=30):
    try:
        cmd = [LARK] + args + ['--as', identity, '--format', 'json']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': {'message': 'lark-cli timeout'}}
    except Exception as e:
        return {'ok': False, 'error': {'message': str(e)}}


def generate_event_title(requester_name: str, content: str) -> str:
    style_section = ''
    try:
        with open(STYLE_GUIDE, 'r') as f:
            style_section = f'\n\n参考以下风格指南生成标题：\n{f.read()}'
    except Exception:
        pass

    prompt = (
        f'请根据以下会议申请内容，为一个飞书日历事件生成简洁的会议主题（不超过20字）。\n'
        f'申请人：{requester_name}\n'
        f'内容：{content}'
        f'{style_section}\n\n'
        f'只输出会议主题，不要任何解释。'
    )
    try:
        result = subprocess.run(
            [CLAUDE, '-p', '--no-session-persistence'],
            input=prompt, capture_output=True, text=True, timeout=30
        )
        title = result.stdout.strip().replace('**', '').strip()
        if title:
            return title
    except Exception:
        pass
    return f'与 {requester_name} 的会议'


# ── Background: sync busy slots from Feishu ────────────────────────────────────

def sync_busy_slots():
    tz = '+08:00'
    now = datetime.now()
    start_str = now.strftime('%Y-%m-%dT00:00:00') + tz
    end_str = (now + timedelta(days=14)).strftime('%Y-%m-%dT23:59:59') + tz

    data = run_lark([
        'calendar', '+freebusy',
        '--user-id', OWNER_OPEN_ID,
        '--start', start_str,
        '--end', end_str,
    ], identity='bot')

    if not data.get('ok'):
        return

    rows = [(item['start_time'], item['end_time']) for item in data.get('data', [])]
    with get_db() as conn:
        conn.execute('DELETE FROM busy_slots')
        conn.executemany('INSERT INTO busy_slots (start_time, end_time) VALUES (?, ?)', rows)
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES ('busy_synced_at', ?)",
            [datetime.now().isoformat()]
        )


# ── Background: process one pending booking ────────────────────────────────────

def process_booking(booking_id):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM bookings WHERE id = ?', [booking_id]).fetchone()
        if not row:
            return
        conn.execute("UPDATE bookings SET status = 'processing' WHERE id = ?", [booking_id])

    name       = row['name']
    content    = row['content']
    start_time = row['start_time']
    end_time   = row['end_time']

    # Step 1: lookup user — internal member gets a calendar invite, external gets skipped
    attendee_open_id = None
    lookup = run_lark(['contact', '+search-user', '--query', name], identity='user')
    if lookup.get('ok'):
        users = (lookup.get('data', {}).get('results', []) or
                 lookup.get('data', {}).get('users', []))
        if users:
            u = users[0]
            attendee_open_id = u.get('open_id') or u.get('user', {}).get('open_id')

    # Step 2: generate AI title and create Feishu calendar event
    title       = generate_event_title(name, content)
    description = f'申请人：{name}\n\n{content}'

    create_args = [
        'calendar', '+create',
        '--summary', title,
        '--start', start_time,
        '--end', end_time,
        '--description', description,
    ]
    if attendee_open_id:
        create_args += ['--attendee-ids', attendee_open_id]

    result = run_lark(create_args, identity='user')

    if not result.get('ok'):
        with get_db() as conn:
            conn.execute(
                "UPDATE bookings SET status = 'failed', retry_count = retry_count + 1 WHERE id = ?",
                [booking_id]
            )
        return

    event = result.get('data', {})
    event_id = event.get('event_id') or event.get('event', {}).get('event_id')

    # Step 3: send IM notification to owner
    try:
        dt = datetime.fromisoformat(start_time.replace('+08:00', ''))
        time_display = dt.strftime('%m月%d日 %H:%M')
    except Exception:
        time_display = start_time

    summary = content[:80] + ('…' if len(content) > 80 else '')
    notify_text = f'📅 新预约\n申请人：{name}\n时间：{time_display}\n内容：{summary}'
    run_lark(
        ['im', '+send', '--receiver-id', OWNER_OPEN_ID, '--text', notify_text],
        identity='bot'
    )

    with get_db() as conn:
        conn.execute(
            '''UPDATE bookings
               SET status = 'success', attendee_open_id = ?, event_id = ?,
                   processed_at = datetime('now','localtime')
               WHERE id = ?''',
            [attendee_open_id, event_id, booking_id]
        )


# ── Background worker loop ─────────────────────────────────────────────────────

def background_worker():
    sync_busy_slots()
    last_sync = time.time()

    while True:
        if time.time() - last_sync >= BUSY_SYNC_INTERVAL:
            sync_busy_slots()
            last_sync = time.time()

        with get_db() as conn:
            pending = conn.execute(
                '''SELECT id FROM bookings
                   WHERE status = 'pending'
                      OR (status = 'failed' AND retry_count < ?)''',
                [MAX_RETRY]
            ).fetchall()

        for row in pending:
            try:
                process_booking(row['id'])
            except Exception:
                pass

        time.sleep(WORKER_TICK)


# ── API Routes ─────────────────────────────────────────────────────────────────

@app.route('/appointment')
@app.route('/appointment/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/appointment/api/slots')
def get_slots():
    """Sync from Feishu on every page open (rate-limited to once per 30s), then return local cache."""
    with get_db() as conn:
        meta = conn.execute(
            "SELECT value FROM cache_meta WHERE key = 'busy_synced_at'"
        ).fetchone()

    should_sync = True
    if meta and meta['value']:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(meta['value'])).total_seconds()
            if elapsed < 30:
                should_sync = False
        except Exception:
            pass

    if should_sync:
        sync_busy_slots()

    with get_db() as conn:
        rows = conn.execute('SELECT start_time, end_time FROM busy_slots').fetchall()
        meta = conn.execute(
            "SELECT value FROM cache_meta WHERE key = 'busy_synced_at'"
        ).fetchone()

    busy = [{'start': r['start_time'], 'end': r['end_time']} for r in rows]
    return jsonify({'ok': True, 'busy': busy, 'cached_at': meta['value'] if meta else None})


@app.route('/appointment/api/lookup-user', methods=['POST'])
def lookup_user():
    """Frontend still calls this; internal/external determination now happens in background worker."""
    return jsonify({'found': False, 'open_id': None})


@app.route('/appointment/api/book', methods=['POST'])
def book():
    """Write booking to local DB and return immediately; background worker handles Feishu."""
    body = request.get_json() or {}
    name       = body.get('name', '').strip()
    content    = body.get('content', '').strip()
    start_time = body.get('start_time', '')
    end_time   = body.get('end_time', '')

    if not all([name, content, start_time, end_time]):
        return jsonify({'error': '缺少必填字段'}), 400

    with _booking_lock:
        with get_db() as conn:
            conflict = conn.execute(
                "SELECT id FROM bookings WHERE start_time = ? AND status NOT IN ('failed')",
                [start_time]
            ).fetchone()
            if conflict:
                return jsonify({'error': '该时段刚刚被预约，请重新选择其他时间'}), 409

            cursor = conn.execute(
                'INSERT INTO bookings (name, content, start_time, end_time) VALUES (?, ?, ?, ?)',
                [name, content, start_time, end_time]
            )
            booking_id = cursor.lastrowid

    return jsonify({'ok': True, 'booking_id': booking_id})


if __name__ == '__main__':
    init_db()
    threading.Thread(target=background_worker, daemon=True).start()
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
