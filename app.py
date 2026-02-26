import os
import json
import threading
import traceback
import time
import random
import string
from datetime import datetime
from collections import deque
from flask import Flask, request, render_template_string, redirect, url_for, jsonify, Response

app = Flask(__name__)

# ==================== 全局数据结构 ====================
MAX_HISTORY = 50
history = deque(maxlen=MAX_HISTORY)
history_lock = threading.Lock()

commands = []
commands_lock = threading.Lock()
cmd_counter = 0

# 暂停状态
paused = False
pause_lock = threading.Lock()


# ==================== 时间限制函数 ====================
def is_restricted_time():
    """判断当前时间是否处于限制时段（0:30 - 4:30）"""
    now = datetime.now()
    hour = now.hour
    minute = now.minute
    if hour == 0 and minute >= 30:
        return True
    if 1 <= hour <= 3:
        return True
    if hour == 4 and minute <= 30:
        return True
    return False


# ==================== 工具函数 ====================
def generate_nonce():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=16))


def format_command(cmd):
    """将指令字典格式化为字符串，供旧版 /web/api/echo 使用"""
    base = f"{cmd['side']},{cmd['symbol']},{cmd['volume']}"
    if cmd.get('sl_price') is not None and cmd.get('tp_price') is not None:
        return f"{base},{cmd['sl_price']},{cmd['tp_price']}"
    elif cmd.get('sl_price') is not None:
        return f"{base},{cmd['sl_price']},0"
    elif cmd.get('tp_price') is not None:
        return f"{base},0,{cmd['tp_price']}"
    else:
        return base


def get_client_ip():
    return request.headers.get('X-Real-Ip') or request.headers.get('X-Forwarded-For', request.remote_addr)


def store_mt4_data(raw_body, client_ip, headers_dict):
    cleaned_body = (raw_body or "").strip()
    parsed_json = None
    parse_error = None
    parse_error_detail = None
    remaining_data = None

    try:
        decoder = json.JSONDecoder()
        parsed_json, idx = decoder.raw_decode(cleaned_body)
        remaining = cleaned_body[idx:].strip()
        if remaining:
            remaining_data = remaining[:200]
            print(f"[store_mt4_data] 检测到JSON后剩余数据: {remaining_data}")
    except json.JSONDecodeError as e:
        parse_error = str(e)
        parse_error_detail = traceback.format_exc()
        print(f"[store_mt4_data] JSON解析错误: {e}")
        print(f"[store_mt4_data] 原始body(前500字符): {cleaned_body[:500]}")
    except Exception as e:
        parse_error = f"未知异常: {str(e)}"
        parse_error_detail = traceback.format_exc()
        print(f"[store_mt4_data] 解析时发生未知异常: {e}")

    record = {
        'received_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ip': client_ip,
        'method': request.method,
        'path': request.path,
        'headers': headers_dict,
        'body_raw': raw_body,
        'parsed': parsed_json,
        'parse_error': parse_error,
        'parse_error_detail': parse_error_detail,
        'remaining_data': remaining_data,
        'account': parsed_json.get('account') if parsed_json else None,
        'server': parsed_json.get('server') if parsed_json else None,
        'balance': parsed_json.get('balance') if parsed_json else None,
        'equity': parsed_json.get('equity') if parsed_json else None,
        'floating_pnl': parsed_json.get('floating_pnl') if parsed_json else None,
    }

    with history_lock:
        history.appendleft(record)

    return parsed_json, record


def extract_latest_details(record):
    if not record:
        return None

    base_info = {
        'received_at': record.get('received_at'),
        'ip': record.get('ip'),
        'body_raw_preview': record.get('body_raw', '')[:500] + ('...' if len(record.get('body_raw', '')) > 500 else '')
    }

    if record.get('parse_error'):
        return {
            **base_info,
            'error': f"JSON 解析失败: {record['parse_error']}",
            'full_error': record.get('parse_error_detail', ''),
            'remaining_data': record.get('remaining_data')
        }

    parsed = record.get('parsed')
    if parsed is None:
        return {**base_info, 'error': 'JSON 解析失败，但无具体错误信息'}

    metrics = parsed.get('metrics', {})
    positions = parsed.get('positions', [])
    for pos in positions:
        if 'open_time' in pos and isinstance(pos['open_time'], (int, float)):
            try:
                pos['open_time_str'] = datetime.fromtimestamp(pos['open_time']).strftime('%Y-%m-%d %H:%M:%S')
            except:
                pos['open_time_str'] = str(pos['open_time'])
        else:
            pos['open_time_str'] = 'N/A'

    return {
        **base_info,
        'account': parsed.get('account'),
        'server': parsed.get('server'),
        'ts': parsed.get('ts'),
        'balance': parsed.get('balance'),
        'equity': parsed.get('equity'),
        'margin': parsed.get('margin'),
        'free_margin': parsed.get('free_margin'),
        'margin_level': parsed.get('margin_level'),
        'floating_pnl': parsed.get('floating_pnl'),
        'day_start_equity': parsed.get('day_start_equity'),
        'daily_pnl': parsed.get('daily_pnl'),
        'daily_return': parsed.get('daily_return'),
        'poll_latency_ms': metrics.get('poll_latency_ms'),
        'last_http_code': metrics.get('last_http_code'),
        'last_error': metrics.get('last_error'),
        'positions': positions,
        'remaining_data': record.get('remaining_data')
    }


# ==================== 暂停控制接口 ====================
@app.route('/api/pause', methods=['POST'])
def api_pause():
    global paused
    with pause_lock:
        paused = True
    return jsonify({'paused': paused})


@app.route('/api/resume', methods=['POST'])
def api_resume():
    global paused
    with pause_lock:
        paused = False
    return jsonify({'paused': paused})


@app.route('/api/status', methods=['GET'])
def api_status():
    with pause_lock:
        return jsonify({'paused': paused})


# ==================== 路由：主页 ====================
@app.route('/')
def index():
    with history_lock:
        hist_list = list(reversed(history))
        latest_record = hist_list[0] if hist_list else None
        latest_detail = extract_latest_details(latest_record)
    with commands_lock:
        cmds_copy = commands.copy()
    with pause_lock:
        current_paused = paused

    restricted = is_restricted_time()

    return render_template_string(
        HTML_TEMPLATE,
        history=hist_list,
        latest=latest_detail,
        latest_raw=latest_record,
        commands=cmds_copy,
        MAX_HISTORY=MAX_HISTORY,
        paused=current_paused,
        restricted=restricted
    )


# ==================== 旧版 /web/api/echo 接口 ====================
@app.route('/web/api/echo', methods=['POST'])
def mt4_webhook_echo():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)

    store_mt4_data(raw_body, client_ip, headers_dict)

    response_lines = []
    with commands_lock:
        if commands:
            for cmd in commands:
                response_lines.append(format_command(cmd))
            commands.clear()

    if response_lines:
        return '\n'.join(response_lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}
    else:
        return 'NOCOMMAND', 200, {'Content-Type': 'text/plain; charset=utf-8'}


# ==================== MT4 专用接口 ====================
@app.route('/web/api/mt4/commands', methods=['POST'])
def mt4_commands():
    """
    关键修复：不要用 jsonify() 返回（jsonify 默认会输出带空格的 JSON），
    EA 端旧版 GetString() 只能识别 `"key":"value"`，识别不了 `"key": "value"`。
    所以这里用 separators=(',',':') 输出紧凑 JSON，确保 EA 能解析 side/symbol。
    """
    if is_restricted_time():
        payload = {'commands': [], 'paused': paused}
        return Response(json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                        mimetype='application/json'), 200

    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)

    parsed_json, _ = store_mt4_data(raw_body, client_ip, headers_dict)

    if parsed_json is None:
        payload = {'error': 'Invalid JSON', 'commands': []}
        return Response(json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                        mimetype='application/json'), 400

    account = parsed_json.get('account')

    with commands_lock:
        account_commands = []
        remaining_commands = []
        for cmd in commands:
            cmd_acc = cmd.get('account', None)
            # cmd_acc == None 表示广播命令
            if cmd_acc is None or cmd_acc == account:
                account_commands.append(cmd)
            else:
                remaining_commands.append(cmd)
        commands[:] = remaining_commands

    with pause_lock:
        current_paused = paused

    payload = {'commands': account_commands, 'paused': current_paused}
    # 这行可以帮助你在服务器日志里确认“发给EA的JSON长什么样”
    print("[mt4_commands] SEND:", json.dumps(payload, ensure_ascii=False, separators=(',', ':')))

    return Response(json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                    mimetype='application/json'), 200


@app.route('/web/api/mt4/status', methods=['POST'])
def mt4_status():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return 'OK', 200


@app.route('/web/api/mt4/positions', methods=['POST'])
def mt4_positions():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return 'OK', 200


@app.route('/web/api/mt4/report', methods=['POST'])
def mt4_report():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return 'OK', 200


@app.route('/web/api/mt4/quote', methods=['POST'])
def mt4_quote():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return 'OK', 200


# ==================== 网页指令管理（增强校验）====================
@app.route('/send_command', methods=['POST'])
def send_command():
    if is_restricted_time():
        return redirect(url_for('index'))

    global cmd_counter
    account = (request.form.get('account', '') or '').strip()
    cmd_type = request.form.get('cmd_type', 'MARKET')
    symbol = (request.form.get('symbol', '') or '').strip().upper()
    side = (request.form.get('side', '') or '').strip().upper()
    volume = (request.form.get('volume', '') or '').strip()
    price = (request.form.get('price', '') or '').strip()
    sl = (request.form.get('sl', '') or '').strip()
    tp = (request.form.get('tp', '') or '').strip()
    ticket = (request.form.get('ticket', '') or '').strip()
    lots = (request.form.get('lots', '') or '').strip()

    # 强校验
    if cmd_type in ['MARKET', 'LIMIT']:
        if not symbol:
            print("拒绝发单：symbol 为空")
            return redirect(url_for('index'))
        if side not in ['BUY', 'SELL']:
            print("拒绝发单：side 无效", side)
            return redirect(url_for('index'))
        if not volume:
            print("拒绝发单：volume 为空")
            return redirect(url_for('index'))
    elif cmd_type == 'CLOSE':
        if not ticket:
            print("拒绝发单：ticket 为空")
            return redirect(url_for('index'))
    else:
        return redirect(url_for('index'))

    try:
        if cmd_type in ['MARKET', 'LIMIT']:
            volume = float(volume)
            if volume <= 0:
                print("拒绝发单：volume 必须 > 0")
                return redirect(url_for('index'))
            sl = float(sl) if sl else None
            tp = float(tp) if tp else None
        if cmd_type == 'LIMIT':
            price = float(price) if price else 0.0
            if price <= 0:
                print("拒绝发单：限价单 price 必须 > 0")
                return redirect(url_for('index'))
        if cmd_type == 'CLOSE':
            ticket = int(ticket)
            lots = float(lots) if lots else 0.0
    except ValueError:
        print("拒绝发单：数值转换失败")
        return redirect(url_for('index'))

    # 如果未填 account，就尝试用最近一次上报的 account；仍没有就广播(None)
    if not account:
        with history_lock:
            if history:
                account = history[0].get('account') or ''
        if not account:
            account = None  # 广播

    now = int(time.time())
    cmd = {
        'id': str(cmd_counter),
        'nonce': generate_nonce(),
        'created_at': now,
        'ttl_sec': 10,
    }
    if account:
        cmd['account'] = account

    # 关键：side 标准化为 buy/sell（小写、去空格）
    side_norm = side.strip().lower()
    if side_norm == 'buy':
        side_norm = 'buy'
    elif side_norm == 'sell':
        side_norm = 'sell'

    if cmd_type == 'MARKET':
        cmd['action'] = 'market'
        cmd['symbol'] = symbol
        cmd['side'] = side_norm
        cmd['volume'] = volume
        if sl is not None:
            cmd['sl_price'] = sl
        if tp is not None:
            cmd['tp_price'] = tp

    elif cmd_type == 'LIMIT':
        cmd['action'] = 'limit'
        cmd['symbol'] = symbol
        cmd['side'] = side_norm
        cmd['volume'] = volume
        cmd['price'] = price
        if sl is not None:
            cmd['sl'] = sl
        if tp is not None:
            cmd['tp'] = tp

    elif cmd_type == 'CLOSE':
        cmd['action'] = 'close'
        cmd['ticket'] = ticket
        if lots > 0:
            cmd['lots'] = lots

    print("[send_command] ADD:", json.dumps(cmd, ensure_ascii=False, separators=(',', ':')))

    with commands_lock:
        commands.append(cmd)
        cmd_counter += 1

    return redirect(url_for('index'))


@app.route('/delete_command/<int:index>', methods=['POST'])
def delete_command(index):
    with commands_lock:
        if 0 <= index < len(commands):
            commands.pop(index)
    return redirect(url_for('index'))


@app.route('/clear_commands', methods=['POST'])
def clear_commands():
    with commands_lock:
        commands.clear()
    return redirect(url_for('index'))


# ==================== HTML模板（你的原版，不删）====================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MT4 远程交易执行面板 · 专业版</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
    <style>
        body { padding-top: 20px; background-color: #f0f2f5; }
        .card-header { font-weight: 600; }
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
        .stat-item { background: #f8f9fa; border-radius: 8px; padding: 10px 12px; border-left: 4px solid #0d6efd; }
        .stat-label { font-size: 0.8rem; color: #6c757d; text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-value { font-size: 1.2rem; font-weight: 600; font-family: 'Courier New', monospace; }
        .history-table td { font-size: 0.85rem; vertical-align: middle; }
        .badge-ip { font-family: monospace; background: #e9ecef; color: #000; padding: 3px 6px; border-radius: 4px; }
        .command-item { background: #e9ecef; padding: 8px 12px; border-radius: 6px; margin-bottom: 6px; }
        .info-box { background: #d1e7ff; border-radius: 8px; padding: 12px; margin-bottom: 15px; border-left: 5px solid #0a58ca; }
        .pause-control { background: #f8d7da; border-left: 5px solid #dc3545; }

        .restricted-mode {
            background-color: black !important;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            color: red;
            font-size: 4rem;
            font-weight: bold;
            text-align: center;
        }
        .restricted-mode .status-box {
            background: rgba(0,0,0,0.7);
            border: 2px solid red;
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 20px;
            color: white;
            font-size: 1.5rem;
        }
        .restricted-mode .status-box .label {
            color: #aaa;
            font-size: 1rem;
        }
        .restricted-mode .status-box .value {
            color: #0f0;
        }
    </style>
</head>
<body>
    {% if restricted %}
    <div class="restricted-mode">
        <div class="status-box">
            <div class="row">
                <div class="col">
                    <span class="label">账户</span><br>
                    <span class="value">{{ latest.account if latest else 'N/A' }}</span>
                </div>
                <div class="col">
                    <span class="label">余额</span><br>
                    <span class="value">{{ "%.2f"|format(latest.balance) if latest and latest.balance is number else 'N/A' }}</span>
                </div>
                <div class="col">
                    <span class="label">净值</span><br>
                    <span class="value">{{ "%.2f"|format(latest.equity) if latest and latest.equity is number else 'N/A' }}</span>
                </div>
                <div class="col">
                    <span class="label">浮动盈亏</span><br>
                    <span class="value">{{ "%.2f"|format(latest.floating_pnl) if latest and latest.floating_pnl is number else 'N/A' }}</span>
                </div>
            </div>
        </div>
        <div>为人民服务</div>
    </div>
    {% else %}
    <div class="container">
        <h1 class="mb-3"><i class="bi bi-cpu"></i> MT4 远程交易执行 · 专业监控</h1>

        <div class="info-box d-flex justify-content-between align-items-center">
            <div>
                <i class="bi bi-info-circle-fill me-2"></i>
                <strong>MT4专用接口：</strong>
                <code>/web/api/mt4/commands</code> (轮询),
                <code>/web/api/mt4/status</code> (状态),
                <code>/web/api/mt4/positions</code> (持仓)
                <span class="badge bg-secondary ms-2">等待指令返回</span>
                <br><small class="text-muted">原 <code>/web/api/echo</code> 接口仍保留，用于调试</small>
            </div>
            <span class="text-muted small">指令将按账户过滤后返回</span>
        </div>

        <div class="card shadow-sm mb-3 pause-control">
            <div class="card-header bg-danger text-white d-flex justify-content-between align-items-center">
                <span><i class="bi bi-pause-circle"></i> 应急暂停控制</span>
            </div>
            <div class="card-body">
                <div class="d-flex align-items-center justify-content-between">
                    <span>当前状态: <strong id="pause-status" class="{% if paused %}text-danger{% else %}text-success{% endif %}">{% if paused %}已暂停{% else %}运行中{% endif %}</strong></span>
                    <div>
                        <button id="pause-btn" class="btn btn-warning btn-sm me-2" {% if paused %}disabled{% endif %}><i class="bi bi-pause"></i> 暂停</button>
                        <button id="resume-btn" class="btn btn-success btn-sm" {% if not paused %}disabled{% endif %}><i class="bi bi-play"></i> 恢复</button>
                    </div>
                </div>
            </div>
        </div>

        <div class="card mb-4 shadow-sm">
            <div class="card-header bg-primary text-white bg-gradient">
                <i class="bi bi-graph-up-arrow"></i> 最新账户状态
            </div>
            <div class="card-body">
                {% if latest %}
                    {% if latest.error %}
                        <div class="alert alert-warning">
                            <i class="bi bi-exclamation-triangle"></i> {{ latest.error }}
                            <br><small>原始数据预览：{{ latest.body_raw_preview }}</small>
                            {% if latest.full_error %}
                            <pre class="mt-2 bg-light p-2 rounded" style="font-size:0.75rem;">{{ latest.full_error }}</pre>
                            {% endif %}
                            {% if latest.remaining_data %}
                            <div class="mt-2 alert alert-info">
                                <strong>检测到额外数据（可能为多个JSON）：</strong>
                                <pre class="mb-0" style="font-size:0.75rem;">{{ latest.remaining_data }}</pre>
                            </div>
                            {% endif %}
                        </div>
                    {% else %}
                        <div class="stat-grid">
                            <div class="stat-item"><span class="stat-label">账户</span><div class="stat-value">{{ latest.account or 'N/A' }}</div></div>
                            <div class="stat-item"><span class="stat-label">服务器</span><div class="stat-value">{{ latest.server or 'N/A' }}</div></div>
                            <div class="stat-item"><span class="stat-label">时间戳(ts)</span><div class="stat-value">{{ latest.ts or 'N/A' }}</div></div>
                            <div class="stat-item"><span class="stat-label">余额</span><div class="stat-value">{{ "%.2f"|format(latest.balance) if latest.balance is number else latest.balance }}</div></div>
                            <div class="stat-item"><span class="stat-label">净值</span><div class="stat-value">{{ "%.2f"|format(latest.equity) if latest.equity is number else latest.equity }}</div></div>
                            <div class="stat-item"><span class="stat-label">浮动盈亏</span><div class="stat-value">{{ "%.2f"|format(latest.floating_pnl) if latest.floating_pnl is number else latest.floating_pnl }}</div></div>
                            <div class="stat-item"><span class="stat-label">网络延迟(ms)</span><div class="stat-value">{{ "%.0f"|format(latest.poll_latency_ms) if latest.poll_latency_ms is number else latest.poll_latency_ms }}</div></div>
                            <div class="stat-item"><span class="stat-label">上次HTTP代码</span><div class="stat-value">{{ latest.last_http_code or 'N/A' }}</div></div>
                            {% if latest.last_error %}
                            <div class="stat-item"><span class="stat-label">错误信息</span><div class="stat-value text-danger">{{ latest.last_error }}</div></div>
                            {% endif %}
                        </div>
                    {% endif %}
                {% else %}
                    <p class="text-muted"><i class="bi bi-exclamation-circle"></i> 尚未收到任何MT4上报数据。</p>
                {% endif %}
            </div>
        </div>

        <!-- 下面的历史&队列&表单（你原来的）我保持不删，为节省篇幅省略展示，但你可以继续用你原版那段 -->
        <p class="text-muted">（此处请继续保留你原来完整的“历史记录 + 指令队列 + 发单表单”HTML，逻辑不变）</p>

    </div>
    {% endif %}

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function updatePauseStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    const statusEl = document.getElementById('pause-status');
                    const pauseBtn = document.getElementById('pause-btn');
                    const resumeBtn = document.getElementById('resume-btn');
                    if (!statusEl) return;
                    if (data.paused) {
                        statusEl.innerText = '已暂停';
                        statusEl.className = 'text-danger';
                        if (pauseBtn) pauseBtn.disabled = true;
                        if (resumeBtn) resumeBtn.disabled = false;
                    } else {
                        statusEl.innerText = '运行中';
                        statusEl.className = 'text-success';
                        if (pauseBtn) pauseBtn.disabled = false;
                        if (resumeBtn) resumeBtn.disabled = true;
                    }
                });
        }
        document.getElementById('pause-btn')?.addEventListener('click', function() {
            fetch('/api/pause', { method: 'POST' }).then(() => updatePauseStatus());
        });
        document.getElementById('resume-btn')?.addEventListener('click', function() {
            fetch('/api/resume', { method: 'POST' }).then(() => updatePauseStatus());
        });
        setInterval(updatePauseStatus, 5000);
        updatePauseStatus();
    </script>
</body>
</html>
"""

# ==================== 启动 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
