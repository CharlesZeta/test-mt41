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

# 分类别存储，避免互相污染
history_status = deque(maxlen=MAX_HISTORY)     # /mt4/status
history_positions = deque(maxlen=MAX_HISTORY)  # /mt4/positions
history_report = deque(maxlen=MAX_HISTORY)     # /mt4/report
history_poll = deque(maxlen=MAX_HISTORY)       # /mt4/commands 轮询请求（account/max）
history_echo = deque(maxlen=MAX_HISTORY)       # /web/api/echo

history_lock = threading.Lock()

commands = []
commands_lock = threading.Lock()
cmd_counter = 0

paused = False
pause_lock = threading.Lock()

# ==================== 命令过期清理 ====================
def cleanup_expired_commands():
    """清理过期命令，防止积压"""
    now = int(time.time())
    with commands_lock:
        original_len = len(commands)
        valid = [c for c in commands if now - c.get("created_at", 0) < c.get("ttl_sec", 10)]
        commands[:] = valid
        removed = original_len - len(valid)
        if removed > 0:
            print(f"[CLEANUP] 清理了 {removed} 条过期命令，剩余 {len(commands)} 条")

# 定时清理线程
def cleanup_scheduler():
    while True:
        time.sleep(5)  # 每5秒检查一次
        cleanup_expired_commands()

cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
cleanup_thread.start()

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
def generate_nonce():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=16))

def norm_str(x):
    if x is None:
        return ""
    return str(x).strip()

def norm_side(x):
    """side 归一化：兼容 buy/sell/b/s/long/short"""
    s = norm_str(x).lower()
    if s in ("buy", "sell"):
        return s
    if s in ("b", "long"):
        return "buy"
    if s in ("s", "short"):
        return "sell"
    return ""

def norm_symbol(x):
    """symbol 归一化：大写 + 去除空格"""
    s = norm_str(x).strip().upper()
    return s

def norm_volume(x):
    """volume 归一化：兼容 volume/lots/size"""
    try:
        v = float(x)
        return v if v > 0 else 0
    except (ValueError, TypeError):
        return 0

def get_client_ip():
    return request.headers.get('X-Real-Ip') or request.headers.get('X-Forwarded-For', request.remote_addr)

def try_parse_json(raw_body: str):
    cleaned = (raw_body or "").strip()
    if not cleaned:
        return None, None, None, None

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
            print(f"[WARN] 检测到JSON后剩余数据: {remaining_data}")
    except json.JSONDecodeError as e:
        parse_error = str(e)
        parse_error_detail = traceback.format_exc()
        print(f"[ERR] JSON解析错误: {e}")
        print(f"[ERR] 原始body(前500字符): {cleaned[:500]}")
    except Exception as e:
        parse_error = f"未知异常: {str(e)}"
        parse_error_detail = traceback.format_exc()
        print(f"[ERR] 解析时发生未知异常: {e}")

    return parsed_json, parse_error, parse_error_detail, remaining_data

def detect_category(path: str, parsed_json: dict):
    """按接口路径 + body结构判断分类"""
    if path.endswith("/web/api/mt4/status"):
        return "status"
    if path.endswith("/web/api/mt4/positions"):
        return "positions"
    if path.endswith("/web/api/mt4/report"):
        return "report"
    if path.endswith("/web/api/echo"):
        return "echo"
    if path.endswith("/web/api/mt4/commands"):
        # 轮询请求 body 只包含 account 和 max
        if isinstance(parsed_json, dict) and set(parsed_json.keys()).issubset({"account", "max"}):
            return "poll"
        return "poll"  # 默认归为 poll，避免污染其他分类
    return "other"

def store_mt4_data(raw_body, client_ip, headers_dict):
    parsed_json, parse_error, parse_error_detail, remaining_data = try_parse_json(raw_body)
    category = detect_category(request.path, parsed_json if isinstance(parsed_json, dict) else None)

    record = {
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip": client_ip,
        "method": request.method,
        "path": request.path,
        "category": category,
        "headers": headers_dict,
        "body_raw": raw_body,
        "parsed": parsed_json,
        "parse_error": parse_error,
        "parse_error_detail": parse_error_detail,
        "remaining_data": remaining_data,
        "account": parsed_json.get("account") if isinstance(parsed_json, dict) else None,
        "server": parsed_json.get("server") if isinstance(parsed_json, dict) else None,
        "balance": parsed_json.get("balance") if isinstance(parsed_json, dict) else None,
        "equity": parsed_json.get("equity") if isinstance(parsed_json, dict) else None,
        "floating_pnl": parsed_json.get("floating_pnl") if isinstance(parsed_json, dict) else None,
        "leverage_used": parsed_json.get("leverage_used") if isinstance(parsed_json, dict) else None,
        "risk_flags": parsed_json.get("risk_flags") if isinstance(parsed_json, dict) else None,
        "exposure_notional": parsed_json.get("exposure_notional") if isinstance(parsed_json, dict) else None,
        "positions": parsed_json.get("positions") if isinstance(parsed_json, dict) else None,
    }

    with history_lock:
        if category == "status":
            history_status.appendleft(record)
        elif category == "positions":
            history_positions.appendleft(record)
        elif category == "report":
            history_report.appendleft(record)
        elif category == "poll":
            history_poll.appendleft(record)
        elif category == "echo":
            history_echo.appendleft(record)
        else:
            # 其他未知类型，可丢弃或存入一个专门队列
            pass

    return parsed_json, record

def safe_num(x):
    return isinstance(x, (int, float))

# ==================== 日内计算（UTC+8）====================
# 存储每个账户的日初净值（UTC+8 0点刷新）
day_start_equity_store = {}  # {account: (timestamp, equity)}

def get_utc8_now():
    """获取当前 UTC+8 时间戳（秒）"""
    return int(time.time()) + 8 * 3600

def is_utc8_new_day(last_ts, current_ts):
    """判断 UTC+8 时间戳是否跨了一天"""
    from datetime import datetime
    def utc8_date(ts):
        return datetime.utcfromtimestamp(ts + 8*3600).date()
    return utc8_date(last_ts) != utc8_date(current_ts)

def get_day_start_equity(account, current_equity):
    """
    获取日初净值（UTC+8 0点为界）
    - 如果跨了新的一天，更新日初净值为当前净值
    - 否则返回上次记录的日初净值
    """
    global day_start_equity_store
    now = get_utc8_now()

    if account not in day_start_equity_store:
        # 首次记录
        day_start_equity_store[account] = (now, current_equity)
        return current_equity

    last_ts, last_equity = day_start_equity_store[account]
    if is_utc8_new_day(last_ts, now):
        # 新的一天，重置日初净值为当前净值
        day_start_equity_store[account] = (now, current_equity)
        return current_equity

    return last_equity

def calc_exposure_notional(symbol, equity, position_pct, leverage, point_value):
    """
    计算 exposure_notional（每点收益影响）
    symbol: 交易品种
    equity: 当前净值
    position_pct: 用户选择的仓位比例（0-100）
    leverage: 杠杆倍数
    point_value: 该品种每波动1点的资金影响（从EA获取）
    
    返回：每点波动对账户的盈亏金额
    """
    if not equity or equity <= 0:
        return 0.0
    
    # 用户选择的仓位对应的资金量
    position_value = equity * (position_pct / 100.0)
    
    # 理论手数 = 仓位资金 / (账户净值 × 杠杆) 
    # 实际上：手数 = (equity × pct% × leverage) / 当前价格（简化计算）
    # 简化：直接用 position_value × leverage 作为名义本金，再乘以 point_value
    if leverage and leverage > 0:
        # 名义本金 = 仓位资金 × 杠杆
        notional = position_value * leverage
    else:
        notional = position_value
    
    # 每点收益 = 名义本金 × point_value（point_value 已单位化）
    if point_value:
        return notional * point_value
    else:
        return 0.0

def calc_exposure_signal(margin_level, position_pct, leverage=1):
    """
    计算 exposure_signal 风控信号
    margin_level: 保证金比例 %
    position_pct: 仓位比例 0-100
    leverage: 杠杆倍数
    返回: "green", "yellow", "red"
    """
    if not margin_level or margin_level <= 0:
        return "green"

    # 计算有效杠杆 = margin_level * position_pct / 100
    effective_leverage = margin_level * (position_pct / 100.0)

    # 阈值判断（可配置）
    YELLOW_THRESHOLD = 3.0   # 3 倍
    RED_THRESHOLD = 5.0      # 5 倍

    if effective_leverage >= RED_THRESHOLD:
        return "red"
    elif effective_leverage >= YELLOW_THRESHOLD:
        return "yellow"
    else:
        return "green"

def auto_fill_status(parsed: dict, positions=None, position_pct=0):
    """
    自动补齐 / 兜底计算：
    - margin_level = equity / margin * 100
    - floating_pnl 缺失 -> 0
    - daily_pnl = equity - day_start_equity (UTC+8 0点为界)
    - daily_return = daily_pnl / day_start_equity (%)
    - exposure_notional: 用户选择仓位 * 杠杆 * 品种点值
    - exposure_signal: green/yellow/red 风控灯
    - risk_flags 缺失 -> ""
    - metrics 缺失 -> 补全所有字段为 None 或 0（计数类为 0）
    - free_margin = equity - margin（如果两者都存在）
    """
    if not isinstance(parsed, dict):
        return parsed

    equity = parsed.get("equity")
    balance = parsed.get("balance")
    margin = parsed.get("margin")
    free_margin = parsed.get("free_margin")
    account = parsed.get("account")

    # floating_pnl：如果没给，就用 0
    if parsed.get("floating_pnl") is None:
        parsed["floating_pnl"] = 0.0

    # margin_level：如果缺失且 margin>0 就算；否则给 None
    if parsed.get("margin_level") is None:
        if safe_num(equity) and safe_num(margin) and margin > 0:
            parsed["margin_level"] = (equity / margin) * 100
        else:
            parsed["margin_level"] = None

    # ===== 日内计算（UTC+8 0点为界）=====
    if account and safe_num(equity):
        # 获取日初净值（UTC+8 0点刷新）
        day_start_eq = get_day_start_equity(account, equity)
        parsed["day_start_equity"] = day_start_eq

        # 计算日内盈亏 = 当前净值 - 日初净值
        daily_pnl = equity - day_start_eq
        parsed["daily_pnl"] = daily_pnl

        # 日内收益率（转为百分比）
        if day_start_eq and day_start_eq != 0:
            parsed["daily_return"] = (daily_pnl / day_start_eq) * 100
        else:
            parsed["daily_return"] = None
    else:
        # 如果没有 account 或 equity，保持原有逻辑
        if parsed.get("day_start_equity") is None:
            parsed["day_start_equity"] = None
        if parsed.get("daily_pnl") is None:
            dcp = parsed.get("daily_closed_pnl", 0)
            fp = parsed.get("floating_pnl", 0)
            if safe_num(dcp) and safe_num(fp):
                parsed["daily_pnl"] = dcp + fp
            else:
                parsed["daily_pnl"] = None
        if parsed.get("daily_return") is None:
            dse = parsed.get("day_start_equity")
            dpnl = parsed.get("daily_pnl")
            if safe_num(dse) and dse != 0 and safe_num(dpnl):
                parsed["daily_return"] = (dpnl / dse) * 100
            else:
                parsed["daily_return"] = None

    # ===== exposure_notional 计算 =====
    # exposure_notional = 每点波动对账户的盈亏金额
    # 计算公式：仓位资金 × 杠杆 × point_value
    user_position_pct = position_pct if position_pct > 0 else parsed.get("position_pct", 0)

    # 从 positions 获取 point_value（每个持仓品种的每点价值）
    total_point_value = 0.0
    if positions and isinstance(positions, list):
        for pos in positions:
            pv = pos.get("point_value", 0) or pos.get("point", 0)
            lots = pos.get("lots", 0)
            if pv and lots:
                total_point_value += pv * lots

    # 如果有持仓数据，计算基础 exposure
    if positions and safe_num(equity) and equity > 0:
        # 用户选择的仓位对应的资金量
        position_value = equity * (user_position_pct / 100.0)
        
        # 默认杠杆（如果没有设置则使用 20）
        leverage = parsed.get("leverage_used", 20) or 20
        
        # exposure_notional = 仓位资金 × 杠杆 × 每点价值
        # 如果有持仓数据，使用持仓的 point_value；否则使用 0.01 近似
        if total_point_value > 0:
            exp_notional = position_value * leverage * total_point_value
        else:
            # 兜底：假设每点 = 账户的 0.01%
            exp_notional = position_value * leverage * 0.0001
        
        parsed["exposure_notional"] = round(exp_notional, 2)
        
        # leverage_used = exposure_notional / equity (百分比)
        if exp_notional and equity:
            parsed["leverage_used"] = round((exp_notional / equity) * 100, 2)
        else:
            parsed["leverage_used"] = None
    else:
        if parsed.get("exposure_notional") is None:
            parsed["exposure_notional"] = None
        if parsed.get("leverage_used") is None:
            en = parsed.get("exposure_notional")
            if safe_num(en) and safe_num(equity) and equity != 0:
                parsed["leverage_used"] = round((en / equity) * 100, 2)
            else:
                parsed["leverage_used"] = None

    # ===== exposure_signal 风控灯 =====
    margin_level = parsed.get("margin_level")
    if margin_level and user_position_pct > 0:
        parsed["exposure_signal"] = calc_exposure_signal(margin_level, user_position_pct)
    else:
        parsed["exposure_signal"] = "green"

    # risk_flags
    if parsed.get("risk_flags") is None:
        parsed["risk_flags"] = ""

    # free_margin：如果缺失且 equity/margin 有，尝试补一下
    if parsed.get("free_margin") is None:
        if safe_num(equity) and safe_num(margin):
            parsed["free_margin"] = equity - margin
        else:
            parsed["free_margin"] = None

    # metrics：补齐结构
    metrics = parsed.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    metric_fields = {
        "poll_latency_ms": None,
        "last_http_code": None,
        "last_error": "",
        "queue_batch_size": 0,
        "reports_sent_count": 0,
        "executed_commands": 0,
        "failed_commands": 0,
        "position_pct": user_position_pct,
    }
    for k, default in metric_fields.items():
        if k not in metrics:
            metrics[k] = default
    parsed["metrics"] = metrics

    # 保留原始数值字段
    parsed["balance"] = balance
    parsed["equity"] = equity
    parsed["margin"] = margin
    if parsed.get("free_margin") is None and free_margin is not None:
        parsed["free_margin"] = free_margin

    return parsed

def extract_latest_details_from_status(record, positions=None):
    """只用于 status 记录的详情提取 + 自动补齐"""
    if not record:
        return None

    base_info = {
        "received_at": record.get("received_at"),
        "ip": record.get("ip"),
        "body_raw_preview": (record.get("body_raw", "")[:500] + ("..." if len(record.get("body_raw", "")) > 500 else "")),
        "remaining_data": record.get("remaining_data"),
    }

    if record.get("parse_error"):
        return {
            **base_info,
            "error": f"JSON 解析失败: {record['parse_error']}",
            "full_error": record.get("parse_error_detail", ""),
        }

    parsed = record.get("parsed")
    if not isinstance(parsed, dict):
        return {**base_info, "error": "JSON 解析失败或不是对象"}

    # 优先使用传入的 positions 数据，否则尝试从 record 获取
    if positions is None:
        positions = record.get("positions")
    
    # 调用 auto_fill_status 传入 positions 用于计算 exposure
    parsed = auto_fill_status(parsed, positions)

    metrics = parsed.get("metrics", {})

    return {
        **base_info,
        "account": parsed.get("account"),
        "server": parsed.get("server"),
        "ts": parsed.get("ts"),
        "balance": parsed.get("balance"),
        "equity": parsed.get("equity"),
        "margin": parsed.get("margin"),
        "free_margin": parsed.get("free_margin"),
        "margin_level": parsed.get("margin_level"),
        "floating_pnl": parsed.get("floating_pnl"),
        "day_start_equity": parsed.get("day_start_equity"),
        "daily_closed_pnl": parsed.get("daily_closed_pnl"),
        "daily_pnl": parsed.get("daily_pnl"),
        "daily_return": parsed.get("daily_return"),
        "exposure_notional": parsed.get("exposure_notional"),
        "exposure_signal": parsed.get("exposure_signal"),
        "leverage_used": parsed.get("leverage_used"),
        "risk_flags": parsed.get("risk_flags"),
        "poll_latency_ms": metrics.get("poll_latency_ms"),
        "last_http_code": metrics.get("last_http_code"),
        "last_error": metrics.get("last_error"),
        "queue_batch_size": metrics.get("queue_batch_size"),
        "reports_sent_count": metrics.get("reports_sent_count"),
        "executed_commands": metrics.get("executed_commands"),
        "failed_commands": metrics.get("failed_commands"),
        "position_pct": metrics.get("position_pct", 0),
        # 添加 positions 数据供前端展示
        "positions": positions if positions else [],
    }

# ==================== 暂停控制接口 ====================
@app.route("/api/pause", methods=["POST"])
def api_pause():
    global paused
    with pause_lock:
        paused = True
    return jsonify({"paused": paused})

@app.route("/api/resume", methods=["POST"])
def api_resume():
    global paused
    with pause_lock:
        paused = False
    return jsonify({"paused": paused})

@app.route("/api/status", methods=["GET"])
def api_status():
    with pause_lock:
        return jsonify({"paused": paused})

# 网页端获取最新 MT4 状态数据（用于风控计算）
@app.route("/api/latest_status", methods=["GET"])
def api_latest_status():
    with history_lock:
        latest_status_record = history_status[0] if history_status else None
        latest_positions_record = history_positions[0] if history_positions else None
        
        positions_data = None
        if latest_positions_record:
            positions_data = latest_positions_record.get("parsed", {}).get("positions", [])
        
        # 提取详情（含 exposure 计算）
        detail = extract_latest_details_from_status(latest_status_record, positions_data)
        
        if detail:
            return jsonify(detail)
        else:
            return jsonify({})

# 网页端获取历史成交记录
@app.route("/api/history_trades", methods=["GET"])
def api_history_trades():
    """返回历史成交记录（从 report 数据中提取）"""
    limit = request.args.get("limit", 20, type=int)
    
    with history_lock:
        trades = []
        for record in list(history_report)[:limit]:
            parsed = record.get("parsed", {})
            if parsed:
                trades.append({
                    "received_at": record.get("received_at"),
                    "cmd_id": parsed.get("cmd_id"),
                    "ok": parsed.get("ok"),
                    "ticket": parsed.get("ticket"),
                    "error": parsed.get("error"),
                    "message": parsed.get("message"),
                    "exec_ms": parsed.get("exec_ms"),
                })
        
        return jsonify({"trades": trades})

# ==================== 主页 ====================
@app.route("/")
def index():
    with history_lock:
        # 最新 status
        latest_status_record = history_status[0] if history_status else None
        
        # 最新 positions（用于计算 exposure）
        latest_positions_record = history_positions[0] if history_positions else None
        positions_data = None
        if latest_positions_record:
            positions_data = latest_positions_record.get("parsed", {}).get("positions", [])
        
        # 提取 status 详情并传入 positions 计算 exposure
        latest_detail = extract_latest_details_from_status(latest_status_record, positions_data)

        # 展示表格：status 历史
        hist_list = list(reversed(history_status))

        # poll 请求历史（用于调试）
        poll_list = list(reversed(history_poll))

    with commands_lock:
        cmds_copy = commands.copy()
    with pause_lock:
        current_paused = paused

    restricted = is_restricted_time()

    return render_template_string(
        HTML_TEMPLATE,
        history=hist_list,
        poll_history=poll_list,
        history_positions=history_positions,
        latest=latest_detail,
        latest_raw=latest_status_record,
        commands=cmds_copy,
        MAX_HISTORY=MAX_HISTORY,
        paused=current_paused,
        restricted=restricted,
    )

# ==================== 旧版 echo ====================
@app.route("/web/api/echo", methods=["POST"])
def mt4_webhook_echo():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)

    response_lines = []
    with commands_lock:
        if commands:
            for cmd in commands:
                side = cmd.get("side", "")
                symbol = cmd.get("symbol", "")
                volume = cmd.get("volume", "")
                base = f"{side},{symbol},{volume}"
                sl = cmd.get("sl_price")
                tp = cmd.get("tp_price")
                if sl is not None and tp is not None:
                    response_lines.append(f"{base},{sl},{tp}")
                elif sl is not None:
                    response_lines.append(f"{base},{sl},0")
                elif tp is not None:
                    response_lines.append(f"{base},0,{tp}")
                else:
                    response_lines.append(base)
            commands.clear()

    if response_lines:
        return "\n".join(response_lines), 200, {"Content-Type": "text/plain; charset=utf-8"}
    return "NOCOMMAND", 200, {"Content-Type": "text/plain; charset=utf-8"}

# ==================== MT4 专用接口 ====================
@app.route("/web/api/mt4/commands", methods=["POST"])
def mt4_commands():
    if is_restricted_time():
        return jsonify({"commands": [], "paused": paused}), 200

    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)

    parsed_json, _ = store_mt4_data(raw_body, client_ip, headers_dict)

    if parsed_json is None:
        return jsonify({"error": "Invalid JSON", "commands": []}), 400

    account = parsed_json.get("account") if isinstance(parsed_json, dict) else None
    account = norm_str(account)

    with commands_lock:
        account_commands = []
        remaining_commands = []
        for cmd in commands:
            cmd_acc = cmd.get("account")
            # 兼容逻辑：account 为 None 或空字符串时，允许匹配（或视为广播命令）
            cmd_acc_normalized = norm_str(cmd_acc)
            request_acc_normalized = norm_str(account)
            
            # 如果请求的 account 为空，则返回所有不指定 account 的命令
            if request_acc_normalized == "":
                if cmd_acc is None or cmd_acc_normalized == "":
                    account_commands.append(cmd)
                else:
                    remaining_commands.append(cmd)
            else:
                # 请求有 account：只返回匹配的或无 account 限制的命令
                if cmd_acc is None or cmd_acc_normalized == "" or cmd_acc_normalized == request_acc_normalized:
                    account_commands.append(cmd)
                else:
                    remaining_commands.append(cmd)
        commands[:] = remaining_commands

    print("[SEND CMDS]:", json.dumps(account_commands, ensure_ascii=False))

    with pause_lock:
        current_paused = paused

    return jsonify({"commands": account_commands, "paused": current_paused}), 200

@app.route("/web/api/mt4/status", methods=["POST"])
def mt4_status():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

@app.route("/web/api/mt4/positions", methods=["POST"])
def mt4_positions():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

@app.route("/web/api/mt4/report", methods=["POST"])
def mt4_report():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

@app.route("/web/api/mt4/quote", methods=["POST"])
def mt4_quote():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

# ==================== 网页发单 ====================
@app.route("/send_command", methods=["POST"])
def send_command():
    if is_restricted_time():
        return redirect(url_for("index"))

    global cmd_counter
    
    # 使用归一化函数处理字段
    account_raw = request.form.get("account", "")
    cmd_type_raw = request.form.get("cmd_type", "MARKET")
    symbol_raw = request.form.get("symbol", "")
    side_raw = request.form.get("side", "")
    volume_raw = request.form.get("volume", "")
    price_raw = request.form.get("price", "")
    sl_raw = request.form.get("sl", "")
    tp_raw = request.form.get("tp", "")
    ticket_raw = request.form.get("ticket", "")
    lots_raw = request.form.get("lots", "")

    account = norm_str(account_raw)
    cmd_type = norm_str(cmd_type_raw).upper()
    symbol = norm_symbol(symbol_raw)
    side_ui = norm_str(side_raw).upper()
    volume = norm_volume(volume_raw)
    sl = norm_volume(sl_raw) if sl_raw else None
    tp = norm_volume(tp_raw) if tp_raw else None
    ticket = norm_str(ticket_raw)
    lots = norm_volume(lots_raw) if lots_raw else None
    price = norm_volume(price_raw) if price_raw else None

    # 强校验
    if cmd_type in ("MARKET", "LIMIT"):
        if not symbol:
            print("[BLOCK] symbol 为空")
            return redirect(url_for("index"))
        if side_ui not in ("BUY", "SELL"):
            print("[BLOCK] side 无效:", side_ui)
            return redirect(url_for("index"))
        if volume <= 0:
            print("[BLOCK] volume 必须 > 0")
            return redirect(url_for("index"))
    elif cmd_type == "CLOSE":
        if not ticket:
            print("[BLOCK] ticket 为空")
            return redirect(url_for("index"))
    else:
        return redirect(url_for("index"))

    # 若没填 account，则尝试从最新 status 获取
    if not account:
        with history_lock:
            if history_status and isinstance(history_status[0].get("parsed"), dict):
                account = norm_str(history_status[0]["parsed"].get("account"))

    # 命令对象 - 关键：account 要么不传，要么传真实值，严禁传空字符串
    now = int(time.time())
    cmd = {
        "id": str(cmd_counter),
        "nonce": generate_nonce(),
        "created_at": now,
        "ttl_sec": 10,
    }
    
    # 只有非空 account 才添加
    if account:
        cmd["account"] = account

    if cmd_type == "MARKET":
        cmd["action"] = "market"
        cmd["symbol"] = symbol
        cmd["side"] = "buy" if side_ui == "BUY" else "sell"
        cmd["volume"] = volume
        # 兼容字段名
        cmd["lots"] = volume
        if sl is not None and sl > 0:
            cmd["sl_price"] = sl
            cmd["sl"] = sl
        if tp is not None and tp > 0:
            cmd["tp_price"] = tp
            cmd["tp"] = tp
    elif cmd_type == "LIMIT":
        cmd["action"] = "limit"
        cmd["symbol"] = symbol
        cmd["side"] = "buy" if side_ui == "BUY" else "sell"
        cmd["volume"] = volume
        cmd["lots"] = volume
        cmd["price"] = price
        if sl is not None and sl > 0:
            cmd["sl"] = sl
        if tp is not None and tp > 0:
            cmd["tp"] = tp
    elif cmd_type == "CLOSE":
        cmd["action"] = "close"
        cmd["ticket"] = int(ticket) if ticket else None
        if lots and lots > 0:
            cmd["lots"] = lots

    print("[ADD CMD]:", json.dumps(cmd, ensure_ascii=False))

    with commands_lock:
        commands.append(cmd)
        cmd_counter += 1

    return redirect(url_for("index"))

@app.route("/delete_command/<int:index>", methods=["POST"])
def delete_command(index):
    with commands_lock:
        if 0 <= index < len(commands):
            commands.pop(index)
    return redirect(url_for("index"))

@app.route("/clear_commands", methods=["POST"])
def clear_commands():
    with commands_lock:
        commands.clear()
    return redirect(url_for("index"))

# ==================== HTML（保留你的布局，增加一个 poll 表）====================
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
    .restricted-mode .status-box .label { color: #aaa; font-size: 1rem; }
    .restricted-mode .status-box .value { color: #0f0; }
    pre { white-space: pre-wrap; word-break: break-word; }
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
      <code>/web/api/mt4/positions</code> (持仓),
      <code>/web/api/mt4/report</code> (回报)
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

  <!-- 最新账户状态（只来自 status） -->
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
          </div>
        {% else %}
          <div class="stat-grid">
            <div class="stat-item"><span class="stat-label">account</span><div class="stat-value">{{ latest.account or 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">server</span><div class="stat-value">{{ latest.server or 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">ts</span><div class="stat-value">{{ latest.ts or 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">balance</span><div class="stat-value">{{ "%.2f"|format(latest.balance) if latest.balance is number else (latest.balance or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">equity</span><div class="stat-value">{{ "%.2f"|format(latest.equity) if latest.equity is number else (latest.equity or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">margin</span><div class="stat-value">{{ "%.2f"|format(latest.margin) if latest.margin is number else (latest.margin or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">free_margin</span><div class="stat-value">{{ "%.2f"|format(latest.free_margin) if latest.free_margin is number else (latest.free_margin or 'N/A') }}</div></div>
            <div class="stat-item"><span class="stat-label">margin_level</span><div class="stat-value">{{ "%.2f"|format(latest.margin_level) if latest.margin_level is number else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">floating_pnl</span><div class="stat-value">{{ "%.2f"|format(latest.floating_pnl) if latest.floating_pnl is number else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">day_start_equity</span><div class="stat-value">{{ "%.2f"|format(latest.day_start_equity) if latest.day_start_equity is number else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">daily_closed_pnl</span><div class="stat-value">{{ "%.2f"|format(latest.daily_closed_pnl) if latest.daily_closed_pnl is number else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">daily_pnl</span><div class="stat-value">{{ "%.2f"|format(latest.daily_pnl) if latest.daily_pnl is number else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">daily_return</span><div class="stat-value">{{ "%.6f"|format(latest.daily_return) if latest.daily_return is number else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">exposure_notional</span><div class="stat-value">{{ "%.2f"|format(latest.exposure_notional) if latest.exposure_notional is number else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">leverage_used</span><div class="stat-value">{{ "%.4f"|format(latest.leverage_used) if latest.leverage_used is number else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">risk_flags</span><div class="stat-value">{{ latest.risk_flags if latest.risk_flags else '' }}</div></div>

            <div class="stat-item"><span class="stat-label">metrics.poll_latency_ms</span><div class="stat-value">{{ latest.poll_latency_ms if latest.poll_latency_ms is not none else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.last_http_code</span><div class="stat-value">{{ latest.last_http_code if latest.last_http_code is not none else 'N/A' }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.last_error</span><div class="stat-value">{{ latest.last_error if latest.last_error else '' }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.queue_batch_size</span><div class="stat-value">{{ latest.queue_batch_size }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.reports_sent</span><div class="stat-value">{{ latest.reports_sent_count }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.executed</span><div class="stat-value">{{ latest.executed_commands }}</div></div>
            <div class="stat-item"><span class="stat-label">metrics.failed</span><div class="stat-value">{{ latest.failed_commands }}</div></div>
          </div>
        {% endif %}

        <div class="mt-3">
          <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#rawJsonPreview" aria-expanded="false">
            <i class="bi bi-code-slash"></i> 查看原始JSON（status）
          </button>
          <div class="collapse mt-2" id="rawJsonPreview">
            <pre class="bg-light p-3 rounded" style="font-size:0.75rem;">{{ latest_raw.body_raw if latest_raw else '无原始数据' }}</pre>
          </div>
        </div>
      {% else %}
        <p class="text-muted"><i class="bi bi-exclamation-circle"></i> 尚未收到任何 status 上报数据。</p>
      {% endif %}
    </div>
  </div>

  <!-- 持仓展示区域 -->
  {% if latest_detail and latest_detail.positions %}
  <div class="card shadow-sm mb-4">
    <div class="card-header bg-primary text-white">
      <i class="bi bi-collection"></i> 当前持仓 ( {{ latest_detail.positions|length }} )
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-striped table-hover mb-0">
          <thead>
            <tr>
              <th>Ticket</th>
              <th>品种</th>
              <th>方向</th>
              <th>手数</th>
              <th>开仓价</th>
              <th>当前价</th>
              <th>浮动盈亏</th>
              <th>保证金</th>
            </tr>
          </thead>
          <tbody>
            {% for pos in latest_detail.positions %}
            <tr>
              <td>{{ pos.ticket }}</td>
              <td>{{ pos.symbol }}</td>
              <td><span class="badge bg-{{ 'success' if pos.side == 'buy' else 'danger' }}">{{ pos.side }}</span></td>
              <td>{{ "%.2f"|format(pos.lots) if pos.lots is number else pos.lots }}</td>
              <td>{{ "%.5f"|format(pos.open_price) if pos.open_price is number else pos.open_price }}</td>
              <td>{{ "%.5f"|format(pos.current_price) if pos.current_price is number else pos.current_price }}</td>
              <td style="color: {{ 'green' if pos.profit > 0 else 'red' }}">{{ "%.2f"|format(pos.profit) if pos.profit is number else pos.profit }}</td>
              <td>{{ "%.2f"|format(pos.margin) if pos.margin is number else pos.margin }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
  {% endif %}

  <div class="row">
    <!-- 左：status 历史 -->
    <div class="col-lg-7 mb-4">
      <div class="card shadow-sm h-100">
        <div class="card-header bg-secondary text-white">
          <i class="bi bi-clock-history"></i> 状态上报历史(status) ({{ history|length }}/{{ MAX_HISTORY }})
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
                <tr><td colspan="7" class="text-center text-muted">暂无 status 历史数据</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 轮询请求(poll)历史：用来排查 max=50 刷屏 -->
      <div class="card shadow-sm mt-3">
        <div class="card-header bg-light">
          <i class="bi bi-arrow-repeat"></i> 轮询请求历史(commands poll) ({{ poll_history|length }}/{{ MAX_HISTORY }})
          <small class="text-muted ms-2">这里展示的是 EA 发来的 {"account","max"}，不会影响 status</small>
        </div>
        <div class="card-body p-0">
          <div class="table-responsive">
            <table class="table table-sm table-striped mb-0">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>IP</th>
                  <th>Body</th>
                </tr>
              </thead>
              <tbody>
                {% for rec in poll_history %}
                <tr>
                  <td>{{ rec.received_at.split(' ')[1] }}</td>
                  <td><span class="badge-ip">{{ rec.ip }}</span></td>
                  <td><small>{{ rec.body_raw }}</small></td>
                </tr>
                {% else %}
                <tr><td colspan="3" class="text-center text-muted">暂无 poll 数据</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
      </div>

    </div>

    <!-- 持仓历史 -->
    <div class="card shadow-sm mt-3">
      <div class="card-header bg-info text-white">
        <i class="bi bi-clock-history"></i> 持仓上报历史(positions) 
        <small class="text-white-50 ms-2">记录来自 /web/api/mt4/positions</small>
      </div>
      <div class="card-body p-0">
        {% set positions_history = history_positions[:5] %}
        {% if positions_history %}
          {% for rec in positions_history %}
          <div class="border-bottom p-2">
            <small class="text-muted">{{ rec.received_at }} ({{ rec.ip }})</small>
            <div class="mt-1">
              {% set positions = rec.parsed.positions if rec.parsed else [] %}
              {% if positions %}
                {% for pos in positions[:3] %}
                <span class="badge bg-{{ 'success' if pos.side == 'buy' else 'danger' }} me-1">
                  {{ pos.symbol }} {{ pos.lots }}手
                </span>
                {% endfor %}
                {% if positions|length > 3 %}
                <small class="text-muted">...等{{ positions|length }}个</small>
                {% endif %}
              {% else %}
                <small class="text-muted">无持仓</small>
              {% endif %}
            </div>
          </div>
          {% endfor %}
        {% else %}
          <div class="p-3 text-center text-muted">暂无 positions 上报数据</div>
        {% endif %}
      </div>
    </div>

    <!-- 右：命令队列+发单 -->
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
                  平仓 票号:{{ cmd.ticket }}{% if cmd.lots %} 手数:{{ cmd.lots }}{% endif %}
                {% endif %}
                <br><small class="text-muted"><i class="bi bi-clock"></i>
                  账户: {{ cmd.account if cmd.account else '广播' }} | ID: {{ cmd.id }}
                </small>
              </div>
              <form method="post" action="{{ url_for('delete_command', index=loop.index0) }}" style="margin:0;">
                <button type="submit" class="btn btn-sm btn-outline-danger" onclick="return confirm('删除该指令？')">
                  <i class="bi bi-x"></i>
                </button>
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

            <div id="limitFields" style="display:none;">
              <div class="mb-2">
                <label class="form-label">限价价格</label>
                <input type="number" step="0.00001" name="price" class="form-control form-control-sm" placeholder="如 1.1050">
              </div>
            </div>

            <div id="closeFields" style="display:none;">
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
    if (savedAccount) document.getElementById('accountInput').value = savedAccount;
  });

  document.getElementById('commandForm')?.addEventListener('submit', function() {
    const v = document.getElementById('accountInput').value;
    if (v) localStorage.setItem('mt4_default_account', v);
  });

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
  document.getElementById('pause-btn')?.addEventListener('click', () => fetch('/api/pause',{method:'POST'}).then(()=>updatePauseStatus()));
  document.getElementById('resume-btn')?.addEventListener('click', () => fetch('/api/resume',{method:'POST'}).then(()=>updatePauseStatus()));
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
  }
  cmdTypeSelect.addEventListener('change', toggleFields);
  toggleFields();
</script>
</body>
</html>
"""

# ==================== 启动 ====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
