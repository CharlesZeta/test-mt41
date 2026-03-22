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

history_lock = threading.RLock()

commands = []
commands_lock = threading.RLock()
cmd_counter = 0

paused = False
pause_lock = threading.RLock()

def generate_unique_cmd_id():
    """生成全局唯一且单调递增的命令 ID"""
    global cmd_counter
    # 使用毫秒级时间戳 + 计数器，确保即使重启也不会重复 (假设重启间隔 > 1ms)
    # 格式: TS_COUNTER (例如 1715000000000_1)
    ms = int(time.time() * 1000)
    with commands_lock:
        cmd_counter += 1
        return f"{ms}_{cmd_counter}"

# ==================== 产品规则表 ====================
PRODUCT_SPECS = {
    # 1. 贵金属 / 原油
    "XAGUSD": {"size": 5000, "lev": 500, "currency": "USD", "type": "metal"},
    "XAUUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "metal"},
    "UKOUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "commodity"},
    "USOUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "commodity"},
    
    # 2. 指数
    "U30USD": {"size": 10, "lev": 100, "currency": "USD", "type": "index"},
    "NASUSD": {"size": 10, "lev": 100, "currency": "USD", "type": "index"},
    "SPXUSD": {"size": 100, "lev": 100, "currency": "USD", "type": "index"},
    "100GBP": {"size": 10, "lev": 100, "currency": "GBP", "type": "index"},
    "D30EUR": {"size": 10, "lev": 100, "currency": "EUR", "type": "index"},
    "E50EUR": {"size": 10, "lev": 100, "currency": "EUR", "type": "index"},
    "H33HKD": {"size": 100, "lev": 100, "currency": "HKD", "type": "index"},
    
    # 3. 虚拟货币
    "BTCUSD": {"size": 1, "lev": 500, "currency": "USD", "type": "crypto"},
    "BCHUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "RPLUSD": {"size": 10000, "lev": 500, "currency": "USD", "type": "crypto"},
    "LTCUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "ETHUSD": {"size": 10, "lev": 500, "currency": "USD", "type": "crypto"},
    "XMRUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "BNBUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "SOLUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "XSIUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "crypto"},
    "DOGUSD": {"size": 100000, "lev": 500, "currency": "USD", "type": "crypto"},
    "ADAUSD": {"size": 10000, "lev": 500, "currency": "USD", "type": "crypto"},
    "AVEUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "DSHUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "crypto"},
}

# 默认规则
DEFAULT_FOREX_SPEC = {"size": 100000, "lev": 500, "type": "forex"}
DEFAULT_STOCK_SPEC = {"size": 100, "lev": 10, "currency": "USD", "type": "stock"}

# ==================== 命令过期清理 ====================
def cleanup_expired_commands():
    """清理过期命令，防止积压，并记录过期状态"""
    now = int(time.time())
    with commands_lock:
        # 识别过期命令 (默认 600s TTL 如果未设置)
        expired = [c for c in commands if now - c.get("created_at", 0) >= c.get("ttl_sec", 600)]
        
        # 保留未过期的命令
        commands[:] = [c for c in commands if now - c.get("created_at", 0) < c.get("ttl_sec", 600)]
        
        if expired:
            print(f"[CLEANUP] 清理了 {len(expired)} 条过期命令")
            with history_lock:
                for cmd in expired:
                    # 构造一个模拟的过期报告
                    record = {
                        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "ip": "127.0.0.1", # Internal
                        "method": "INTERNAL",
                        "path": "cleanup",
                        "category": "report",
                        "headers": {},
                        "body_raw": "{}",
                        "parsed": {
                            "cmd_id": cmd.get("id"),
                            "ok": False,
                            "ticket": 0,
                            "error": "ORDER_EXPIRED",
                            "message": f"订单超时未执行 (TTL {cmd.get('ttl_sec', 600)}s)",
                            "exec_ms": 0,
                            "desc": "ORDER_EXPIRED"
                        }
                    }
                    history_report.appendleft(record)

# 定时清理线程
def cleanup_scheduler():
    while True:
        time.sleep(5)  # 每5秒检查一次
        cleanup_expired_commands()

cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
cleanup_thread.start()

# ==================== 时间限制函数 ====================
def is_restricted_time():
    """判断当前时间是否处于限制时段（功能已关闭）"""
    # 暂时关闭时间限制，允许全天交易
    return False
    
    # now = datetime.now()
    # h = now.hour
    # # 规则：只有 5:00 - 24:00 允许交易 (即 [5, 24))
    # # 禁止时段: 0, 1, 2, 3, 4 点
    # if 0 <= h < 5:
    #     return True
    # return False

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
                
                # 如果是交易执行报告 (ok=True/False 且有 cmd_id)，则持久化保存
                # 排除 QUOTE_DATA 和 INTERNAL 类型，以及 quote 指令报告(q_开头)
                cmd_id_str = str(parsed.get("cmd_id", ""))
                if desc != "QUOTE_DATA" and record.get("method") != "INTERNAL" and parsed.get("cmd_id") and not cmd_id_str.startswith("q_"):
                    trade_record = {
                        "received_at": record.get("received_at"),
                        "cmd_id": parsed.get("cmd_id"),
                        "ok": parsed.get("ok"),
                        "ticket": parsed.get("ticket"),
                        "error": parsed.get("error"),
                        "message": parsed.get("message"),
                        "exec_ms": parsed.get("exec_ms"),
                    }
                    save_history_trade(trade_record)

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

# ==================== 风控逻辑 ====================
# 全局风控状态
risk_state = {
    "is_fused": False,          # 是否熔断 (当日不再交易)
    "cooldown_until": 0,        # 冷静期结束时间戳 (0 表示无冷静期)
    "consecutive_losses": 0,    # 连续亏损次数
    "last_reset_day": None,     # 上次重置风控状态的日期
    "locked_tickets": set()     # 锁定的订单 Ticket (禁止手动操作)
}
risk_lock = threading.RLock()

def get_utc8_date_str():
    """获取当前 UTC+8 日期字符串 YYYY-MM-DD"""
    return datetime.utcfromtimestamp(time.time() + 8*3600).strftime("%Y-%m-%d")

def check_risk_status(account, current_equity, day_start_equity):
    """
    检查风控状态，返回 (是否允许交易, 拒绝原因/状态描述, 状态类型)
    状态类型: "normal", "fused", "cooldown", "restricted_time"
    """
    global risk_state
    now = int(time.time())
    today_str = get_utc8_date_str()
    
    with risk_lock:
        # 1. 跨天重置
        if risk_state["last_reset_day"] != today_str:
            risk_state["is_fused"] = False
            risk_state["consecutive_losses"] = 0
            risk_state["cooldown_until"] = 0
            risk_state["last_reset_day"] = today_str
            print(f"[RISK] New day {today_str}, reset risk state.")

        # 2. 检查熔断 (当日不再交易)
        if risk_state["is_fused"]:
            return False, "触发熔断机制，今日交易已停止", "fused"

        # 3. 检查冷静期
        if now < risk_state["cooldown_until"]:
            remaining = int((risk_state["cooldown_until"] - now) / 60)
            return False, f"处于冷静期，还需等待 {remaining} 分钟", "cooldown"

        # 4. 检查日内亏损 (累计亏损 > 9%)
        if day_start_equity > 0:
            daily_pnl_pct = (current_equity - day_start_equity) / day_start_equity
            if daily_pnl_pct < -0.09: # 亏损超过 9%
                risk_state["is_fused"] = True
                print(f"[RISK] Daily loss {daily_pnl_pct*100:.2f}% > 9%, FUSED.")
                return False, "日内亏损超限，触发熔断", "fused"

        # 5. 检查时间限制 (0:00 - 5:00)
        # 注意：这里复用 is_restricted_time 逻辑，但为了统一返回格式，单独判断
        if is_restricted_time():
             return False, "交易已暂停 (系统维护时段)", "restricted_time"

    return True, "交易正常", "normal"

def update_risk_after_trade(trade_profit, open_price, close_price, side):
    """
    每笔交易结算后更新风控计数器
    trade_profit: 交易盈亏 (USD)
    """
    global risk_state
    with risk_lock:
        if trade_profit < 0:
            risk_state["consecutive_losses"] += 1
            print(f"[RISK] Loss detected. Consecutive: {risk_state['consecutive_losses']}")
            
            # 规则：连续亏损 3 次 -> 熔断
            if risk_state["consecutive_losses"] >= 3:
                risk_state["is_fused"] = True
                print("[RISK] Consecutive losses >= 3, FUSED.")
            
            # 规则：连续亏损 2 次 -> 冷静 1 小时
            elif risk_state["consecutive_losses"] == 2:
                risk_state["cooldown_until"] = int(time.time()) + 3600
                print("[RISK] Consecutive losses == 2, Cooldown 1h.")
        else:
            # 盈利则重置连亏计数 (除非需求是"累计"连亏? 通常连亏是指连续的)
            # 用户描述："连续亏损不超过3次"，意味着盈利会打断连亏
            if trade_profit > 0:
                risk_state["consecutive_losses"] = 0

# ==================== 数据持久化 (每日盈亏 & 历史委托) ====================
DAILY_STATS_FILE = "daily_stats.json"
HISTORY_TRADES_FILE = "history_trades.json"
daily_stats_lock = threading.RLock()
history_file_lock = threading.RLock()

def load_daily_stats():
    if not os.path.exists(DAILY_STATS_FILE):
        return {}
    try:
        with open(DAILY_STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_daily_stats(stats):
    try:
        with open(DAILY_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERR] Save daily stats failed: {e}")

def load_history_trades():
    if not os.path.exists(HISTORY_TRADES_FILE):
        return []
    try:
        with open(HISTORY_TRADES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except:
        return []

def save_history_trade(trade_record):
    """追加单条历史记录到文件"""
    with history_file_lock:
        current_history = load_history_trades()
        # 避免重复添加 (根据 cmd_id)
        if trade_record.get("cmd_id"):
            if any(t.get("cmd_id") == trade_record["cmd_id"] for t in current_history):
                return
        
        current_history.insert(0, trade_record) # 最新的在前
        # 限制历史记录数量，防止文件过大 (例如保留最近 1000 条)
        if len(current_history) > 1000:
            current_history = current_history[:1000]
            
        try:
            with open(HISTORY_TRADES_FILE, "w", encoding="utf-8") as f:
                json.dump(current_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ERR] Save history trade failed: {e}")

def delete_history_trade_by_id(cmd_id):
    """根据 cmd_id 删除历史记录"""
    with history_file_lock:
        current_history = load_history_trades()
        new_history = [t for t in current_history if str(t.get("cmd_id")) != str(cmd_id)]
        
        if len(new_history) != len(current_history):
            try:
                with open(HISTORY_TRADES_FILE, "w", encoding="utf-8") as f:
                    json.dump(new_history, f, ensure_ascii=False, indent=2)
                return True
            except Exception as e:
                print(f"[ERR] Delete history trade failed: {e}")
        return False

def update_daily_stats_from_record(record):
    """从 status 记录中提取 daily_pnl 并更新到文件"""
    if not record or not record.get("parsed"):
        return
        
    # 先补全计算字段 (daily_pnl)
    parsed = auto_fill_status(record["parsed"])
    
    daily_pnl = parsed.get("daily_pnl")
    if daily_pnl is None:
        return
        
    # 获取日期 (UTC+8)
    ts = parsed.get("ts") or int(time.time())
    # 转为 UTC+8 datetime
    dt_utc8 = datetime.utcfromtimestamp(ts + 8*3600)
    date_str = dt_utc8.strftime("%Y-%m-%d") # Key: "YYYY-MM-DD"
    
    # 只需要更新当天的，因为 daily_pnl 是累计值（截至当前）
    # 但要注意：如果是补发昨天的包，不应覆盖。
    # 简单起见，我们假设收到的都是最新的。
    
    with daily_stats_lock:
        stats = load_daily_stats()
        # 更新当天的盈亏 (覆盖旧值，因为是累计)
        # 格式：{"YYYY-MM-DD": float}
        current_val = round(float(daily_pnl), 2)
        
        # 仅当数值变化时才保存和打印日志，避免频繁 I/O
        if stats.get(date_str) != current_val:
            stats[date_str] = current_val
            save_daily_stats(stats)
            print(f"[DAILY_STATS] Saved {date_str}: {current_val} (Account: {parsed.get('account')})")

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

def calc_lots_from_margin_usd(symbol, margin_usd, data):
    """根据投入保证金金额(USD)计算手数"""
    equity = data.get("equity", 0)
    # 获取最新价格
    price = get_latest_price(symbol)
    if not price or price <= 0:
        print(f"[CALC_LOTS] Error: Price 0 or missing for {symbol}")
        return 0.0
    
    # 查找规则
    spec = PRODUCT_SPECS.get(symbol)
    if not spec:
        # 默认回退逻辑
        if len(symbol) == 6 and symbol.isupper():
            spec = DEFAULT_FOREX_SPEC.copy()
            # 简单推断 settle currency: 后3位
            spec["currency"] = symbol[3:]
        else:
            spec = DEFAULT_STOCK_SPEC.copy()
    
    # 获取结算货币对 USD 的汇率
    settle_currency = spec.get("currency", "USD")
    rate_to_usd = get_rate_to_usd(settle_currency)
    
    contract_size = spec["size"]
    leverage = spec["lev"]
    
    # 计算公式: 
    # Notional (Base) = Price * ContractSize * Lots
    # Margin (Base) = Notional / Leverage
    # Margin (USD) = Margin (Base) * RateToUSD
    # -> Margin (USD) = (Price * ContractSize * Lots / Leverage) * RateToUSD
    # -> Lots = (Margin (USD) * Leverage) / (Price * ContractSize * RateToUSD)
    
    # 防止除零
    if price <= 0 or rate_to_usd <= 0:
        return 0.0
        
    lots = (margin_usd * leverage) / (price * contract_size * rate_to_usd)
    print(f"[CALC_LOTS] {symbol} Margin=${margin_usd} Lev={leverage} Price={price} Rate={rate_to_usd} -> Lots={lots:.4f}")
    return lots

def get_rate_to_usd(currency):
    """获取指定货币兑 USD 的汇率 (例如 EUR -> USD, JPY -> USD)"""
    if currency == "USD":
        return 1.0
    
    # 尝试查找 XXUSD
    pair1 = f"{currency}USD"
    price1 = get_latest_price(pair1)
    if price1:
        return price1
        
    # 尝试查找 USDXX (反向)
    pair2 = f"USD{currency}"
    price2 = get_latest_price(pair2)
    if price2 and price2 > 0:
        return 1.0 / price2
        
    # 简单兜底: 如果是 HKD (固定汇率近似)
    if currency == "HKD": return 1.0 / 7.8
    
    # 兜底: 找不到汇率时，暂时按 1:1 处理并警告 (避免无法下单)
    print(f"[WARN] Cannot find rate for {currency} -> USD, using 1.0")
    return 1.0

def get_latest_price(symbol):
    """从 history_report 或 latest_quote 中查找最新价格"""
    # 优先查 history_report 中的 QUOTE_DATA
    # 注意：此函数如果在 with history_lock 内部调用是安全的（锁可重入）
    # 但如果是跨线程或在非锁环境下调用，建议调用方自己管理锁
    # 这里加锁是为了安全，RLock 允许同一线程多次获取
    with history_lock:
        for record in history_report:
            parsed = record.get("parsed")
            if parsed and parsed.get("desc") == "QUOTE_DATA" and parsed.get("symbol") == symbol:
                try:
                    msg = json.loads(parsed.get("message", "{}"))
                    if "bid" in msg:
                        # print(f"[PRICE] Found {symbol} bid={msg['bid']}") # 可选：过于频繁可不打
                        return (msg["bid"] + msg["ask"]) / 2 # 取中间价估算
                except:
                    pass
    print(f"[PRICE] Warn: No quote found for {symbol}")
    return None

def calc_lot_info(symbol, price, lots):
    """计算单手信息 (供前端展示)"""
    spec = PRODUCT_SPECS.get(symbol)
    if not spec:
        if len(symbol) == 6: spec = DEFAULT_FOREX_SPEC.copy(); spec["currency"] = symbol[3:]
        else: spec = DEFAULT_STOCK_SPEC.copy()
        
    settle_curr = spec.get("currency", "USD")
    rate = get_rate_to_usd(settle_curr)
    
    # 单手保证金 (USD)
    margin_per_lot_usd = (price * spec["size"] / spec["lev"]) * rate
    
    # 每点价值 (USD) - 估算
    # Point Value (Base) = ContractSize * PointSize
    # 这里简化：假设 forex point=0.00001 or 0.001 (JPY)
    # 实际上需要知道 PointSize。对于大多数 API，point value 也可以通过 symbol properties 获取
    # 这里做个简单估算：
    # Forex (非JPY): 100000 * 0.00001 = 1 Base -> * Rate
    # JPY: 100000 * 0.001 = 100 JPY -> / USDJPY
    # XAU: 100 * 0.01 = 1 USD
    
    point_val_usd = 0
    if spec["type"] == "forex":
        if "JPY" in symbol:
            point_val_usd = (spec["size"] * 0.001) * rate 
        else:
            point_val_usd = (spec["size"] * 0.00001) * rate
    elif spec["type"] == "metal":
        point_val_usd = spec["size"] * 0.01 # XAUUSD point is usually 0.01
    else:
        point_val_usd = spec["size"] * 0.01 * rate # 默认泛化
        
    return {
        "margin_per_lot_usd": margin_per_lot_usd,
        "point_val_usd": point_val_usd,
        "spec": spec
    }

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
    
    # 注入全局风控状态 (重要)
    global risk_state
    current_risk_status = "normal"
    risk_msg = ""
    locked_tickets_list = []
    
    with risk_lock:
        if risk_state["is_fused"]:
            current_risk_status = "fused"
            risk_msg = "触发熔断机制"
        elif int(time.time()) < risk_state["cooldown_until"]:
            current_risk_status = "cooldown"
            risk_msg = "处于冷静期"
        
        # 转换为 list 传给前端
        locked_tickets_list = list(risk_state["locked_tickets"])
    
    # 如果处于限制时间，也可以在这里覆盖，或者由前端 checkTradeTime 处理
    if is_restricted_time():
        current_risk_status = "restricted_time"
        risk_msg = "系统维护时段"

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
        # 添加风控状态
        "risk_status": current_risk_status,
        "risk_msg": risk_msg,
        "locked_tickets": locked_tickets_list
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
    symbol_filter = request.args.get("symbol", "").upper()
    print(f"[LATEST_STATUS] enter symbol={symbol_filter}")
    
    # 1. 在锁内提取所有需要的数据，避免锁竞争和死锁风险
    detail = None
    latest_quote = None
    current_price = 0
    
    with history_lock:
        latest_status_record = history_status[0] if history_status else None
        latest_positions_record = history_positions[0] if history_positions else None
        
        # 查找最新的 QUOTE_DATA 报告
        for record in history_report:
            parsed = record.get("parsed")
            if parsed and parsed.get("desc") == "QUOTE_DATA":
                record_symbol = parsed.get("symbol", "")
                
                if symbol_filter and record_symbol and record_symbol != symbol_filter:
                    continue 
                
                try:
                    msg = parsed.get("message", "{}")
                    quote_data = json.loads(msg)
                    if isinstance(quote_data, dict) and "bid" in quote_data:
                        latest_quote = {
                            "bid": quote_data.get("bid"),
                            "ask": quote_data.get("ask"),
                            "spread": parsed.get("spread"),
                            "ts": parsed.get("ts"),
                            "symbol": record_symbol
                        }
                        # 记录当前价格供后续计算
                        current_price = latest_quote["bid"]
                        print(f"[LATEST_STATUS] quote found symbol={record_symbol} bid={latest_quote['bid']}")
                        break 
                except:
                    continue

        positions_data = None
        if latest_positions_record:
            positions_data = latest_positions_record.get("parsed", {}).get("positions", [])
        
        # 提取详情
        detail = extract_latest_details_from_status(latest_status_record, positions_data)
        
    # 2. 锁已释放，执行计算逻辑
    if detail:
        if latest_quote:
            detail["latest_quote"] = latest_quote
        
        # 注入产品规则和计算信息
        if symbol_filter and current_price > 0:
            # calc_lot_info 内部会调用 get_rate_to_usd -> get_latest_price，后者会再次获取 history_lock
            # 由于已释放外层锁，这里是安全的
            lot_info = calc_lot_info(symbol_filter, current_price, 1.0)
            detail["symbol_rules"] = lot_info
            print(f"[LATEST_STATUS] symbol_rules loaded symbol={symbol_filter} margin_per_lot={lot_info.get('margin_per_lot_usd')}")
        
        print(f"[LATEST_STATUS] response ok symbol={symbol_filter}")
        return jsonify(detail)
    else:
        return jsonify({})

# 网页端获取历史成交记录
@app.route("/api/history_trades", methods=["GET"])
def api_history_trades():
    """返回历史成交记录（从持久化文件中读取）"""
    limit = request.args.get("limit", 20, type=int)
    
    # 优先从文件读取持久化的历史记录
    all_trades = load_history_trades()
    
    # 分页/截断
    trades = all_trades[:limit]
    
    return jsonify({"trades": trades})

@app.route("/api/history_trades/delete", methods=["POST"])
def delete_history_trade_api():
    """删除单条历史记录 (需密码验证)"""
    data = request.json
    cmd_id = data.get("cmd_id")
    password = data.get("password", "")
    
    if password != "1234567dads":
        return jsonify({"success": False, "message": "密码错误"}), 403
        
    if not cmd_id:
        return jsonify({"success": False, "message": "缺少指令ID"}), 400
        
    if delete_history_trade_by_id(cmd_id):
        return jsonify({"success": True, "message": "删除成功"})
    else:
        return jsonify({"success": False, "message": "记录不存在或删除失败"}), 404

# ==================== 主页 ====================
HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover" />
  <title>量化交易终端</title>
  <style>
    :root {
      --blue: #007aff;
      --red: #ff3b30;
      --green: #34c759;
      --bg: #ffffff;
      --text: #333333;
      --muted: #8e8e93;
      --line: #c6c6c8;
      --nav-bg: #f8f8f8;
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; outline: none; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); padding-bottom: 60px; padding-top: 50px; }
    
    .top-bar { position: fixed; top: 0; left: 0; right: 0; height: 50px; background: var(--bg); border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; padding: 0 15px; font-size: 18px; font-weight: bold; z-index: 100; }
    .top-bar-title { flex: 1; }
    .top-bar-actions { display: flex; gap: 15px; font-size: 24px; color: var(--blue); cursor: pointer; }
    .top-bar-actions span:active { opacity: 0.7; }
    .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; height: 55px; background: var(--nav-bg); border-top: 1px solid var(--line); display: flex; justify-content: space-around; align-items: center; z-index: 100; padding-bottom: env(safe-area-inset-bottom); }
    .nav-item { display: flex; flex-direction: column; align-items: center; color: var(--muted); font-size: 10px; cursor: pointer; flex: 1; }
    .nav-item.active { color: var(--blue); }
    .nav-icon { font-size: 22px; margin-bottom: 2px; }
    
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    
    /* Quotes Tab */
    .q-row { padding: 10px 15px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; cursor: pointer; position: relative; }
    .q-row.edit-mode .q-left { margin-left: 30px; }
    .q-delete-btn { position: absolute; left: -30px; top: 50%; transform: translateY(-50%); color: var(--red); font-size: 24px; transition: 0.3s; opacity: 0; pointer-events: none; }
    .q-row.edit-mode .q-delete-btn { left: 10px; opacity: 1; pointer-events: auto; }
    .q-left { display: flex; flex-direction: column; transition: 0.3s; }
    .q-sym { font-size: 16px; font-weight: bold; }
    .q-time, .q-spread { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .q-right { display: flex; flex-direction: column; align-items: flex-end; }
    .q-prices { display: flex; gap: 15px; font-size: 18px; font-weight: bold; }
    .q-prices .blue { color: var(--blue); }
    .q-prices .red { color: var(--red); }
    .q-hl { display: flex; gap: 10px; font-size: 11px; color: var(--muted); margin-top: 4px; }

    /* Trade Tab */
    .trade-header { padding: 10px 15px; border-bottom: 1px solid var(--line); display: flex; align-items: center; gap: 10px; }
    .sym-title { font-size: 20px; font-weight: bold; }
    .sym-desc { font-size: 14px; color: var(--muted); }
    .trade-type { text-align: center; padding: 15px; font-size: 16px; font-weight: bold; border-bottom: 1px solid var(--line); cursor: pointer; }
    
    .lots-stepper { display: flex; justify-content: space-between; align-items: center; padding: 15px; border-bottom: 1px solid var(--line); }
    .l-btn { color: var(--blue); font-weight: bold; font-size: 16px; padding: 5px; cursor: pointer; min-width: 40px; text-align: center; }
    .lots-stepper input { text-align: center; font-size: 22px; font-weight: bold; border: none; outline: none; width: 80px; color: var(--text); }
    
    .trade-prices { display: flex; justify-content: space-around; padding: 20px 15px; font-size: 32px; font-weight: bold; }
    .trade-prices .red { color: var(--red); }
    .trade-prices .blue { color: var(--blue); }
    
    .t-row { display: flex; align-items: center; padding: 10px 15px; border-bottom: 1px solid var(--line); }
    .t-btn { color: var(--blue); font-size: 24px; width: 40px; text-align: center; cursor: pointer; font-weight: 300; }
    .t-input-wrap { flex: 1; display: flex; justify-content: center; align-items: center; gap: 10px; }
    .t-input-wrap .t-label { color: var(--muted); font-size: 14px; }
    .t-input-wrap input { font-size: 18px; border: none; outline: none; width: 100px; text-align: center; font-weight: bold; }
    
    .sl-tp-row { display: flex; border-bottom: 1px solid var(--line); }
    .t-col { flex: 1; display: flex; align-items: center; padding: 10px 5px; }
    .sl-col { border-bottom: 2px solid var(--red); margin-right: 5px; }
    .tp-col { border-bottom: 2px solid var(--green); margin-left: 5px; }
    .t-col input { flex: 1; width: 100%; text-align: center; border: none; outline: none; font-size: 18px; font-weight: bold; }
    
    .t-duration { padding: 15px; display: flex; justify-content: space-between; border-bottom: 1px solid var(--line); font-size: 14px; }
    .t-duration .t-label { color: var(--muted); }
    .t-duration .t-val { color: var(--text); }
    
    .trade-actions { display: flex; padding: 15px; gap: 10px; margin-top: 10px; }
    .btn-sell, .btn-buy, .btn-place { flex: 1; padding: 15px; border: none; border-radius: 5px; font-size: 16px; font-weight: bold; color: #fff; cursor: pointer; }
    .btn-sell { background: var(--red); }
    .btn-buy { background: var(--blue); }
    .btn-place { background: var(--text); }
    .btn-sell:active, .btn-buy:active, .btn-place:active { opacity: 0.8; }

    /* Positions & History Tab */
    .pos-header { padding: 15px; background: var(--nav-bg); border-bottom: 1px solid var(--line); }
    .pos-h-row { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; }
    .pos-h-row:last-child { margin-bottom: 0; }
    .pos-h-row .label { color: var(--muted); }
    .pos-h-row .val { font-weight: bold; }
    .pos-list-title { background: #f0f0f0; padding: 5px 15px; font-size: 12px; color: #666; font-weight: bold; }
    
    .pos-item { padding: 10px 15px; border-bottom: 1px solid var(--line); cursor: pointer; }
    .p-row1 { display: flex; justify-content: space-between; margin-bottom: 5px; align-items: center; }
    .p-sym { font-weight: bold; font-size: 16px; }
    .p-type { font-size: 14px; margin-left: 5px; }
    .p-type.buy { color: var(--blue); }
    .p-type.sell { color: var(--red); }
    .p-lots { font-size: 14px; margin-left: 5px; }
    .p-profit { font-weight: bold; font-size: 16px; }
    .p-profit.blue { color: var(--blue); }
    .p-profit.red { color: var(--red); }
    .p-row2 { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); }

    /* Modals */
    .modalMask { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: none; align-items: flex-end; z-index: 1000; }
    .modal { width: 100%; background: #fff; border-radius: 15px 15px 0 0; padding-bottom: env(safe-area-inset-bottom); animation: slideUp 0.3s ease-out; max-height: 80vh; overflow-y: auto; }
    @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
    .modalHeader { padding: 15px; text-align: center; font-weight: bold; font-size: 16px; border-bottom: 1px solid var(--line); position: relative; }
    .modalHeader .close { position: absolute; right: 15px; top: 15px; color: var(--blue); cursor: pointer; }
    .select-item { padding: 15px; border-bottom: 1px solid var(--line); font-size: 16px; text-align: center; cursor: pointer; }
    .select-item:active { background: #f0f0f0; }

    /* Quiz specific */
    .modalBody { padding: 15px; }
    .quiz-item { margin-bottom: 15px; }
    .quiz-q { font-size: 14px; font-weight: bold; margin-bottom: 10px; }
    .quiz-opts { display: flex; gap: 10px; flex-direction: column; }
    .quiz-opt { padding: 10px; border: 1px solid var(--line); border-radius: 8px; text-align: center; font-size: 14px; cursor: pointer; }
    .quiz-opt.selected { background: var(--blue); color: #fff; border-color: var(--blue); }
    .cta { width: 100%; padding: 15px; background: var(--blue); color: #fff; border: none; border-radius: 8px; font-size: 16px; font-weight: bold; margin-top: 10px; }
    /* Add Symbol Modal specific */
    .cat-tabs { display: flex; overflow-x: auto; border-bottom: 1px solid var(--line); background: var(--nav-bg); }
    .cat-tab { padding: 12px 15px; white-space: nowrap; color: var(--muted); font-weight: bold; cursor: pointer; }
    .cat-tab.active { color: var(--blue); border-bottom: 2px solid var(--blue); }
    .sym-add-item { display: flex; justify-content: space-between; align-items: center; padding: 15px; border-bottom: 1px solid var(--line); }
    .sym-add-info { display: flex; flex-direction: column; }
    .sym-add-name { font-weight: bold; font-size: 16px; }
    .sym-add-desc { font-size: 12px; color: var(--muted); }
    .sym-add-btn { color: var(--green); font-size: 24px; cursor: pointer; }
    .sym-add-btn.added { color: var(--muted); cursor: default; }

  </style>
</head>
<body>
  <div class="top-bar">
    <div class="top-bar-title" id="topBarTitle">行情</div>
    <div class="top-bar-actions" id="quotesActions">
      <span onclick="openAddSymbol()">+</span>
      <span id="btnEditQuotes" onclick="toggleEditQuotes()">✎</span>
    </div>
  </div>

  <!-- Quotes Tab -->
  <div class="tab-content active" id="tab-quotes">
    <div id="quotesList"></div>
  </div>

  <!-- Trade Tab -->
  <div class="tab-content" id="tab-trade">
    <div class="trade-header">
      <div class="sym-title" id="tradeSym">XAUUSD</div>
      <div class="sym-desc" id="tradeSymDesc">Spot Gold</div>
    </div>
    <div class="trade-type" onclick="$('orderTypeMask').style.display='flex'">
      <span id="orderTypeText">市价执行</span>
    </div>
    <div class="lots-stepper">
      <div class="l-btn" onclick="adjustLots(-0.1)">-0.1</div>
      <div class="l-btn" onclick="adjustLots(-0.01)">-0.01</div>
      <input type="number" id="inpLots" value="0.11" step="0.01" oninput="onLotsChange(this)">
      <div class="l-btn" onclick="adjustLots(0.01)">+0.01</div>
      <div class="l-btn" onclick="adjustLots(0.1)">+0.1</div>
    </div>
    <div class="trade-prices">
      <div class="t-bid red" id="tBid">5108.93</div>
      <div class="t-ask blue" id="tAsk">5109.04</div>
    </div>
    <div class="t-row" id="rowPrice" style="display:none;">
      <div class="t-btn" onclick="adjustInput('inpPrice', -1)">-</div>
      <div class="t-input-wrap">
        <span class="t-label">价格:</span>
        <input type="number" id="inpPrice" placeholder="0.00">
      </div>
      <div class="t-btn" onclick="adjustInput('inpPrice', 1)">+</div>
    </div>
    <div class="sl-tp-row">
      <div class="t-col sl-col">
        <div class="t-btn" onclick="adjustInput('inpSl', -1)">-</div>
        <input type="number" id="inpSl" placeholder="0.00">
        <div class="t-btn" onclick="adjustInput('inpSl', 1)">+</div>
      </div>
      <div class="t-col tp-col">
        <div class="t-btn" onclick="adjustInput('inpTp', -1)">-</div>
        <input type="number" id="inpTp" placeholder="0.00">
        <div class="t-btn" onclick="adjustInput('inpTp', 1)">+</div>
      </div>
    </div>
    <div class="t-duration" onclick="$('ttlMask').style.display='flex'">
      <span class="t-label">期限:</span>
      <span class="t-val" id="ttlDisplay">直到取消</span>
      <input type="hidden" id="inpTTL" value="0">
    </div>
    
    <div class="trade-actions" id="actionMarket">
      <button class="btn-sell" onclick="initiateOrder('SELL')">Sell by Market</button>
      <button class="btn-buy" onclick="initiateOrder('BUY')">Buy by Market</button>
    </div>
    <div class="trade-actions" id="actionPending" style="display:none;">
      <button class="btn-place" onclick="initiateOrder('PENDING')">下单</button>
    </div>
  </div>

  <!-- Positions Tab -->
  <div class="tab-content" id="tab-positions">
    <div class="pos-header">
      <div class="pos-h-row"><span class="label">结余:</span><span class="val" id="valBalance">0.00</span></div>
      <div class="pos-h-row"><span class="label">净值:</span><span class="val" id="valEquity">0.00</span></div>
      <div class="pos-h-row"><span class="label">可用预付款:</span><span class="val" id="valFreeMargin">0.00</span></div>
    </div>
    <div class="pos-list-title">持仓</div>
    <div id="list-positions"></div>
    <div class="pos-list-title">订单</div>
    <div id="list-orders"></div>
  </div>

  <!-- History Tab -->
  <div class="tab-content" id="tab-history">
    <div class="pos-header">
      <div class="pos-h-row"><span class="label">利润:</span><span class="val" id="valProfit">0.00</span></div>
      <div class="pos-h-row"><span class="label">结余:</span><span class="val" id="valHistBalance">0.00</span></div>
    </div>
    <div id="list-history"></div>
  </div>

  <!-- Bottom Nav -->
  <div class="bottom-nav">
    <div class="nav-item active" onclick="switchTab('quotes', '行情', this)">
      <div class="nav-icon">📊</div>
      <span>行情</span>
    </div>
    <div class="nav-item" onclick="switchTab('trade', '交易', this)">
      <div class="nav-icon">⚖️</div>
      <span>交易</span>
    </div>
    <div class="nav-item" onclick="switchTab('positions', '持仓', this)">
      <div class="nav-icon">💼</div>
      <span>持仓</span>
    </div>
    <div class="nav-item" onclick="switchTab('history', '历史', this)">
      <div class="nav-icon">🕒</div>
      <span>历史</span>
    </div>
  </div>

  <!-- Order Type Modal -->
  <div class="modalMask" id="orderTypeMask" onclick="closeModal(event, 'orderTypeMask')">
    <div class="modal">
      <div class="modalHeader">选择交易类型 <span class="close" onclick="$('orderTypeMask').style.display='none'">取消</span></div>
      <div class="modalBody" style="padding:0;">
        <div class="select-item" onclick="setOrderType('market', '市价执行', '')">市价执行</div>
        <div class="select-item" onclick="setOrderType('limit', 'Buy Limit', 'BUY')">Buy Limit</div>
        <div class="select-item" onclick="setOrderType('limit', 'Sell Limit', 'SELL')">Sell Limit</div>
        <div class="select-item" onclick="setOrderType('limit', 'Buy Stop', 'BUY')">Buy Stop</div>
        <div class="select-item" onclick="setOrderType('limit', 'Sell Stop', 'SELL')">Sell Stop</div>
      </div>
    </div>
  </div>

  <!-- TTL Modal -->
  <div class="modalMask" id="ttlMask" onclick="closeModal(event, 'ttlMask')">
    <div class="modal">
      <div class="modalHeader">期限 <span class="close" onclick="$('ttlMask').style.display='none'">取消</span></div>
      <div class="modalBody" style="padding:0;">
        <div class="select-item" onclick="setTTL(0, '直到取消')">直到取消 (GTC)</div>
        <div class="select-item" onclick="setTTL(10, '10 分钟')">10 分钟</div>
        <div class="select-item" onclick="setTTL(60, '1 小时')">1 小时</div>
        <div class="select-item" onclick="setTTL(1440, '1 天')">1 天</div>
      </div>
    </div>
  </div>

  <!-- Modify Position Modal -->
  <div class="modalMask" id="modifyMask" onclick="closeModal(event, 'modifyMask')">
    <div class="modal">
      <div class="modalHeader">修改订单 <span class="close" onclick="$('modifyMask').style.display='none'">取消</span></div>
      <div class="modalBody">
        <div class="sl-tp-row" style="border:none; margin-bottom: 20px;">
          <div class="t-col sl-col">
            <div class="t-btn" onclick="adjustInput('modSlPrice', -1)">-</div>
            <input type="number" id="modSlPrice" placeholder="止损">
            <div class="t-btn" onclick="adjustInput('modSlPrice', 1)">+</div>
          </div>
          <div class="t-col tp-col">
            <div class="t-btn" onclick="adjustInput('modTpPrice', -1)">-</div>
            <input type="number" id="modTpPrice" placeholder="止盈">
            <div class="t-btn" onclick="adjustInput('modTpPrice', 1)">+</div>
          </div>
        </div>
        <button class="cta" onclick="submitModifyOrder()">修改</button>
        <button class="cta" style="background:var(--red); margin-top:10px;" onclick="closePosition()">平仓</button>
      </div>
    </div>
  </div>

  <!-- Quiz Modal -->
  <div class="modalMask" id="quizMask" onclick="closeModal(event, 'quizMask')">
    <div class="modal">
      <div class="modalHeader">执行前风控检查 <span class="close" onclick="$('quizMask').style.display='none'">取消</span></div>
      <div class="modalBody">
        <div class="quiz-item">
          <div class="quiz-q">1. 该笔交易是否顺应大级别趋势？</div>
          <div class="quiz-opts">
            <div class="quiz-opt" onclick="selectQuiz(this, 1, 'A')">A. 是的，顺势</div>
            <div class="quiz-opt" onclick="selectQuiz(this, 1, 'B')">B. 否，逆势博弈</div>
          </div>
        </div>
        <div class="quiz-item">
          <div class="quiz-q">2. 盈亏比是否达到你的交易系统标准？</div>
          <div class="quiz-opts">
            <div class="quiz-opt" onclick="selectQuiz(this, 2, 'A')">A. 已达标</div>
            <div class="quiz-opt" onclick="selectQuiz(this, 2, 'B')">B. 未达标</div>
          </div>
        </div>
        <button class="cta" onclick="confirmOrderAfterQuiz()">确认并发送订单</button>
      </div>
    </div>
  </div>

  <!-- Add Symbol Modal -->
  <div class="modalMask" id="addSymbolMask" onclick="closeModal(event, 'addSymbolMask')">
    <div class="modal" style="height: 90vh;">
      <div class="modalHeader">添加品种 <span class="close" onclick="$('addSymbolMask').style.display='none'">取消</span></div>
      <div class="cat-tabs" id="addSymbolCats">
        <!-- 动态生成分类 Tab -->
      </div>
      <div class="modalBody" id="addSymbolList" style="padding:0; overflow-y: auto; height: calc(100% - 100px);">
        <!-- 动态生成品种列表 -->
      </div>
    </div>
  </div>

  <script>
    const $ = id => document.getElementById(id);
    window.quantState = {
      price: 0,
      lots: 0.11,
      orderType: 'market',
      pendingSide: '',
      currentModifyTicket: null
    };
    let quoteInterval = null;
    let quizAnswers = {};

    function fmtNum(n, d) { return parseFloat(n || 0).toFixed(d); }

    const categoryPairs = {
      'Forex': [
        { name: 'USDCHF', desc: 'US Dollar vs Swiss Franc', price: 0.8840, spread: 5, low: 0.8800, high: 0.8900 },
        { name: 'GBPUSD', desc: 'Great Britain Pound vs US Dollar', price: 1.2640, spread: 8, low: 1.2600, high: 1.2680 },
        { name: 'EURUSD', desc: 'Euro vs US Dollar', price: 1.1561, spread: 5, low: 1.1507, high: 1.1572 },
        { name: 'USDJPY', desc: 'US Dollar vs Japanese Yen', price: 149.50, spread: 7, low: 149.00, high: 150.00 },
        { name: 'USDCAD', desc: 'US Dollar vs Canadian Dollar', price: 1.3580, spread: 6, low: 1.3500, high: 1.3600 },
        { name: 'AUDUSD', desc: 'Australian Dollar vs US Dollar', price: 0.6520, spread: 5, low: 0.6500, high: 0.6550 },
        { name: 'EURGBP', desc: 'Euro vs Great Britain Pound', price: 0.8580, spread: 6, low: 0.8550, high: 0.8600 },
        { name: 'EURAUD', desc: 'Euro vs Australian Dollar', price: 1.6533, spread: 10, low: 1.6500, high: 1.6600 },
        { name: 'EURCHF', desc: 'Euro vs Swiss Franc', price: 0.9050, spread: 7, low: 0.9000, high: 0.9100 },
        { name: 'EURJPY', desc: 'Euro vs Japanese Yen', price: 162.74, spread: 9, low: 162.00, high: 163.00 },
        { name: 'GBPCHF', desc: 'Great Britain Pound vs Swiss Franc', price: 1.0418, spread: 12, low: 1.0400, high: 1.0500 },
        { name: 'CADJPY', desc: 'Canadian Dollar vs Japanese Yen', price: 115.48, spread: 10, low: 115.00, high: 116.00 },
        { name: 'GBPJPY', desc: 'Great Britain Pound vs Japanese Yen', price: 210.35, spread: 15, low: 210.00, high: 211.00 },
        { name: 'AUDNZD', desc: 'Australian Dollar vs New Zealand Dollar', price: 1.1918, spread: 8, low: 1.1900, high: 1.2000 },
        { name: 'AUDCAD', desc: 'Australian Dollar vs Canadian Dollar', price: 0.9568, spread: 8, low: 0.9500, high: 0.9600 },
        { name: 'AUDCHF', desc: 'Australian Dollar vs Swiss Franc', price: 0.5473, spread: 10, low: 0.5400, high: 0.5500 },
        { name: 'AUDJPY', desc: 'Australian Dollar vs Japanese Yen', price: 110.51, spread: 10, low: 110.00, high: 111.00 },
        { name: 'CHFJPY', desc: 'Swiss Franc vs Japanese Yen', price: 201.88, spread: 12, low: 201.00, high: 202.00 },
        { name: 'EURNZD', desc: 'Euro vs New Zealand Dollar', price: 1.9707, spread: 15, low: 1.9600, high: 1.9800 },
        { name: 'EURCAD', desc: 'Euro vs Canadian Dollar', price: 1.5822, spread: 12, low: 1.5800, high: 1.5900 },
        { name: 'CADCHF', desc: 'Canadian Dollar vs Swiss Franc', price: 0.5719, spread: 10, low: 0.5700, high: 0.5800 },
        { name: 'NZDJPY', desc: 'New Zealand Dollar vs Japanese Yen', price: 92.71, spread: 10, low: 92.00, high: 93.00 },
        { name: 'NZDUSD', desc: 'New Zealand Dollar vs US Dollar', price: 0.5872, spread: 6, low: 0.5800, high: 0.5900 },
        { name: 'GBPAUD', desc: 'Great Britain Pound vs Australian Dollar', price: 1.9032, spread: 15, low: 1.9000, high: 1.9100 },
        { name: 'GBPCAD', desc: 'Great Britain Pound vs Canadian Dollar', price: 1.8213, spread: 15, low: 1.8200, high: 1.8300 },
        { name: 'GBPNZD', desc: 'Great Britain Pound vs New Zealand Dollar', price: 2.2686, spread: 20, low: 2.2600, high: 2.2800 },
        { name: 'NZDCAD', desc: 'New Zealand Dollar vs Canadian Dollar', price: 0.8027, spread: 10, low: 0.8000, high: 0.8100 },
        { name: 'NZDCHF', desc: 'New Zealand Dollar vs Swiss Franc', price: 0.4591, spread: 12, low: 0.4500, high: 0.4600 },
        { name: 'USDSGD', desc: 'US Dollar vs Singapore Dollar', price: 1.2805, spread: 10, low: 1.2800, high: 1.2900 },
        { name: 'USDHKD', desc: 'US Dollar vs Hong Kong Dollar', price: 7.8190, spread: 5, low: 7.8100, high: 7.8300 },
        { name: 'USDCNH', desc: 'US Dollar vs Chinese Yuan', price: 6.9147, spread: 20, low: 6.9000, high: 6.9500 }
      ],
      'Metals': [
        { name: 'XAGUSD', desc: 'Spot Silver', price: 82.76, spread: 20, low: 82.00, high: 83.00 },
        { name: 'XAUUSD', desc: 'Spot Gold', price: 5110.43, spread: 17, low: 5015.04, high: 5197.72 }
      ],
      'Indices': [
        { name: 'U30USD', desc: 'Wall Street 30', price: 47836.1, spread: 28, low: 47000.0, high: 48000.0 },
        { name: 'NASUSD', desc: 'US Tech 100', price: 24922.8, spread: 12, low: 24000.0, high: 25000.0 },
        { name: 'SPXUSD', desc: 'US SPX 500', price: 6803.72, spread: 15, low: 6700.0, high: 6900.0 },
        { name: '100GBP', desc: 'UK 100', price: 10428.9, spread: 10, low: 10400.0, high: 10500.0 },
        { name: 'D30EUR', desc: 'Germany 30', price: 23822.2, spread: 10, low: 23000.0, high: 24000.0 },
        { name: 'E50EUR', desc: 'Euro STOXX 50', price: 5773.4, spread: 8, low: 5700.0, high: 5800.0 },
        { name: 'H33HKD', desc: 'Hong Kong 50', price: 25503.1, spread: 569, low: 24774.4, high: 25542.1 }
      ],
      'Commodities': [
        { name: 'UKOUSD', desc: 'UK Brent Oil', price: 87.35, spread: 25, low: 86.00, high: 88.00 },
        { name: 'USOUSD', desc: 'US Crude Oil', price: 84.29, spread: 24, low: 83.00, high: 85.00 }
      ],
      'Crypto': [
        { name: 'BTCUSD', desc: 'Bitcoin', price: 70594, spread: 50, low: 69000, high: 71000 },
        { name: 'BCHUSD', desc: 'Bitcoin Cash', price: 456.94, spread: 30, low: 450, high: 460 },
        { name: 'RPLUSD', desc: 'Ripple', price: 1.4003, spread: 5, low: 1.35, high: 1.45 },
        { name: 'LTCUSD', desc: 'Litecoin', price: 55.28, spread: 10, low: 50, high: 60 },
        { name: 'ETHUSD', desc: 'Ethereum', price: 2061.8, spread: 15, low: 2000, high: 2100 },
        { name: 'XMRUSD', desc: 'Monero', price: 357.28, spread: 20, low: 350, high: 360 },
        { name: 'BNBUSD', desc: 'Binance Coin', price: 641.10, spread: 15, low: 630, high: 650 },
        { name: 'SOLUSD', desc: 'Solana', price: 87.5, spread: 10, low: 85, high: 90 },
        { name: 'LNKUSD', desc: 'Chainlink', price: 9.123, spread: 5, low: 9.00, high: 9.20 },
        { name: 'XSIUSD', desc: 'Shiba Inu', price: 0.201, spread: 2, low: 0.19, high: 0.21 },
        { name: 'DOGUSD', desc: 'Dogecoin', price: 0.0933, spread: 2, low: 0.09, high: 0.10 },
        { name: 'ADAUSD', desc: 'Cardano', price: 0.2673, spread: 2, low: 0.25, high: 0.28 },
        { name: 'AVEUSD', desc: 'Aave', price: 116.35, spread: 10, low: 110, high: 120 },
        { name: 'DSHUSD', desc: 'Dash', price: 34.063, spread: 10, low: 30, high: 40 }
      ],
      'Stocks': [
        { name: 'AAPL', desc: 'Apple Inc', price: 260.39, spread: 10, low: 255, high: 265 },
        { name: 'AMZN', desc: 'Amazon.com', price: 218.76, spread: 10, low: 215, high: 220 },
        { name: 'BABA', desc: 'Alibaba Group', price: 130.22, spread: 15, low: 125, high: 135 },
        { name: 'GOOGL', desc: 'Alphabet Inc', price: 301.18, spread: 12, low: 295, high: 305 },
        { name: 'META', desc: 'Meta Platforms', price: 660.93, spread: 15, low: 650, high: 670 },
        { name: 'MSFT', desc: 'Microsoft Corp', price: 410.64, spread: 15, low: 400, high: 415 },
        { name: 'NFLX', desc: 'Netflix Inc', price: 99.14, spread: 8, low: 95, high: 105 },
        { name: 'NVDA', desc: 'NVIDIA Corp', price: 178.51, spread: 12, low: 170, high: 185 },
        { name: 'TSLA', desc: 'Tesla Inc', price: 405.46, spread: 15, low: 400, high: 410 },
        { name: 'ABBV', desc: 'AbbVie Inc', price: 232.09, spread: 10, low: 230, high: 235 },
        { name: 'ABNB', desc: 'Airbnb Inc', price: 135.71, spread: 10, low: 130, high: 140 },
        { name: 'ABT', desc: 'Abbott Laboratories', price: 110.86, spread: 8, low: 108, high: 112 },
        { name: 'ADBE', desc: 'Adobe Inc', price: 281.64, spread: 12, low: 280, high: 285 },
        { name: 'AMD', desc: 'Advanced Micro Devices', price: 198.97, spread: 10, low: 195, high: 200 },
        { name: 'AVGO', desc: 'Broadcom Inc', price: 332.70, spread: 15, low: 330, high: 335 },
        { name: 'C', desc: 'Citigroup Inc', price: 108.86, spread: 8, low: 105, high: 110 },
        { name: 'CRM', desc: 'Salesforce Inc', price: 201.29, spread: 10, low: 200, high: 205 },
        { name: 'DIS', desc: 'Walt Disney Co', price: 102.35, spread: 8, low: 100, high: 105 },
        { name: 'GS', desc: 'Goldman Sachs', price: 835.15, spread: 20, low: 830, high: 840 },
        { name: 'INTC', desc: 'Intel Corp', price: 45.80, spread: 5, low: 45, high: 47 },
        { name: 'JNJ', desc: 'Johnson & Johnson', price: 239.52, spread: 10, low: 235, high: 240 },
        { name: 'MA', desc: 'Mastercard Inc', price: 524.28, spread: 15, low: 520, high: 530 },
        { name: 'MCD', desc: 'McDonalds Corp', price: 327.32, spread: 12, low: 325, high: 330 },
        { name: 'KO', desc: 'Coca-Cola Co', price: 76.91, spread: 5, low: 75, high: 78 },
        { name: 'MMM', desc: '3M Co', price: 156.12, spread: 8, low: 155, high: 158 },
        { name: 'NIO', desc: 'NIO Inc', price: 4.56, spread: 2, low: 4.50, high: 4.60 },
        { name: 'PLTR', desc: 'Palantir Tech', price: 152.50, spread: 10, low: 150, high: 155 },
        { name: 'SHOP', desc: 'Shopify Inc', price: 134.68, spread: 10, low: 130, high: 140 },
        { name: 'TSM', desc: 'Taiwan Semiconductor', price: 344.40, spread: 15, low: 340, high: 350 },
        { name: 'V', desc: 'Visa Inc', price: 319.47, spread: 12, low: 315, high: 325 }
      ]
    };
    
    // 扁平化所有可用的大类列表，供内部查询
    const allAvailablePairs = Object.values(categoryPairs).flat();

    // 初始化收藏列表 (从 localStorage 读取)
    let savedQuotes = [];
    try {
        const stored = localStorage.getItem('mt4_saved_quotes');
        if (stored) {
            savedQuotes = JSON.parse(stored);
        } else {
            // 默认收藏
            savedQuotes = ['D30EUR', 'NASUSD', 'U30USD', 'XAUUSD', 'EURUSD', 'USOUSD', 'H33HKD'];
            localStorage.setItem('mt4_saved_quotes', JSON.stringify(savedQuotes));
        }
    } catch(e) {
        savedQuotes = ['XAUUSD', 'EURUSD'];
    }
    
    let isEditMode = false;

    window.onload = () => {
      renderQuotes();
      selectSymbol('XAUUSD', 5110.43, 'Spot Gold');
      setInterval(refreshData, 3000);
    };

    function renderQuotes() {
      let html = '';
      const displayPairs = allAvailablePairs.filter(p => savedQuotes.includes(p.name));
      
      displayPairs.forEach(p => {
        const time = new Date().toLocaleTimeString('en-US', {hour12:false});
        // 从后端获取的价格如果存在，可以替换这里的静态价格
        // 这里只是初始渲染，实际价格会通过 refreshData 更新
        const bid = p.price;
        const ask = p.price + (p.spread * 0.01);
        const digits = p.name === 'EURUSD' ? 4 : 2;
        html += `
        <div class="q-row ${isEditMode ? 'edit-mode' : ''}" onclick="${isEditMode ? '' : `selectSymbol('${p.name}', ${p.price}, '${p.desc}')`}">
          <div class="q-delete-btn" onclick="removeQuote('${p.name}', event)">⊖</div>
          <div class="q-left">
            <div class="q-sym">${p.name}</div>
            <div class="q-time">${time}</div>
            <div class="q-spread">点差: ${p.spread}</div>
          </div>
          <div class="q-right">
            <div class="q-prices">
              <div class="blue" id="q_bid_${p.name}">${bid.toFixed(digits)}</div>
              <div class="red" id="q_ask_${p.name}">${ask.toFixed(digits)}</div>
            </div>
            <div class="q-hl">
              <div>最低: ${p.low.toFixed(digits)}</div>
              <div>最高: ${p.high.toFixed(digits)}</div>
            </div>
          </div>
        </div>`;
      });
      $('quotesList').innerHTML = html || '<div style="padding:20px;text-align:center;color:#888;">暂无收藏品种，请点击右上角添加</div>';
    }

    window.toggleEditQuotes = function() {
        isEditMode = !isEditMode;
        $('btnEditQuotes').style.color = isEditMode ? 'var(--red)' : 'var(--blue)';
        renderQuotes();
    };

    window.removeQuote = function(name, event) {
        event.stopPropagation();
        savedQuotes = savedQuotes.filter(q => q !== name);
        localStorage.setItem('mt4_saved_quotes', JSON.stringify(savedQuotes));
        renderQuotes();
    };

    window.openAddSymbol = function() {
        renderAddSymbolCats();
        // 默认选中第一个分类
        renderAddSymbolList(Object.keys(categoryPairs)[0]);
        $('addSymbolMask').style.display = 'flex';
    };

    function renderAddSymbolCats() {
        let html = '';
        Object.keys(categoryPairs).forEach((cat, idx) => {
            html += `<div class="cat-tab ${idx === 0 ? 'active' : ''}" onclick="switchAddCat(this, '${cat}')">${cat}</div>`;
        });
        $('addSymbolCats').innerHTML = html;
    }

    window.switchAddCat = function(el, cat) {
        document.querySelectorAll('.cat-tab').forEach(t => t.classList.remove('active'));
        el.classList.add('active');
        renderAddSymbolList(cat);
    };

    function renderAddSymbolList(cat) {
        let html = '';
        const pairs = categoryPairs[cat] || [];
        pairs.forEach(p => {
            const isAdded = savedQuotes.includes(p.name);
            html += `
            <div class="sym-add-item">
                <div class="sym-add-info">
                    <div class="sym-add-name">${p.name}</div>
                    <div class="sym-add-desc">${p.desc}</div>
                </div>
                <div class="sym-add-btn ${isAdded ? 'added' : ''}" onclick="${isAdded ? '' : `addQuote('${p.name}', this)`}">
                    ${isAdded ? '✓' : '⊕'}
                </div>
            </div>`;
        });
        $('addSymbolList').innerHTML = html;
    }

    window.addQuote = function(name, btnEl) {
        if (!savedQuotes.includes(name)) {
            savedQuotes.push(name);
            localStorage.setItem('mt4_saved_quotes', JSON.stringify(savedQuotes));
            btnEl.innerHTML = '✓';
            btnEl.classList.add('added');
            btnEl.onclick = null; // 移除点击事件
            renderQuotes(); // 更新主页面列表
        }
    };

    window.switchTab = function(tabId, title, el) {
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
      $('tab-' + tabId).classList.add('active');
      el.classList.add('active');
      $('topBar').innerText = title;
      
      if(tabId === 'history') fetchHistoryTrades();
    };

    window.selectSymbol = function(name, price, desc) {
      $('tradeSym').innerText = name;
      if (desc) $('tradeSymDesc').innerText = desc;
      window.quantState.price = price;
      
      if(quoteInterval) clearInterval(quoteInterval);
      window.API.submitOrder(name, 'QUOTE', 'quote', 0, 0, 0, {}); 
      quoteInterval = setInterval(() => {
          window.API.submitOrder(name, 'QUOTE', 'quote', 0, 0, 0, {});
      }, 1000);
      
      setTimeout(refreshData, 500);
      switchTab('trade', '交易', document.querySelectorAll('.nav-item')[1]);
    };

    window.adjustLots = function(delta) {
        let val = parseFloat($('inpLots').value) || 0;
        val += delta;
        if(val < 0.01) val = 0.01;
        $('inpLots').value = val.toFixed(2);
        window.quantState.lots = val;
    };
    window.onLotsChange = function(el) {
        let val = parseFloat(el.value);
        if(isNaN(val) || val < 0) val = 0.01;
        window.quantState.lots = val;
    };

    window.adjustInput = function(id, delta) {
        let el = $(id);
        let val = parseFloat(el.value) || 0;
        let step = val < 10 ? 0.0001 : 0.01;
        val += delta * step;
        if(val < 0) val = 0;
        el.value = val.toFixed(step < 0.01 ? 4 : 2);
    };

    window.setOrderType = function(typeCode, typeName, pendingSide) {
      window.quantState.orderType = typeCode;
      window.quantState.pendingSide = pendingSide;
      $('orderTypeText').innerText = typeName;
      $('orderTypeMask').style.display = 'none';
      
      if (typeCode === 'market') {
        $('rowPrice').style.display = 'none';
        $('actionMarket').style.display = 'flex';
        $('actionPending').style.display = 'none';
      } else {
        $('rowPrice').style.display = 'flex';
        $('actionMarket').style.display = 'none';
        $('actionPending').style.display = 'flex';
        if(!$('inpPrice').value || $('inpPrice').value == 0) $('inpPrice').value = window.quantState.price;
      }
    };

    window.setTTL = function(val, text) {
      $('inpTTL').value = val;
      $('ttlDisplay').innerText = text;
      $('ttlMask').style.display = 'none';
    };

    window.closeModal = function(e, id) { if(e.target.id === id) $(id).style.display = 'none'; };

    window.initiateOrder = function(side) {
      if(side !== 'PENDING') window.quantState.pendingSide = side;
      quizAnswers = {};
      document.querySelectorAll('.quiz-opt').forEach(el => el.classList.remove('selected'));
      $('quizMask').style.display = 'flex';
    };

    window.selectQuiz = function(el, qIndex, answer) {
      el.parentElement.querySelectorAll('.quiz-opt').forEach(opt => opt.classList.remove('selected'));
      el.classList.add('selected');
      quizAnswers[qIndex] = answer;
    };

    window.confirmOrderAfterQuiz = function() {
      if(Object.keys(quizAnswers).length < 2) return alert('请先完成风控检查！');
      $('quizMask').style.display = 'none';
      
      const lotsToSend = parseFloat($('inpLots').value) || 0.01;
      const params = {
          inpPrice: parseFloat($('inpPrice').value) || 0,
          inpTp: parseFloat($('inpTp').value) || 0,
          inpSl: parseFloat($('inpSl').value) || 0,
          inpTTL: parseFloat($('inpTTL').value) || 0
      };
      
      window.API.submitOrder(
        $('tradeSym').innerText, 
        window.quantState.pendingSide, 
        window.quantState.orderType, 
        0, 20, lotsToSend, params
      ).then(res => {
        if(res.success) { alert('指令发送成功'); refreshData(); }
        else alert('发送失败: ' + res.message);
      });
    };

    // --- Backend API Wrappers ---
    window.API = {
      submitOrder: async function(symbol, side, type, marginPct, leverage, lots, params) {
        try {
          const res = await fetch('/api/v1/order', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol, side, type, marginPct, leverage, lots, ...params })
          });
          return await res.json();
        } catch (e) { return { success: false, message: 'Network error' }; }
      },
      modifyPosition: async function(positionId, tpPrice, slPrice) {
        try {
          const res = await fetch('/api/v1/position/modify', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ positionId, tpPrice, slPrice })
          });
          return await res.json();
        } catch (e) { return { success: false }; }
      },
      cancelCommand: async function(id) { alert("暂不支持前端撤单"); }
    };

    async function refreshData() {
        try {
            const sym = $('tradeSym').innerText;
            const res = await fetch(`/api/latest_status?symbol=${sym}`);
            if(!res.ok) return;
            const data = await res.json();
            
            if(data) {
                $('valBalance').innerText = fmtNum(data.balance, 2);
                $('valEquity').innerText = fmtNum(data.equity, 2);
                $('valFreeMargin').innerText = fmtNum(data.free_margin, 2);
                $('valHistBalance').innerText = fmtNum(data.balance, 2);
                $('valProfit').innerText = fmtNum(data.daily_pnl, 2);
                
                if(data.latest_quote) {
                    window.quantState.price = data.latest_quote.bid;
                    $('tBid').innerText = fmtNum(data.latest_quote.bid, sym==='XAUUSD'?2:4);
                    $('tAsk').innerText = fmtNum(data.latest_quote.ask, sym==='XAUUSD'?2:4);
                    
                    // 同步更新行情列表中的价格
                    if($('q_bid_'+sym)) $('q_bid_'+sym).innerText = fmtNum(data.latest_quote.bid, sym==='XAUUSD'?2:4);
                    if($('q_ask_'+sym)) $('q_ask_'+sym).innerText = fmtNum(data.latest_quote.ask, sym==='XAUUSD'?2:4);
                }
                updatePositionsList(data.positions || []);
                
                const pendingRes = await fetch('/api/pending_commands');
                if(pendingRes.ok) {
                    const pendingData = await pendingRes.json();
                    updatePendingOrdersList(pendingData.commands || []);
                }
            }
        } catch(e) { console.error("Refresh Error:", e); }
    }

    function updatePositionsList(positions) {
        let html = '';
        positions.forEach(pos => {
            const isBuy = pos.side.toLowerCase() === 'buy';
            const symDigits = pos.symbol === 'XAUUSD' || pos.symbol.includes('JPY') ? 2 : 4;
            const openPrice = parseFloat(pos.open_price).toFixed(symDigits);
            const currentPrice = parseFloat(pos.current_price).toFixed(symDigits);
            
            html += `
            <div class="pos-item" onclick="openModifyOrder('${pos.ticket}', '${pos.tp}', '${pos.sl}', '${pos.symbol}', '${pos.lots}')">
              <div class="p-row1">
                <div><span class="p-sym">${pos.symbol}</span>, <span class="p-type ${isBuy?'buy':'sell'}">${pos.side}</span> <span class="p-lots">${pos.lots}</span></div>
                <div class="p-profit ${pos.profit>=0?'blue':'red'}">${fmtNum(pos.profit, 2)}</div>
              </div>
              <div class="p-row2">
                <span>${openPrice} → ${currentPrice}</span>
                <span>#${pos.ticket}</span>
              </div>
            </div>`;
        });
        $('list-positions').innerHTML = html || '<div style="padding:15px;text-align:center;color:#888;">暂无持仓</div>';
    }

    function updatePendingOrdersList(commands) {
        const visible = (commands || []).filter(c => c.action !== 'quote');
        let html = '';
        visible.forEach(cmd => {
            const isBuy = (cmd.side||'').toLowerCase() === 'buy';
            html += `
            <div class="pos-item">
              <div class="p-row1">
                <div><span class="p-sym">${cmd.symbol}</span>, <span class="p-type ${isBuy?'buy':'sell'}">${cmd.action} ${cmd.side||''}</span> <span class="p-lots">${cmd.volume}</span></div>
                <div class="p-profit" style="color:var(--red)" onclick="window.API.cancelCommand('${cmd.id}')">撤单</div>
              </div>
              <div class="p-row2">
                <span>${cmd.price || 'Market'}</span>
                <span>${new Date(cmd.created_at*1000).toLocaleTimeString()}</span>
              </div>
            </div>`;
        });
        $('list-orders').innerHTML = html || '<div style="padding:15px;text-align:center;color:#888;">暂无订单</div>';
    }

    async function fetchHistoryTrades() {
        try {
            const res = await fetch('/api/history_trades?limit=50');
            const data = await res.json();
            renderHistoryList(data.trades || []);
        } catch(e) {}
    }

    function renderHistoryList(trades) {
        let html = '';
        trades.forEach(t => {
            if(String(t.cmd_id).startsWith('q_')) return;
            
            let detail = t.message || '';
            try {
                if(detail.startsWith('{')) {
                    const obj = JSON.parse(detail);
                    detail = `[${obj.symbol || ''}] ${obj.action || ''} ${obj.volume || ''}手 <br/> 价格: ${obj.price || obj.open_price || ''} <br/> 利润: ${obj.profit || ''}`;
                }
            } catch(e) {}

            html += `
            <div class="pos-item">
              <div class="p-row1">
                <div><span class="p-sym">#${t.cmd_id}</span> <span class="p-type ${t.ok?'buy':'sell'}">${t.ok?'成功':'失败'}</span></div>
                <div class="p-profit" style="color:var(--muted);font-size:12px">${t.received_at}</div>
              </div>
              <div class="p-row2">
                <span style="flex:1;word-break:break-all;">${detail}</span>
              </div>
            </div>`;
        });
        $('list-history').innerHTML = html || '<div style="padding:15px;text-align:center;color:#888;">暂无记录</div>';
    }

    window.openModifyOrder = function(ticket, tp, sl, symbol, lots) {
        window.quantState.currentModifyTicket = ticket;
        window.quantState.currentModifySymbol = symbol;
        window.quantState.currentModifyLots = parseFloat(lots) || 0;
        $('modTpPrice').value = tp && tp !== 'undefined' && tp !== '0' ? tp : '';
        $('modSlPrice').value = sl && sl !== 'undefined' && sl !== '0' ? sl : '';
        $('modifyMask').style.display = 'flex';
    };

    window.submitModifyOrder = function() {
        if(!window.quantState.currentModifyTicket) return;
        const tp = parseFloat($('modTpPrice').value) || 0;
        const sl = parseFloat($('modSlPrice').value) || 0;
        window.API.modifyPosition(window.quantState.currentModifyTicket, tp, sl)
        .then(res => {
            if(res.success) { alert('已发送修改'); $('modifyMask').style.display='none'; refreshData(); }
            else alert(res.message);
        });
    };
    
    window.closePosition = function() {
        if(!window.quantState.currentModifyTicket) return;
        const sym = window.quantState.currentModifySymbol || $('tradeSym').innerText;
        const lots = window.quantState.currentModifyLots || 0;
        window.API.submitOrder(sym, 'CLOSE', 'market', 0, 20, lots, {ticket: window.quantState.currentModifyTicket})
        .then(res => {
            if(res.success) { alert('已发送平仓指令'); $('modifyMask').style.display='none'; refreshData(); }
            else alert(res.message);
        });
    }
  </script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

# ==================== API 接口实现 (v1) ====================

@app.route('/api/v1/order', methods=['POST'])
def submit_order_v1():
    global cmd_counter
    
    # 1. 基础时间限制 (0:00 - 5:00)
    if is_restricted_time():
        return jsonify({"success": False, "message": "非交易时段 (0:00-5:00)，禁止下单"}), 403

    data = request.json
    print(f"【API】收到下单请求: {data}")
    print(f"[ORDER] Recv: {json.dumps(data)}")
    
    symbol = norm_symbol(data.get('symbol'))
    side_raw = data.get('side', '')
    cmd_type_raw = data.get('type', 'market')
    
    # 2. 风控状态检查 (熔断/冷静期)
    # QUOTE 和 CLOSE (如果不是手动平锁定仓位) 是否受限? 
    # QUOTE 不受限。CLOSE 受限吗? "超过就熔断，当日不再交易" -> 通常指不开新仓
    # 但如果是平仓，应该是允许的，除非是锁定的仓位。
    # 这里我们只限制 开仓 (MARKET/LIMIT)
    
    if side_raw != 'QUOTE' and side_raw != 'CLOSE' and cmd_type_raw != 'quote':
        # 获取账户信息用于风控检查
        account = None
        current_equity = 0
        day_start_equity = 0
        with history_lock:
            if history_status and isinstance(history_status[0].get("parsed"), dict):
                parsed = history_status[0]["parsed"]
                account = norm_str(parsed.get("account"))
                current_equity = parsed.get("equity", 0)
                day_start_equity = parsed.get("day_start_equity", 0)
        
        if account:
            allowed, msg, status_type = check_risk_status(account, current_equity, day_start_equity)
            if not allowed:
                print(f"[RISK] Order rejected: {msg}")
                return jsonify({"success": False, "message": msg, "risk_status": status_type}), 403

    # 3. 锁定仓位检查 (针对 CLOSE)
    if side_raw == 'CLOSE':
        ticket = str(data.get("ticket"))
        with risk_lock:
            if ticket in risk_state["locked_tickets"]:
                return jsonify({"success": False, "message": "该仓位已锁定，禁止手动平仓"}), 403

    # 优先放行 QUOTE 命令，无需计算 lots
    if side_raw == 'QUOTE' or cmd_type_raw == 'quote':
        print(f"[ORDER][QUOTE] Processing for {symbol}")
        # 构造简单命令对象
        now = int(time.time())
        cmd = {
            "id": "q_" + generate_unique_cmd_id(), # 添加前缀以便过滤
            "nonce": generate_nonce(),
            "created_at": now,
            "ttl_sec": 10, # quote 有效期短
            "symbol": symbol,
            "action": "quote",
            "account": "" # 尝试填充
        }
        
        # 尝试填充 account
        with history_lock:
            if history_status and isinstance(history_status[0].get("parsed"), dict):
                cmd["account"] = norm_str(history_status[0]["parsed"].get("account"))
        
        with commands_lock:
            commands.append(cmd)
            # cmd_counter 已经在 generate_unique_cmd_id 中增加
            
        print(f"[ORDER][QUOTE] queued id={cmd['id']}")
        return jsonify({"success": True, "message": "报价请求已发送", "order": cmd})

    # 优先从 inpMarginUSD (滑块值) 计算 lots
    inp_margin_usd = float(data.get('marginPct', 0) or 0) # 兼容前端字段 marginPct
    
    # 获取当前价格用于计算
    current_price = get_latest_price(symbol) or float(data.get('price', 0) or 0)
    
    lots = 0.0
    if inp_margin_usd > 0 and current_price > 0:
        # 使用 calc_lots_from_margin_usd 统一计算
        # 注意：这里构造一个伪造的 data 对象传给计算函数，因为它只需要 equity (可选) 和查价格
        # 实际上 calc_lots_from_margin_usd 内部会再次查价格
        lots = calc_lots_from_margin_usd(symbol, inp_margin_usd, {"equity": 0})
        
        # [修复]：如果算出来太小，尝试给个最小量（配合前端）
        # 或者至少不要 round 成 0.0
        if 0 < lots < 0.01:
            lots = 0.01
        else:
            lots = round(lots, 2)
    
    # 如果计算失败或未提供 margin，尝试使用前端传的 lots (兼容旧逻辑)
    if lots <= 0:
        print(f"[ORDER] Calc lots failed or 0, fallback to frontend lots")
        lots = float(data.get('lots', 0))
    
    if lots <= 0 and side_raw != 'CLOSE':
        print(f"[ORDER][REJECT] Invalid lots: {lots}")
        return jsonify({"success": False, "message": "计算手数无效，请检查投入金额或价格"}), 400

    # 构造命令对象
    now = int(time.time())
    
    # 获取有效期 (分钟)，默认 10 分钟
    # 修复：从 data 中获取 inpTTL
    ttl_mins = float(data.get('inpTTL', 0) or 0)
    if ttl_mins <= 0:
        ttl_mins = 10
    ttl_sec = int(ttl_mins * 60)
    
    cmd = {
        "id": generate_unique_cmd_id(),
        "nonce": generate_nonce(),
        "created_at": now,
        "ttl_sec": ttl_sec,
        "symbol": symbol,
        "volume": lots,
        "lots": lots
    }
    
    # 填充 account
    account = None
    with history_lock:
        # 优先从 history_status 获取 (最近一次心跳)
        if history_status and isinstance(history_status[0].get("parsed"), dict):
            account = norm_str(history_status[0]["parsed"].get("account"))
        
        # 如果 history_status 为空，尝试从 history_report 获取 (可能是刚启动)
        if not account and history_report:
            for rep in history_report:
                if rep.get("parsed") and rep["parsed"].get("account"):
                    account = norm_str(rep["parsed"].get("account"))
                    break
    
    # 强制校验：如果依然无法获取 account，则禁止下单，防止生成无效命令
    # 注意：某些情况下 history_status 可能为空（如 EA 尚未连接），此时允许 account 为空
    # 但为了避免指令无法被 EA 认领，最好还是要求 account
    if not account:
        print("[WARN] 无法获取当前账户信息 (account unknown)，尝试允许空账户下单")
        # return jsonify({"success": False, "message": "无法获取当前账户信息，请等待 EA 连接"}), 400
        account = "" # 允许为空
    
    cmd["account"] = account

    # 判断类型
    # type: "market", "limit", "market_tpsl", "limit_tpsl", "quote"
    # 平仓逻辑特殊处理
    if side_raw == 'CLOSE':
        cmd["action"] = "close"
        cmd["ticket"] = int(data.get("ticket"))
    # QUOTE 已提前处理
    elif "limit" in cmd_type_raw:
        cmd["action"] = "limit"
        cmd["side"] = "buy" if side_raw.upper() == "BUY" else "sell"
        # 获取价格
        price = float(data.get('inpPrice', 0) or 0)
        if price <= 0:
             return jsonify({"success": False, "message": "限价单必须输入触发价格"}), 400
        cmd["price"] = price
    else:
        cmd["action"] = "market"
        cmd["side"] = "buy" if side_raw.upper() == "BUY" else "sell"
    
    # TP/SL
    tp = float(data.get('inpTp', 0) or 0)
    sl = float(data.get('inpSl', 0) or 0)
    if tp > 0: cmd["tp"] = tp
    if sl > 0: cmd["sl"] = sl
    
    with commands_lock:
        commands.append(cmd)
        # cmd_counter 已在 generate_unique_cmd_id 中增加
    
    print(f"[ORDER][QUEUE] action={cmd.get('action')} symbol={symbol} lots={lots} account={account} id={cmd['id']}")
    return jsonify({
        "success": True, 
        "message": "指令已发送到队列",
        "order": cmd
    })

@app.route('/api/v1/position/modify', methods=['POST'])
def modify_position_v1():
    data = request.json
    position_id = data.get('positionId') # Ticket
    
    # 锁定检查
    if position_id:
        with risk_lock:
            if str(position_id) in risk_state["locked_tickets"]:
                return jsonify({"success": False, "message": "该仓位已锁定，禁止修改"}), 403

    tp = float(data.get('tpPrice', 0) or 0)
    sl = float(data.get('slPrice', 0) or 0)
    
    # 构造 modify 命令
    now = int(time.time())
    
    cmd = {
        "id": generate_unique_cmd_id(),
        "nonce": generate_nonce(),
        "created_at": now,
        "ttl_sec": 60,
        "action": "modify",
        "ticket": int(position_id),
        "tp": tp,
        "sl": sl
    }
    
    # 填充 account
    with history_lock:
        if history_status and isinstance(history_status[0].get("parsed"), dict):
            account = norm_str(history_status[0]["parsed"].get("account"))
            if account:
                cmd["account"] = account

    with commands_lock:
        commands.append(cmd)
        # cmd_counter 已在 generate_unique_cmd_id 中增加
        
    return jsonify({"success": True, "message": "修改指令已发送"})

@app.route('/api/v1/position/lock', methods=['POST'])
def lock_position_v1():
    data = request.json
    position_id = data.get('positionId')
    print(f"【API】执行锁仓(标记锁定): {position_id}")
    
    # 锁仓逻辑更新：不再反向开仓，而是标记为"Locked"，禁止手动操作
    # 只有达到 TP/SL 才能结算
    
    # 1. 验证持仓是否存在
    target_pos = None
    with history_lock:
        if history_positions and history_positions[0].get("parsed"):
            positions = history_positions[0]["parsed"].get("positions", [])
            for p in positions:
                if str(p.get("ticket")) == str(position_id):
                    target_pos = p
                    break
    
    if not target_pos:
        return jsonify({"success": False, "message": "未找到持仓，无法锁仓"}), 404
        
    # 2. 标记锁定
    global risk_state
    with risk_lock:
        risk_state["locked_tickets"].add(str(position_id))
        
    return jsonify({"success": True, "message": "仓位已锁定 (禁止手动平仓/修改，等待止盈止损结算)"})

@app.route('/api/v1/calendar', methods=['GET'])
def get_calendar_pnl_v1():
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    
    if not year or not month:
        now = datetime.now()
        year = now.year
        month = now.month

    with daily_stats_lock:
        stats = load_daily_stats()
        
    data = {}
    # 筛选指定年月的记录
    prefix = f"{year}-{month:02d}"
    
    for date_str, pnl in stats.items():
        if date_str.startswith(prefix):
            try:
                # 提取日期 (day)
                day = int(date_str.split("-")[2])
                data[day] = pnl
            except:
                continue
                
    return jsonify(data)

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

# 获取当前待发送的指令队列 (供前端展示 "当前委托" 中的排队指令)
@app.route("/api/pending_commands", methods=["GET"])
def api_pending_commands():
    with commands_lock:
        # 返回副本，避免并发问题，并过滤掉 quote 指令
        visible_commands = [c for c in commands if c.get("action") != "quote"]
        return jsonify({"commands": visible_commands})

@app.route("/web/api/mt4/status", methods=["POST"])
def mt4_status():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    _, record = store_mt4_data(raw_body, client_ip, headers_dict)
    
    # 异步或同步更新日历统计
    update_daily_stats_from_record(record)
    
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

# ==================== Tick 推送接口 ====================
@app.route('/api/tick', methods=['POST'])
def receive_tick():
    """
    接收 EA 推送的 Tick 数据 (单向推送)
    """
    try:
        ticks = request.json
        if not isinstance(ticks, list):
            ticks = [ticks]
            
        receive_time = int(time.time() * 1000)
        
        # 这里可以将 Tick 数据存入缓存，供前端轮询
        # 简单实现：只保存最新的 QUOTE_DATA 到 history_report 队列，模拟成 QUOTE 报告
        # 以便前端通过 /api/latest_status 轮询到
        
        for tick in ticks:
            symbol = tick.get('symbol')
            bid = tick.get('bid')
            ask = tick.get('ask')
            tick_time = tick.get('tick_time')
            
            if not symbol or not bid or not ask: continue
            
            # 构造一个伪造的 QUOTE_DATA 报告存入 history_report
            # 这样前端 refreshData -> /api/latest_status -> latest_quote 逻辑就能复用
            
            quote_msg = json.dumps({
                "bid": bid,
                "ask": ask
            })
            
            record = {
                "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ip": get_client_ip(),
                "method": "POST",
                "path": "/api/tick",
                "category": "report", # 伪装成 report
                "headers": {},
                "body_raw": json.dumps(tick),
                "parsed": {
                    "desc": "QUOTE_DATA",
                    "spread": tick.get('spread', 0), 
                    "ts": tick_time, 
                    "message": quote_msg,
                    "symbol": symbol, # 显式存储 symbol
                    "account": "tick_stream" 
                }
            }
            
            with history_lock:
                history_report.appendleft(record)

        return '', 204 # No Content, 快速响应
        
    except Exception as e:
        print(f"Error processing tick: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== 网页发单 ====================
@app.route("/send_command", methods=["POST"])
def send_command():
    if is_restricted_time():
        return redirect(url_for("index"))
    
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

    # 强校验与风控
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
            
        # 风控：单笔手数限制 (示例: 最大 5 手)
        if volume > 5.0:
            print("[RISK] 单笔手数超过限制 (Max 5.0)")
            return jsonify({"success": False, "message": "单笔手数超过限制 (Max 5.0)"}), 400
            
        # 风控：资金预检 (简单模拟)
        # 假设我们从 latest_status 获取 free_margin
        # 注意：这里可能拿到的是旧数据，严格风控应在 EA 端再次校验
        # 这里仅作前端/网关层面的快速拦截
        with history_lock:
            latest_status = history_status[0] if history_status else None
            if latest_status:
                # 修复：从 parsed 结构中读取 free_margin
                parsed = latest_status.get("parsed", {})
                free_margin = parsed.get("free_margin", 0)
                
                # 粗略估算保证金：1手 ~ 1000 USD (假设杠杆100，合约100000)
                # 实际应根据 symbol 和 leverage 计算，这里仅作演示
                est_margin = volume * 1000 
                
                print(f"[RISK] Pre-Check: FreeMargin={free_margin}, EstReq={est_margin}, Vol={volume}")
                
                if free_margin < est_margin * 1.1: # 保留 10% 缓冲
                    print(f"[RISK] 资金不足预警: Free={free_margin}, EstReq={est_margin}")
                    # return jsonify({"success": False, "message": "资金不足 (预估)"}), 400
                    # 暂时仅打印日志，不硬拦截，以免误杀
    elif cmd_type == "QUOTE":
        # 放行 quote 请求，即使 volume=0
        pass
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
        "id": generate_unique_cmd_id(),
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
        # cmd_counter 已在 generate_unique_cmd_id 中增加

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
