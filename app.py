import os
import json
import threading
import traceback
import time
import random
import string
from datetime import datetime, time as dt_time
from collections import deque
from flask import Flask, request, render_template_string, redirect, url_for, jsonify

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
    cleaned_body = raw_body.strip()
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
            print(f"检测到JSON后剩余数据: {remaining_data}")
    except json.JSONDecodeError as e:
        parse_error = str(e)
        parse_error_detail = traceback.format_exc()
        print(f"JSON解析错误: {e}")
        print(f"原始body(前500字符): {cleaned_body[:500]}")
    except Exception as e:
        parse_error = f"未知异常: {str(e)}"
        parse_error_detail = traceback.format_exc()
        print(f"解析时发生未知异常: {e}")

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

    parsed_json, record = store_mt4_data(raw_body, client_ip, headers_dict)

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
        return jsonify({'commands': [], 'paused': paused}), 200

    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)

    parsed_json, record = store_mt4_data(raw_body, client_ip, headers_dict)

    if parsed_json is None:
        return jsonify({'error': 'Invalid JSON', 'commands': []}), 400

    account = parsed_json.get('account')

    with commands_lock:
        account_commands = []
        remaining_commands = []
        for cmd in commands:
            # 如果命令没有 account 字段（即 cmd.get('account') is None），或账户匹配，则取出
            if cmd.get('account') is None or cmd.get('account') == account:
                account_commands.append(cmd)
            else:
                remaining_commands.append(cmd)
        commands[:] = remaining_commands

    # 调试打印：观察下发给 EA 的命令
    print("SEND CMDS:", json.dumps(account_commands, ensure_ascii=False))

    with pause_lock:
        current_paused = paused

    return jsonify({
        'commands': account_commands,
        'paused': current_paused
    }), 200

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

    # ===== 强校验：确保必要字段有效 =====
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

    # ===== 修复：账户处理，避免空字符串 =====
    if not account:
        with history_lock:
            if history:
                latest = history[0]
                account = latest.get('account')
        # 如果仍然没有，设为 None（即不加入 account 字段）
        if not account:
            account = None
    else:
        account = account.strip()

    now = int(time.time())
    # 基础命令结构（不包含 account）
    cmd = {
        'id': str(cmd_counter),
        'nonce': generate_nonce(),
        'created_at': now,
        'ttl_sec': 10,
    }
    # 只有 account 存在时才加入
    if account:
        cmd['account'] = account

    if cmd_type == 'MARKET':
        cmd['action'] = 'market'
        cmd['symbol'] = symbol
        cmd['side'] = side.lower()
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
        if lots > 0:
            cmd['lots'] = lots

    # 调试打印：观察加入队列的命令
    print("ADD CMD:", json.dumps(cmd, ensure_ascii=False))

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

# ==================== HTML模板（与之前完全相同）====================
HTML_TEMPLATE = """
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

        /* 限制模式样式 */
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
    <!-- 限制模式显示 -->
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
    <!-- 正常模式显示 -->
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

        <!-- 暂停控制卡片 -->
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

        <!-- 最新上报详细数据卡片 -->
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
                            <div class="stat-item"><span class="stat-label">已用预付款</span><div class="stat-value">{{ "%.2f"|format(latest.margin) if latest.margin is number else latest.margin }}</div></div>
                            <div class="stat-item"><span class="stat-label">可用预付款</span><div class="stat-value">{{ "%.2f"|format(latest.free_margin) if latest.free_margin is number else latest.free_margin }}</div></div>
                            <div class="stat-item"><span class="stat-label">预付款比例</span><div class="stat-value">{{ "%.2f"|format(latest.margin_level) if latest.margin_level is number else latest.margin_level }}%</div></div>
                            <div class="stat-item"><span class="stat-label">浮动盈亏</span><div class="stat-value">{{ "%.2f"|format(latest.floating_pnl) if latest.floating_pnl is number else latest.floating_pnl }}</div></div>
                            <div class="stat-item"><span class="stat-label">日初始净值</span><div class="stat-value">{{ "%.2f"|format(latest.day_start_equity) if latest.day_start_equity is number else latest.day_start_equity }}</div></div>
                            <div class="stat-item"><span class="stat-label">日盈亏</span><div class="stat-value">{{ "%.2f"|format(latest.daily_pnl) if latest.daily_pnl is number else latest.daily_pnl }}</div></div>
                            <div class="stat-item"><span class="stat-label">日盈亏率</span><div class="stat-value">{{ "%.5f"|format(latest.daily_return) if latest.daily_return is number else latest.daily_return }}</div></div>
                            <div class="stat-item"><span class="stat-label">网络延迟(ms)</span><div class="stat-value">{{ "%.0f"|format(latest.poll_latency_ms) if latest.poll_latency_ms is number else latest.poll_latency_ms }}</div></div>
                            <div class="stat-item"><span class="stat-label">上次HTTP代码</span><div class="stat-value">{{ latest.last_http_code or 'N/A' }}</div></div>
                            {% if latest.last_error %}
                            <div class="stat-item"><span class="stat-label">错误信息</span><div class="stat-value text-danger">{{ latest.last_error }}</div></div>
                            {% endif %}
                        </div>

                        <!-- 持仓列表（如果有） -->
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
                                                <th>订单号</th>
                                                <th>品种</th>
                                                <th>类型</th>
                                                <th>手数</th>
                                                <th>开仓价</th>
                                                <th>止损</th>
                                                <th>止盈</th>
                                                <th>开仓时间</th>
                                                <th>利润</th>
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

                        <!-- 如果有剩余数据，提示 -->
                        {% if latest.remaining_data %}
                        <div class="mt-3 alert alert-info">
                            <strong>检测到额外数据（可能为多个JSON）：</strong>
                            <pre class="mb-0" style="font-size:0.75rem;">{{ latest.remaining_data }}</pre>
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
                    <p class="text-muted"><i class="bi bi-exclamation-circle"></i> 尚未收到任何MT4上报数据，请等待终端上报或使用curl测试。</p>
                {% endif %}
            </div>
        </div>

        <!-- 两列布局：左侧历史记录，右侧指令管理 -->
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
                                        <th>时间</th>
                                        <th>账户</th>
                                        <th>余额</th>
                                        <th>净值</th>
                                        <th>浮动盈亏</th>
                                        <th>IP</th>
                                        <th>操作</th>
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
                                    <br><small class="text-muted"><i class="bi bi-clock"></i> {{ cmd.timestamp if cmd.timestamp else '' }} | 账户: {{ cmd.account if cmd.account else '无' }} | ID: {{ cmd.id }}</small>
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

                <!-- 增强版发单表单 -->
                <div class="card shadow-sm">
                    <div class="card-header bg-warning">
                        <i class="bi bi-pencil-square"></i> 下达新交易指令
                    </div>
                    <div class="card-body">
                        <form method="post" action="{{ url_for('send_command') }}" id="commandForm">
                            <div class="mb-2">
                                <label class="form-label">账户 <span class="text-muted">(可选，将自动保存)</span></label>
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

                            <!-- 市价/限价通用字段 -->
                            <div id="tradeFields">
                                <div class="mb-2">
                                    <label class="form-label">品种 <span class="text-muted">(如 EURUSD)</span></label>
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

                            <!-- 限价单专用字段 -->
                            <div id="limitFields" style="display: none;">
                                <div class="mb-2">
                                    <label class="form-label">限价价格</label>
                                    <input type="number" step="0.00001" name="price" class="form-control form-control-sm" placeholder="如 1.1050">
                                </div>
                            </div>

                            <!-- 平仓专用字段 -->
                            <div id="closeFields" style="display: none;">
                                <div class="mb-2">
                                    <label class="form-label">订单号 (ticket)</label>
                                    <input type="number" name="ticket" class="form-control form-control-sm" placeholder="如 12345678">
                                </div>
                                <div class="mb-2">
                                    <label class="form-label">手数 <span class="text-muted">(可选，留空则全部平仓)</span></label>
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
        // 账户本地存储
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

        // 暂停控制
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

        // 指令类型切换
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
