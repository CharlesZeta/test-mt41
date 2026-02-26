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
MAX_STATUS_HISTORY = 50     # 只存“真正的状态上报”
MAX_POLL_HISTORY = 200      # 存轮询请求（可选，用于排查）
MAX_RAW_HISTORY = 200       # 全量原始请求（可选）

status_history = deque(maxlen=MAX_STATUS_HISTORY)
poll_history = deque(maxlen=MAX_POLL_HISTORY)
raw_history = deque(maxlen=MAX_RAW_HISTORY)

history_lock = threading.Lock()

commands = []
commands_lock = threading.Lock()
cmd_counter = 0

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

def norm_str(x):
    if x is None:
        return ""
    return str(x).strip()

def norm_side(x):
    s = norm_str(x).lower()
    if s in ("buy", "sell"):
        return s
    if s in ("b", "long"):
        return "buy"
    if s in ("s", "short"):
        return "sell"
    return ""

def get_client_ip():
    return request.headers.get('X-Real-Ip') or request.headers.get('X-Forwarded-For', request.remote_addr)

def safe_json_load(raw_body: str):
    cleaned = (raw_body or "").strip()
    if not cleaned:
        return None, "empty body", None, None

    parsed_json = None
    parse_error = None
    parse_error_detail = None
    remaining_data = None

    try:
        decoder = json.JSONDecoder()
        parsed_json, idx = decoder.raw_decode(cleaned)
        remaining = cleaned[idx:].strip()
        if remaining:
            remaining_data = remaining[:200]
    except json.JSONDecodeError as e:
        parse_error = str(e)
        parse_error_detail = traceback.format_exc()
    except Exception as e:
        parse_error = f"unknown error: {str(e)}"
        parse_error_detail = traceback.format_exc()

    return parsed_json, parse_error, parse_error_detail, remaining_data

def push_raw_record(kind: str, raw_body: str, parsed_json, parse_error, parse_error_detail, remaining_data):
    rec = {
        "received_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "ip": get_client_ip(),
        "method": request.method,
        "path": request.path,
        "kind": kind,  # status / positions / report / quote / poll / echo / other
        "headers": dict(request.headers),
        "body_raw": raw_body,
        "parsed": parsed_json,
        "parse_error": parse_error,
        "parse_error_detail": parse_error_detail,
        "remaining_data": remaining_data,
    }
    with history_lock:
        raw_history.appendleft(rec)
    return rec

def push_status(parsed_json, raw_body):
    """只存真正的账户状态数据"""
    # 兼容：如果没 metrics 字段也别炸
    metrics = parsed_json.get("metrics") if isinstance(parsed_json, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}

    rec = {
        "received_at": datetime.now().strftime('%H:%M:%S'),
        "account": parsed_json.get("account"),
        "server": parsed_json.get("server"),
        "ts": parsed_json.get("ts"),
        "balance": parsed_json.get("balance"),
        "equity": parsed_json.get("equity"),
        "margin": parsed_json.get("margin"),
        "free_margin": parsed_json.get("free_margin"),
        "margin_level": parsed_json.get("margin_level"),
        "floating_pnl": parsed_json.get("floating_pnl"),
        "day_start_equity": parsed_json.get("day_start_equity"),
        "daily_closed_pnl": parsed_json.get("daily_closed_pnl"),
        "daily_pnl": parsed_json.get("daily_pnl"),
        "daily_return": parsed_json.get("daily_return"),
        "exposure_notional": parsed_json.get("exposure_notional"),
        "leverage_used": parsed_json.get("leverage_used"),
        "risk_flags": parsed_json.get("risk_flags"),

        # metrics
        "m_poll_latency_ms": metrics.get("poll_latency_ms"),
        "m_last_http_code": metrics.get("last_http_code"),
        "m_last_error": metrics.get("last_error"),
        "m_queue_batch_size": metrics.get("queue_batch_size"),
        "m_reports_sent_count": metrics.get("reports_sent_count"),
        "m_executed_commands": metrics.get("executed_commands"),
        "m_failed_commands": metrics.get("failed_commands"),

        # 原始预览
        "raw_preview": (raw_body[:300] + ("..." if len(raw_body) > 300 else "")),
    }

    with history_lock:
        status_history.appendleft(rec)

def push_poll(parsed_json, raw_body):
    rec = {
        "received_at": datetime.now().strftime('%H:%M:%S'),
        "account": (parsed_json or {}).get("account") if isinstance(parsed_json, dict) else None,
        "raw_preview": (raw_body[:200] + ("..." if len(raw_body) > 200 else "")),
    }
    with history_lock:
        poll_history.appendleft(rec)

def format_command_echo(cmd):
    """旧版 /web/api/echo 使用: side,symbol,volume,sl,tp"""
    side = cmd.get("side", "")
    symbol = cmd.get("symbol", "")
    volume = cmd.get("volume", "")
    base = f"{side},{symbol},{volume}"
    sl = cmd.get("sl_price", None)
    tp = cmd.get("tp_price", None)
    if sl is not None and tp is not None:
        return f"{base},{sl},{tp}"
    elif sl is not None:
        return f"{base},{sl},0"
    elif tp is not None:
        return f"{base},0,{tp}"
    return base


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


# ==================== 主页 ====================
@app.route('/')
def index():
    with history_lock:
        status_list = list(status_history)
        poll_list = list(poll_history)
        latest_status = status_list[0] if status_list else None
        raw_list = list(raw_history)[:50]  # 页面上只展示最近 50 条 raw（避免太大）

    with commands_lock:
        cmds_copy = list(commands)

    with pause_lock:
        current_paused = paused

    restricted = is_restricted_time()

    return render_template_string(
        HTML_TEMPLATE,
        status_history=status_list,
        poll_history=poll_list,
        raw_history=raw_list,
        latest_status=latest_status,
        commands=cmds_copy,
        MAX_STATUS_HISTORY=MAX_STATUS_HISTORY,
        paused=current_paused,
        restricted=restricted
    )


# ==================== 旧版 echo ====================
@app.route('/web/api/echo', methods=['POST'])
def mt4_webhook_echo():
    raw_body = request.get_data(as_text=True)
    parsed_json, parse_error, parse_error_detail, remaining_data = safe_json_load(raw_body)
    push_raw_record("echo", raw_body, parsed_json, parse_error, parse_error_detail, remaining_data)

    response_lines = []
    with commands_lock:
        if commands:
            for cmd in commands:
                response_lines.append(format_command_echo(cmd))
            commands.clear()

    if response_lines:
        return '\n'.join(response_lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}
    return 'NOCOMMAND', 200, {'Content-Type': 'text/plain; charset=utf-8'}


# ==================== MT4 专用接口 ====================
@app.route('/web/api/mt4/commands', methods=['POST'])
def mt4_commands():
    # 限制时段直接不给命令
    with pause_lock:
        current_paused = paused

    raw_body = request.get_data(as_text=True)
    parsed_json, parse_error, parse_error_detail, remaining_data = safe_json_load(raw_body)

    push_raw_record("poll", raw_body, parsed_json, parse_error, parse_error_detail, remaining_data)
    # 轮询请求不写入 status_history（防止 None 污染）
    if isinstance(parsed_json, dict):
        push_poll(parsed_json, raw_body)

    if is_restricted_time():
        return jsonify({'commands': [], 'paused': current_paused}), 200

    if parsed_json is None or not isinstance(parsed_json, dict):
        return jsonify({'error': 'Invalid JSON', 'commands': []}), 400

    account = parsed_json.get('account')

    # 取出队列里属于该账户的命令
    with commands_lock:
        account_commands = []
        remaining_commands = []
        for cmd in commands:
            # 只要 cmd 没写 account，视为广播；写了则必须匹配
            cmd_acc = cmd.get('account', None)
            if cmd_acc is None or cmd_acc == "" or cmd_acc == account:
                account_commands.append(cmd)
            else:
                remaining_commands.append(cmd)
        commands[:] = remaining_commands

    return jsonify({
        'commands': account_commands,
        'paused': current_paused
    }), 200


@app.route('/web/api/mt4/status', methods=['POST'])
def mt4_status():
    raw_body = request.get_data(as_text=True)
    parsed_json, parse_error, parse_error_detail, remaining_data = safe_json_load(raw_body)
    push_raw_record("status", raw_body, parsed_json, parse_error, parse_error_detail, remaining_data)

    if isinstance(parsed_json, dict) and not parse_error:
        push_status(parsed_json, raw_body)

    return 'OK', 200


@app.route('/web/api/mt4/positions', methods=['POST'])
def mt4_positions():
    raw_body = request.get_data(as_text=True)
    parsed_json, parse_error, parse_error_detail, remaining_data = safe_json_load(raw_body)
    push_raw_record("positions", raw_body, parsed_json, parse_error, parse_error_detail, remaining_data)
    return 'OK', 200


@app.route('/web/api/mt4/report', methods=['POST'])
def mt4_report():
    raw_body = request.get_data(as_text=True)
    parsed_json, parse_error, parse_error_detail, remaining_data = safe_json_load(raw_body)
    push_raw_record("report", raw_body, parsed_json, parse_error, parse_error_detail, remaining_data)
    return 'OK', 200


@app.route('/web/api/mt4/quote', methods=['POST'])
def mt4_quote():
    raw_body = request.get_data(as_text=True)
    parsed_json, parse_error, parse_error_detail, remaining_data = safe_json_load(raw_body)
    push_raw_record("quote", raw_body, parsed_json, parse_error, parse_error_detail, remaining_data)
    return 'OK', 200


# ==================== 网页发指令 ====================
@app.route('/send_command', methods=['POST'])
def send_command():
    if is_restricted_time():
        return redirect(url_for('index'))

    global cmd_counter
    account = norm_str(request.form.get('account', ''))
    cmd_type = norm_str(request.form.get('cmd_type', 'MARKET')).upper()

    symbol = norm_str(request.form.get('symbol', '')).upper()
    side_raw = request.form.get('side', '')
    side = norm_side(side_raw)

    volume_raw = norm_str(request.form.get('volume', ''))
    price_raw = norm_str(request.form.get('price', ''))
    sl_raw = norm_str(request.form.get('sl', ''))
    tp_raw = norm_str(request.form.get('tp', ''))
    ticket_raw = norm_str(request.form.get('ticket', ''))
    lots_raw = norm_str(request.form.get('lots', ''))

    # 强校验
    if cmd_type in ('MARKET', 'LIMIT'):
        if not symbol or side not in ('buy', 'sell') or not volume_raw:
            return redirect(url_for('index'))
    elif cmd_type == 'CLOSE':
        if not ticket_raw:
            return redirect(url_for('index'))
    else:
        return redirect(url_for('index'))

    try:
        volume = float(volume_raw) if volume_raw else 0.0
        if cmd_type in ('MARKET', 'LIMIT') and volume <= 0:
            return redirect(url_for('index'))

        sl = float(sl_raw) if sl_raw else None
        tp = float(tp_raw) if tp_raw else None

        if cmd_type == 'LIMIT':
            price = float(price_raw) if price_raw else 0.0
            if price <= 0:
                return redirect(url_for('index'))
        else:
            price = None

        if cmd_type == 'CLOSE':
            ticket = int(ticket_raw)
            lots = float(lots_raw) if lots_raw else 0.0
        else:
            ticket = None
            lots = 0.0
    except ValueError:
        return redirect(url_for('index'))

    # account：如果留空，自动用最近一次 status 的 account；如果仍没有，则不写 account（广播）
    if not account:
        with history_lock:
            if status_history and status_history[0].get("account"):
                account = str(status_history[0]["account"])
            else:
                account = ""  # 空表示广播（不强制匹配）

    now = int(time.time())
    cmd = {
        "id": str(cmd_counter),
        "nonce": generate_nonce(),
        "created_at": now,
        "ttl_sec": 10,
    }
    if account:
        cmd["account"] = account

    if cmd_type == 'MARKET':
        cmd["action"] = "market"
        cmd["symbol"] = symbol
        cmd["side"] = side
        cmd["volume"] = volume
        if sl is not None:
            cmd["sl_price"] = sl
        if tp is not None:
            cmd["tp_price"] = tp

    elif cmd_type == 'LIMIT':
        cmd["action"] = "limit"
        cmd["symbol"] = symbol
        cmd["side"] = side
        cmd["volume"] = volume
        cmd["price"] = price
        if sl is not None:
            cmd["sl"] = sl
        if tp is not None:
            cmd["tp"] = tp

    elif cmd_type == 'CLOSE':
        cmd["action"] = "close"
        cmd["ticket"] = ticket
        if lots and lots > 0:
            cmd["lots"] = lots

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


# ==================== HTML 模板（保留你的监控风格 + 补齐字段）====================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>MT4 远程交易执行面板</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
  <style>
    body { padding-top: 20px; background-color: #f0f2f5; }
    .card-header { font-weight: 600; }
    .stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; }
    .stat-item { background: #f8f9fa; border-radius: 8px; padding: 10px 12px; border-left: 4px solid #0d6efd; }
    .stat-label { font-size: 0.8rem; color: #6c757d; text-transform: uppercase; letter-spacing: 0.5px; }
    .stat-value { font-size: 1.05rem; font-weight: 600; font-family: 'Courier New', monospace; }
    .badge-ip { font-family: monospace; background: #e9ecef; color: #000; padding: 3px 6px; border-radius: 4px; }
    .command-item { background: #e9ecef; padding: 8px 12px; border-radius: 6px; margin-bottom: 6px; }
    .info-box { background: #d1e7ff; border-radius: 8px; padding: 12px; margin-bottom: 15px; border-left: 5px solid #0a58ca; }
    .pause-control { background: #f8d7da; border-left: 5px solid #dc3545; }

    .restricted-mode {
      background-color: black !important;
      min-height: 100vh;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      color: red; font-size: 4rem; font-weight: bold; text-align: center;
    }
    .restricted-mode .status-box {
      background: rgba(0,0,0,0.7);
      border: 2px solid red;
      border-radius: 10px;
      padding: 20px; margin-bottom: 20px;
      color: white; font-size: 1.2rem;
    }
    .restricted-mode .status-box .label { color: #aaa; font-size: 0.9rem; }
    .restricted-mode .status-box .value { color: #0f0; font-family: monospace; }
  </style>
</head>

<body>
{% if restricted %}
  <div class="restricted-mode">
    <div class="status-box">
      <div class="row">
        <div class="col">
          <span class="label">账户</span><br/>
          <span class="value">{{ latest_status.account if latest_status else 'N/A' }}</span>
        </div>
        <div class="col">
          <span class="label">余额</span><br/>
          <span class="value">{{ '%.2f'|format(latest_status.balance) if latest_status and latest_status.balance is number else 'N/A' }}</span>
        </div>
        <div class="col">
          <span class="label">净值</span><br/>
          <span class="value">{{ '%.2f'|format(latest_status.equity) if latest_status and latest_status.equity is number else 'N/A' }}</span>
        </div>
        <div class="col">
          <span class="label">浮动盈亏</span><br/>
          <span class="value">{{ '%.2f'|format(latest_status.floating_pnl) if latest_status and latest_status.floating_pnl is number else 'N/A' }}</span>
        </div>
      </div>
    </div>
    <div>为人民服务</div>
  </div>
{% else %}
  <div class="container">
    <h3 class="mb-3"><i class="bi bi-cpu"></i> MT4 远程交易执行 · 监控面板</h3>

    <div class="info-box d-flex justify-content-between align-items-center">
      <div>
        <i class="bi bi-info-circle-fill me-2"></i>
        <strong>MT4接口：</strong>
        <code>/web/api/mt4/commands</code> (轮询),
        <code>/web/api/mt4/status</code> (状态),
        <code>/web/api/mt4/positions</code> (持仓),
        <code>/web/api/mt4/report</code> (回报)
        <br/><small class="text-muted">说明：轮询请求不会再污染“账户状态”，避免刷 None。</small>
      </div>
    </div>

    <div class="card shadow-sm mb-3 pause-control">
      <div class="card-header bg-danger text-white d-flex justify-content-between align-items-center">
        <span><i class="bi bi-pause-circle"></i> 应急暂停控制</span>
      </div>
      <div class="card-body">
        <div class="d-flex align-items-center justify-content-between">
          <span>当前状态:
            <strong id="pause-status" class="{% if paused %}text-danger{% else %}text-success{% endif %}">
              {% if paused %}已暂停{% else %}运行中{% endif %}
            </strong>
          </span>
          <div>
            <button id="pause-btn" class="btn btn-warning btn-sm me-2" {% if paused %}disabled{% endif %}>
              <i class="bi bi-pause"></i> 暂停
            </button>
            <button id="resume-btn" class="btn btn-success btn-sm" {% if not paused %}disabled{% endif %}>
              <i class="bi bi-play"></i> 恢复
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 最新状态 -->
    <div class="card mb-4 shadow-sm">
      <div class="card-header bg-primary text-white bg-gradient">
        <i class="bi bi-graph-up-arrow"></i> 最新账户状态（完整字段）
      </div>
      <div class="card-body">
        {% if latest_status %}
          <div class="stat-grid">
            <div class="stat-item"><span class="stat-label">account</span><div class="stat-value">{{ latest_status.account or 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">server</span><div class="stat-value">{{ latest_status.server or 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">ts</span><div class="stat-value">{{ latest_status.ts or 'N/A' }}</div></div>

            <div class="stat-item"><span class="stat-label">balance</span><div class="stat-value">{{ '%.2f'|format(latest_status.balance) if latest_status.balance is number else (latest_status.balance or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">equity</span><div class="stat-value">{{ '%.2f'|format(latest_status.equity) if latest_status.equity is number else (latest_status.equity or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">margin</span><div class="stat-value">{{ '%.2f'|format(latest_status.margin) if latest_status.margin is number else (latest_status.margin or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">free_margin</span><div class="stat-value">{{ '%.2f'|format(latest_status.free_margin) if latest_status.free_margin is number else (latest_status.free_margin or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">margin_level</span><div class="stat-value">{{ '%.2f'|format(latest_status.margin_level) if latest_status.margin_level is number else (latest_status.margin_level or 'N/A') }}</div></div>

            <div class="stat-item"><span class="stat-label">floating_pnl</span><div class="stat-value">{{ '%.2f'|format(latest_status.floating_pnl) if latest_status.floating_pnl is number else (latest_status.floating_pnl or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">day_start_equity</span><div class="stat-value">{{ '%.2f'|format(latest_status.day_start_equity) if latest_status.day_start_equity is number else (latest_status.day_start_equity or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">daily_closed_pnl</span><div class="stat-value">{{ '%.2f'|format(latest_status.daily_closed_pnl) if latest_status.daily_closed_pnl is number else (latest_status.daily_closed_pnl or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">daily_pnl</span><div class="stat-value">{{ '%.2f'|format(latest_status.daily_pnl) if latest_status.daily_pnl is number else (latest_status.daily_pnl or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">daily_return</span><div class="stat-value">{{ '%.6f'|format(latest_status.daily_return) if latest_status.daily_return is number else (latest_status.daily_return or 'N/A') }}</div></div>

            <div class="stat-item"><span class="stat-label">exposure_notional</span><div class="stat-value">{{ '%.2f'|format(latest_status.exposure_notional) if latest_status.exposure_notional is number else (latest_status.exposure_notional or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">leverage_used</span><div class="stat-value">{{ '%.4f'|format(latest_status.leverage_used) if latest_status.leverage_used is number else (latest_status.leverage_used or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">risk_flags</span><div class="stat-value">{{ latest_status.risk_flags or '' }}</div></div>

            <div class="stat-item"><span class="stat-label">metrics.poll_latency_ms</span><div class="stat-value">{{ latest_status.m_poll_latency_ms or 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.last_http_code</span><div class="stat-value">{{ latest_status.m_last_http_code or 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.last_error</span><div class="stat-value">{{ latest_status.m_last_error or '' }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.queue_batch_size</span><div class="stat-value">{{ latest_status.m_queue_batch_size or 0 }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.reports_sent</span><div class="stat-value">{{ latest_status.m_reports_sent_count or 0 }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.executed</span><div class="stat-value">{{ latest_status.m_executed_commands or 0 }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.failed</span><div class="stat-value">{{ latest_status.m_failed_commands or 0 }}</div></div>
          </div>

          <div class="mt-3">
            <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#rawPreview">
              <i class="bi bi-code-slash"></i> 查看原始JSON预览
            </button>
            <div class="collapse mt-2" id="rawPreview">
              <pre class="bg-light p-3 rounded" style="font-size:0.75rem;">{{ latest_status.raw_preview }}</pre>
            </div>
          </div>
        {% else %}
          <p class="text-muted mb-0">尚未收到任何 /mt4/status 上报。</p>
        {% endif %}
      </div>
    </div>

    <div class="row">
      <!-- 左：状态历史 -->
      <div class="col-lg-7 mb-4">
        <div class="card shadow-sm h-100">
          <div class="card-header bg-secondary text-white">
            <i class="bi bi-clock-history"></i> 状态历史（仅 status，上限 {{ MAX_STATUS_HISTORY }}）
          </div>
          <div class="card-body p-0">
            <div class="table-responsive">
              <table class="table table-striped table-hover mb-0">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>账户</th>
                    <th>余额</th>
                    <th>净值</th>
                    <th>浮动盈亏</th>
                    <th>风险</th>
                  </tr>
                </thead>
                <tbody>
                  {% for rec in status_history %}
                  <tr>
                    <td>{{ rec.received_at }}</td>
                    <td>{{ rec.account or '-' }}</td>
                    <td>{{ '%.2f'|format(rec.balance) if rec.balance is number else (rec.balance or '-') }}</td>
                    <td>{{ '%.2f'|format(rec.equity) if rec.equity is number else (rec.equity or '-') }}</td>
                    <td>{{ '%.2f'|format(rec.floating_pnl) if rec.floating_pnl is number else (rec.floating_pnl or '-') }}</td>
                    <td>{{ rec.risk_flags or '' }}</td>
                  </tr>
                  {% else %}
                  <tr><td colspan="6" class="text-center text-muted">暂无 status 历史</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- 轮询历史（可折叠） -->
        <div class="card shadow-sm mt-3">
          <div class="card-header bg-light">
            <button class="btn btn-sm btn-outline-primary" type="button" data-bs-toggle="collapse" data-bs-target="#pollCollapse">
              <i class="bi bi-arrow-repeat"></i> 轮询历史（commands poll） ({{ poll_history|length }})
            </button>
          </div>
          <div class="collapse" id="pollCollapse">
            <div class="card-body">
              <ul class="mb-0">
                {% for p in poll_history %}
                  <li><code>{{ p.received_at }}</code> account={{ p.account or 'N/A' }} | {{ p.raw_preview }}</li>
                {% endfor %}
              </ul>
            </div>
          </div>
        </div>

      </div>

      <!-- 右：命令队列 + 发单 -->
      <div class="col-lg-5 mb-4">
        <div class="card shadow-sm mb-4">
          <div class="card-header bg-success text-white d-flex justify-content-between align-items-center">
            <span><i class="bi bi-list-check"></i> 待发送指令队列 ({{ commands|length }})</span>
            <form method="post" action="{{ url_for('clear_commands') }}" style="display:inline;">
              <button type="submit" class="btn btn-sm btn-light" onclick="return confirm('确定清空所有指令？')">
                <i class="bi bi-trash"></i> 清空
              </button>
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
                    平仓 ticket={{ cmd.ticket }}{% if cmd.lots %} lots={{ cmd.lots }}{% endif %}
                  {% endif %}
                  <br><small class="text-muted">账户: {{ cmd.account if cmd.account else '广播' }} | ID: {{ cmd.id }}</small>
                </div>
                <form method="post" action="{{ url_for('delete_command', index=loop.index0) }}" style="margin:0;">
                  <button type="submit" class="btn btn-sm btn-outline-danger" onclick="return confirm('删除该指令？')">
                    <i class="bi bi-x"></i>
                  </button>
                </form>
              </div>
              {% endfor %}
            {% else %}
              <p class="text-muted mb-0"><i class="bi bi-inbox"></i> 队列为空。</p>
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
                <label class="form-label">账户（可选，不填=自动取最新status账户；仍无则广播）</label>
                <input type="text" name="account" class="form-control form-control-sm" placeholder="833711" id="accountInput">
              </div>
              <div class="mb-2">
                <label class="form-label">指令类型</label>
                <select name="cmd_type" class="form-select form-select-sm" id="cmdTypeSelect" required>
                  <option value="MARKET" selected>市价单</option>
                  <option value="LIMIT">限价单</option>
                  <option value="CLOSE">平仓</option>
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
                    <option value="BUY">BUY</option>
                    <option value="SELL">SELL</option>
                  </select>
                </div>
                <div class="mb-2">
                  <label class="form-label">手数</label>
                  <input type="number" step="0.01" min="0.01" name="volume" class="form-control form-control-sm" value="0.1">
                </div>
                <div class="row">
                  <div class="col mb-2">
                    <label class="form-label">止损(SL)</label>
                    <input type="number" step="0.00001" name="sl" class="form-control form-control-sm" placeholder="可选">
                  </div>
                  <div class="col mb-2">
                    <label class="form-label">止盈(TP)</label>
                    <input type="number" step="0.00001" name="tp" class="form-control form-control-sm" placeholder="可选">
                  </div>
                </div>
              </div>

              <div id="limitFields" style="display:none;">
                <div class="mb-2">
                  <label class="form-label">限价价格</label>
                  <input type="number" step="0.00001" name="price" class="form-control form-control-sm" placeholder="如 1.1050">
                </div>
              </div>

              <div id="closeFields" style="display:none;">
                <div class="mb-2">
                  <label class="form-label">ticket</label>
                  <input type="number" name="ticket" class="form-control form-control-sm" placeholder="如 12345678">
                </div>
                <div class="mb-2">
                  <label class="form-label">lots（可选，空=全平）</label>
                  <input type="number" step="0.01" min="0.01" name="lots" class="form-control form-control-sm" placeholder="可选">
                </div>
              </div>

              <button type="submit" class="btn btn-primary w-100 mt-2">
                <i class="bi bi-send"></i> 加入指令队列
              </button>
            </form>
          </div>
        </div>

        <div class="card shadow-sm mt-3">
          <div class="card-header bg-light">
            <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#rawCollapse">
              <i class="bi bi-bug"></i> 最近 raw 请求（调试）
            </button>
          </div>
          <div class="collapse" id="rawCollapse">
            <div class="card-body">
              {% for r in raw_history %}
                <div class="mb-2">
                  <div><strong>{{ r.received_at }}</strong> [{{ r.kind }}] {{ r.path }} | ip={{ r.ip }}</div>
                  <pre class="bg-light p-2 rounded" style="font-size:0.75rem;white-space:pre-wrap;">{{ r.body_raw }}</pre>
                </div>
              {% endfor %}
            </div>
          </div>
        </div>

      </div>
    </div>

  </div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
  <script>
    // 暂停控制
    function updatePauseStatus() {
      fetch('/api/status')
        .then(r => r.json())
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
    document.getElementById('pause-btn')?.addEventListener('click', () => fetch('/api/pause', {method:'POST'}).then(updatePauseStatus));
    document.getElementById('resume-btn')?.addEventListener('click', () => fetch('/api/resume', {method:'POST'}).then(updatePauseStatus));
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
    }
    cmdTypeSelect.addEventListener('change', toggleFields);
    toggleFields();
  </script>
{% endif %}
</body>
</html>
"""

# ==================== 启动 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
