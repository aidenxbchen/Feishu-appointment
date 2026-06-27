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
import re
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

WORKER_TICK = 30  # seconds between pending-booking sweeps
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
                title         TEXT,
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

            CREATE TABLE IF NOT EXISTS member_aliases (
                alias       TEXT PRIMARY KEY,
                member_name TEXT NOT NULL
            );
        ''')
        try:
            conn.execute('ALTER TABLE bookings ADD COLUMN title TEXT')
        except Exception:
            pass


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


def extract_users(lookup_result):
    if not lookup_result.get('ok'):
        return []
    return (lookup_result.get('data', {}).get('results', []) or
            lookup_result.get('data', {}).get('users', []))


_roster_cache: list = []
_roster_cache_time: float = 0


def get_team_roster(force: bool = False) -> list:
    """Fetch internal Feishu team members. force=True always re-fetches (used on page load)."""
    global _roster_cache, _roster_cache_time
    if not force and _roster_cache:
        return _roster_cache

    seen: set = set()
    roster = []
    # two smaller batches to avoid stdout truncation
    for surnames in ['李,王,张,陈,刘,赵,黄,周,吴,孙', '林,郑,徐,朱,杨,高,余,韩,郭,何,谢,曾,许,邓,萧,冯,唐,傅,邹,熊']:
        result = run_lark(
            ['contact', '+search-user', '--queries', surnames, '--exclude-external-users'],
            identity='user', timeout=25
        )
        users = result.get('data', {}).get('users', []) or result.get('data', {}).get('results', [])
        for u in users:
            name = u.get('localized_name') or u.get('name', '')
            oid = u.get('open_id', '')
            email = u.get('enterprise_email', '')
            email_prefix = email.split('@')[0] if email else ''
            if oid and oid not in seen and name:
                seen.add(oid)
                roster.append({'name': name, 'email_prefix': email_prefix, 'open_id': oid})

    if roster:
        _roster_cache = roster
        _roster_cache_time = time.time()
        # Sync English names from 个人说明书 docs into alias table
        threading.Thread(target=_sync_en_name_aliases, args=(roster,), daemon=True).start()

    return _roster_cache


def _sync_en_name_aliases(roster: list):
    """Paginate 个人说明书 docs, extract English names, upsert into member_aliases.
    Skips cross-tenant docs (former employees / external) to avoid stale mappings."""
    import re
    oid_to_name = {m['open_id']: m['name'] for m in roster}
    pairs = []
    page_token = None

    while True:
        args = ['docs', '+search', '--query', '个人说明书', '--page-size', '20']
        if page_token:
            args += ['--page-token', page_token]
        result = run_lark(args, identity='user', timeout=25)
        data = result.get('data', {})
        items = data.get('results', [])

        for item in items:
            meta = item.get('result_meta', {})
            # skip cross-tenant documents (former/external members)
            if item.get('is_cross_tenant') or meta.get('is_cross_tenant'):
                continue
            owner_id = meta.get('owner_id', '')
            member_name = oid_to_name.get(owner_id)
            if not member_name:
                continue
            title = item.get('title_highlighted', '').replace('<h>', '').replace('</h>', '')
            m = re.match(r'^([A-Za-z][A-Za-z\s\.]+?)\s*个人说明书', title)
            if not m:
                continue
            en_name = m.group(1).strip()
            if en_name:
                pairs.append((en_name, member_name))

        if not data.get('has_more'):
            break
        page_token = data.get('page_token')
        if not page_token:
            break

    if pairs:
        with get_db() as conn:
            conn.executemany(
                'INSERT OR IGNORE INTO member_aliases (alias, member_name) VALUES (?, ?)',
                pairs
            )
        print(f'[roster] synced {len(pairs)} English name aliases from 个人说明书')


def normalize_name_with_ai(input_name: str) -> str:
    """Resolve a fuzzy name to a Chinese name: alias table first, then Claude with roster context."""
    # 1. Check manually maintained alias table first
    with get_db() as conn:
        row = conn.execute(
            'SELECT member_name FROM member_aliases WHERE lower(alias) = lower(?)',
            [input_name.strip()]
        ).fetchone()
    if row:
        return row['member_name']

    # 2. Ask Claude with roster + email prefix context
    roster = get_team_roster()
    if roster:
        lines = []
        for m in roster:
            ep = f" | {m['email_prefix']}" if m['email_prefix'] else ''
            lines.append(f"{m['name']}{ep}")
        roster_ctx = (
            '\n\n团队成员名单（格式：中文名 | 邮箱前缀，邮箱前缀即其英文/拼音名）：\n'
            + '\n'.join(lines)
        )
    else:
        roster_ctx = ''

    prompt = (
        f'预约表单的姓名栏填写的是："{input_name}"\n'
        f'你的任务是把拼音/英文名/昵称（非中文字符）映射到团队中文名。'
        f'常见情况：中文全拼（lixinyu→李鑫宇）、首字母缩写（lxy→李鑫宇）、'
        f'姓名倒置（xinyuli→李鑫宇）、拼音英文名（xinyu→李鑫宇）、昵称等。'
        f'{roster_ctx}\n\n'
        f'重要限制：\n'
        f'1. 如果输入本身已包含中文字符，直接原样返回，不做任何映射。\n'
        f'2. 只有在输入是纯拼音/英文/缩写时才尝试匹配名单。\n'
        f'3. 无法从名单中确定对应关系时，原样返回输入内容，不要猜测。\n'
        f'只输出姓名本身，不要任何解释。'
    )
    try:
        result = subprocess.run(
            [CLAUDE, '-p', '--no-session-persistence'],
            input=prompt, capture_output=True, text=True, timeout=15
        )
        guessed = result.stdout.strip().replace('**', '').strip()
        if guessed:
            return guessed
    except Exception:
        pass
    return input_name


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
    display_name = name  # resolved name used for title/notification; falls back to raw input

    users = extract_users(run_lark(['contact', '+search-user', '--query', name], identity='user'))

    if not users and not re.search(r'[一-鿿]', name):
        # Only attempt AI normalization for non-Chinese input (pinyin / English name / initials).
        # If the input is already Chinese characters, the person is external — don't remap to a team member.
        normalized = normalize_name_with_ai(name)
        if normalized and normalized != name:
            users = extract_users(run_lark(['contact', '+search-user', '--query', normalized], identity='user'))
            if users:
                display_name = normalized  # confirmed match against roster

    if users:
        u = users[0]
        # prefer the canonical name returned by Feishu
        display_name = u.get('localized_name') or u.get('name') or display_name
        attendee_open_id = u.get('open_id') or u.get('user', {}).get('open_id')

    # Step 2: use pre-generated title if available, else generate now with resolved name
    title       = row['title'] if row['title'] else generate_event_title(display_name, content)
    description = f'申请人：{display_name}\n\n{content}'

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
    notify_text = f'📅 新预约\n申请人：{display_name}\n时间：{time_display}\n内容：{summary}'
    run_lark(
        ['im', '+messages-send', '--user-id', OWNER_OPEN_ID, '--text', notify_text],
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
    while True:
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
    """On each page load: sync busy slots from Feishu and refresh team roster in background."""
    sync_busy_slots()
    threading.Thread(target=get_team_roster, kwargs={'force': True}, daemon=True).start()

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
    title      = body.get('title', '').strip() or None

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
                'INSERT INTO bookings (name, content, start_time, end_time, title) VALUES (?, ?, ?, ?, ?)',
                [name, content, start_time, end_time, title]
            )
            booking_id = cursor.lastrowid

    return jsonify({'ok': True, 'booking_id': booking_id, 'title': title or f'与 {name} 的会议'})


if __name__ == '__main__':
    init_db()
    threading.Thread(target=background_worker, daemon=True).start()
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
