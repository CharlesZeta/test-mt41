import os
import json
import threading
import traceback
import time
import random
import string
from datetime import datetime
from collections import deque
from flask import Flask, request, render_template_string, redirect, url_for, jsonify, send_file

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
            # 日志记录 report
            if record.get("parsed"):
                parsed = record["parsed"]
                desc = parsed.get("desc", "")
                if desc in ("SPREAD_REJECT", "SPREAD_OK", "SPREAD_EXCEED_ON_FILL"):
                    print(f"[SPREAD_LOG] {parsed.get('code')} {desc} "
                          f"account={parsed.get('account')} "
                          f"spread={parsed.get('spread')} "
                          f"threshold={parsed.get('threshold')} "
                          f"cmd_id={parsed.get('cmd_id')}")
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
PREVIEW_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>交易UI原型（可滚动/可加品种/可转动滑轮）</title>
  <style>
    :root{
      --bg:#f6f7f9;
      --card:#ffffff;
      --text:#111;
      --muted:#7a7f87;
      --line:#e9ecf1;
      --green:#25b97a;
      --red:#ef4d5c;
      --yellow:#f6c343;
      --chip:#f1f3f6;
      --shadow: 0 10px 30px rgba(0,0,0,.08);
      --radius: 16px;
      --safe-top: env(safe-area-inset-top, 0px);
      --safe-bottom: env(safe-area-inset-bottom, 0px);
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
      color:var(--text);
      background:var(--bg);
    }

    /* 容器：可上下滑动 */
    .app{
      max-width: 420px;
      margin: 0 auto;
      min-height: 100vh;
      padding: calc(12px + var(--safe-top)) 14px calc(90px + var(--safe-bottom));
    }

    /* 顶部 */
    .topbar{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding: 8px 0 12px;
    }
    .title{
      font-size: 24px;
      font-weight: 800;
      letter-spacing: .2px;
    }
    .btn{
      border:1px solid var(--line);
      background:#fff;
      border-radius:999px;
      padding:8px 12px;
      font-weight:600;
      color:#222;
    }

    /* Tab */
    .tabs{
      display:flex;
      gap:10px;
      align-items:center;
      padding: 4px 0 10px;
    }
    .tab{
      padding:8px 12px;
      border-radius:999px;
      background:transparent;
      color:var(--muted);
      font-weight:700;
      border:1px solid transparent;
    }
    .tab.active{
      background: var(--chip);
      color: var(--text);
      border-color: var(--line);
    }

    /* 品种行 */
    .symRow{
      display:flex;
      align-items:flex-start;
      justify-content:space-between;
      gap:10px;
      padding: 8px 0;
    }
    .symLeft{
      display:flex;
      flex-direction:column;
      gap:4px;
    }
    .symName{
      display:flex;
      align-items:center;
      gap:8px;
      font-size: 22px;
      font-weight: 900;
    }
    .symBadge{
      font-size: 12px;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--chip);
      border: 1px solid var(--line);
      color:#333;
      font-weight: 700;
    }
    .symPnl{
      color: var(--red);
      font-weight: 800;
    }
    .symRight{
      display:flex;
      align-items:center;
      gap:8px;
    }
    .iconBtn{
      width:34px;height:34px;
      border-radius:10px;
      border:1px solid var(--line);
      background:#fff;
      display:grid;place-items:center;
    }

    /* 行情 + 下单区域 */
    .grid{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      align-items:start;
    }
    .card{
      background: var(--card);
      border:1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }

    /* 盘口卡片（左）- 新布局 */
    .orderbook{
      padding: 12px;
    }
    .obTopStats{
      display:grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-bottom: 12px;
      font-variant-numeric: tabular-nums;
    }
    .obStatLabel{
      font-size: 10px;
      font-weight: 800;
      color: var(--muted);
      margin-bottom: 2px;
    }
    .obStatVal{
      font-weight: 1000;
      font-size: 12px;
      color: #222;
    }

    .midPrice{
      text-align:center;
      margin: 8px 0 4px;
      font-size: 26px;
      font-weight: 1000;
      letter-spacing: .3px;
    }
    .midSub{
      text-align:center;
      margin-top:-4px;
      color:var(--muted);
      font-weight:700;
      font-size:11px;
    }
    .statsNew{
      margin-top: 12px;
      padding-top: 10px;
      border-top: 1px dashed var(--line);
    }
    .statRowNew{
      display:flex;
      justify-content:space-between;
      gap:12px;
      margin: 5px 0;
      font-variant-numeric: tabular-nums;
      font-size: 12px;
    }
    .statRowNew.k{ color:var(--muted); font-weight: 800; }
    .statRowNew.v{ font-weight: 1000; }

    /* 下单卡片（右） */
    .order{
      padding: 12px;
    }
    .row{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      margin-bottom:10px;
    }
    .chips{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
    }
    .chip{
      padding:8px 10px;
      border-radius: 12px;
      background: #fff;
      border:1px solid var(--line);
      font-weight:800;
      min-width: 72px;
      text-align:center;
    }
    .chip.primary{
      background: var(--chip);
    }
    .label{
      color:var(--muted);
      font-weight:800;
      font-size:12px;
    }
    .val{
      font-weight:900;
      font-variant-numeric: tabular-nums;
    }

    .field{
      border:1px solid var(--line);
      background: #f7f8fb;
      border-radius: 14px;
      padding: 10px 12px;
      display:flex;
      justify-content:space-between;
      align-items:center;
      color:#9aa0a8;
      font-weight:800;
      margin-bottom:10px;
      font-size: 12px;
    }
    .field strong{ color:#222; }
    .field[role="button"]{
      cursor:pointer;
      transition: background .15s ease, border-color .15s ease;
    }
    .field[role="button"]:hover{
      background: #eef1f7;
      border-color: #d3d8e2;
    }
    .field input{
      border:none;
      background:transparent;
      color:#222;
      font-weight:900;
      text-align:right;
      width:80px;
      outline:none;
      font-family:inherit;
    }

    .toggles{
      display:flex;
      flex-direction:column;
      gap:10px;
      margin: 12px 0;
    }
    .toggleRow{
      display:flex;
      align-items:center;
      gap:10px;
      font-weight:900;
    }
    .radio{
      width:18px;height:18px;border-radius:50%;
      border:2px solid #cfd6df;
      background:#fff;
    }

    /* 自定义滑轮（可拖动/可点刻度） */
    .wheelWrap{
      margin: 10px 0 14px;
      padding: 10px 6px 4px;
    }
    .wheel{
      position:relative;
      height: 44px;
      user-select:none;
      touch-action: none;
    }
    .track{
      position:absolute;
      left:8px; right:8px;
      top: 22px;
      height: 4px;
      background: #e8edf4;
      border-radius:999px;
    }
    .marks{
      position:absolute;
      left:8px; right:8px;
      top: 16px;
      display:flex;
      justify-content:space-between;
      align-items:center;
    }
    .mark{
      width:12px;height:12px;
      border-radius: 4px;
      background:#111;
      opacity:.1;
      cursor:pointer;
    }
    .mark.active{
      opacity:1;
      background:#111;
    }
    .thumb{
      position:absolute;
      top: 10px;
      width:24px;height:24px;
      border-radius: 8px;
      background:#fff;
      border:2px solid #111;
      box-shadow: 0 8px 20px rgba(0,0,0,.12);
      transform: translateX(-50%);
      cursor:grab;
    }

    /* 风控提示条（黄灯示例，可切换颜色） */
    .riskTip{
      display:flex;
      align-items:flex-start;
      gap:10px;
      padding: 10px 12px;
      border-radius: 14px;
      border:1px solid #f0e2b3;
      background:#fff6d6;
      color:#4b3d13;
      font-weight:800;
      line-height:1.35;
      margin: 10px 0 12px;
    }
    .lamp{
      width:18px;height:18px;border-radius:50%;
      background: var(--yellow);
      box-shadow: 0 0 0 3px rgba(246,195,67,.25);
      flex:0 0 auto;
      margin-top:2px;
    }
    .riskTip small{
      display:block;
      font-weight:800;
      color:#6b5a1a;
      margin-top:4px;
    }

    .stats{
      border-top:1px solid var(--line);
      padding-top:10px;
      margin-top:10px;
    }
    .statRow{
      display:flex;
      justify-content:space-between;
      gap:12px;
      margin: 6px 0;
      font-variant-numeric: tabular-nums;
    }
    .statRow .k{ color:var(--muted); font-weight:900; }
    .statRow .v{ font-weight:1000; }

    .cta{
      border:none;
      width:100%;
      padding: 14px 14px;
      border-radius: 14px;
      color:#fff;
      font-size: 18px;
      font-weight:1000;
      margin-top:10px;
    }
    .cta.buy{ background: var(--green); }
    .cta.sell{ background: var(--red); }

    /* 持仓/委托列表 */
    .section{
      margin-top: 14px;
    }
    .segTabs{
      display:flex;
      gap:14px;
      align-items:center;
      padding: 10px 4px 8px;
      font-weight:1000;
    }
    .seg{
      color:var(--muted);
      position:relative;
      padding: 8px 2px;
    }
    .seg.active{ color:var(--text); }
    .seg.active::after{
      content:"";
      position:absolute;
      left:0; right:0; bottom:0;
      height:3px;
      border-radius: 999px;
      background: #f6c343;
    }
    .listCard{
      background:#fff;
      border:1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow:hidden;
    }
    .posItem{
      padding: 12px;
      border-top:1px solid var(--line);
    }
    .posItem:first-child{ border-top:none; }
    .posTop{
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:10px;
    }
    .posTitle{
      display:flex; gap:8px; align-items:center;
      font-weight:1000;
    }
    .sideTag{
      padding:2px 6px;
      border-radius:8px;
      font-size:12px;
      font-weight:1000;
      border:1px solid var(--line);
      background: #fff;
    }
    .sideTag.sell{ color: var(--red); border-color: rgba(239,77,92,.35); }
    .sideTag.buy{ color: var(--green); border-color: rgba(37,185,122,.35); }

    .posPnl{
      font-size: 26px;
      font-weight: 1100;
      color: var(--red);
      font-variant-numeric: tabular-nums;
    }
    .posGrid{
      display:grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap:10px;
      margin-top: 10px;
      font-variant-numeric: tabular-nums;
    }
    .mini{
      color:var(--muted);
      font-size:12px;
      font-weight:900;
    }
    .big{
      font-weight:1100;
      margin-top:2px;
    }
    .posActions{
      display:grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap:10px;
      margin-top: 12px;
    }
    .ghost{
      border:1px solid var(--line);
      background:#f7f8fb;
      border-radius: 12px;
      padding: 10px 10px;
      font-weight:1000;
    }

    /* 底部导航 */
    .nav{
      position: fixed;
      left:0; right:0; bottom:0;
      background:#fff;
      border-top: 1px solid var(--line);
      padding: 10px 16px calc(10px + var(--safe-bottom));
      display:flex;
      justify-content:space-around;
      gap:10px;
    }
    .navItem{
      display:flex;
      flex-direction:column;
      align-items:center;
      gap:4px;
      color:var(--muted);
      font-weight:900;
      font-size:12px;
    }
    .navItem.active{ color:var(--text); }

    /* 弹窗 */
    .modalMask{
      position: fixed;
      inset:0;
      background: rgba(0,0,0,.35);
      display:none;
      align-items:flex-end;
      justify-content:center;
      padding: 16px;
      z-index: 99;
    }
    .modal{
      width: min(420px, 100%);
      background:#fff;
      border-radius: 18px;
      border:1px solid var(--line);
      box-shadow: var(--shadow);
      overflow:hidden;
    }
    .modalHeader{
      padding: 12px;
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:10px;
      border-bottom:1px solid var(--line);
      font-weight:1100;
    }
    .modalBody{
      padding: 12px;
      max-height: 62vh;
      overflow:auto;
    }
    .search{
      width:100%;
      padding: 10px 12px;
      border-radius: 12px;
      border:1px solid var(--line);
      background:#f7f8fb;
      font-weight:900;
      outline:none;
    }
    .pairRow{
      display:flex;
      justify-content:space-between;
      align-items:center;
      padding: 12px 6px;
      border-bottom:1px solid var(--line);
      cursor:pointer;
    }
    .pairRow:last-child{ border-bottom:none; }
    .pairRow strong{ font-weight:1100; }
    .pairRow span{ color:var(--muted); font-weight:900; }

    .addRow{
      display:flex;
      gap:8px;
      margin-top:10px;
    }
    .addRow input{
      flex:1;
      padding: 10px 12px;
      border-radius: 12px;
      border:1px solid var(--line);
      background:#fff;
      font-weight:900;
      outline:none;
    }
    .primaryBtn{
      border:none;
      background:#111;
      color:#fff;
      border-radius: 12px;
      padding: 10px 12px;
      font-weight:1100;
    }

    /* 小屏优化 */
    @media (max-width: 380px){
      .grid{ grid-template-columns: 1fr; }
      .posGrid{ grid-template-columns: 1fr 1fr; }
      .posActions{ grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">

    <div class="topbar">
      <div class="title">模拟交易</div>
      <button class="btn" id="btnLive">返回实盘</button>
    </div>

    <div class="tabs">
      <button class="tab">现货</button>
      <button class="tab active">U本位</button>
      <button class="tab">币本位</button>
    </div>

    <div class="symRow">
      <div class="symLeft">
        <div class="symName">
          <span id="symName">ETHUSDT</span>
          <span class="symBadge">永续</span>
          <span class="symBadge" style="cursor:pointer" id="btnPick">▼</span>
        </div>
        <div class="symPnl" id="symChg">-3.71%</div>
      </div>
      <div class="symRight">
        <button class="iconBtn" title="K线">📈</button>
        <button class="iconBtn" title="设置">⚙️</button>
        <button class="iconBtn" title="更多">⋯</button>
      </div>
    </div>

    <div class="grid">
      <!-- 左：盘口 -->
      <div class="card orderbook">
        <div class="obTopStats">
          <div>
            <div class="obStatLabel">仓位入金额</div>
            <div class="obStatVal" id="posEntry">-- USDT</div>
          </div>
          <div>
            <div class="obStatLabel">仓位当前资金额度</div>
            <div class="obStatVal" id="posNow">-- USDT</div>
          </div>
          <div>
            <div class="obStatLabel">杠杆比</div>
            <div class="obStatVal" id="posLev">--x</div>
          </div>
        </div>

        <div class="midPrice" id="mid">--</div>
        <div class="midSub">当前合约最新价 (USDT)</div>

        <div class="statsNew">
          <div class="statRowNew">
            <span class="k">当日收益</span>
            <span class="v" id="dayPnl">-- USDT</span>
          </div>
          <div class="statRowNew">
            <span class="k">当日收益率</span>
            <span class="v" id="dayPnlPct">--%</span>
          </div>
          <div class="statRowNew">
            <span class="k">可用保证金</span>
            <span class="v" id="availMargin">-- USDT</span>
          </div>
        </div>
      </div>

      <!-- 右：下单 -->
      <div class="card order">
        <div class="row">
          <div class="chips">
            <div class="chip primary">全仓</div>
            <div class="chip" id="btnLev">20x</div>
            <div class="chip">联</div>
          </div>
          <div class="label">可用 <span class="val" id="avail">--</span> USDT</div>
        </div>

        <!-- 交易类型选择 -->
        <div class="field" id="orderTypeField" role="button">
          <span id="orderTypeText">市价</span>
          <strong>交易类型 ▾</strong>
        </div>

        <!-- 数量输入 (手动添加以确保兼容性) -->
        <div class="field">
          <span>数量 (手)</span>
          <input type="number" id="tradeLots" placeholder="0.01" step="0.01">
        </div>

        <!-- 动态表单区域 -->
        <div id="dynamicFields"></div>

        <!-- 滑轮组：仓位比例 -->
        <div class="wheelWrap">
          <div class="row" style="margin-bottom:6px;">
            <div class="label">仓位比例</div>
            <div class="val"><span id="pctText">75</span>%</div>
          </div>
          <div class="wheel" id="wheel">
            <div class="track"></div>
            <div class="marks" id="marks"></div>
            <div class="thumb" id="thumb" aria-label="slider thumb"></div>
          </div>
        </div>

        <!-- 风控提示条 -->
        <div class="riskTip" id="riskTip">
          <div class="lamp" id="lamp"></div>
          <div>
            风险暴露较高：每点波动≈ <span id="perPointMoney">--</span> USDT（<span id="perPointPct">--</span>%）
            <small>根据仓位比例和杠杆计算</small>
          </div>
        </div>

        <div class="toggles">
          <div class="toggleRow"><span class="radio"></span> 止盈/止损</div>
          <div class="toggleRow"><span class="radio"></span> 只减仓</div>
        </div>

        <div class="stats">
          <div class="statRow"><span class="k">占用保证金</span><span class="v" id="mLong">-- USDT</span></div>
          <div class="statRow"><span class="k">强平价格</span><span class="v" id="liqLong">-- USDT</span></div>
          <div class="statRow"><span class="k">每点波动(资金/占比)</span><span class="v">≈ <span id="ppLong">--</span> USDT / <span id="ppLongPct">--</span>%</span></div>
        </div>

        <button class="cta buy" id="btnBuy">买入/做多</button>

        <div class="stats" style="margin-top:14px">
          <div class="statRow"><span class="k">占用保证金</span><span class="v" id="mShort">-- USDT</span></div>
          <div class="statRow"><span class="k">强平价格</span><span class="v" id="liqShort">-- USDT</span></div>
          <div class="statRow"><span class="k">每点波动(资金/占比)</span><span class="v">≈ <span id="ppShort">--</span> USDT / <span id="ppShortPct">--</span>%</span></div>
        </div>

        <button class="cta sell" id="btnSell">卖出/做空</button>
      </div>
    </div>

    <!-- 持仓/委托 -->
    <div class="section">
      <div class="segTabs">
        <div class="seg active" id="segPos" onclick="switchTab('positions')">持有仓位 (0)</div>
        <div class="seg" id="segOrd" onclick="switchTab('orders')">当前委托 (0)</div>
      </div>
      <div class="listCard" id="list"></div>
    </div>

  </div>

  <!-- 底部导航 -->
  <div class="nav">
    <div class="navItem">行情</div>
    <div class="navItem active">交易</div>
    <div class="navItem">资产</div>
  </div>

  <!-- 品种选择弹窗 -->
  <div class="modalMask" id="pairMask">
    <div class="modal">
      <div class="modalHeader">
        <span>选择交易品种</span>
        <button class="btn" id="closePair">关闭</button>
      </div>
      <div class="modalBody">
        <input class="search" id="pairSearch" placeholder="搜索" />
        <div id="pairList" style="margin-top:10px"></div>
        <div style="margin-top:12px; font-weight:1100">添加自定义品种</div>
        <div class="addRow">
          <input id="pairNew" placeholder="例如：XAUUSDT / SOLUSDT" />
          <button class="primaryBtn" id="addPair">添加</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 杠杆弹窗 -->
  <div class="modalMask" id="levMask">
    <div class="modal">
      <div class="modalHeader">
        <span>调整杠杆</span>
        <button class="btn" id="closeLev">关闭</button>
      </div>
      <div class="modalBody">
        <div class="row" style="margin-bottom:6px">
          <div class="label">当前杠杆</div>
          <div class="val"><span id="levVal">20</span>x</div>
        </div>
        <div class="wheelWrap" style="margin-top:0">
          <div class="wheel" id="levWheel">
            <div class="track"></div>
            <div class="marks" id="levMarks"></div>
            <div class="thumb" id="levThumb"></div>
          </div>
          <div class="mini" style="margin-top:10px">
            * 选择超过 10x 杠杆会增加强平风险，请注意风险。
          </div>
        </div>
        <button class="cta" style="background:#f6c343;color:#222" id="confirmLev">确认</button>
      </div>
    </div>
  </div>

  <script>
    // -----------------------------
    // 数据（状态）
    // -----------------------------
    const state = {
      // 默认品种列表
      pairs: [
        {sym: "EURUSD", last: 1.1050, chg: 0.05},
        {sym: "GBPUSD", last: 1.2650, chg: -0.10},
        {sym: "XAUUSDT", last: 2350.0, chg: 0.5},
        {sym: "BTCUSDT", last: 65807.2, chg: -1.93},
        {sym: "ETHUSDT", last: 1934.09, chg: -3.61},
      ],
      activeSym: "EURUSD",
      equity: 0,
      pct: 75,
      leverage: 20,
      orderType: "市价",
      account: {
        entry: 0,
        now: 0,
        dayPnl: 0,
        dayPnlPct: 0,
        availMargin: 0
      },
      risk: {
        long: {margin: 0, liq: 0, perPoint: 0, perPct: 0},
        short: {margin: 0, liq: 0, perPoint: 0, perPct: 0},
        tipLevel: "green"
      },
      // MT4 原始数据
      mt4: {
        positions: [],
        trades: []
      },
      currentTab: "positions"
    };

    const $ = (id) => document.getElementById(id);

    function fmtNum(n, dp=2){
      const x = Number(n);
      if (!isFinite(x)) return "--";
      return x.toLocaleString("en-US", {minimumFractionDigits: dp, maximumFractionDigits: dp});
    }

    // -----------------------------
    // 渲染
    // -----------------------------
    function renderHeader(){
      const pair = state.pairs.find(p => p.sym === state.activeSym);
      if (pair){
        $("symName").textContent = pair.sym;
        $("symChg").textContent = (pair.chg).toFixed(2) + "%";
        $("symChg").style.color = pair.chg < 0 ? "var(--red)" : "var(--green)";
        $("mid").textContent = fmtNum(pair.last, pair.last < 10 ? 4 : 2);
      }
      $("avail").textContent = fmtNum(state.equity, 2);
    }

    function renderAccountPanel(){
      const a = state.account;
      $("posEntry").textContent = fmtNum(a.entry, 2) + " USDT";
      $("posNow").textContent = fmtNum(a.now, 2) + " USDT";
      $("posLev").textContent = state.leverage + "x";
      const pnlPrefix = a.dayPnl >= 0 ? "+" : "";
      $("dayPnl").textContent = pnlPrefix + fmtNum(a.dayPnl, 2) + " USDT";
      $("dayPnl").style.color = a.dayPnl >= 0 ? "var(--green)" : "var(--red)";
      const pctPrefix = a.dayPnlPct >= 0 ? "+" : "";
      $("dayPnlPct").textContent = pctPrefix + a.dayPnlPct.toFixed(2) + "%";
      $("dayPnlPct").style.color = a.dayPnlPct >= 0 ? "var(--green)" : "var(--red)";
      $("availMargin").textContent = fmtNum(a.availMargin, 2) + " USDT";
    }

    function renderRisk(){
      const r = state.risk;
      $("mLong").textContent = fmtNum(r.long.margin, 2) + " USDT";
      $("liqLong").textContent = fmtNum(r.long.liq, 2) + " USDT";
      $("ppLong").textContent = fmtNum(r.long.perPoint, 2);
      $("ppLongPct").textContent = fmtNum(r.long.perPct, 2);

      $("mShort").textContent = fmtNum(r.short.margin, 2) + " USDT";
      $("liqShort").textContent = fmtNum(r.short.liq, 2) + " USDT";
      $("ppShort").textContent = fmtNum(r.short.perPoint, 2);
      $("ppShortPct").textContent = fmtNum(r.short.perPct, 2);

      $("perPointMoney").textContent = fmtNum(r.long.perPoint, 1);
      $("perPointPct").textContent = fmtNum(r.long.perPct, 2);

      const lamp = $("lamp");
      const tip = $("riskTip");
      if (r.tipLevel === "green"){
        lamp.style.background = "var(--green)";
        lamp.style.boxShadow = "0 0 0 3px rgba(37,185,122,.18)";
        tip.style.background = "#e8fff5";
        tip.style.borderColor = "rgba(37,185,122,.25)";
        tip.style.color = "#0f3a2a";
      } else if (r.tipLevel === "red"){
        lamp.style.background = "var(--red)";
        lamp.style.boxShadow = "0 0 0 3px rgba(239,77,92,.18)";
        tip.style.background = "#ffecec";
        tip.style.borderColor = "rgba(239,77,92,.25)";
        tip.style.color = "#4a151a";
      } else {
        lamp.style.background = "var(--yellow)";
        lamp.style.boxShadow = "0 0 0 3px rgba(246,195,67,.25)";
        tip.style.background = "#fff6d6";
        tip.style.borderColor = "#f0e2b3";
        tip.style.color = "#4b3d13";
      }
    }

    const ORDER_TYPES = ["市价", "市价止盈止损", "限价", "限价止盈止损"];
    function renderOrderType(){
      $("orderTypeText").textContent = state.orderType;
      const box = $("dynamicFields");
      if (!box) return;
      let html = "";
      if (state.orderType === "市价"){
        html = "";
      } else if (state.orderType === "市价止盈止损"){
        html = `
          <div class="field"><span>止盈触发价</span><input type="text" id="inpTp" placeholder="0.00"></div>
          <div class="field"><span>止损触发价</span><input type="text" id="inpSl" placeholder="0.00"></div>
        `;
      } else if (state.orderType === "限价"){
        html = `
          <div class="field"><span>交易触发价</span><input type="text" id="inpPrice" placeholder="0.00"></div>
        `;
      } else if (state.orderType === "限价止盈止损"){
        html = `
          <div class="field"><span>止盈触发价</span><input type="text" id="inpTp" placeholder="0.00"></div>
          <div class="field"><span>止损触发价</span><input type="text" id="inpSl" placeholder="0.00"></div>
          <div class="field"><span>交易触发价</span><input type="text" id="inpPrice" placeholder="0.00"></div>
        `;
      }
      box.innerHTML = html;
    }

    function renderLists() {
      const list = $("list");
      const segPos = $("segPos");
      const segOrd = $("segOrd");
      
      if(state.currentTab === "positions") {
        segPos.classList.add("active");
        segOrd.classList.remove("active");
        
        const positions = state.mt4.positions || [];
        segPos.textContent = `持有仓位 (${positions.length})`;
        
        if(positions.length === 0){
          list.innerHTML = '<div style="padding:20px;text-align:center;color:#888">暂无持仓</div>';
          return;
        }

        list.innerHTML = positions.map(pos => {
          const side = (pos.side || "").toLowerCase();
          const profit = pos.profit || 0;
          return `
            <div class="posItem">
              <div class="posTop">
                <div>
                  <div class="posTitle">
                    <span class="sideTag ${side}">${side === "buy" ? "买" : "卖"}</span>
                    ${pos.symbol} <span class="symBadge">${pos.lots}手</span>
                  </div>
                  <div class="mini">未实现盈亏</div>
                  <div class="posPnl" style="color:${profit >= 0 ? 'var(--green)' : 'var(--red)'}">
                    ${profit >= 0 ? "+" : ""}${fmtNum(profit, 2)}
                  </div>
                </div>
                <div style="text-align:right">
                  <div class="mini">开仓价</div>
                  <div class="big">${fmtNum(pos.open_price, 4)}</div>
                  <div class="mini" style="margin-top:6px">当前价</div>
                  <div class="big">${fmtNum(pos.current_price, 4)}</div>
                </div>
              </div>
            </div>
          `;
        }).join("");
        
      } else {
        segPos.classList.remove("active");
        segOrd.classList.add("active");
        
        const trades = state.mt4.trades || [];
        segOrd.textContent = `当前委托 (${trades.length})`; // 其实是历史成交
        
        if(trades.length === 0){
          list.innerHTML = '<div style="padding:20px;text-align:center;color:#888">暂无成交记录</div>';
          return;
        }

        list.innerHTML = trades.map(t => {
          return `
            <div class="posItem">
              <div class="posTop">
                <div>
                  <div class="posTitle">
                    <span class="sideTag ${t.ok ? 'buy' : 'sell'}">${t.ok ? '成功' : '失败'}</span>
                    订单 #${t.ticket || '-'}
                  </div>
                  <div class="mini">${t.message || t.error}</div>
                </div>
                <div style="text-align:right">
                   <div class="mini">耗时</div>
                   <div class="big">${t.exec_ms}ms</div>
                </div>
              </div>
            </div>
          `;
        }).join("");
      }
    }
    
    function switchTab(t){
      state.currentTab = t;
      renderLists();
    }
    window.switchTab = switchTab;

    function renderAll(){
      renderHeader();
      renderRisk();
      renderAccountPanel();
      renderOrderType();
      renderLists();
      $("pctText").textContent = state.pct;
      $("btnLev").textContent = state.leverage + "x";
      $("levVal").textContent = state.leverage;
      if(wheelCfg) updateWheelUI(wheelCfg, state.pct);
      if(levWheelCfg) updateWheelUI(levWheelCfg, state.leverage);
    }

    // -----------------------------
    // 滑轮组件
    // -----------------------------
    function makeWheel({rootId, marksId, thumbId, min, max, step, marks, onChange, getValue, setValue}){
      const root = $(rootId);
      const marksEl = $(marksId);
      const thumb = $(thumbId);

      marksEl.innerHTML = "";
      marks.forEach(v => {
        const m = document.createElement("div");
        m.className = "mark";
        m.title = String(v);
        m.addEventListener("click", () => {
          setValue(v);
          onChange(v);
          updateWheelUI(cfg, v);
        });
        marksEl.appendChild(m);
      });

      function valueToPct(v){ return (v - min) / (max - min); }
      function pctToValue(p){
        const raw = min + p * (max - min);
        const snapped = Math.round(raw / step) * step;
        return Math.min(max, Math.max(min, snapped));
      }

      let dragging = false;
      function setFromClientX(clientX){
        const rect = root.getBoundingClientRect();
        const left = rect.left + 8;
        const right = rect.right - 8;
        const p = Math.min(1, Math.max(0, (clientX - left) / (right - left)));
        const v = pctToValue(p);
        setValue(v);
        onChange(v);
        updateWheelUI(cfg, v);
      }

      const onDown = (e) => {
        dragging = true;
        thumb.style.cursor = "grabbing";
        const x = (e.touches && e.touches[0]) ? e.touches[0].clientX : e.clientX;
        setFromClientX(x);
      };
      const onMove = (e) => {
        if (!dragging) return;
        const x = (e.touches && e.touches[0]) ? e.touches[0].clientX : e.clientX;
        setFromClientX(x);
      };
      const onUp = () => {
        dragging = false;
        thumb.style.cursor = "grab";
      };

      root.addEventListener("mousedown", onDown);
      root.addEventListener("touchstart", onDown, {passive: false});
      window.addEventListener("mousemove", onMove);
      window.addEventListener("touchmove", onMove, {passive: false});
      window.addEventListener("mouseup", onUp);
      window.addEventListener("touchend", onUp);

      const cfg = { min, max, marks, root, marksEl, thumb, valueToPct, getValue };
      return cfg;
    }

    function updateWheelUI(cfg, value){
      const p = cfg.valueToPct(value);
      cfg.thumb.style.left = `calc(${(p * 100).toFixed(4)}% + 8px)`;
      const children = Array.from(cfg.marksEl.children);
      let bestIdx = 0, bestDist = Infinity;
      cfg.marks.forEach((v, i) => {
        const d = Math.abs(v - value);
        if (d < bestDist){ bestDist = d; bestIdx = i; }
      });
      children.forEach((el, i) => {
        el.classList.toggle("active", i === bestIdx);
      });
    }

    const wheelCfg = makeWheel({
      rootId: "wheel", marksId: "marks", thumbId: "thumb",
      min: 0, max: 100, step: 1, marks: [0, 25, 50, 75, 100],
      getValue: () => state.pct,
      setValue: (v) => { state.pct = v; $("pctText").textContent = v; },
      onChange: (v) => {
        if (v >= 90) state.risk.tipLevel = "red";
        else if (v >= 70) state.risk.tipLevel = "yellow";
        else state.risk.tipLevel = "green";
        renderRisk();
      }
    });

    const levWheelCfg = makeWheel({
      rootId:"levWheel", marksId: "levMarks", thumbId: "levThumb",
      min: 1, max: 100, step: 1, marks: [1, 20, 40, 60, 80, 100],
      getValue: () => state.leverage,
      setValue: (v) => { state.leverage = v; $("levVal").textContent = v; },
      onChange: (v) => {
        if (v > 50) state.risk.tipLevel = "red";
        else if (v > 10) state.risk.tipLevel="yellow";
        else state.risk.tipLevel="green";
        renderRisk();
      }
    });

    // -----------------------------
    // API 交互
    // -----------------------------
    async function fetchMT4Data() {
      try {
        const resp = await fetch("/api/latest_status");
        if (!resp.ok) return;
        const data = await resp.json();
        if (data) {
          // 更新 state
          state.equity = data.equity || 0;
          state.account.entry = data.equity || 0; // 暂用equity
          state.account.now = data.equity || 0; 
          state.account.dayPnl = data.daily_pnl || 0;
          state.account.dayPnlPct = data.daily_return || 0;
          state.account.availMargin = data.free_margin || 0;
          
          state.mt4.positions = data.positions || [];
          
          // 更新 Risk (简单模拟)
          const exposure = data.exposure_notional || 0;
          state.risk.long.perPoint = exposure; 
          state.risk.long.perPct = (exposure / (state.equity||1)) * 100;
          
          // 重新渲染
          renderAll();
        }
      } catch(e) { console.error(e); }
    }
    
    async function fetchHistory() {
      try {
        const resp = await fetch("/api/history_trades?limit=10");
        if(resp.ok) {
           const d = await resp.json();
           state.mt4.trades = d.trades || [];
           renderLists();
        }
      } catch(e) {}
    }
    
    async function sendCommand(side) {
      const lots = $("tradeLots").value;
      if(!lots || lots <= 0) { alert("请输入数量"); return; }
      
      const formData = new FormData();
      formData.append("symbol", state.activeSym);
      formData.append("side", side);
      formData.append("volume", lots);
      
      let cmdType = "MARKET";
      if(state.orderType.includes("限价")) cmdType = "LIMIT";
      else if(state.orderType.includes("市价")) cmdType = "MARKET";
      
      formData.append("cmd_type", cmdType);
      
      const price = $("inpPrice") ? $("inpPrice").value : "";
      const sl = $("inpSl") ? $("inpSl").value : "";
      const tp = $("inpTp") ? $("inpTp").value : "";
      
      if(price) formData.append("price", price);
      if(sl) formData.append("sl", sl);
      if(tp) formData.append("tp", tp);
      
      try {
        const resp = await fetch("/send_command", {method: "POST", body: formData});
        if(resp.ok) alert("指令已发送");
        else alert("发送失败");
      } catch(e) { alert("错误: " + e.message); }
    }

    // -----------------------------
    // 事件绑定
    // -----------------------------
    $("btnBuy").addEventListener("click", () => sendCommand("BUY"));
    $("btnSell").addEventListener("click", () => sendCommand("SELL"));

    function openMask(maskId){$(maskId).style.display="flex";}
    function closeMask(maskId){$(maskId).style.display="none";}
    
    // 品种选择
    function renderPairList(filter=""){
      const wrap = $("pairList");
      wrap.innerHTML = "";
      const q = filter.trim().toUpperCase();
      state.pairs
        .filter(p => !q || p.sym.includes(q))
        .forEach(p => {
          const row = document.createElement("div");
          row.className = "pairRow";
          row.innerHTML = `
            <div><strong>${p.sym}</strong> <span>永续</span></div>
            <div style="text-align:right">
              <strong>${fmtNum(p.last, p.last < 10?4:2)}</strong><br/>
              <span style="color:${p.chg<0?'var(--red)':'var(--green)'}">${p.chg.toFixed(2)}%</span>
            </div>
          `;
          row.addEventListener("click", () => {
            state.activeSym = p.sym;
            closeMask("pairMask");
            renderAll();
          });
          wrap.appendChild(row);
        });
    }
    $("btnPick").addEventListener("click", () => {renderPairList(""); openMask("pairMask");});
    $("closePair").addEventListener("click", () => closeMask("pairMask"));
    $("pairMask").addEventListener("click", (e) => { if (e.target.id ==="pairMask") closeMask("pairMask");});
    $("pairSearch").addEventListener("input", (e) => renderPairList(e.target.value));
    $("addPair").addEventListener("click", () => {
        const sym = $("pairNew").value.trim().toUpperCase();
        if(!sym) return;
        state.pairs.unshift({sym, last: 0, chg: 0});
        $("pairNew").value = "";
        renderPairList($("pairSearch").value);
    });
    
    // 杠杆
    $("btnLev").addEventListener("click", () => openMask("levMask"));
    $("closeLev").addEventListener("click", () => closeMask("levMask"));
    $("levMask").addEventListener("click", (e) => { if (e.target.id ==="levMask") closeMask("levMask");});
    $("confirmLev").addEventListener("click", () => { closeMask("levMask"); $("btnLev").textContent = state.leverage + "x"; });
    
    // 交易类型
    $("orderTypeField").addEventListener("click", () => {
      const idx = ORDER_TYPES.indexOf(state.orderType);
      const next = ORDER_TYPES[(idx + 1 + ORDER_TYPES.length) % ORDER_TYPES.length];
      state.orderType = next;
      renderOrderType();
    });

    // -----------------------------
    // 启动
    // -----------------------------
    renderAll();
    requestAnimationFrame(() => {
      if(wheelCfg) updateWheelUI(wheelCfg, state.pct);
      if(levWheelCfg) updateWheelUI(levWheelCfg, state.leverage);
    });
    window.addEventListener("resize", () => {
      if(wheelCfg) updateWheelUI(wheelCfg, state.pct);
      if(levWheelCfg) updateWheelUI(levWheelCfg, state.leverage);
    });
    
    // 轮询
    setInterval(fetchMT4Data, 3000);
    setInterval(fetchHistory, 5000);
    fetchMT4Data();
    fetchHistory();
    
  </script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(PREVIEW_TEMPLATE)

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

# ==================== 启动 ====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
