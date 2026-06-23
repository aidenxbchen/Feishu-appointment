#!/usr/bin/env python3
"""
Appointment booking web app — integrates with Feishu Calendar.
Serves at /appointment on the configured domain.
"""
import subprocess
import json
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='static')

LARK = '/home/admin/.hermes/node/bin/lark-cli'
CLAUDE = '/home/admin/.local/bin/claude'
OWNER_OPEN_ID = 'ou_5f427cfac9103e9e0b2428236d377586'
STYLE_GUIDE = os.path.join(os.path.dirname(__file__), 'title_style_guide.md')


def run_lark(args, identity='user', timeout=30):
    cmd = [LARK] + args + ['--as', identity, '--format', 'json']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(result.stdout)
    except Exception:
        return {'ok': False, 'error': {'message': result.stderr or result.stdout or 'Unknown error'}}


# ── AI title generation via local Claude CLI ───────────────────────────────────

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
        title = result.stdout.strip()
        # Strip markdown formatting like **bold**
        title = title.replace('**', '').strip()
        if title:
            return title
    except Exception:
        pass
    return f'与 {requester_name} 的会议'


# ── API Routes ─────────────────────────────────────────────────────────────────

@app.route('/appointment')
@app.route('/appointment/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/appointment/api/slots')
def get_slots():
    """Return busy hourly slots for a date range (using bot identity)."""
    tz = '+08:00'
    start_str = request.args.get('start', datetime.now().strftime('%Y-%m-%dT00:00:00') + tz)
    end_dt = datetime.now() + timedelta(days=14)
    end_str = request.args.get('end', end_dt.strftime('%Y-%m-%dT23:59:59') + tz)

    data = run_lark([
        'calendar', '+freebusy',
        '--user-id', OWNER_OPEN_ID,
        '--start', start_str,
        '--end', end_str,
    ], identity='bot')

    if not data.get('ok'):
        return jsonify({'error': data.get('error', {}).get('message', 'Failed to get freebusy')}), 500

    # data.data is directly an array of {start_time, end_time, rsvp_status}
    raw = data.get('data', [])
    busy = [{'start': item['start_time'], 'end': item['end_time']} for item in raw]

    return jsonify({'ok': True, 'busy': busy})


@app.route('/appointment/api/lookup-user', methods=['POST'])
def lookup_user():
    """Find a Feishu user by name (requires user identity)."""
    body = request.get_json() or {}
    name = body.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400

    data = run_lark([
        'contact', '+search-user',
        '--query', name,
    ], identity='user')

    if not data.get('ok'):
        err = data.get('error', {})
        if err.get('subtype') == 'token_missing':
            return jsonify({'found': False, 'open_id': None,
                            'message': '用户身份未授权，日程将不邀请对方'})
        return jsonify({'found': False, 'open_id': None,
                        'message': f'搜索失败：{err.get("message", "")}'})

    users = data.get('data', {}).get('results', []) or data.get('data', {}).get('users', [])
    if not users:
        return jsonify({'found': False, 'open_id': None,
                        'message': f'未在飞书中找到「{name}」，日程将不邀请对方'})

    user = users[0]
    open_id = user.get('open_id') or user.get('user', {}).get('open_id')
    display_name = user.get('name') or user.get('user', {}).get('name', name)
    return jsonify({'found': True, 'open_id': open_id, 'name': display_name})


@app.route('/appointment/api/book', methods=['POST'])
def book():
    """Create a Feishu calendar event (requires user identity)."""
    body = request.get_json() or {}
    requester_name = body.get('name', '').strip()
    content = body.get('content', '').strip()
    start_time = body.get('start_time', '')
    end_time = body.get('end_time', '')
    attendee_open_id = body.get('attendee_open_id')

    if not all([requester_name, content, start_time, end_time]):
        return jsonify({'error': '缺少必填字段'}), 400

    title = generate_event_title(requester_name, content)
    description = f'申请人：{requester_name}\n\n{content}'

    create_args = [
        'calendar', '+create',
        '--summary', title,
        '--start', start_time,
        '--end', end_time,
        '--description', description,
    ]
    if attendee_open_id:
        create_args += ['--attendee-ids', attendee_open_id]

    data = run_lark(create_args, identity='user')

    if not data.get('ok'):
        err = data.get('error', {})
        if err.get('subtype') == 'token_missing':
            return jsonify({'error': '飞书用户身份已过期，请联系管理员重新授权'}), 503
        return jsonify({'error': err.get('message', '创建日程失败')}), 500

    event_data = data.get('data', {})
    event_id = event_data.get('event_id') or event_data.get('event', {}).get('event_id')
    return jsonify({'ok': True, 'title': title, 'event_id': event_id})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
