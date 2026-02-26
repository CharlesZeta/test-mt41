import os
import json
import threading
import traceback
import time
import random
import string
from datetime import datetime
from collections import deque, defaultdict
from flask import Flask, request, render_template_string, redirect, url_for, jsonify

app = Flask(__name__)

# ==================== 全局数据结构 ====================
MAX_HISTORY = 50
history = deque(maxlen=MAX_HISTORY)
history_lock = threading.Lock()

# 按账号分队列：commands_by_account["833711"] -> deque([...])
# 特殊 key: "*" 表示广播队列（任何账号都可以取到）
commands_by_account = defaultdict(lambda: deque(maxlen=500))
commands_lock = threading.Lock()

# 记录最近一次 MT4 轮询（commands）上来的账号，按 IP 记
last_account_by_ip = {}
last_account_lock = threading.Lock()

# 暂停状态
paused = False
pause_lock = threading.Lock()

# ==================== 时间限制函数 ====================
def is_restricted_time():
    """判断当前时间是否处于限制时段（0:30 - 4:30）"""
    now = datetime.now()
    h, m = now.hour, now.minute
    if h == 0 and m >= 30:
        return True
    if 1 <= h <= 3:
        return True
    if h == 4 and m <= 30:
        return True
    return False

# ==================== 工具函数 ====================
def generate_nonce(k=16):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=k))

def now_ts():
    return int(time.time())

def mk_cmd_id():
    # 避免重启重复：毫秒时间戳 + nonce
    return f"{int(time.time()*1000)}_{generate_nonce(8)}"

def norm_str(x):
    return (x or "").strip()

def norm_side(x):
    s = norm_str(x).lower()
    if s in ("buy", "sell"):
        return s
    if s in ("b", "long"):
        return "buy"
    if s in ("s", "short"):
        return "sell"
    return ""

def norm_symbol(x):
    return norm_str(x).upper()

def get_client_ip():
    return request.headers.get('X-Real-Ip') or request.headers.get('X-Forwarded-For', request.remote_addr)

def safe_json_load(raw_text: str):
    """更稳健的 JSON 解析：允许 body 前后空白；若多 JSON 拼接，只取第一个并记录剩余"""
    cleaned = (raw_text or "").strip()
    if not cleaned:
        return None, "empty_body", None
    try:
        dec = json.JSONDecoder()
        obj, idx = dec.raw_decode(cleaned)
        remaining = cleaned[idx:].strip()
        return obj, None, (remaining[:200] if remaining else None)
    except json.JSONDecodeError as e:
        return None, str(e), None
    except Exception as e:
        return None, f"unknown_error:{e}", None

def store_mt4_data(raw_body, client_ip, headers_dict):
    parsed_json, parse_error, remaining_data = safe_json_load(raw_body)
    record = {
        'received_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ip': client_ip,
        'method': request.method,
        'path': request.path,
        'headers': headers_dict,
        'body_raw': raw_body,
        'parsed': parsed_json,
        'parse_error': parse_error,
        'remaining_data': remaining_data,
        'account': parsed_json.get('account') if isinstance(parsed_json, dict) else None,
        'server': parsed_json.get('server') if isinstance(parsed_json, dict) else None,
        'balance': parsed_json.get('balance') if isinstance(parsed_json, dict) else None,
        'equity': parsed_json.get('equity') if isinstance(parsed_json, dict) else None,
        'floating_pnl': parsed_json.get('floating_pnl') if isinstance(parsed_json, dict) else None,
    }
    with history_lock:
        history.appendleft(record)
    return parsed_json, record

def validate_cmd(cmd: dict):
    """
    校验命令完整性：
    - market/limit 必须有 symbol、side buy/sell、volume>0
    - close 必须有 ticket
    """
    action = (cmd.get("action") or "").lower().strip()

    if action in ("market", "limit"):
        sym = norm_str(cmd.get("symbol"))
        side = norm_side(cmd.get("side"))
        vol = cmd.get("volume")
        try:
            vol_ok = float(vol) > 0
        except Exception:
            vol_ok = False

        if not sym:
            return False, "missing_symbol"
        if side not in ("buy", "sell"):
            return False, "invalid_side"
        if not vol_ok:
            return False, "invalid_volume"

        # 统一 side 写回（确保下发就是 buy/sell）
        cmd["side"] = side
        return True, "ok"

    if action == "close":
        ticket = cmd.get("ticket")
        try:
            return (int(ticket) > 0), "ok" if int(ticket) > 0 else "missing_ticket"
        except Exception:
            return False, "missing_ticket"

    if action == "quote":
        syms = cmd.get("symbols", [])
        if not isinstance(syms, list) or not syms:
            return False, "missing_symbols"
        return True, "ok"

    return False, "unknown_action"

def enqueue_cmd(cmd: dict):
    """
    入队：按 account 分队列
    - cmd["account"] 缺失 => 广播队列 "*"
    """
    acct = cmd.get("account")
    acct = norm_str(acct)
    key = acct if acct else "*"
    with commands_lock:
        commands_by_account[key].append(cmd)

def dequeue_cmds_for_account(account: str, max_n=50):
    """
    出队：先取该 account 队列，再取广播队列
    """
    acct = norm_str(account)
    out = []
    with commands_lock:
        # 专属队列
        if acct and acct in commands_by_account:
            q = commands_by_account[acct]
            while q and len(out) < max_n:
                out.append(q.popleft())

        # 广播队列
        qb = commands_by_account["*"]
        while qb and len(out) < max_n:
            out.append(qb.popleft())

    return out

# ==================== 暂停控制接口 ====================
@app.route('/api/pause', methods=['POST'])
def api_pause():
    global paused
    with pause_lock:
        paused = True
    return jsonify({'paused': True})

@app.route('/api/resume', methods=['POST'])
def api_resume():
    global paused
    with pause_lock:
        paused = False
    return jsonify({'paused': False})

@app.route('/api/status', methods=['GET'])
def api_status():
    with pause_lock:
        return jsonify({'paused': paused})

# ==================== 主页 ====================
@app.route('/')
def index():
    with history_lock:
        hist_list = list(reversed(history))
        latest_record = hist_list[0] if hist_list else None

    # 展示队列（汇总）
    with commands_lock:
        q_summary = {k: len(v) for k, v in commands_by_account.items() if len(v) > 0}

    with pause_lock:
        current_paused = paused

    restricted = is_restricted_time()

    return render_template_string(
        HTML_TEMPLATE,
        history=hist_list,
        latest=latest_record,
        paused=current_paused,
        restricted=restricted,
        q_summary=q_summary
    )

# ==================== MT4 专用接口 ====================
@app.route('/web/api/mt4/commands', methods=['POST'])
def mt4_commands():
    if is_restricted_time():
        with pause_lock:
            return jsonify({'commands': [], 'paused': paused}), 200

    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    parsed_json, _ = store_mt4_data(raw_body, client_ip, headers_dict)

    if not isinstance(parsed_json, dict):
        return jsonify({'error': 'Invalid JSON', 'commands': []}), 400

    account = norm_str(parsed_json.get('account'))

    # 记录该 IP 最近轮询账号，供网页默认账号用
    if account:
        with last_account_lock:
            last_account_by_ip[client_ip] = account

    # 出队
    cmds = dequeue_cmds_for_account(account, max_n=50)

    # 下发前再做一遍强校验，防止队列被污染
    valid_cmds = []
    rejected = []
    for c in cmds:
        ok, reason = validate_cmd(c)
        if ok:
            valid_cmds.append(c)
        else:
            rejected.append({**c, "_reject": reason})

    if rejected:
        print("REJECTED:", json.dumps(rejected, ensure_ascii=False))
    print("SEND CMDS:", json.dumps(valid_cmds, ensure_ascii=False))

    with pause_lock:
        current_paused = paused

    return jsonify({'commands': valid_cmds, 'paused': current_paused}), 200

@app.route('/web/api/mt4/status', methods=['POST'])
def mt4_status():
    raw_body = request.get_data(as_text=True)
    store_mt4_data(raw_body, get_client_ip(), dict(request.headers))
    return 'OK', 200

@app.route('/web/api/mt4/positions', methods=['POST'])
def mt4_positions():
    raw_body = request.get_data(as_text=True)
    store_mt4_data(raw_body, get_client_ip(), dict(request.headers))
    return 'OK', 200

@app.route('/web/api/mt4/report', methods=['POST'])
def mt4_report():
    raw_body = request.get_data(as_text=True)
    store_mt4_data(raw_body, get_client_ip(), dict(request.headers))
    return 'OK', 200

@app.route('/web/api/mt4/quote', methods=['POST'])
def mt4_quote():
    raw_body = request.get_data(as_text=True)
    store_mt4_data(raw_body, get_client_ip(), dict(request.headers))
    return 'OK', 200

# ==================== 网页发单 ====================
@app.route('/send_command', methods=['POST'])
def send_command():
    if is_restricted_time():
        return redirect(url_for('index'))

    client_ip = get_client_ip()

    # 表单读取
    account = norm_str(request.form.get('account', ''))
    cmd_type = norm_str(request.form.get('cmd_type', 'MARKET')).upper()

    symbol = norm_symbol(request.form.get('symbol', ''))
    side_ui = norm_str(request.form.get('side', '')).upper()  # BUY/SELL
    volume_raw = norm_str(request.form.get('volume', ''))
    price_raw = norm_str(request.form.get('price', ''))
    sl_raw = norm_str(request.form.get('sl', ''))
    tp_raw = norm_str(request.form.get('tp', ''))
    ticket_raw = norm_str(request.form.get('ticket', ''))
    lots_raw = norm_str(request.form.get('lots', ''))

    # 若没填账号：使用该 IP 最近 MT4 轮询账号
    if not account:
        with last_account_lock:
            account = last_account_by_ip.get(client_ip, "")

    # 基础校验 + 类型转换
    try:
        if cmd_type in ('MARKET', 'LIMIT'):
            if not symbol:
                print("拒绝发单：symbol 为空")
                return redirect(url_for('index'))

            if side_ui not in ('BUY', 'SELL'):
                print("拒绝发单：side 无效", side_ui)
                return redirect(url_for('index'))

            if not volume_raw:
                print("拒绝发单：volume 为空")
                return redirect(url_for('index'))

            volume = float(volume_raw)
            if volume <= 0:
                print("拒绝发单：volume 必须 > 0")
                return redirect(url_for('index'))

            sl = float(sl_raw) if sl_raw else None
            tp = float(tp_raw) if tp_raw else None

            if cmd_type == 'LIMIT':
                price = float(price_raw) if price_raw else 0.0
                if price <= 0:
                    print("拒绝发单：LIMIT price 必须 > 0")
                    return redirect(url_for('index'))

        elif cmd_type == 'CLOSE':
            if not ticket_raw:
                print("拒绝发单：ticket 为空")
                return redirect(url_for('index'))
            ticket = int(ticket_raw)
            lots = float(lots_raw) if lots_raw else 0.0

        else:
            print("拒绝发单：cmd_type 无效", cmd_type)
            return redirect(url_for('index'))

    except Exception as e:
        print("拒绝发单：参数解析失败", e)
        return redirect(url_for('index'))

    # 构造命令（注意：account 只有非空才写入）
    cmd = {
        "id": mk_cmd_id(),
        "nonce": generate_nonce(),
        "created_at": now_ts(),
        "ttl_sec": 10,
    }
    if account:
        cmd["account"] = account

    if cmd_type == 'MARKET':
        cmd["action"] = "market"
        cmd["symbol"] = symbol
        cmd["side"] = "buy" if side_ui == "BUY" else "sell"
        cmd["volume"] = volume
        if sl is not None:
            cmd["sl_price"] = sl
        if tp is not None:
            cmd["tp_price"] = tp

    elif cmd_type == 'LIMIT':
        cmd["action"] = "limit"
        cmd["symbol"] = symbol
        cmd["side"] = "buy" if side_ui == "BUY" else "sell"
        cmd["volume"] = volume
        cmd["price"] = price
        if sl is not None:
            cmd["sl"] = sl
        if tp is not None:
            cmd["tp"] = tp

    elif cmd_type == 'CLOSE':
        cmd["action"] = "close"
        cmd["ticket"] = ticket
        if lots > 0:
            cmd["lots"] = lots

    # 入队前强校验：确保不会产生空字段命令
    ok, reason = validate_cmd(cmd)
    if not ok:
        print("拒绝入队：命令非法", reason, json.dumps(cmd, ensure_ascii=False))
        return redirect(url_for('index'))

    print("ADD CMD:", json.dumps(cmd, ensure_ascii=False))
    enqueue_cmd(cmd)
    return redirect(url_for('index'))

@app.route('/clear_commands', methods=['POST'])
def clear_commands():
    with commands_lock:
        commands_by_account.clear()
    return redirect(url_for('index'))

# ==================== HTML（精简版）====================
HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>MT4 Remote Panel</title>
  <style>
    body { font-family: Arial; margin: 20px; }
    .box { border: 1px solid #ddd; padding: 12px; margin-bottom: 14px; border-radius: 8px; }
    .muted { color: #666; font-size: 12px; }
    input, select { padding: 6px; margin: 4px 0; width: 240px; }
    button { padding: 8px 12px; }
    code { background: #f5f5f5; padding: 2px 6px; border-radius: 4px; }
  </style>
</head>
<body>
{% if restricted %}
  <h1 style="color:red">限制时段（0:30-4:30）</h1>
{% endif %}

<div class="box">
  <h3>队列概览</h3>
  <div class="muted">按账号分队列，"*" 为广播队列（任何账号都能取）。</div>
  <pre>{{ q_summary }}</pre>
  <form method="post" action="{{ url_for('clear_commands') }}">
    <button type="submit" onclick="return confirm('清空全部队列？')">清空队列</button>
  </form>
</div>

<div class="box">
  <h3>下达指令</h3>
  <form method="post" action="{{ url_for('send_command') }}">
    <div>
      <label>账户（可空，自动用最近 MT4 轮询账号）</label><br/>
      <input name="account" placeholder="833711"/>
    </div>
    <div>
      <label>类型</label><br/>
      <select name="cmd_type">
        <option value="MARKET">MARKET</option>
        <option value="LIMIT">LIMIT</option>
        <option value="CLOSE">CLOSE</option>
      </select>
    </div>
    <div>
      <label>品种</label><br/>
      <input name="symbol" placeholder="EURUSD"/>
    </div>
    <div>
      <label>方向</label><br/>
      <select name="side">
        <option value="BUY">BUY</option>
        <option value="SELL">SELL</option>
      </select>
    </div>
    <div>
      <label>手数</label><br/>
      <input name="volume" value="0.1"/>
    </div>
    <div>
      <label>限价价格（LIMIT 用）</label><br/>
      <input name="price" placeholder="1.10000"/>
    </div>
    <div>
      <label>止损 SL（可选）</label><br/>
      <input name="sl" placeholder="可选"/>
    </div>
    <div>
      <label>止盈 TP（可选）</label><br/>
      <input name="tp" placeholder="可选"/>
    </div>
    <div>
      <label>ticket（CLOSE 用）</label><br/>
      <input name="ticket" placeholder="12345678"/>
    </div>
    <div>
      <label>lots（CLOSE 可选）</label><br/>
      <input name="lots" placeholder="0.1"/>
    </div>
    <button type="submit">加入队列</button>
  </form>
</div>

<div class="box">
  <h3>最近上报（history, {{ history|length }}/50）</h3>
  {% if latest %}
    <div class="muted">最新路径：{{ latest.path }} | IP: {{ latest.ip }} | time: {{ latest.received_at }}</div>
    <pre style="white-space:pre-wrap">{{ latest.body_raw[:800] }}</pre>
  {% else %}
    <div class="muted">暂无数据</div>
  {% endif %}
</div>

<div class="box">
  <h3>MT4 接口</h3>
  <div><code>/web/api/mt4/commands</code> 轮询</div>
  <div><code>/web/api/mt4/status</code> 状态</div>
  <div><code>/web/api/mt4/positions</code> 持仓</div>
  <div><code>/web/api/mt4/report</code> 回报</div>
</div>
</body>
</html>
"""

# ==================== 启动 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
