import os
import json
import threading
import traceback
import time
import random
import string
from datetime import datetime
from collections import deque
from flask import Flask, request, render_template_string, redirect, url_for, jsonify

app = Flask(__name__)

# ==================== 全局数据结构 ====================
MAX_HISTORY = 50

# 主历史：只存“有效上报”(status/positions/report/quote)
history = deque(maxlen=MAX_HISTORY)
history_lock = threading.Lock()

# 轮询历史：只存“commands轮询请求”(可选展示/排查)
poll_history = deque(maxlen=MAX_HISTORY)
poll_history_lock = threading.Lock()

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

def _safe_json_parse(raw_body: str):
    cleaned_body = (raw_body or "").strip()
    if not cleaned_body:
        return None, "empty_body", None, None

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
            print(f"[WARN] 检测到JSON后剩余数据(前200): {remaining_data}")
    except json.JSONDecodeError as e:
        parse_error = str(e)
        parse_error_detail = traceback.format_exc()
        print(f"[ERROR] JSON解析错误: {e}")
        print(f"[ERROR] 原始body(前500): {cleaned_body[:500]}")
    except Exception as e:
        parse_error = f"未知异常: {str(e)}"
        parse_error_detail = traceback.format_exc()
        print(f"[ERROR] 解析时发生未知异常: {e}")

    return parsed_json, parse_error, parse_error_detail, remaining_data

def store_mt4_data(raw_body, client_ip, headers_dict, category: str):
    """
    category:
      - 'poll' : /mt4/commands 轮询请求
      - 'status'/'positions'/'report'/'quote' : 有效上报
      - 'echo' : 旧接口调试
    """
    parsed_json, parse_error, parse_error_detail, remaining_data = _safe_json_parse(raw_body)

    record = {
        'received_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ip': client_ip,
        'method': request.method,
        'path': request.path,
        'category': category,
        'headers': headers_dict,
        'body_raw': raw_body,
        'parsed': parsed_json,
        'parse_error': parse_error,
        'parse_error_detail': parse_error_detail,
        'remaining_data': remaining_data,
        # 常用字段（有就填，没有就 None）
        'account': parsed_json.get('account') if isinstance(parsed_json, dict) else None,
        'server': parsed_json.get('server') if isinstance(parsed_json, dict) else None,
        'balance': parsed_json.get('balance') if isinstance(parsed_json, dict) else None,
        'equity': parsed_json.get('equity') if isinstance(parsed_json, dict) else None,
        'floating_pnl': parsed_json.get('floating_pnl') if isinstance(parsed_json, dict) else None,
    }

    # 分类入库：轮询请求不进主 history，避免“空值污染”
    if category == "poll":
        with poll_history_lock:
            poll_history.appendleft(record)
    else:
        with history_lock:
            history.appendleft(record)

    return parsed_json, record

def extract_latest_details(record):
    if not record:
        return None

    base_info = {
        'received_at': record.get('received_at'),
        'ip': record.get('ip'),
        'category': record.get('category'),
        'body_raw_preview': (record.get('body_raw', '')[:500] + ('...' if len(record.get('body_raw', '')) > 500 else ''))
    }

    if record.get('parse_error'):
        return {
            **base_info,
            'error': f"JSON 解析失败: {record['parse_error']}",
            'full_error': record.get('parse_error_detail', ''),
            'remaining_data': record.get('remaining_data')
        }

    parsed = record.get('parsed')
    if not isinstance(parsed, dict):
        return {**base_info, 'error': 'JSON 解析失败或不是对象'}

    metrics = parsed.get('metrics', {}) if isinstance(parsed.get('metrics', {}), dict) else {}

    positions = parsed.get('positions', [])
    if isinstance(positions, list):
        for pos in positions:
            if isinstance(pos, dict) and 'open_time' in pos and isinstance(pos['open_time'], (int, float)):
                try:
                    pos['open_time_str'] = datetime.fromtimestamp(pos['open_time']).strftime('%Y-%m-%d %H:%M:%S')
                except:
                    pos['open_time_str'] = str(pos['open_time'])
            elif isinstance(pos, dict):
                pos['open_time_str'] = 'N/A'

    # 把你 EA status 里给到的字段尽量都映射出来（没有就 None）
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
        'daily_closed_pnl': parsed.get('daily_closed_pnl'),
        'daily_pnl': parsed.get('daily_pnl'),
        'daily_return': parsed.get('daily_return'),

        'exposure_notional': parsed.get('exposure_notional'),
        'leverage_used': parsed.get('leverage_used'),
        'risk_flags': parsed.get('risk_flags'),

        'metrics_poll_latency_ms': metrics.get('poll_latency_ms'),
        'metrics_last_http_code': metrics.get('last_http_code'),
        'metrics_last_error': metrics.get('last_error'),
        'metrics_queue_batch_size': metrics.get('queue_batch_size'),
        'metrics_reports_sent_count': metrics.get('reports_sent_count'),
        'metrics_executed_commands': metrics.get('executed_commands'),
        'metrics_failed_commands': metrics.get('failed_commands'),

        'positions': positions if isinstance(positions, list) else [],
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
    # 最新记录：直接取 history[0]（因为 appendleft）
    with history_lock:
        latest_record = history[0] if history else None
        latest_detail = extract_latest_details(latest_record)
        # 表格展示：给你一份“倒序(旧->新)”更好看；但 latest 不再用它
        history_list_for_table = list(reversed(history))

    with poll_history_lock:
        poll_list_for_table = list(reversed(poll_history))

    with commands_lock:
        cmds_copy = commands.copy()

    with pause_lock:
        current_paused = paused

    restricted = is_restricted_time()

    return render_template_string(
        HTML_TEMPLATE,
        history=history_list_for_table,
        poll_history=poll_list_for_table,
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

    store_mt4_data(raw_body, client_ip, headers_dict, category="echo")

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
    if is_restricted_time():
        with pause_lock:
            current_paused = paused
        return jsonify({'commands': [], 'paused': current_paused}), 200

    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)

    # 这是轮询请求：单独存 poll_history（不污染主 history）
    parsed_json, _ = store_mt4_data(raw_body, client_ip, headers_dict, category="poll")

    if parsed_json is None or not isinstance(parsed_json, dict):
        return jsonify({'error': 'Invalid JSON', 'commands': []}), 400

    account = parsed_json.get('account')

    with commands_lock:
        account_commands = []
        remaining_commands = []
        for cmd in commands:
            # 重要：account 为空(None) 代表“广播给所有账户”
            if cmd.get('account') is None or cmd.get('account') == account:
                account_commands.append(cmd)
            else:
                remaining_commands.append(cmd)
        commands[:] = remaining_commands

    print("[SEND CMDS]", json.dumps(account_commands, ensure_ascii=False))

    with pause_lock:
        current_paused = paused

    return jsonify({'commands': account_commands, 'paused': current_paused}), 200


@app.route('/web/api/mt4/status', methods=['POST'])
def mt4_status():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict, category="status")
    return 'OK', 200

@app.route('/web/api/mt4/positions', methods=['POST'])
def mt4_positions():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict, category="positions")
    return 'OK', 200

@app.route('/web/api/mt4/report', methods=['POST'])
def mt4_report():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict, category="report")
    return 'OK', 200

@app.route('/web/api/mt4/quote', methods=['POST'])
def mt4_quote():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict, category="quote")
    return 'OK', 200


# ==================== 网页指令管理 ====================
@app.route('/send_command', methods=['POST'])
def send_command():
    if is_restricted_time():
        return redirect(url_for('index'))

    global cmd_counter
    account = request.form.get('account', '').strip()
    cmd_type = request.form.get('cmd_type', 'MARKET')
    symbol = request.form.get('symbol', '').strip().upper()
    side = request.form.get('side', '').strip().upper()
    volume = request.form.get('volume', '').strip()
    price = request.form.get('price', '').strip()
    sl = request.form.get('sl', '').strip()
    tp = request.form.get('tp', '').strip()
    ticket = request.form.get('ticket', '').strip()
    lots = request.form.get('lots', '').strip()

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

    # 账户处理：
    # - 空 → 尝试从最新 status 记录里拿
    # - 仍为空 → None（代表广播给所有账户）
    if not account:
        with history_lock:
            if history and isinstance(history[0].get("parsed"), dict):
                account = history[0]["parsed"].get("account")
        if not account:
            account = None

    now = int(time.time())
    cmd = {
        'id': str(cmd_counter),
        'nonce': generate_nonce(),
        'created_at': now,
        'ttl_sec': 10,
    }
    if account:
        cmd['account'] = str(account)

    if cmd_type == 'MARKET':
        cmd['action'] = 'market'
        cmd['symbol'] = symbol
        cmd['side'] = side.lower()  # buy/sell
        cmd['volume'] = volume
        if sl is not None:
            cmd['sl_price'] = sl
        if tp is not None:
            cmd['tp_price'] = tp

    elif cmd_type == 'LIMIT':
        cmd['action'] = 'limit'
        cmd['symbol'] = symbol
        cmd['side'] = side.lower()
        cmd['volume'] = volume
        cmd['price'] = price
        if sl is not None:
            cmd['sl'] = sl
        if tp is not None:
            cmd['tp'] = tp

    elif cmd_type == 'CLOSE':
        cmd['action'] = 'close'
        cmd['ticket'] = ticket
        if lots and lots > 0:
            cmd['lots'] = lots

    print("[ADD CMD]", json.dumps(cmd, ensure_ascii=False))

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



# ==================== HTML模板（保持你的原版，不删）====================
HTML_TEMPLATE = """\
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
                <i class="bi bi-graph-up-arrow"></i> 最新账户状态（完整字段）
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
                            <div class="stat-item"><span class="stat-label">ACCOUNT</span><div class="stat-value">{{ latest.account or 'N/A' }}</div></div>
                            <div class="stat-item"><span class="stat-label">SERVER</span><div class="stat-value">{{ latest.server or 'N/A' }}</div></div>
                            <div class="stat-item"><span class="stat-label">TS</span><div class="stat-value">{{ latest.ts or 'N/A' }}</div></div>

                            <div class="stat-item"><span class="stat-label">BALANCE</span><div class="stat-value">{{ "%.2f"|format(latest.balance) if latest.balance is number else latest.balance }}</div></div>
                            <div class="stat-item"><span class="stat-label">EQUITY</span><div class="stat-value">{{ "%.2f"|format(latest.equity) if latest.equity is number else latest.equity }}</div></div>

                            <div class="stat-item"><span class="stat-label">MARGIN</span><div class="stat-value">{{ "%.2f"|format(latest.margin) if latest.margin is number else latest.margin }}</div></div>
                            <div class="stat-item"><span class="stat-label">FREE_MARGIN</span><div class="stat-value">{{ "%.2f"|format(latest.free_margin) if latest.free_margin is number else latest.free_margin }}</div></div>
                            <div class="stat-item"><span class="stat-label">MARGIN_LEVEL</span><div class="stat-value">{{ "%.2f"|format(latest.margin_level) if latest.margin_level is number else latest.margin_level }}{% if latest.margin_level is number %}%{% endif %}</div></div>

                            <div class="stat-item"><span class="stat-label">FLOATING_PNL</span><div class="stat-value">{{ "%.2f"|format(latest.floating_pnl) if latest.floating_pnl is number else latest.floating_pnl }}</div></div>
                            <div class="stat-item"><span class="stat-label">DAY_START_EQUITY</span><div class="stat-value">{{ "%.2f"|format(latest.day_start_equity) if latest.day_start_equity is number else latest.day_start_equity }}</div></div>
                            <div class="stat-item"><span class="stat-label">DAILY_CLOSED_PNL</span><div class="stat-value">{{ "%.2f"|format(latest.daily_closed_pnl) if latest.daily_closed_pnl is number else latest.daily_closed_pnl }}</div></div>
                            <div class="stat-item"><span class="stat-label">DAILY_PNL</span><div class="stat-value">{{ "%.2f"|format(latest.daily_pnl) if latest.daily_pnl is number else latest.daily_pnl }}</div></div>
                            <div class="stat-item"><span class="stat-label">DAILY_RETURN</span><div class="stat-value">{{ "%.8f"|format(latest.daily_return) if latest.daily_return is number else latest.daily_return }}</div></div>

                            <div class="stat-item"><span class="stat-label">EXPOSURE_NOTIONAL</span><div class="stat-value">{{ "%.2f"|format(latest.exposure_notional) if latest.exposure_notional is number else latest.exposure_notional }}</div></div>
                            <div class="stat-item"><span class="stat-label">LEVERAGE_USED</span><div class="stat-value">{{ "%.4f"|format(latest.leverage_used) if latest.leverage_used is number else latest.leverage_used }}</div></div>
                            <div class="stat-item"><span class="stat-label">RISK_FLAGS</span><div class="stat-value">{{ latest.risk_flags or '' }}</div></div>

                            <div class="stat-item"><span class="stat-label">METRICS.POLL_LATENCY_MS</span><div class="stat-value">{{ "%.0f"|format(latest.poll_latency_ms) if latest.poll_latency_ms is number else latest.poll_latency_ms }}</div></div>
                            <div class="stat-item"><span class="stat-label">METRICS.LAST_HTTP_CODE</span><div class="stat-value">{{ latest.last_http_code or 'N/A' }}</div></div>
                            <div class="stat-item"><span class="stat-label">METRICS.LAST_ERROR</span><div class="stat-value">{{ latest.last_error or '' }}</div></div>
                            <div class="stat-item"><span class="stat-label">METRICS.QUEUE_BATCH_SIZE</span><div class="stat-value">{{ latest.queue_batch_size or 0 }}</div></div>
                            <div class="stat-item"><span class="stat-label">METRICS.REPORTS_SENT</span><div class="stat-value">{{ latest.reports_sent_count or 0 }}</div></div>
                            <div class="stat-item"><span class="stat-label">METRICS.EXECUTED</span><div class="stat-value">{{ latest.executed_commands or 0 }}</div></div>
                            <div class="stat-item"><span class="stat-label">METRICS.FAILED</span><div class="stat-value">{{ latest.failed_commands or 0 }}</div></div>
                        </div>

                        {% if latest.positions %}
                        <div class="mt-4">
                            <button class="btn btn-sm btn-outline-primary" type="button" data-bs-toggle="collapse" data-bs-target="#positionsCollapse" aria-expanded="false">
                                <i class="bi bi-list-ul"></i> 显示持仓 ({{ latest.positions|length }})
                            </button>
                            <div class="collapse mt-2" id="positionsCollapse">
                                <div class="card card-body p-0">
                                    <table class="table table-sm table-striped mb-0">
                                        <thead>
                                            <tr>
                                                <th>订单号</th><th>品种</th><th>类型</th><th>手数</th><th>开仓价</th><th>止损</th><th>止盈</th><th>开仓时间</th><th>利润</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for pos in latest.positions %}
                                            <tr>
                                                <td>{{ pos.ticket }}</td>
                                                <td>{{ pos.symbol }}</td>
                                                <td>{{ pos.type }}</td>
                                                <td>{{ pos.lots }}</td>
                                                <td>{{ pos.open_price }}</td>
                                                <td>{{ pos.sl }}</td>
                                                <td>{{ pos.tp }}</td>
                                                <td>{{ pos.open_time_str }}</td>
                                                <td>{{ "%.2f"|format(pos.profit) if pos.profit is number else pos.profit }}</td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                        {% endif %}

                    {% endif %}
                    <div class="mt-3">
                        <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#rawJsonPreview" aria-expanded="false">
                            <i class="bi bi-code-slash"></i> 查看原始JSON
                        </button>
                        <div class="collapse mt-2" id="rawJsonPreview">
                            <pre class="bg-light p-3 rounded" style="font-size:0.75rem;">{{ latest_raw.body_raw if latest_raw else '无原始数据' }}</pre>
                        </div>
                    </div>
                {% else %}
                    <p class="text-muted"><i class="bi bi-exclamation-circle"></i> 尚未收到任何 MT4 status 上报，请等待终端上报。</p>
                {% endif %}
            </div>
        </div>

        <div class="row">
            <div class="col-lg-7 mb-4">
                <div class="card shadow-sm h-100">
                    <div class="card-header bg-secondary text-white">
                        <i class="bi bi-clock-history"></i> 最近上报历史 ({{ history|length }}/{{ MAX_HISTORY }})
                    </div>
                    <div class="card-body p-0">
                        <div class="table-responsive">
                            <table class="table table-striped table-hover history-table mb-0">
                                <thead>
                                    <tr>
                                        <th>时间</th><th>账户</th><th>余额</th><th>净值</th><th>浮动盈亏</th><th>IP</th><th>操作</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for rec in history %}
                                    <tr>
                                        <td>{{ rec.received_at.split(' ')[1] }}</td>
                                        <td>{{ rec.account or '-' }}</td>
                                        <td>{{ "%.2f"|format(rec.balance) if rec.balance is number else rec.balance }}</td>
                                        <td>{{ "%.2f"|format(rec.equity) if rec.equity is number else rec.equity }}</td>
                                        <td>{{ "%.2f"|format(rec.floating_pnl) if rec.floating_pnl is number else rec.floating_pnl }}</td>
                                        <td><span class="badge-ip">{{ rec.ip }}</span></td>
                                        <td>
                                            <button class="btn btn-sm btn-outline-info" type="button"
                                                    data-bs-toggle="collapse" data-bs-target="#raw-{{ loop.index }}"
                                                    aria-expanded="false"><i class="bi bi-eye"></i></button>
                                            <div class="collapse mt-1" id="raw-{{ loop.index }}">
                                                <div class="card card-body p-2">
                                                    <small>{{ rec.body_raw }}</small>
                                                </div>
                                            </div>
                                        </td>
                                    </tr>
                                    {% else %}
                                    <tr><td colspan="7" class="text-center text-muted">暂无历史数据</td></tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <div class="col-lg-5 mb-4">
                <div class="card shadow-sm mb-4">
                    <div class="card-header bg-success text-white d-flex justify-content-between align-items-center">
                        <span><i class="bi bi-list-check"></i> 待发送指令队列 ({{ commands|length }})</span>
                        <form method="post" action="{{ url_for('clear_commands') }}" style="display:inline;">
                            <button type="submit" class="btn btn-sm btn-light" onclick="return confirm('确定清空所有指令？')"><i class="bi bi-trash"></i> 清空</button>
                        </form>
                    </div>
                    <div class="card-body">
                        {% if commands %}
                            {% for cmd in commands %}
                            <div class="command-item d-flex justify-content-between align-items-center">
                                <div>
                                    <strong>{{ cmd.action }}</strong>
                                    {% if cmd.action == 'market' %}
                                        {{ cmd.side.upper() }} {{ cmd.symbol }} {{ cmd.volume }}手
                                        {% if cmd.sl_price %} SL:{{ cmd.sl_price }}{% endif %}
                                        {% if cmd.tp_price %} TP:{{ cmd.tp_price }}{% endif %}
                                    {% elif cmd.action == 'limit' %}
                                        {{ cmd.side.upper() }} {{ cmd.symbol }} {{ cmd.volume }}手 @ {{ cmd.price }}
                                        {% if cmd.sl %} SL:{{ cmd.sl }}{% endif %}
                                        {% if cmd.tp %} TP:{{ cmd.tp }}{% endif %}
                                    {% elif cmd.action == 'close' %}
                                        平仓 票号:{{ cmd.ticket }}{% if cmd.lots %} 手数:{{ cmd.lots }}{% endif %}
                                    {% endif %}
                                    <br><small class="text-muted"><i class="bi bi-clock"></i> 账户: {{ cmd.account if cmd.account else '无' }} | ID: {{ cmd.id }}</small>
                                </div>
                                <form method="post" action="{{ url_for('delete_command', index=loop.index0) }}" style="margin:0;">
                                    <button type="submit" class="btn btn-sm btn-outline-danger" onclick="return confirm('删除该指令？')"><i class="bi bi-x"></i></button>
                                </form>
                            </div>
                            {% endfor %}
                        {% else %}
                            <p class="text-muted mb-0"><i class="bi bi-inbox"></i> 队列为空，暂无待发指令。</p>
                        {% endif %}
                    </div>
                </div>

                <div class="card shadow-sm">
                    <div class="card-header bg-warning">
                        <i class="bi bi-pencil-square"></i> 下达新交易指令
                    </div>
                    <div class="card-body">
                        <form method="post" action="{{ url_for('send_command') }}" id="commandForm">
                            <div class="mb-2">
                                <label class="form-label">账户 <span class="text-muted">(可选)</span></label>
                                <input type="text" name="account" class="form-control form-control-sm" placeholder="833711" id="accountInput">
                            </div>
                            <div class="mb-2">
                                <label class="form-label">指令类型</label>
                                <select name="cmd_type" class="form-select form-select-sm" id="cmdTypeSelect" required>
                                    <option value="MARKET" selected>市价单 (MARKET)</option>
                                    <option value="LIMIT">限价单 (LIMIT)</option>
                                    <option value="CLOSE">平仓 (CLOSE)</option>
                                </select>
                            </div>

                            <div id="tradeFields">
                                <div class="mb-2">
                                    <label class="form-label">品种</label>
                                    <input type="text" name="symbol" class="form-control form-control-sm" placeholder="EURUSD">
                                </div>
                                <div class="mb-2">
                                    <label class="form-label">方向</label>
                                    <select name="side" class="form-select form-select-sm">
                                        <option value="BUY">买入 (BUY)</option>
                                        <option value="SELL">卖出 (SELL)</option>
                                    </select>
                                </div>
                                <div class="mb-2">
                                    <label class="form-label">手数</label>
                                    <input type="number" step="0.01" min="0.01" name="volume" class="form-control form-control-sm" value="0.1">
                                </div>
                                <div class="row">
                                    <div class="col mb-2">
                                        <label class="form-label">止损 (SL)</label>
                                        <input type="number" step="0.00001" name="sl" class="form-control form-control-sm" placeholder="可选">
                                    </div>
                                    <div class="col mb-2">
                                        <label class="form-label">止盈 (TP)</label>
                                        <input type="number" step="0.00001" name="tp" class="form-control form-control-sm" placeholder="可选">
                                    </div>
                                </div>
                            </div>

                            <div id="limitFields" style="display: none;">
                                <div class="mb-2">
                                    <label class="form-label">限价价格</label>
                                    <input type="number" step="0.00001" name="price" class="form-control form-control-sm" placeholder="如 1.1050">
                                </div>
                            </div>

                            <div id="closeFields" style="display: none;">
                                <div class="mb-2">
                                    <label class="form-label">订单号 (ticket)</label>
                                    <input type="number" name="ticket" class="form-control form-control-sm" placeholder="如 12345678">
                                </div>
                                <div class="mb-2">
                                    <label class="form-label">手数 (可选)</label>
                                    <input type="number" step="0.01" min="0.01" name="lots" class="form-control form-control-sm" placeholder="可选">
                                </div>
                            </div>

                            <button type="submit" class="btn btn-primary w-100 mt-2"><i class="bi bi-send"></i> 加入指令队列</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>

    </div>
    {% endif %}

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const savedAccount = localStorage.getItem('mt4_default_account');
            if (savedAccount) {
                document.getElementById('accountInput').value = savedAccount;
            }
        });

        document.getElementById('commandForm')?.addEventListener('submit', function() {
            const accountInput = document.getElementById('accountInput').value;
            if (accountInput) {
                localStorage.setItem('mt4_default_account', accountInput);
            }
        });

        function updatePauseStatus() {
            fetch('/api/status')
                .then(response => response.json())
                .then(data => {
                    const statusEl = document.getElementById('pause-status');
                    const pauseBtn = document.getElementById('pause-btn');
                    const resumeBtn = document.getElementById('resume-btn');
                    if (data.paused) {
                        statusEl.innerText = '已暂停';
                        statusEl.className = 'text-danger';
                        pauseBtn.disabled = true;
                        resumeBtn.disabled = false;
                    } else {
                        statusEl.innerText = '运行中';
                        statusEl.className = 'text-success';
                        pauseBtn.disabled = false;
                        resumeBtn.disabled = true;
                    }
                });
        }

        document.getElementById('pause-btn')?.addEventListener('click', function() {
            fetch('/api/pause', { method: 'POST' })
                .then(response => response.json())
                .then(data => updatePauseStatus());
        });

        document.getElementById('resume-btn')?.addEventListener('click', function() {
            fetch('/api/resume', { method: 'POST' })
                .then(response => response.json())
                .then(data => updatePauseStatus());
        });

        setInterval(updatePauseStatus, 5000);
        updatePauseStatus();

        const cmdTypeSelect = document.getElementById('cmdTypeSelect');
        const tradeFields = document.getElementById('tradeFields');
        const limitFields = document.getElementById('limitFields');
        const closeFields = document.getElementById('closeFields');

        function toggleFields() {
            const type = cmdTypeSelect.value;
            tradeFields.style.display = (type === 'MARKET' || type === 'LIMIT') ? 'block' : 'none';
            limitFields.style.display = (type === 'LIMIT') ? 'block' : 'none';
            closeFields.style.display = (type === 'CLOSE') ? 'block' : 'none';

            document.querySelector('[name="symbol"]').required = (type === 'MARKET' || type === 'LIMIT');
            document.querySelector('[name="side"]').required = (type === 'MARKET' || type === 'LIMIT');
            document.querySelector('[name="volume"]').required = (type === 'MARKET' || type === 'LIMIT');
            document.querySelector('[name="ticket"]').required = (type === 'CLOSE');
        }

        cmdTypeSelect.addEventListener('change', toggleFields);
        toggleFields();
    </script>
</body>
</html>
"""

# ==================== 启动 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)


