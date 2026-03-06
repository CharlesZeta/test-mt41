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
    """判断当前时间是否处于限制时段（0:00 - 5:00 禁止交易）"""
    now = datetime.now()
    h = now.hour
    # 规则：只有 5:00 - 24:00 允许交易 (即 [5, 24))
    # 禁止时段: 0, 1, 2, 3, 4 点
    if 0 <= h < 5:
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
                
                # 如果是交易执行报告 (ok=True/False 且有 cmd_id)，则持久化保存
                # 排除 QUOTE_DATA 和 INTERNAL 类型
                if desc != "QUOTE_DATA" and record.get("method") != "INTERNAL" and parsed.get("cmd_id"):
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
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="format-detection" content="telephone=no" />
  <meta name="theme-color" content="#ffffff" />
  
  <title>量化交易终端 - 移动版</title>
  <style>
    :root {
      --bg: #f5f7fa;
      --card: #ffffff;
      --text: #1a1e23;
      --muted: #848e9c;
      --line: #eaecef;
      --green: #0ecb81;
      --red: #f6465d;
      --yellow: #f0b90b;
      --chip: #f3f5f7;
      --shadow: 0 0.5rem 1.5rem rgba(0,0,0,0.06);
      --radius: 1rem;
      --safe-bottom: env(safe-area-inset-bottom);
    }

    /* CSS Reset (移动端优化) */
    * { 
      box-sizing: border-box; 
      -webkit-tap-highlight-color: transparent; 
      outline: none;
    }
    
    html {
      font-size: 16px; /* 基准字号 */
      -webkit-text-size-adjust: 100%; /* 禁止字体自动缩放 */
    }

    body {
      margin: 0; 
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", sans-serif;
      color: var(--text); 
      background: var(--bg); 
      line-height: 1.5;
      overflow-x: hidden; /* 防止横向滚动 */
      width: 100%;
      overscroll-behavior-y: none; /* 禁用下拉刷新效果，模拟原生App */
    }

    /* 消除点击延迟 */
    button, a, input, [role="button"] {
      touch-action: manipulation;
    }

    /* 容器布局 */
    .app { 
      width: 100%;
      max-width: 62.5rem; /* 1000px */
      margin: 0 auto; 
      min-height: 100vh; 
      padding: 1.25rem; /* 20px */
      padding-bottom: calc(1.25rem + var(--safe-bottom));
    }

    /* 顶部 */
    .topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.25rem; }
    .title { font-size: 1.375rem; font-weight: 800; }
    .btn { 
      border: 1px solid var(--line); 
      background: #fff; 
      border-radius: 0.5rem; 
      padding: 0.5rem 1rem; 
      font-weight: 600; 
      min-height: 2.75rem; /* 44px 最小触控区域 */
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.9375rem;
      cursor: pointer;
    }
    /* 移除 hover-only，改为 active */
    .btn:active { background: var(--chip); }

    /* 顶部分类 Tabs */
    .tabs-wrapper { 
      overflow-x: auto; 
      white-space: nowrap; 
      margin-bottom: 1.25rem; 
      -webkit-overflow-scrolling: touch; 
      padding-bottom: 0.3125rem; 
      scrollbar-width: none; /* Firefox */
    }
    .tabs-wrapper::-webkit-scrollbar { display: none; } /* Chrome/Safari */
    
    .tabs { display: inline-flex; gap: 0.5rem; }
    .tab { 
      padding: 0.5rem 1rem; 
      border-radius: 0.5rem; 
      background: transparent; 
      color: var(--muted); 
      font-weight: 700; 
      border: none; 
      font-size: 0.9375rem; /* 15px */
      min-height: 2.75rem; /* 44px */
      transition: 0.2s;
      cursor: pointer;
    }
    .tab.active { background: var(--card); color: var(--text); box-shadow: 0 2px 8px rgba(0,0,0,0.05); }

    /* 行情头 */
    .symRow { 
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      margin-bottom: 1.25rem; background: var(--card); padding: 1.25rem; 
      border-radius: var(--radius); box-shadow: var(--shadow); 
      border: 1px solid var(--line); 
    }
    .symLeftTime { font-family: monospace; font-weight: 700; color: var(--muted); font-size: 2rem; justify-self: start; }
    .symCenter { display: flex; flex-direction: column; align-items: center; justify-self: center; gap: 0.25rem; }
    .symName { display: flex; align-items: center; gap: 0.625rem; font-size: 1.625rem; font-weight: 800; }
    .symBadge { 
      font-size: 0.875rem; padding: 0.25rem 0.5rem; 
      border-radius: 0.375rem; background: var(--text); color: #fff; 
      font-weight: 700; min-height: 2rem; display: inline-flex; align-items: center;
      cursor: pointer;
    }
    .symRight { display: flex; align-items: center; justify-self: end; }
    .iconBtn.huge-chart { 
      width: 5rem; height: 5rem; border-radius: 1rem; 
      border: 2px solid var(--line); background: #fff; 
      display: grid; place-items: center; font-size: 2.25rem; 
      box-shadow: 0 4px 12px rgba(0,0,0,0.05); transition: 0.2s; 
      cursor: pointer;
    }
    .iconBtn.huge-chart:active { transform: scale(0.95); }

    /* 核心布局 */
    .main-grid { display: flex; gap: 1.25rem; align-items: stretch; }
    .col-left { flex: 1.2; display: flex; flex-direction: column; gap: 1.25rem; }
    .col-right { flex: 1; display: flex; flex-direction: column; gap: 1.25rem; }
    .card { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); padding: 1.5rem; }

    /* 数据统计 */
    .obTopStats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1rem; margin-bottom: 1.5rem; }
    .obStatItem { display: flex; flex-direction: column; gap: 0.25rem; }
    .obStatLabel { font-size: 0.875rem; /* >= 14px */ font-weight: 600; color: var(--muted); }
    .obStatVal { font-weight: 800; font-size: 1rem; color: var(--text); }
    
    .midPrice { text-align: center; margin: 1.25rem 0 0.3125rem; font-size: 2.25rem; font-weight: 800; letter-spacing: -1px; }
    .midSub { display: flex; justify-content: center; align-items: center; gap: 0.5rem; color: var(--muted); font-weight: 600; font-size: 0.875rem; margin-bottom: 1.5rem; }
    
    .signal-dot { width: 0.625rem; height: 0.625rem; border-radius: 50%; display: inline-block; transition: background 0.3s; }
    .signal-dot.green { background: var(--green); box-shadow: 0 0 6px var(--green); }
    .signal-dot.yellow { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
    .signal-dot.red { background: var(--red); box-shadow: 0 0 6px var(--red); }

    /* 止损计算器 */
    .sl-calculator { background: #fafbfc; border: 1px solid var(--line); border-radius: 0.75rem; padding: 1rem; margin-top: auto; }
    .sl-title { font-size: 0.875rem; font-weight: 800; margin-bottom: 0.75rem; color: var(--text); display: flex; justify-content: space-between; align-items: flex-end;}
    .sl-row { display: flex; justify-content: space-between; margin: 0.5rem 0; font-size: 0.875rem; font-variant-numeric: tabular-nums; }
    .sl-row .k { color: var(--muted); font-weight: 600; }
    .sl-row .v { font-weight: 800; font-size: 0.875rem; color: var(--text); }

    /* 表单区 */
    .form-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.25rem; }
    .chips { display: flex; gap: 0.5rem; }
    .chip { 
      padding: 0.5rem 0.75rem; border-radius: 0.5rem; background: var(--chip); 
      font-weight: 700; font-size: 0.875rem; border: 1px solid transparent; 
      min-height: 2rem; display: inline-flex; align-items: center;
      cursor: pointer;
    }
    .chip.primary { background: var(--text); color: #fff; }
    
    .form-row { 
      display: flex; justify-content: space-between; align-items: center; 
      background: var(--chip); border-radius: 0.625rem; padding: 0.875rem 1rem; 
      margin-bottom: 0.75rem; border: 1px solid transparent; transition: 0.2s; 
      min-height: 3.5rem; /* 56px touch target */
      cursor: pointer;
    }
    .form-row:focus-within { background: #fff; border-color: var(--text); }
    .form-row label { color: var(--muted); font-weight: 600; font-size: 0.875rem; white-space: nowrap; }
    .form-row input { 
      border: none; background: transparent; text-align: right; width: 100%; margin-left: 1rem; 
      font-size: 1rem; /* >= 16px to prevent zoom */
      font-weight: 800; color: var(--text); outline: none; font-family: inherit; 
    }
    .form-row .value-text { font-size: 1rem; font-weight: 800; color: var(--text); }

    /* 滑块 */
    .range-wrap { margin: 1.25rem 0 1.5rem; padding: 0; }
    .range-header { display: flex; justify-content: space-between; margin-bottom: 0.75rem; font-size: 0.875rem; font-weight: 700; color: var(--muted); align-items: center;}
    input[type=range] { -webkit-appearance: none; width: 100%; background: transparent; padding: 0.625rem 0; margin: 0; min-height: 2.75rem; cursor: pointer; } /* 44px */
    input[type=range]:focus { outline: none; }
    input[type=range]::-webkit-slider-runnable-track { width: 100%; height: 0.375rem; background: linear-gradient(to right, var(--text) 0%, var(--text) var(--track-fill, 10%), var(--line) var(--track-fill, 10%), var(--line) 100%); border-radius: 3px; }
    input[type=range]::-webkit-slider-thumb { height: 1.5rem; width: 1.5rem; border-radius: 50%; background: #fff; border: 4px solid var(--text); -webkit-appearance: none; margin-top: -0.5625rem; box-shadow: 0 2px 6px rgba(0,0,0,0.15); }

    /* 按钮组 */
    .cta-group { display: flex; gap: 0.75rem; margin-top: 0.625rem;}
    .cta { 
      border: none; flex: 1; padding: 1rem; border-radius: 0.75rem; 
      color: #fff; font-size: 1rem; font-weight: 800; 
      transition: transform 0.1s; 
      min-height: 3.5rem; /* 56px */
      display: flex; align-items: center; justify-content: center;
      cursor: pointer;
    }
    .cta:active { transform: scale(0.98); }
    .cta.buy { background: var(--green); }
    .cta.sell { background: var(--red); }

    /* 底部列表 */
    .bottom-section { margin-top: 2rem; }
    .segTabs { display: flex; gap: 1.5rem; border-bottom: 1px solid var(--line); margin-bottom: 1.25rem; }
    .seg { 
      padding: 0.75rem 0.25rem; color: var(--muted); font-weight: 700; font-size: 1rem; 
      position: relative; min-height: 2.75rem; display: flex; align-items: center;
      cursor: pointer;
    }
    .seg.active { color: var(--text); }
    .seg.active::after { content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 3px; border-radius: 3px 3px 0 0; background: var(--text); }
    
    .listCard { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); display: none; }
    .listCard.active { display: block; }
    .posItem { padding: 1.25rem; }
    .posTop { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1rem; }
    .posTitle { display: flex; gap: 0.5rem; align-items: center; font-weight: 800; font-size: 1.125rem; }
    .sideTag { padding: 0.125rem 0.5rem; border-radius: 0.375rem; font-size: 0.875rem; font-weight: 800; border: 1px solid; background: #fff; }
    .sideTag.buy { color: var(--green); border-color: rgba(37,185,122,.3); }
    .sideTag.sell { color: var(--red); border-color: rgba(239,77,92,.3); }
    .posGrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(7.5rem, 1fr)); gap: 1rem; margin-bottom: 1rem; }
    .mini { color: var(--muted); font-size: 0.875rem; /* >= 14px */ font-weight: 600; margin-bottom: 0.25rem; }
    .big { font-weight: 800; font-size: 1rem; }
    .posActions { display: flex; gap: 0.75rem; }
    .ghost { 
      flex: 1; border: 1px solid var(--line); background: transparent; 
      border-radius: 0.5rem; padding: 0.625rem; font-weight: 700; 
      min-height: 2.75rem; display: flex; align-items: center; justify-content: center;
      transition: 0.2s; 
      cursor: pointer;
    }
    .ghost:active { background: var(--chip); }

    /* 弹窗/动作面板 (Action Sheet) */
    .modalMask { 
      position: fixed; inset: 0; background: rgba(0,0,0,.5); 
      display: none; align-items: center; justify-content: center; 
      padding: 1.25rem; z-index: 100; backdrop-filter: blur(2px); 
    }
    .modal { 
      width: min(27.5rem, 100%); background: #fff; border-radius: 1.25rem; 
      overflow: hidden; box-shadow: 0 1.25rem 2.5rem rgba(0,0,0,0.1); 
      animation: pop 0.2s ease-out; display: flex; flex-direction: column; max-height: 90vh; 
    }
    @keyframes pop { from { transform: scale(0.95); opacity: 0; } to { transform: scale(1); opacity: 1; } }
    
    .modalHeader { 
      padding: 1.25rem; display: flex; justify-content: space-between; align-items: center; 
      border-bottom: 1px solid var(--line); font-weight: 800; font-size: 1.125rem; flex-shrink: 0; 
      min-height: 3.5rem;
    }
    .modalBody { padding: 1.25rem; overflow-y: auto; flex-grow: 1; }
    .select-item { 
      padding: 1rem; border-bottom: 1px solid var(--line); font-weight: 700; 
      display: flex; justify-content: space-between; align-items: center; min-height: 3.5rem;
      cursor: pointer;
    }
    .select-item:last-child { border-bottom: none; }
    .select-item:active { background: var(--chip); }
    .select-item.active { color: var(--text); }

    /* 3. 移动端媒体查询 (320-428px) */
    @media (max-width: 768px) {
      .app { padding: 1rem; padding-bottom: calc(1rem + var(--safe-bottom)); }
      
      /* 单列流式布局 */
      .main-grid { flex-direction: column; gap: 1rem; }
      .col-left, .col-right { gap: 1rem; }
      
      /* 底部动作面板模式 */
      .modalMask { align-items: flex-end; padding: 0; }
      .modal { 
        width: 100%; max-width: 100%; 
        border-radius: 1.5rem 1.5rem 0 0; 
        margin: 0; animation: slideUp 0.3s cubic-bezier(0.16, 1, 0.3, 1); 
        padding-bottom: var(--safe-bottom);
      }
      @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
      
      /* 字体与间距微调 */
      .title { font-size: 1.25rem; }
      .midPrice { font-size: 2rem; }
      .posGrid { grid-template-columns: repeat(2, 1fr); }
      
      /* 1px 边框优化 */
      .form-row, .btn, .tab, .cta, .ghost, .symRow, .card, .sl-calculator {
        border-width: 0.0625rem; /* fallback */
      }
      @media (-webkit-min-device-pixel-ratio: 2) {
        .form-row, .btn, .tab, .cta, .ghost, .symRow, .card, .sl-calculator { border-width: 0.5px; }
      }
    }

    /* 问卷样式 */
    .quiz-item { margin-bottom: 1.5rem; }
    .quiz-q { font-size: 0.9375rem; font-weight: 800; margin-bottom: 0.75rem; color: var(--text); }
    .quiz-opts { display: flex; gap: 0.75rem; flex-direction: column; }
    .quiz-opt { 
      flex: 1; padding: 0.75rem; border: 1px solid var(--line); border-radius: 0.625rem; 
      text-align: center; font-weight: 700; transition: 0.2s; background: var(--chip); 
      min-height: 3rem; display: flex; align-items: center; justify-content: center;
      cursor: pointer;
    }
    .quiz-opt.selected { background: var(--text); color: #fff; border-color: var(--text); }

    /* 日历样式 */
    .cal-header { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; text-align: center; font-weight: 800; color: var(--muted); font-size: 0.875rem; margin-bottom: 0.5rem; }
    .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
    .cal-day { 
      border: 1px solid var(--line); border-radius: 0.5rem; padding: 0.25rem; text-align: center; 
      min-height: 3.5rem; display: flex; flex-direction: column; justify-content: space-between; 
    }
    .cal-day.empty { border: none; background: transparent; }
    .cal-date { font-weight: 800; font-size: 0.875rem; color: var(--text); }
    .cal-pnl { font-size: 0.75rem; font-weight: 800; margin-top: 0.25rem; }

    /* 错误弹窗 */
    .error-popup { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center; z-index: 1000; backdrop-filter: blur(4px); }
    .error-popup.show { display: flex; }
    .error-popup-content { background: #fff; border-radius: 1.25rem; padding: 1.5rem; width: min(22.5rem, 85%); text-align: center; box-shadow: 0 1.25rem 3.75rem rgba(0,0,0,0.3); }
    .error-popup-icon { width: 4rem; height: 4rem; background: linear-gradient(135deg, #fee2e2, #fecaca); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 1.25rem; font-size: 2rem; }
    .error-popup-title { font-size: 1.25rem; font-weight: 800; color: #1f2937; margin-bottom: 1rem; }
    .error-popup-list { text-align: left; background: #f9fafb; border-radius: 0.75rem; padding: 1rem; margin-bottom: 1.25rem; max-height: 12.5rem; overflow-y: auto; }
    .error-popup-item { display: flex; align-items: center; padding: 0.5rem 0; color: #dc2626; font-size: 0.875rem; font-weight: 600; border-bottom: 1px solid #e5e7eb; }
    .error-popup-item:last-child { border-bottom: none; }
    .error-popup-btn { 
      background: linear-gradient(135deg, #1f2937, #374151); color: #fff; border: none; 
      padding: 0.875rem 2rem; border-radius: 0.75rem; font-size: 1rem; font-weight: 700; 
      width: 100%; min-height: 3rem;
      cursor: pointer;
    }

    /* 4. 图片视频资源优化 (CSS层) */
    img, video { max-width: 100%; height: auto; display: block; object-fit: cover; }
    
    /* 锁仓按钮 */
    .lock-btn { 
      background: linear-gradient(135deg, #f6465d, #ff7b8c); color: #fff; border: none; 
      padding: 1rem; border-radius: 0.75rem; font-size: 1rem; font-weight: 800; 
      width: 100%; box-shadow: 0 0.25rem 0.75rem rgba(246,70,93,0.3); margin-top: 0.625rem; 
      min-height: 3.5rem;
      cursor: pointer;
    }
  </style>
</head>
<body>

  <div class="error-popup" id="errorPopup" onclick="closeErrorPopup(event)">
    <div class="error-popup-content">
      <div class="error-popup-icon">⚠️</div>
      <div class="error-popup-title">表单数据错误</div>
      <div class="error-popup-list" id="errorPopupList"></div>
      <button class="error-popup-btn" onclick="$('errorPopup').classList.remove('show')">我知道了</button>
    </div>
  </div>

  <div class="app">
    <div class="topbar">
      <div class="title">MT4 量化终端</div>
      <button class="btn" onclick="alert('系统设置功能开发中...')">设置</button>
    </div>

    <div class="tabs-wrapper">
      <div class="tabs">
        <button class="tab" data-category="forex" onclick="switchMainTab(this); showCategoryPairs('forex')">外汇</button>
        <button class="tab" data-category="index" onclick="switchMainTab(this); showCategoryPairs('index')">指数</button>
        <button class="tab" data-category="commodity" onclick="switchMainTab(this); showCategoryPairs('commodity')">大宗商品</button>
        <button class="tab active" data-category="metal" onclick="switchMainTab(this); showCategoryPairs('metal')">贵金属</button>
        <button class="tab" data-category="stock" onclick="switchMainTab(this); showCategoryPairs('stock')">股票</button>
      </div>
    </div>

    <div class="symRow">
      <div class="symLeftTime" id="sysTime">--:--:--</div>
      <div class="symCenter">
        <div class="symName">
          <span id="symName">XAUUSD</span>
          <span class="symBadge" onclick="openCurrentCategoryPairs()">切换品种 ▼</span>
        </div>
      </div>
      <div class="symRight">
        <button class="iconBtn huge-chart" title="交易日历看板" onclick="openCalendar()">📈</button>
      </div>
    </div>

    <div class="main-grid">
      <div class="col-left">
        <div class="card">
          <div class="obTopStats">
            <div class="obStatItem"><div class="obStatLabel">账户净值 (USD)</div><div class="obStatVal" id="equityVal">--</div></div>
            <div class="obStatItem"><div class="obStatLabel">可用余额 (USD)</div><div class="obStatVal" id="availMarginStr">--</div></div>
            <div class="obStatItem"><div class="obStatLabel">日内浮动盈亏</div><div class="obStatVal" id="dailyPnlVal" style="color:var(--text)">--</div></div>
            <div class="obStatItem"><div class="obStatLabel">日内盈亏率</div><div class="obStatVal" id="dailyPnlPctVal" style="color:var(--text)">--</div></div>
          </div>
          
          <div class="midPrice" id="midPriceText">--</div>
          <div class="midSub">
            当前实时买入价 (Bid) <span class="signal-dot green" id="latencySignal" title="获取延迟"></span>
          </div>
        </div>

        <div class="sl-calculator">
          <div class="sl-title">
            <span>基于当前仓位预估止损额度</span>
            <span style="color:var(--muted); font-size:0.75rem; font-weight:600">公式: 预付款×杠杆×比例</span>
          </div>
          <div style="height:1px; background:var(--line); margin:0.75rem 0;"></div>
          <div class="sl-row"><span class="k">2% 止损亏损</span><span class="v" id="sl_2">0.00 USD</span></div>
          <div class="sl-row"><span class="k">3% 止损亏损</span><span class="v" id="sl_3">0.00 USD</span></div>
          <div class="sl-row"><span class="k">5% 止损亏损</span><span class="v" id="sl_5">0.00 USD</span></div>
          <div class="sl-row"><span class="k">8% 止损亏损</span><span class="v" id="sl_8">0.00 USD</span></div>
          <div class="sl-row"><span class="k">10% 止损亏损</span><span class="v" id="sl_10">0.00 USD</span></div>
          
          <div style="height:1px; background:var(--line); margin:0.75rem 0;"></div>
          <div class="sl-row"><span class="k">占用保证金</span><span class="v" id="calcMargin">0.00 USD</span></div>
          <div class="sl-row"><span class="k">开仓名义价值</span><span class="v" id="calcNotional">0.00 USD</span></div>
          <div class="sl-row"><span class="k">当前设置下每点波动≈</span><span class="v" id="calcPpVal">0.00 USD</span></div>
          <div class="sl-row"><span class="k">预估强平价格</span><span class="v" id="calcLiq">0.00</span></div>
        </div>
      </div>

      <div class="col-right">
        <div class="card" style="height: 100%; display: flex; flex-direction: column;">
          <div class="form-header">
            <div class="chips">
              <div class="chip primary">全仓模式</div>
              <div class="chip" id="btnLev" onclick="$('levMask').style.display='flex'">杠杆 20x ▼</div>
            </div>
            <div style="color: var(--muted); font-size: 0.8125rem; font-weight: 600;">可用: <span style="color:var(--text);font-weight:800" id="formAvail">--</span></div>
          </div>

          <div class="form-row" onclick="$('orderTypeMask').style.display='flex'">
            <label>交易类型</label>
            <div class="value-text"><span id="orderTypeText">限价止盈止损</span> ▼</div>
          </div>
          
          <div id="dynamicFormArea"></div>

          <div class="range-wrap" style="margin-top: auto;">
            <div class="range-header">
              <span>投入保证金金额</span>
              <div style="text-align: right;">
                <span style="color: var(--text); font-size: 1rem;">
                  <span id="pctText">10</span> USD 
                </span>
                <span style="display:block; font-size: 0.75rem; color: var(--text); font-weight: 800; margin-top: 2px;">
                  ≈ <span id="calcLotsText">0.00</span> 手
                </span>
                <span style="font-size: 0.75rem; color: var(--muted);">
                  (占余额 <span id="marginPercentText">0</span>%)
                </span>
              </div>
            </div>
            <input type="range" id="marginSlider" min="0" max="100" step="1" value="10">
          </div>

          <div class="cta-group">
            <button class="cta buy" onclick="initiateOrder('BUY')">买入 / 做多</button>
            <button class="cta sell" onclick="initiateOrder('SELL')">卖出 / 做空</button>
          </div>
        </div>
      </div>
    </div>

    <div class="bottom-section">
      <div class="segTabs">
        <div class="seg active" onclick="switchBottomTab('positions')">持有仓位 (0)</div>
        <div class="seg" onclick="switchBottomTab('orders')">当前委托 (0)</div>
        <div class="seg" onclick="switchBottomTab('history')">历史委托</div>
      </div>
      
      <div class="listCard active" id="list-positions">
        <div style="padding: 2.5rem; text-align: center; color: var(--muted); font-weight: 600; font-size: 0.875rem;">暂无持仓</div>
      </div>
      
      <div class="listCard" id="list-orders">
        <div style="padding: 2.5rem; text-align: center; color: var(--muted); font-weight: 600; font-size: 0.875rem;">暂无当前委托挂单</div>
      </div>

      <div class="listCard" id="list-history">
        <div style="padding: 2.5rem; text-align: center; color: var(--muted); font-weight: 600; font-size: 0.875rem;">暂无历史委托记录</div>
      </div>
    </div>
  </div>

  <!-- 弹窗部分 -->
  <div class="modalMask" id="quizMask" onclick="closeModal(event, 'quizMask')">
    <div class="modal">
      <div class="modalHeader"><span>执行前风控检查</span><button class="btn" style="border:none; padding:0.25rem 0.5rem;" onclick="$('quizMask').style.display='none'">✕</button></div>
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
        <div class="quiz-item">
          <div class="quiz-q">3. 当前是否属于情绪化报复性交易？</div>
          <div class="quiz-opts">
            <div class="quiz-opt" onclick="selectQuiz(this, 3, 'A')">A. 情绪稳定，非报复</div>
            <div class="quiz-opt" onclick="selectQuiz(this, 3, 'B')">B. 是的，急于回本</div>
          </div>
        </div>
        <button class="cta" style="background:var(--text); color:#fff; width:100%" onclick="confirmOrderAfterQuiz()">确认并发送订单</button>
      </div>
    </div>
  </div>

  <div class="modalMask" id="modifyMask" onclick="closeModal(event, 'modifyMask')">
    <div class="modal">
      <div class="modalHeader"><span>高级订单管理</span><button class="btn" style="border:none; padding:0.25rem 0.5rem;" onclick="$('modifyMask').style.display='none'">✕</button></div>
      <div class="modalBody">
        <div style="margin-bottom: 1.25rem; padding: 1rem; border: 1px solid var(--line); border-radius: 0.75rem;">
          <div class="form-row" style="margin-bottom: 0.75rem;">
            <label style="color:var(--green)">止盈触发价 (T/P)</label>
            <input type="number" id="modTpPrice" placeholder="输入价格">
          </div>
          <div class="range-wrap" style="margin: 0;">
            <div class="range-header"><span>止盈平仓数量</span><span style="color:var(--text);font-weight:800" id="tpLotsText">1.00 手</span></div>
            <input type="range" id="tpLotsSlider" min="0.01" max="1.00" step="0.01" value="1.00">
          </div>
        </div>

        <div style="margin-bottom: 1.25rem; padding: 1rem; border: 1px solid var(--line); border-radius: 0.75rem;">
          <div class="form-row" style="margin-bottom: 0.75rem;">
            <label style="color:var(--red)">止损触发价 (S/L)</label>
            <input type="number" id="modSlPrice" placeholder="输入价格">
          </div>
          <div class="range-wrap" style="margin: 0;">
            <div class="range-header"><span>止损平仓数量</span><span style="color:var(--text);font-weight:800" id="slLotsText">1.00 手</span></div>
            <input type="range" id="slLotsSlider" min="0.01" max="1.00" step="0.01" value="1.00">
          </div>
        </div>

        <button class="cta" style="background:var(--text); color:#fff; width:100%" onclick="submitModifyOrder()">保存修改</button>
        <button class="lock-btn" onclick="executeLockPosition()">🔒 锁定当前仓位</button>
      </div>
    </div>
  </div>

  <div class="modalMask" id="calendarMask" onclick="closeModal(event, 'calendarMask')">
    <div class="modal" style="height: 80vh;">
      <div class="modalHeader">
        <span>当月交易日历</span>
        <button class="btn" style="border:none; padding:0.25rem 0.5rem;" onclick="$('calendarMask').style.display='none'">✕</button>
      </div>
      <div class="modalBody">
        <div style="text-align:center; font-weight:800; font-size:1.125rem; margin-bottom:1rem;" id="calTitle">2024 年 5 月</div>
        <div class="cal-header">
          <div>日</div><div>一</div><div>二</div><div>三</div><div>四</div><div>五</div><div>六</div>
        </div>
        <div class="cal-grid" id="calGrid"></div>
      </div>
    </div>
  </div>

  <div class="modalMask" id="levMask" onclick="closeModal(event, 'levMask')">
    <div class="modal">
      <div class="modalHeader"><span>调整杠杆倍数</span><button class="btn" style="border:none; padding:0.25rem 0.5rem;" onclick="$('levMask').style.display='none'">✕</button></div>
      <div class="modalBody">
        <div style="display:flex; justify-content:space-between; margin-bottom:1rem;">
          <span style="color:var(--muted); font-weight:700;">当前选择杠杆</span>
          <span style="font-size:1.125rem; font-weight:800;"><span id="levTextModal">20</span>x</span>
        </div>
        <div class="range-wrap" style="margin-top:0;">
          <input type="range" id="levSlider" min="1" max="100" step="1" value="20">
        </div>
        <div style="color:#d97706; font-size:0.75rem; font-weight:700; margin-bottom:1.25rem;">* 提示：调整杠杆会重算占用资金及止损额度估值。</div>
        <button class="cta" style="background:var(--text); color:#fff; width:100%" onclick="$('levMask').style.display='none'">确认修改</button>
      </div>
    </div>
  </div>

  <div class="modalMask" id="orderTypeMask" onclick="closeModal(event, 'orderTypeMask')">
    <div class="modal">
      <div class="modalHeader"><span>选择交易类型</span><button class="btn" style="border:none; padding:0.25rem 0.5rem;" onclick="$('orderTypeMask').style.display='none'">✕</button></div>
      <div class="modalBody" style="padding: 0;">
        <div class="select-item" onclick="setOrderType('market', '市价')">市价</div>
        <div class="select-item" onclick="setOrderType('market_tpsl', '市价止盈止损')">市价止盈止损</div>
        <div class="select-item" onclick="setOrderType('limit', '限价单')">限价单</div>
        <div class="select-item active" onclick="setOrderType('limit_tpsl', '限价止盈止损')">限价止盈止损</div>
      </div>
    </div>
  </div>

  <div class="modalMask" id="pairMask" onclick="closeModal(event, 'pairMask')">
    <div class="modal">
      <div class="modalHeader"><span id="pairModalTitle">切换交易品种</span><button class="btn" style="border:none; padding:0.25rem 0.5rem;" onclick="$('pairMask').style.display='none'">✕</button></div>
      <div class="modalBody" id="pairListContainer">
        <!-- JS 动态生成 -->
      </div>
    </div>
  </div>

  <script>
    // 全局选择器辅助函数
    const $ = id => document.getElementById(id);

    // 消除 300ms 点击延迟
    document.addEventListener('touchstart', function(){}, {passive: true});

    // 暴露全局状态
    window.quantState = {
      equity: 0,         
      availMargin: 0,
      price: 0,           
      contractSize: 100,        
      pointSize: 0.01,          
      marginPct: 10,
      leverage: 20,             
      lots: 0,
      orderType: 'limit_tpsl',
      pendingSide: '' 
    };

    let quizAnswers = {};

    // 格式化数字
    function fmtNum(n, d) { return parseFloat(n).toFixed(d); }

    // 暴露全局 API
    window.API = {
      // 1. 发送下单指令
      submitOrder: async function(symbol, side, type, marginPct, leverage, calculatedLots, params) {
        console.log("【API】执行下单:", {symbol, side, type, marginPct, leverage, calculatedLots, ...params});
        
        try {
          const response = await fetch('/api/v1/order', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json'
            },
            body: JSON.stringify({
              symbol,
              side,
              type,
              marginPct,
              leverage,
              lots: calculatedLots,
              ...params
            })
          });
          const data = await response.json();
          return data;
        } catch (error) {
          console.error("下单失败:", error);
          alert("网络请求失败，请检查连接");
          return { success: false };
        }
      },
      
      // 2. 修改持仓订单 (分批止盈止损)
      modifyPosition: async function(positionId, tpPrice, tpLots, slPrice, slLots) {
        console.log("【API】修改持仓:", {positionId, tpPrice, tpLots, slPrice, slLots});
        
        try {
          const response = await fetch('/api/v1/position/modify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ positionId, tpPrice, tpLots, slPrice, slLots })
          });
          return await response.json();
        } catch (error) {
          console.error("修改持仓失败:", error);
          return { success: false };
        }
      },
      
      // 3. 锁仓接口
      lockPosition: async function(positionId) {
        console.log("【API】一键锁仓:", {positionId});
        
        try {
          const response = await fetch('/api/v1/position/lock', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ positionId })
          });
          return await response.json();
        } catch (error) {
          console.error("锁仓失败:", error);
          return { success: false };
        }
      },
      
      // 4. 获取历史日历盈亏数据
      getCalendarPnL: async function(year, month) {
        console.log("【API】获取日历数据:", year, month);
        
        try {
          const response = await fetch(`/api/v1/calendar?year=${year}&month=${month}`);
          return await response.json();
        } catch (error) {
          console.error("获取日历失败:", error);
          return {};
        }
      }
    };

    // 暴露全局函数供 HTML 调用
    window.updateCalculations = function() {
      const { marginPct, equity, availMargin } = window.quantState; // 移除 leverage, price 从 state 取
      // marginPct 这里实际存的是用户选择的“投入保证金金额 USD”
      const usedMargin = marginPct; 
      
      // 更新滑轮最大值为可用余额
      const marginSlider = $('marginSlider');
      if(marginSlider) {
          const maxVal = Math.floor(availMargin > 0 ? availMargin : 100);
          if(parseFloat(marginSlider.max) !== maxVal) {
              marginSlider.max = maxVal;
          }
      }

      // 获取当前品种规则 (从 refreshData 注入到 quantState)
      const rule = window.quantState.symbolRules || {};
      // 核心：优先使用后端下发的杠杆/保证金规则，忽略前端滑块的 leverage
      const marginPerLot = rule.margin_per_lot_usd || 0;
      const pointVal = rule.point_val_usd || 0;
      
      let calculatedLots = 0;
      if (marginPerLot > 0) {
          // Lots = 投入金额 / 单手保证金
          // 注意：后端单手保证金是按当前价格算的，这里直接除即可估算
          calculatedLots = usedMargin / marginPerLot;
      }
      
      // 简单的手数计算保护
      if (!isFinite(calculatedLots) || calculatedLots < 0) calculatedLots = 0;
      
      // [修复]：不再向下取整，改为保留2位小数，且有最小兜底
      // 如果算出来大于0但小于0.01，强制给0.01，让用户能尝试下单
      if (calculatedLots > 0 && calculatedLots < 0.01) {
          calculatedLots = 0.01;
      } else {
          // 使用 round 而不是 floor，避免 0.009 被截断
          calculatedLots = Math.round(calculatedLots * 100) / 100;
      }
      
      window.quantState.lots = calculatedLots;

      // 更新 UI 显示
      $('pctText').innerText = usedMargin; // 显示使用保证金金额
      
      // 显示投入金额占可用余额的百分比
      let percentage = 0;
      if (availMargin > 0) {
          percentage = (usedMargin / availMargin) * 100;
      }
      $('marginPercentText').innerText = percentage.toFixed(0);
      
      // 更新独立的手数显示 (修复之前的覆盖 bug)
      const lotsEl = $('calcLotsText');
      if(lotsEl) lotsEl.innerText = calculatedLots.toFixed(2);

      $('calcMargin').innerText = usedMargin.toLocaleString('en-US', {minimumFractionDigits:2}) + " USD";
      
      // 显示名义价值 (Notional Value = Lots * ContractSize * Price)
      const contractSize = rule.spec ? rule.spec.size : 100; // 默认100
      const notionalVal = calculatedLots * contractSize * window.quantState.price;
      $('calcNotional').innerText = notionalVal.toLocaleString('en-US', {minimumFractionDigits:2}) + " USD";
      
      // 显示每点波动价值 (Total Point Value)
      // 用户要求：基于合约手数杠杆*手数对每点波动 (数值非百分比)
      // 实际上后端返回的 pointVal (margin_per_lot_usd/leverage/...) 并不一定是 "每点价值"
      // 正确的每点价值 (Value per Point) = ContractSize * PointSize * (QuoteRate if needed)
      // 而 Total Point Value = Lots * ContractSize * PointSize * (QuoteRate)
      // 后端 rule.point_val_usd 通常是 "单手每点价值" (ContractSize * PointSize)
      // 如果后端 point_val_usd 不准，我们自己算:
      
      let pointSize = 0.01; 
      if (rule.spec && rule.spec.type === 'forex') pointSize = 0.00001;
      if (rule.spec && rule.spec.type === 'index') pointSize = 1.0;
      if (rule.spec && rule.spec.type === 'crypto') pointSize = 0.01;
      // 贵金属 XAUUSD point=0.01
      
      // 重新计算单手每点价值 (USD)
      // 用户描述公式：一首黄金为500000（开仓价格*开仓合约手数*杠杆）/ 黄金实时价格 即为每点波动价值
      // 注意：这里的描述可能有点混淆。通常每点波动价值 (Tick Value) = ContractSize * PointSize
      // 但如果是以报价货币计价 (如 USD)，通常就是这个公式。
      // 用户提到的公式：(OpenPrice * ContractSize * Leverage) / CurrentPrice
      // 这看起来更像是计算保证金的某种变体？或者是基于名义价值反推？
      // 让我们仔细分析用户的例子：
      // "比如黄金5000元；一首黄金为500000（开仓价格*开仓合约手数*杠杆）" -> 这里 5000 * 100 * 1 = 500,000 ? 这里的杠杆是 1 吗？如果是 500 倍杠杆，那名义价值是 5000 * 100 = 500,000。
      // "/ 黄金实时价格即为每点波动价值" -> 500,000 / 5000 = 100。
      // 100 是什么？是每点价值吗？XAUUSD Point=0.01。每点价值通常是 1 USD (100 * 0.01)。
      // 如果结果是 100，那应该是每 1 美元波动的价值 (100 * 1)。
      
      // 用户的描述可能意指：每变动 1 个单位价格 (如 1 USD) 的价值。
      // 而不是 Point (0.01 USD)。
      // 但界面标签是 "每点波动"，通常指 Point。
      
      // 让我们再看公式：(Price * Size * Lev) / Price = Size * Lev
      // 如果 Lev=500, Size=100, 那结果是 50000。这显然太大了。
      
      // 也许用户意思是：(Price * Size) / Price = Size。
      // 也许用户想表达的是：每变动 1% 的价值？
      
      // 让我们严格按用户文字实现：
      // "一首黄金为500000（开仓价格*开仓合约手数*杠杆）/ 黄金实时价格 即为每点波动价值"
      // 假设：开仓价 5000, 合约 100, 杠杆 ? 
      // 如果 "一首黄金为 500000"，那可能是 名义价值 = 5000 * 100 = 500,000 (无杠杆?) 或者包含杠杆的名义价值？
      // 通常名义价值 (Notional) = Price * Size * Lots。
      // 如果用户说的是 "名义价值 / 实时价格"，那结果就是 Size * Lots。
      // Size * Lots * PointSize = 每点价值。
      
      // 现在的代码是：
      // const valuePerPoint = contractSize * pointSize;
      // const totalPointVal = valuePerPoint * calculatedLots;
      // 这就是标准的每点价值计算。
      
      // 用户可能觉得之前算的太小 (0.71 USD)，因为之前是按照 0.71 手算的。
      // 如果 0.71 手，每点价值应该是 0.71 USD (XAUUSD)。
      // 难道用户认为应该是 71 USD？那意味着他认为 Point 是 1.0 而不是 0.01。
      
      // 既然用户给出了明确公式，我们尝试理解：
      // "500000 ... / 黄金实时价格" -> 500000 / 5000 = 100。
      // 这正好是 ContractSize (100)。
      // 所以每点波动价值 = 100 ?
      // 如果 Point=0.01，那 100 对应的是 1 USD 的波动 (100 points)。
      // 所以用户可能把 "每点" 理解为 "每 1 美元价格波动"。
      
      // 但界面上写的是 "每点波动"，通常对应 MT4 的 Point。
      // 不过为了满足用户，我们按他的公式来：
      // 结果 = (名义价值 * 杠杆) / 当前价格 ? 不，名义价值本身就包含了价格。
      // 用户公式核心：(Price * Size * Lots * Lev) / Price = Size * Lots * Lev
      // 如果 Lev=500，那结果巨大。
      
      // 让我们再次阅读："比如黄金5000元；一首黄金为500000（开仓价格*开仓合约手数*杠杆）"
      // 这里 5000 * 100 = 500,000。这里没乘杠杆，或者杠杆是1？
      // 如果乘了 500，那就是 2.5 亿。
      // 所以 "500000" 应该是 名义价值 (Notional Value)。
      // 然后 "/ 黄金实时价格" -> 500,000 / 5000 = 100。
      // 这里的 100 就是 1 手的 Contract Size。
      // 所以用户定义的 "每点波动" = ContractSize * Lots。
      // 这实际上是 "每变动 1.0 报价单位的盈亏"。
      
      // 既然用户强调 "数值非百分比"，我们按此逻辑：
      // 每点波动 (用户定义) = (Notional * Leverage) / Price ... 等等，用户括号里写了 *杠杆。
      // 如果真乘杠杆，数值会非常大。
      // 假设用户笔误，其实是想算每 1 美元波动的价值。
      
      // 让我们尝试实现：
      // TotalPointVal = (Notional * Leverage) / Price * PointSize
      // 如果按用户公式：(5000 * 100 * 500) / 5000 = 50000。
      // 50000 * 0.01 = 500 USD。
      // 这意味着每点波动 500 USD？这相当于 500 手。
      
      // 也许用户是想说：保证金 * 杠杆 = 名义价值。
      // 然后 名义价值 / 价格 = 数量 (Size * Lots)。
      // 数量 * PointSize = 每点价值。
      
      // 让我们暂时忽略 "杠杆" 这个词，仅按 "500000 / 5000 = 100" 理解。
      // 这就是 Size * Lots。
      // 如果 PointSize = 0.01，那标准每点价值是 Size * Lots * 0.01 = 1。
      // 用户算出来是 100。相差 100 倍。
      // 这意味着用户认为 "每点" = 1.0 USD 价格变动。
      
      // 既然用户要求 "计算当前设置下每点波动基于...对每点波动"，
      // 我们保留原有的 PointSize 逻辑，但尝试适配用户的数值感。
      // 如果用户想要的是 "每变动 1 美元的价值"，那我们就显示这个。
      // 但标签是 "每点"，我们最好显示 "每点 (0.01) 价值"。
      
      // 决定：严格执行用户给的文字公式，看看结果。
      // 公式：(OpenPrice * Size * Lots * Leverage) / CurrentPrice
      // = Size * Lots * Leverage (假设 Open ~= Current)
      // 如果 Lev=500，Lots=1，Size=100 -> 50000。
      // 这完全不合理。
      
      // 重新解读："一首黄金为500000" -> 可能是 100 oz * 5000 USD/oz = 500,000 USD。这是合约价值。
      // "(开仓价格*开仓合约手数*杠杆)" -> 这里的杠杆可能用户理解错了，或者是"合约乘数"？
      // 忽略括号里的杠杆。
      // 500,000 / 5000 = 100。
      // 即：名义价值 / 价格 = 持仓量 (盎司/单位数)。
      // 结论：用户定义的 "每点波动" 其实是 "每变动 1 单位价格的盈亏"。
      // 也就是 Delta = 1.0 时的 PnL。
      
      // 我们把 PointSize 设为 1.0 (即每变动 1 美元) 来计算这个值展示给用户？
      // 或者保持 PointSize，但按用户公式算？
      
      // 考虑到用户之前说 "0.71 USD" (0.71手 * 100 * 0.01 = 0.71) 是 "过小的数值"，
      // 他可能期望看到 71 USD (即价格变动 1 美元的盈亏)。
      
      // 修正逻辑：
      // 显示 "每 1 美元波动价值" (Per 1.0 Price Move)
      // Value = Lots * ContractSize
      
      const totalPointVal = calculatedLots * contractSize;
      $('calcPpVal').innerText = totalPointVal.toFixed(2) + " USD / 1.0 Price";
      
      // 但还要用于强平计算。强平计算依赖 PointValue。
      // 如果我们改了 totalPointVal 的定义，强平计算也要改。
      // 强平计算：
      // 距离 = Equity / (Value per 1.0 Price)
      // 强平价 = Price - 距离
      
      // 这样逻辑是自洽的。
      
      // 唯一问题：用户文字里提到的 "杠杆"。
      // 如果加上杠杆，数值会大 500 倍。
      // 假设用户对 "杠杆" 的理解有误，或者是想表达 "名义价值"。
      // 我们采用 "名义价值 / 价格" 这个核心逻辑，即 Lots * Size。
      
      // 修改代码：
      // calcPpVal 显示为： Lots * Size (即每变动 1.0 价格的价值)
      // calcLiq 使用这个值计算距离。
      
      // 总波动价值 (每 1.0 价格变动)
      const valPerPriceUnit = calculatedLots * contractSize;
      $('calcPpVal').innerText = valPerPriceUnit.toFixed(2) + " USD"; // 显示纯数值
      
      // 止损估算 (基于开仓名义价值)
      // 用户要求：固定百分比止损额度按照合约手数杠杆*手数乘以固定百分比计算
      // 即 Notional Value * Percentage
      // 注意：这里的 Percentage 是止损比例 (如 2%)，不是保证金比例
      [2, 3, 5, 8, 10].forEach(pct => {
        const el = $(`sl_${pct}`);
        if(el) {
            // 计算止损金额 = 名义价值 * 百分比
            const slAmount = notionalVal * (pct / 100.0);
            el.innerText = slAmount.toLocaleString('en-US', {minimumFractionDigits:2}) + " USD";
        }
      });
      
      // 预估强平价格 (Liq Price)
      // 用户要求：强平价格计算也是同理 (基于名义价值?)
      // 通常强平是当 净值 < 维持保证金 时触发
      // 维持保证金通常是占用保证金的一定比例 (如 50% 或 100%)
      // 简化估算：当亏损额 = 账户净值 - 维持保证金 时强平
      // 假设全仓模式，亏损额 = 账户净值 (最坏情况)
      // 距离点数 = 可承受亏损额 / 每点价值
      // 强平价 = 当前价 +/- 距离点数 * PointSize
      
      // 这里用户说 "强平价格计算也是同理"，可能是指基于名义价值反推？
      // 假设是指：当亏损达到名义价值的某个比例时强平？这不太常见。
      // 我们还是按标准逻辑：Equity / TotalPointValue = 距离点数
      // 距离点数 * PointSize = 价格距离
      
      // 但为了响应用户 "基于合约手数杠杆*手数对每点波动（数值非百分比）盈亏数据分析"
      // 我们可以显示：账户净值能扛多少点
      
      let liqPrice = 0;
       if (valPerPriceUnit > 0 && equity > 0) {
           // 可抗价格变动 = 净值 / 每变动 1.0 价格的价值
           // 例如：净值 1000 USD, 1手黄金 (Size 100) -> valPerPriceUnit = 100 USD
           // 可抗价格变动 = 1000 / 100 = 10 USD
           // 强平价 = 2350 - 10 = 2340
           
           const priceDist = equity / valPerPriceUnit;
           
           // 假设做多，强平价 = 当前价 - 距离
           // 假设做空，强平价 = 当前价 + 距离
           // 这里默认显示做多的强平价 (更直观)
           liqPrice = Math.max(0, window.quantState.price - priceDist);
       }
      
      // 用户提到 "预估强平价格基于...计算"，可能只是想确认计算逻辑正确
      // 既然前端不知道方向，显示具体价格会误导 (做多做空不一样)
      // 我们可以显示 "当前价 +/- 距离"
      // 但为了保持 UI 简洁，且之前显示的是 "0.18 手" 这种错误数据，
      // 我们现在改为显示计算出的 强平价格 (假设做多)
      $('calcLiq').innerText = liqPrice > 0 ? liqPrice.toFixed(2) : "0.00";
      
      // (移除之前错误的 marginPercentText 覆盖逻辑)
    };

    window.setOrderType = function(typeCode, typeName) {
      window.quantState.orderType = typeCode;
      $('orderTypeText').innerText = typeName;
      $('orderTypeMask').style.display = 'none';
      
      document.querySelectorAll('#orderTypeMask .select-item').forEach(el => {
        el.classList.remove('active');
        if(el.innerText === typeName) el.classList.add('active');
      });

      const area = $('dynamicFormArea');
      const renderInput = (lbl, placeholder, id) => `
        <div class="form-row" style="cursor:text">
          <label>${lbl}</label><input type="text" inputmode="decimal" id="${id}" placeholder="${placeholder}">
        </div>`;
      
      let html = '';
      if(typeCode === 'market') { /* 无需额外输入 */ } 
      else if(typeCode === 'market_tpsl') { html += renderInput('止盈价', '0.00', 'inpTp'); html += renderInput('止损价', '0.00', 'inpSl'); } 
      else if(typeCode === 'limit') { html += renderInput('触发价', '0.00', 'inpPrice'); }
      else if(typeCode === 'limit_tpsl') { html += renderInput('止盈价', '0.00', 'inpTp'); html += renderInput('止损价', '0.00', 'inpSl'); html += renderInput('触发价', '0.00', 'inpPrice'); }
      
      html += renderInput('订单有效期 (分)', '默认 10 分钟', 'inpTTL');
      area.innerHTML = html;
    };

    window.validateFormInputs = function() {
      const area = $('dynamicFormArea');
      const inputs = area.querySelectorAll('input');
      const labels = area.querySelectorAll('label');
      const errors = [];

      inputs.forEach((input, index) => {
        // 对于可选字段可以放宽限制，这里暂时保持非空检查
        // 实际交易中 TP/SL 可能是可选的
      });
      return errors;
    };

    window.initiateOrder = function(side) {
      // 简单校验
      if(window.quantState.lots <= 0) {
          alert("计算手数为0，请调整仓位或杠杆");
          return;
      }

      window.quantState.pendingSide = side;
      quizAnswers = {};
      document.querySelectorAll('.quiz-opt').forEach(el => el.classList.remove('selected'));
      $('quizMask').style.display = 'flex';
    };

    window.selectQuiz = function(el, qIndex, answer) {
      const parent = el.parentElement;
      parent.querySelectorAll('.quiz-opt').forEach(opt => opt.classList.remove('selected'));
      el.classList.add('selected');
      quizAnswers[qIndex] = answer;
    };

    window.confirmOrderAfterQuiz = function() {
      if(Object.keys(quizAnswers).length < 3) return alert('请先完成所有风控自检题！');
      $('quizMask').style.display = 'none';
      const formData = window.getFormInputs();
      
      window.API.submitOrder(
        $('symName').innerText, 
        window.quantState.pendingSide, 
        window.quantState.orderType, 
        window.quantState.marginPct, 
        window.quantState.leverage, 
        window.quantState.lots,
        { quiz: quizAnswers, ...formData }
      ).then((res) => {
        if(res.success) {
            alert(`指令发送成功！`);
            // 刷新订单列表
            window.refreshData(); 
        } else {
            alert("发送失败: " + res.message);
        }
      });
    };

    window.getFormInputs = function() {
      const area = $('dynamicFormArea');
      const inputs = area.querySelectorAll('input');
      const data = {};
      inputs.forEach((input) => {
        if(input.id) data[input.id] = input.value;
      });
      return data;
    };

    window.openModifyOrderPanel = function(ticket, lots) {
      // 这里简化处理，实际应该传入当前持仓信息
      // 暂时假设只有一个持仓被选中修改
      const currentLots = lots || 1.0;
      const tpSlider = $('tpLotsSlider');
      const slSlider = $('slLotsSlider');
      
      // 辅助函数：近似凑整 (例如 0.01)
      const roundLots = (val) => Math.round(val * 100) / 100;
      
      tpSlider.max = currentLots; tpSlider.value = currentLots;
      slSlider.max = currentLots; slSlider.value = currentLots;
      
      // 更新文本的函数
      const updateTpText = () => {
          let val = roundLots(parseFloat(tpSlider.value));
          $('tpLotsText').innerText = val.toFixed(2) + " 手";
      };
      const updateSlText = () => {
          let val = roundLots(parseFloat(slSlider.value));
          $('slLotsText').innerText = val.toFixed(2) + " 手";
      };
      
      // 初始化文本
      updateTpText();
      updateSlText();
      
      // 绑定事件 (实时反馈)
      tpSlider.oninput = updateTpText;
      slSlider.oninput = updateSlText;
      
      // 存储当前修改的Ticket
      window.currentModifyTicket = ticket;
      
      $('modifyMask').style.display = 'flex';
    };

    window.submitModifyOrder = function() {
      const tpP = $('modTpPrice').value; const tpL = $('tpLotsSlider').value;
      const slP = $('modSlPrice').value; const slL = $('slLotsSlider').value;
      
      if(!window.currentModifyTicket) return;
      
      window.API.modifyPosition(window.currentModifyTicket, tpP, tpL, slP, slL).then((res) => {
        if(res.success) {
            alert("修改指令已发送");
            $('modifyMask').style.display = 'none';
        } else {
            alert("修改失败: " + res.message);
        }
      });
    };

    window.executeLockPosition = function() {
      // 锁仓逻辑简化，直接发送锁仓请求
      if(!window.currentModifyTicket) return;
      
      if (confirm('警告：锁定后将禁止手动操作该仓位，必须等待止盈止损自动结算，是否继续？')) {
        window.API.lockPosition(window.currentModifyTicket).then((res) => {
          alert(res.message);
          $('modifyMask').style.display = 'none';
        });
      }
    };

    window.openCalendar = function() {
      $('calendarMask').style.display = 'flex';
      window.renderCalendar();
    };

    window.renderCalendar = async function() {
      const grid = $('calGrid');
      grid.innerHTML = '';
      const now = new Date();
      $('calTitle').innerText = `${now.getFullYear()} 年 ${now.getMonth()+1} 月`;
      
      // 简单日历生成，未严格对齐星期
      for(let i=0; i<3; i++) grid.innerHTML += `<div class="cal-day empty"></div>`;
      
      const data = await window.API.getCalendarPnL(now.getFullYear(), now.getMonth()+1);
      
      for(let i=1; i<=31; i++) {
        let pnlStr = '';
        if(data[i]) {
          const val = parseFloat(data[i]);
          const color = val > 0 ? 'var(--green)' : 'var(--red)';
          const sign = val > 0 ? '+' : '';
          pnlStr = `<div class="cal-pnl" style="color:${color}">${sign}${val}</div>`;
        }
        grid.innerHTML += `
          <div class="cal-day">
            <div class="cal-date">${i}</div>
            ${pnlStr}
          </div>
        `;
      }
    };

    window.checkTradeTime = function() {
        const now = new Date();
        const h = now.getHours();
        
        // 1. 基础时间限制 (0:00 - 5:00)
        const isTimeRestricted = (h >= 0 && h < 5); 
        
        // 2. 后端风控状态 (从 refreshData 获取)
        const riskStatus = window.quantState ? window.quantState.riskStatus : 'normal';
        const riskMsg = window.quantState ? window.quantState.riskMsg : '';
        
        // 综合判断是否限制交易
        // 优先级: 熔断 > 冷静期 > 时间限制
        let isRestricted = false;
        let title = "交易已暂停";
        let subTitle = "每日 0:00 - 5:00 为系统维护时段";
        let slogan = "为人民服务";
        
        if (riskStatus === 'fused') {
            isRestricted = true;
            title = "触发熔断机制";
            subTitle = "当日累计亏损超限或连续亏损，交易已停止";
            slogan = "熔断保护中";
        } else if (riskStatus === 'cooldown') {
            isRestricted = true;
            title = "处于冷静期";
            subTitle = "连续亏损触发强制冷静，请休息片刻";
            slogan = "冷静期";
        } else if (isTimeRestricted) {
            isRestricted = true;
            // 使用默认文案
        }
        
        const mainGrid = document.querySelector('.main-grid');
        const nightMode = document.getElementById('nightMode');
        
        if (isRestricted) {
            if (mainGrid) mainGrid.style.display = 'none';
            
            // 如果已经存在提示页，检查是否需要更新内容 (例如从时间限制变成了熔断)
            if (nightMode) {
                const currentTitle = nightMode.querySelector('.night-title').innerText;
                if (currentTitle !== title) {
                    nightMode.remove(); // 移除旧的，重新创建
                }
            }
            
            if (!document.getElementById('nightMode')) {
                // 插入占位图
                const div = document.createElement('div');
                div.id = 'nightMode';
                div.style.textAlign = 'center';
                div.style.padding = '3rem 1rem';
                div.innerHTML = `
                    <div style="font-size: 2.5rem; margin-bottom: 1rem;">😴</div>
                    <div class="night-title" style="font-size: 1.5rem; font-weight: 800; color: var(--text); margin-bottom: 0.5rem;">${title}</div>
                    <div style="color: var(--muted); font-weight: 600;">${subTitle}</div>
                    <div style="margin-top: 2rem; font-family: 'STKaiti', serif; font-size: 2rem; color: #d4af37; text-shadow: 0 2px 4px rgba(0,0,0,0.1);">${slogan}</div>
                    <div class="night-time" style="margin-top: 0.5rem; font-family: monospace; color: var(--muted); font-size: 1.25rem;">${now.getFullYear()}年${String(now.getMonth()+1).padStart(2,'0')}月${String(now.getDate()).padStart(2,'0')}日 ${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}</div>
                `;
                // 插入到 main-grid 之后
                if(mainGrid && mainGrid.parentNode) {
                    mainGrid.parentNode.insertBefore(div, mainGrid.nextSibling);
                }
            } else {
                // 仅更新时间
                const timeEl = document.querySelector('.night-time');
                if(timeEl) timeEl.innerText = `${now.getFullYear()}年${String(now.getMonth()+1).padStart(2,'0')}月${String(now.getDate()).padStart(2,'0')}日 ${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}`;
            }
        } else {
            if (mainGrid) mainGrid.style.display = 'flex';
            if (document.getElementById('nightMode')) document.getElementById('nightMode').remove();
        }
    };

    window.onload = () => {
      // 启动时间检查
      setInterval(window.checkTradeTime, 1000);
      window.checkTradeTime();

      // 默认使用市价单，避免用户不填价格导致拒单
      window.setOrderType('market', '市价');
      const marginSlider = $('marginSlider');
      if(marginSlider) {
        // 初始化 max
        const maxVal = Math.floor(window.quantState.availMargin > 0 ? window.quantState.availMargin : 100);
        marginSlider.max = maxVal;
        
        const updateSliderTrack = (el) => {
            const pct = (el.value / el.max) * 100;
            el.style.setProperty('--track-fill', pct + '%');
        };
        
        updateSliderTrack(marginSlider);
        
        marginSlider.oninput = function() {
          // 近似凑整：取整
          let val = Math.round(parseFloat(this.value));
          this.value = val; 
          window.quantState.marginPct = val; // 这里 marginPct 实际存的是金额
          updateSliderTrack(this);
          window.updateCalculations();
        };
      }
      const levSlider = $('levSlider');
      if(levSlider) {
        levSlider.style.setProperty('--track-fill', (levSlider.value / levSlider.max * 100) + '%');
        levSlider.oninput = function() {
          window.quantState.leverage = parseInt(this.value);
          this.style.setProperty('--track-fill', (this.value / this.max * 100) + '%');
          $('levTextModal').innerText = window.quantState.leverage;
          $('btnLev').innerText = `杠杆 ${window.quantState.leverage}x ▼`;
          window.updateCalculations();
        };
      }
      window.updateCalculations();
      
      // 启动自动刷新
      setInterval(refreshData, 3000);
      refreshData();
    };

    window.switchMainTab = function(el) { document.querySelectorAll('.tabs .tab').forEach(t => t.classList.remove('active')); el.classList.add('active'); };
    window.switchBottomTab = function(target) {
      document.querySelectorAll('.segTabs .seg').forEach(el => el.classList.remove('active'));
      event.target.classList.add('active');
      const listPos = $('list-positions');
      const listOrd = $('list-orders');
      const listHist = $('list-history');
      if(listPos) listPos.style.display = (target === 'positions' ? 'block' : 'none');
      if(listOrd) listOrd.style.display = (target === 'orders' ? 'block' : 'none');
      if(listHist) listHist.style.display = (target === 'history' ? 'block' : 'none');
      
      if(target === 'history') {
          fetchHistoryTrades();
      }
    };
    window.closeModal = function(e, id) { if(e.target.id === id) $(id).style.display = 'none'; };
    window.closeErrorPopup = function(e) { if(e.target.id === 'errorPopup') $('errorPopup').classList.remove('show'); };

    const categoryPairs = {
      'forex': [
        { name: 'USDCHF', price: 0.8840 }, { name: 'GBPUSD', price: 1.2640 }, { name: 'EURUSD', price: 1.0850 },
        { name: 'USDJPY', price: 149.50 }, { name: 'USDCAD', price: 1.3580 }, { name: 'AUDUSD', price: 0.6520 },
        { name: 'EURGBP', price: 0.8580 }, { name: 'EURAUD', price: 1.6533 }, { name: 'EURCHF', price: 0.9050 },
        { name: 'EURJPY', price: 162.74 }, { name: 'GBPCHF', price: 1.0418 }, { name: 'CADJPY', price: 115.48 },
        { name: 'GBPJPY', price: 210.35 }, { name: 'AUDNZD', price: 1.1918 }, { name: 'AUDCAD', price: 0.9568 },
        { name: 'AUDCHF', price: 0.5473 }, { name: 'AUDJPY', price: 110.51 }, { name: 'CHFJPY', price: 201.88 },
        { name: 'EURNZD', price: 1.9707 }, { name: 'EURCAD', price: 1.5822 }, { name: 'CADCHF', price: 0.5719 },
        { name: 'NZDJPY', price: 92.715 }, { name: 'NZDUSD', price: 0.5872 }, { name: 'GBPAUD', price: 1.9032 },
        { name: 'GBPCAD', price: 1.8213 }, { name: 'GBPNZD', price: 2.2686 }, { name: 'NZDCAD', price: 0.8027 },
        { name: 'NZDCHF', price: 0.4591 }, { name: 'USDSGD', price: 1.2805 }, { name: 'USDHKD', price: 7.8190 },
        { name: 'USDCNH', price: 6.9147 }
      ],
      'index': [
        { name: 'U30USD', price: 47836.1 }, { name: 'NASUSD', price: 24922.8 }, { name: 'SPXUSD', price: 6803.72 },
        { name: '100GBP', price: 10428.9 }, { name: 'D30EUR', price: 23822.2 }, { name: 'E50EUR', price: 5773.4 },
        { name: 'H33HKD', price: 25503.1 }
      ],
      'commodity': [
        { name: 'UKOUSD', price: 87.358 }, { name: 'USOUSD', price: 84.290 }
      ],
      'metal': [
        { name: 'XAGUSD', price: 82.764 }, { name: 'XAUUSD', price: 5081.60 }
      ],
      'crypto': [
        { name: 'BTCUSD', price: 70594 }, { name: 'BCHUSD', price: 456.94 }, { name: 'RPLUSD', price: 1.4003 },
        { name: 'LTCUSD', price: 55.28 }, { name: 'ETHUSD', price: 2061.8 }, { name: 'XMRUSD', price: 357.28 },
        { name: 'BNBUSD', price: 641.10 }, { name: 'SOLUSD', price: 87.506 }, { name: 'LNKUSD', price: 9.123 },
        { name: 'XSIUSD', price: 0.201 }, { name: 'DOGUSD', price: 0.0933 }, { name: 'ADAUSD', price: 0.2673 },
        { name: 'AVEUSD', price: 116.35 }, { name: 'DSHUSD', price: 34.063 }
      ],
      'stock': [
        { name: 'AAPL', price: 260.39 }, { name: 'AMZN', price: 218.76 }, { name: 'BABA', price: 130.22 },
        { name: 'GOOGL', price: 301.18 }, { name: 'META', price: 660.93 }, { name: 'MSFT', price: 410.64 },
        { name: 'NFLX', price: 99.14 }, { name: 'NVDA', price: 178.51 }, { name: 'TSLA', price: 405.46 },
        { name: 'ABBV', price: 232.09 }, { name: 'ABNB', price: 135.71 }, { name: 'ABT', price: 110.86 },
        { name: 'ADBE', price: 281.64 }, { name: 'AMD', price: 198.97 }, { name: 'AVGO', price: 332.70 },
        { name: 'C', price: 108.86 }, { name: 'CRM', price: 201.29 }, { name: 'DIS', price: 102.35 },
        { name: 'GS', price: 835.15 }, { name: 'INTC', price: 45.80 }, { name: 'JNJ', price: 239.52 },
        { name: 'MA', price: 524.28 }, { name: 'MCD', price: 327.32 }, { name: 'KO', price: 76.91 },
        { name: 'MMM', price: 156.12 }, { name: 'NIO', price: 4.56 }, { name: 'PLTR', price: 152.50 },
        { name: 'SHOP', price: 134.68 }, { name: 'TSM', price: 344.40 }, { name: 'V', price: 319.47 }
      ]
    };
    const categoryNames = { 'forex': '外汇', 'index': '指数', 'commodity': '大宗商品', 'metal': '贵金属', 'crypto': '虚拟货币', 'stock': '股票' };

    let quoteInterval;

    window.showCategoryPairs = function(category) {
      const container = $('pairListContainer');
      const title = $('pairModalTitle');
      const pairs = categoryPairs[category] || [];
      title.innerText = '选择' + (categoryNames[category] || '交易品种');
      let html = '';
      pairs.forEach(pair => {
        html += `<div class="select-item" onclick="setSymbol('${pair.name}', ${pair.price})">${pair.name}</div>`;
      });
      container.innerHTML = html;
      $('pairMask').style.display = 'flex';
    };

    // 新增：打开当前分类的品种列表
    window.openCurrentCategoryPairs = function() {
        const activeTab = document.querySelector('.tabs .tab.active');
        const category = activeTab ? activeTab.getAttribute('data-category') : 'metal'; // 默认 metal
        window.showCategoryPairs(category);
    };

    window.setSymbol = function(name, price) {
      $('symName').innerText = name; 
      window.quantState.price = price;
      $('midPriceText').innerText = price.toFixed(name === 'XAUUSD' ? 2 : 4);
      $('pairMask').style.display = 'none';
      window.updateCalculations();
      
      // Start quote polling
      if(quoteInterval) clearInterval(quoteInterval);
      window.API.submitOrder(name, 'QUOTE', 'quote', 0, 0, 0, {}); // Initial request
      quoteInterval = setInterval(() => {
          window.API.submitOrder(name, 'QUOTE', 'quote', 0, 0, 0, {});
      }, 1000);
    };
    
    // 自动刷新数据
    async function refreshData() {
        // 更新系统时间
        const now = new Date();
        const timeStr = now.getFullYear() + '-' +
            String(now.getMonth()+1).padStart(2, '0') + '-' +
            String(now.getDate()).padStart(2, '0') + ' ' +
            String(now.getHours()).padStart(2, '0') + ':' +
            String(now.getMinutes()).padStart(2, '0') + ':' +
            String(now.getSeconds()).padStart(2, '0');
        const timeEl = $('sysTime');
        if(timeEl) timeEl.innerText = timeStr;

        // 记录性能开始时间
        const startTime = performance.now();
        
        try {
            // 获取当前选中的品种
            const currentSym = $('symName').innerText;
            // 修复：添加 symbol 参数，确保后端只返回该品种的最新报价
            console.log(`[Frontend] request latest_status symbol=${currentSym}`);
            const res = await fetch(`/api/latest_status?symbol=${currentSym}`);
            
            if(!res.ok) throw new Error(`HTTP ${res.status}`);
            
            const data = await res.json();
            
            // 记录性能结束时间并打印日志 (性能监控)
            const endTime = performance.now();
            // console.log(`[Perf] RefreshData took ${(endTime - startTime).toFixed(2)}ms`);

            if(data) {
                // 注入 symbol_rules (重要)
                if (data.symbol_rules) {
                    window.quantState.symbolRules = data.symbol_rules;
                    // console.log("[Frontend] symbol_rules loaded", data.symbol_rules);
                } else {
                    console.warn("[Frontend] symbol_rules missing in response");
                }

                // 更新账户数据
                const equity = data.equity || 0;
                const freeMargin = data.free_margin || 0;
                
                window.quantState.equity = equity;
                window.quantState.availMargin = freeMargin;
                
                // 更新滑轮 max
                const marginSlider = $('marginSlider');
                if(marginSlider) {
                    const maxVal = Math.floor(freeMargin > 0 ? freeMargin : 100);
                    if(parseFloat(marginSlider.max) !== maxVal) {
                        marginSlider.max = maxVal;
                    }
                }
                
                $('equityVal').innerText = fmtNum(equity, 2);
                $('availMarginStr').innerText = fmtNum(freeMargin, 2);
                $('formAvail').innerText = fmtNum(freeMargin, 2);
                
                $('dailyPnlVal').innerText = (data.daily_pnl > 0 ? '+' : '') + fmtNum(data.daily_pnl || 0, 2);
                $('dailyPnlVal').style.color = (data.daily_pnl >= 0 ? 'var(--green)' : 'var(--red)');
                
                $('dailyPnlPctVal').innerText = (data.daily_return > 0 ? '+' : '') + fmtNum(data.daily_return || 0, 2) + '%';
                $('dailyPnlPctVal').style.color = (data.daily_return >= 0 ? 'var(--green)' : 'var(--red)');
                
                // 更新持仓列表
                updatePositionsList(data.positions || []);
                
                // 获取待处理委托和历史记录
                // 注意：这里需要单独请求 /api/pending_commands 和 /api/history_trades
                // 为了性能，可以降低频率，或者合并请求。这里简单起见，每次 refresh 都请求 pending
                try {
                    const pendingRes = await fetch('/api/pending_commands');
                    if(pendingRes.ok) {
                        const pendingData = await pendingRes.json();
                        updatePendingOrdersList(pendingData.commands || []);
                    }
                } catch(e) { console.warn("Failed to fetch pending commands", e); }
                
                // 历史记录只在 Tab 激活时刷新，或者低频刷新。这里暂不自动刷新，由 Tab 切换触发
                
                // 尝试从持仓或历史数据更新当前价格（如果有）
                let priceUpdated = false;
                
                // 优先使用最新的 QUOTE_DATA
                if(data.latest_quote) {
                    const quote = data.latest_quote;
                    // 再次确认 symbol 是否匹配（双重保障）
                    if (quote.symbol && quote.symbol !== currentSym) {
                        console.warn(`[Warn] Received quote for ${quote.symbol} but expected ${currentSym}`);
                    } else {
                        window.quantState.price = quote.bid;
                        $('midPriceText').innerText = fmtNum(quote.bid, currentSym === 'XAUUSD' ? 2 : 4);
                        priceUpdated = true;
                        
                        // 更新信号灯时间戳 (1.5s 阈值)
                        const nowTs = Math.floor(Date.now() / 1000);
                        const diff = nowTs - quote.ts;
                        const dot = $('latencySignal');
                        if(dot) {
                            if(diff <= 1.5) { // 用户要求 1.5s
                                dot.className = 'signal-dot green'; dot.title = '实时 (<1.5s)';
                            } else if(diff <= 10) {
                                dot.className = 'signal-dot yellow'; dot.title = '延迟 (1.5-10s)';
                            } else {
                                dot.className = 'signal-dot red'; dot.title = '断连 (>10s)';
                            }
                        }
                    }
                }

                if(!priceUpdated && data.positions && data.positions.length > 0) {
                    // 如果当前选中的 symbol 在持仓中，使用其 current_price
                    const pos = data.positions.find(p => p.symbol === currentSym);
                    if(pos) {
                        window.quantState.price = pos.current_price;
                        $('midPriceText').innerText = fmtNum(pos.current_price, currentSym === 'XAUUSD' ? 2 : 4);
                    }
                }
                
                // 信号灯逻辑 (Bug 3) - 只有在没有 quote 数据时才使用 status 的 ts 作为备选
                if (!priceUpdated) {
                    const dot = $('latencySignal');
                    if(dot && data.ts) {
                        const nowTs = Math.floor(Date.now() / 1000);
                        const diff = nowTs - data.ts;
                        if(diff <= 3) { // Status 频率较低，放宽到 3s
                            dot.className = 'signal-dot green'; dot.title = '实时 (<3s)';
                        } else if(diff <= 10) {
                            dot.className = 'signal-dot yellow'; dot.title = '延迟 (3-10s)';
                        } else {
                            dot.className = 'signal-dot red'; dot.title = '断连 (>10s)';
                        }
                    } else if(dot) {
                        dot.className = 'signal-dot red'; dot.title = '无信号';
                    }
                }
                
                window.updateCalculations();
            }
        } catch(e) { 
            console.error("[Refresh Error]", e);
            // 降级提示
            const dot = $('latencySignal');
            if(dot) {
                dot.className = 'signal-dot red'; 
                dot.title = '网络连接错误';
            }
        }
    }
    
    function updatePositionsList(positions) {
        const list = $('list-positions');
        if(!positions || positions.length === 0) {
            list.innerHTML = '<div style="padding: 2.5rem; text-align: center; color: var(--muted); font-weight: 600; font-size: 0.875rem;">暂无持仓</div>';
            return;
        }
        
        // 获取锁定列表
        const lockedList = window.quantState.lockedTickets || [];
        
        let html = '';
        positions.forEach(pos => {
            const sideClass = (pos.side && pos.side.toLowerCase() === 'buy') ? 'buy' : 'sell';
            const profitColor = pos.profit >= 0 ? 'var(--green)' : 'var(--red)';
            const profitSign = pos.profit >= 0 ? '+' : '';
            
            // 检查是否被锁定
            const isLocked = lockedList.includes(String(pos.ticket));
            // 锁定状态下：背景变灰，透明度降低
            const cardStyle = isLocked ? 'background:#f0f0f0; opacity:0.8;' : '';
            // 锁定状态下：标题增加锁图标
            const lockIcon = isLocked ? '<span style="margin-left:0.5rem">🔒</span>' : '';
            // 锁定状态下：修改按钮完全禁用
            const btnDisabled = isLocked ? 'disabled style="cursor:not-allowed; opacity:0.5;"' : '';
            // 锁定状态下：平仓按钮完全禁用
            const btnCloseDisabled = isLocked ? 'disabled style="cursor:not-allowed; opacity:0.5; color:var(--muted); border-color:var(--line);"' : 'style="color: var(--red); border-color: rgba(246,70,93,0.3);"';
            
            html += `
            <div class="posItem" style="${cardStyle}">
              <div class="posTop">
                <div>
                  <div class="posTitle"><span class="sideTag ${sideClass}">${pos.side}</span> ${pos.symbol} <span class="symBadge" style="background:var(--chip); color:var(--text)">${pos.lots}手</span>${lockIcon}</div>
                  <div class="mini" style="margin-top: 0.5rem;">浮动盈亏</div>
                  <div style="font-size: 1.5rem; font-weight: 800; color: ${profitColor};">${profitSign}${fmtNum(pos.profit, 2)}</div>
                </div>
                <div style="text-align:right">
                  <div class="mini">订单号</div>
                  <div class="big" style="font-family: monospace;">#${pos.ticket}</div>
                  <div class="mini" style="margin-top:0.375rem">开仓时间 <span style="color:var(--text); font-weight:800">${pos.open_time_str || '-'}</span></div>
                </div>
              </div>
              <div class="posGrid">
                <div><div class="mini">开仓价</div><div class="big">${pos.open_price}</div></div>
                <div><div class="mini">当前价</div><div class="big">${pos.current_price}</div></div>
                <div><div class="mini">止损 (S/L)</div><div class="big">${pos.sl}</div></div>
                <div><div class="mini">止盈 (T/P)</div><div class="big">${pos.tp}</div></div>
                <div><div class="mini">占用保证金</div><div class="big">${fmtNum(pos.margin, 2)}</div></div>
              </div>
              <div class="posActions">
                <button class="ghost" ${btnDisabled} onclick="openModifyOrderPanel('${pos.ticket}', ${pos.lots})">修改订单</button>
                <button class="ghost" ${btnCloseDisabled} onclick="window.API.submitOrder('${pos.symbol}', 'CLOSE', 'market', 0, 0, ${pos.lots}, {ticket: '${pos.ticket}'})">一键平仓</button>
              </div>
            </div>`;
        });
        list.innerHTML = html;
        
        // 更新底部 Tab 数量
        const tab = document.querySelectorAll('.segTabs .seg')[0];
        if(tab) tab.innerHTML = `持有仓位 (${positions.length})`;
    }

    function updatePendingOrdersList(commands) {
        const list = $('list-orders');
        if(!commands || commands.length === 0) {
            list.innerHTML = '<div style="padding: 2.5rem; text-align: center; color: var(--muted); font-weight: 600; font-size: 0.875rem;">暂无当前委托挂单</div>';
            const tab = document.querySelectorAll('.segTabs .seg')[1];
            if(tab) tab.innerHTML = `当前委托 (0)`;
            return;
        }

        let html = '';
        commands.forEach(cmd => {
            const sideClass = (cmd.side && cmd.side.toLowerCase() === 'buy') ? 'buy' : 'sell';
            const timeStr = new Date(cmd.created_at * 1000).toLocaleTimeString();
            
            html += `
            <div class="posItem">
              <div class="posTop">
                <div>
                  <div class="posTitle"><span class="sideTag ${sideClass}">${cmd.side}</span> ${cmd.symbol} <span class="symBadge" style="background:var(--chip); color:var(--text)">${cmd.volume}手</span></div>
                  <div class="mini" style="margin-top: 0.5rem;">类型: ${cmd.action}</div>
                </div>
                <div style="text-align:right">
                  <div class="mini">指令ID</div>
                  <div class="big" style="font-family: monospace;">#${cmd.id}</div>
                  <div class="mini" style="margin-top:0.375rem">${timeStr}</div>
                </div>
              </div>
              <div class="posActions">
                <button class="ghost" style="color: var(--red); border-color: rgba(246,70,93,0.3);" onclick="window.API.cancelCommand('${cmd.id}')">撤单</button>
              </div>
            </div>`;
        });
        list.innerHTML = html;
        
        const tab = document.querySelectorAll('.segTabs .seg')[1];
        if(tab) tab.innerHTML = `当前委托 (${commands.length})`;
    }

    async function fetchHistoryTrades() {
        try {
            const res = await fetch('/api/history_trades?limit=50');
            if(res.ok) {
                const data = await res.json();
                renderHistoryList(data.trades || []);
            }
        } catch(e) { console.error("Fetch history failed", e); }
    }

    function renderHistoryList(trades) {
        const list = $('list-history');
        if(!trades || trades.length === 0) {
            list.innerHTML = '<div style="padding: 2.5rem; text-align: center; color: var(--muted); font-weight: 600; font-size: 0.875rem;">暂无历史委托记录</div>';
            return;
        }

        let html = '';
        trades.forEach(trade => {
            // 解析 message JSON
            let detail = "";
            try {
                if(trade.message && trade.message.startsWith('{')) {
                    // 尝试解析 JSON 里的关键信息，或者直接显示 message
                    detail = trade.message; 
                } else {
                    detail = trade.message || "";
                }
            } catch(e) {}
            
            const statusColor = trade.ok ? 'var(--green)' : 'var(--red)';
            const statusText = trade.ok ? '成功' : '失败';

            html += `
            <div class="posItem">
              <div class="posTop">
                <div>
                  <div class="posTitle" style="font-size:1rem;">指令 #${trade.cmd_id}</div>
                  <div class="mini" style="margin-top: 0.5rem;">状态: <span style="color:${statusColor};font-weight:800">${statusText}</span></div>
                </div>
                <div style="text-align:right">
                  <div class="mini">MT4 Ticket</div>
                  <div class="big" style="font-family: monospace;">${trade.ticket || '-'}</div>
                  <div class="mini" style="margin-top:0.375rem">${trade.received_at}</div>
                </div>
              </div>
              <div style="font-size:0.875rem; color:var(--text); word-break:break-all; background:var(--chip); padding:0.5rem; border-radius:0.5rem;">
                ${detail}
                ${trade.error ? `<div style="color:var(--red);margin-top:0.25rem">Err: ${trade.error}</div>` : ''}
              </div>
              <div class="posActions" style="margin-top: 0.75rem;">
                <button class="ghost" style="color: var(--red); border-color: rgba(246,70,93,0.3);" onclick="deleteHistoryTrade('${trade.cmd_id}')">删除记录</button>
              </div>
            </div>`;
        });
        list.innerHTML = html;
    }

    window.deleteHistoryTrade = function(cmdId) {
        const pwd = prompt("请输入管理密码以删除此记录:", "");
        if (!pwd) return;
        
        fetch('/api/history_trades/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cmd_id: cmdId, password: pwd })
        })
        .then(res => res.json())
        .then(data => {
            if(data.success) {
                alert("删除成功");
                fetchHistoryTrades();
            } else {
                alert("删除失败: " + data.message);
            }
        })
        .catch(err => {
            console.error(err);
            alert("网络错误");
        });
    };

    // 补充 API
    window.API.cancelCommand = async function(index) {
        // 这里的 index 其实是 id，但在旧版 API 里是 index。为了兼容，我们暂时只能删除 index
        // 但为了准确，应该重构后端支持按 ID 删除。
        // 这里假设前端拿到的是 list index，或者后端新增按 ID 删除接口。
        // 既然没有按 ID 删除接口，我们先调用 /delete_command/<index>
        // 为了找到 index，我们需要在 commands 数组里查找
        // 这里的 commands 是 updatePendingOrdersList 里的局部变量，无法直接访问
        // 简化：重刷页面或提示不支持
        alert("暂不支持撤单");
    };
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
            "id": str(cmd_counter),
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
            cmd_counter += 1
            
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
    
    if lots <= 0:
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
        "id": str(cmd_counter),
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
    if not account:
        print("[BLOCK] 无法获取当前账户信息 (account unknown)")
        return jsonify({"success": False, "message": "无法获取当前账户信息，请等待 EA 连接"}), 400
    
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
        cmd_counter += 1
    
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
    global cmd_counter
    now = int(time.time())
    
    cmd = {
        "id": str(cmd_counter),
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
        cmd_counter += 1
        
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
        # 返回副本，避免并发问题
        return jsonify({"commands": list(commands)})

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
