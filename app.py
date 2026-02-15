from flask import Flask, request, jsonify, render_template_string, session, send_file
from flask_cors import CORS
import time
from datetime import datetime
import pytz
import json
import sqlite3
from contextlib import closing
import pandas as pd
import io
import hashlib
import threading
from collections import defaultdict, deque

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'

# 启用 CORS 支持公网访问
CORS(app, resources={r"/*": {"origins": "*"}})

# 数据库配置
DATABASE = 'trading_signals.db'
MT4_DATABASE = 'mt4_trading.db'

# 交易时段与资金配置
TRADING_TIME_START = (4, 30)   # 04:30
TRADING_TIME_END = (23, 59)    # 近似到 24:00
BASE_CAPITAL = 100000.0        # 账户基准资金（占位，可改为从实盘读取）


# ==================== MT4 数据库初始化（需要在 init_db 之前定义）====================

def init_mt4_db():
    """初始化 MT4 数据库"""
    with closing(sqlite3.connect(MT4_DATABASE)) as conn:
        with conn:
            cursor = conn.cursor()
            # 状态历史表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mt4_status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    balance REAL,
                    equity REAL,
                    margin REAL,
                    margin_level REAL,
                    daily_pnl REAL,
                    daily_return REAL,
                    leverage_used REAL,
                    data_json TEXT,
                    created_at REAL DEFAULT (julianday('now'))
                )
            ''')
            # 执行回报表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mt4_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account INTEGER NOT NULL,
                    cmd_id TEXT NOT NULL,
                    ok BOOLEAN,
                    action TEXT,
                    symbol TEXT,
                    ticket INTEGER,
                    error INTEGER,
                    message TEXT,
                    ts INTEGER,
                    created_at REAL DEFAULT (julianday('now'))
                )
            ''')
            # 报价历史表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mt4_quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account INTEGER NOT NULL,
                    request_id TEXT,
                    symbol TEXT NOT NULL,
                    bid REAL,
                    ask REAL,
                    spread_points REAL,
                    ts INTEGER,
                    created_at REAL DEFAULT (julianday('now'))
                )
            ''')
            # 创建索引
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_status_account_ts ON mt4_status_history(account, ts)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reports_cmd_id ON mt4_reports(cmd_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_quotes_symbol ON mt4_quotes(symbol, ts)')


def is_trading_time_now():
    """判断当前是否在交易时间（东八区 4:30 - 24:00）"""
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)

    start_hour, start_minute = TRADING_TIME_START
    end_hour, end_minute = TRADING_TIME_END

    start = now.replace(hour=start_hour, minute=start_minute,
                        second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=end_minute,
                      second=59, microsecond=999999)

    return start <= now <= end


# 数据库初始化函数
def init_db():
    with closing(sqlite3.connect(DATABASE)) as conn:
        with conn:  # 自动提交事务
            with closing(conn.cursor()) as cursor:
                # 创建信号表
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    entry_time REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    prediction_minutes INTEGER NOT NULL,
                    received_at REAL NOT NULL,
                    processed BOOLEAN DEFAULT FALSE,
                    raw_data TEXT NOT NULL,
                    client_ip TEXT NOT NULL DEFAULT ''
                )
                ''')

                # 创建结果表
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    entry_time REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_time REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    result TEXT NOT NULL,
                    prediction_minutes INTEGER NOT NULL,
                    received_at REAL NOT NULL,
                    raw_data TEXT NOT NULL
                )
                ''')

                # MT4 指令表（用于缓存网页下发给 MT4 EA 的指令）
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS mt4_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',   -- pending / sent / done
                    payload TEXT NOT NULL
                )
                ''')

                # MT4 报价表（保存各品种最近一次 MT4 回传的价格）
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS mt4_quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    bid REAL NOT NULL,
                    ask REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                ''')

                # 创建索引提高查询性能
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_trade_id ON signals(trade_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_results_trade_id ON results(trade_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_mt4_quotes_symbol ON mt4_quotes(symbol)')
        
        # 初始化 MT4 数据库
        init_mt4_db()


# 数据库辅助函数
def get_db():
    """获取数据库连接"""
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row  # 使返回的行像字典一样工作
    return db


def query_db(query, args=(), one=False):
    """执行查询"""
    with closing(get_db()) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute(query, args)
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv


def execute_db(query, args=()):
    """执行写入操作"""
    with closing(get_db()) as conn:
        with conn:  # 自动提交事务
            with closing(conn.cursor()) as cur:
                cur.execute(query, args)
                return cur.lastrowid


# ==================== MT4 命令队列管理器 ====================

class CommandQueue:
    """命令队列管理器 - 按 account 维度管理"""
    
    def __init__(self):
        # account -> deque(commands)
        self.queues: dict = defaultdict(deque)
        # cmd_id -> command (用于幂等检查)
        self.cmd_registry: dict = {}
        # account -> 去重窗口 (时间戳 -> cmd_id)
        self.dedupe_windows: dict = defaultdict(deque)
        # 去重窗口时长（秒）
        self.dedupe_window_sec = 1.5
        # 账户名称到账户ID的映射
        self.account_name_to_id: dict = {}
        # 线程锁
        self.lock = threading.RLock()
    
    def register_account_name(self, account_name: str, account_id: int):
        """注册账户名称到账户ID的映射"""
        with self.lock:
            self.account_name_to_id[account_name] = account_id
    
    def get_account_id(self, account=None, account_name=None):
        """根据账户ID或账户名称获取账户ID"""
        if account is not None:
            return account
        if account_name is not None:
            return self.account_name_to_id.get(account_name)
        return None
    
    def generate_cmd_id(self, account: int, action: str) -> str:
        """生成唯一命令ID"""
        timestamp = int(time.time() * 1000)
        unique_str = f"{account}_{action}_{timestamp}_{time.time_ns()}"
        return hashlib.md5(unique_str.encode()).hexdigest()[:16]
    
    def get_dedupe_key(self, cmd: dict) -> str:
        """生成去重键（用于合并连点）"""
        parts = [
            str(cmd.get('account', '')),
            cmd.get('action', ''),
            cmd.get('symbol', ''),
            cmd.get('side', ''),
        ]
        if cmd.get('risk_alloc_pct') is not None:
            parts.append(f"risk_{cmd['risk_alloc_pct']}")
        elif cmd.get('volume') is not None:
            parts.append(f"vol_{cmd['volume']}")
        if cmd.get('price') is not None:
            parts.append(f"price_{cmd['price']}")
        if cmd.get('sl') is not None:
            parts.append(f"sl_{cmd['sl']}")
        if cmd.get('tp') is not None:
            parts.append(f"tp_{cmd['tp']}")
        return "|".join(parts)
    
    def add_command(self, cmd_data: dict) -> dict:
        """添加命令到队列（带幂等和去重）"""
        with self.lock:
            account = self.get_account_id(cmd_data.get('account'), cmd_data.get('account_name'))
            if account is None:
                raise ValueError("必须提供 account 或 account_name 之一")
            action = cmd_data.get('action')
            
            # 构建命令对象
            cmd = {
                "id": self.generate_cmd_id(account, action),
                "action": action,
                "account": account,
                "created_at": int(time.time()),
                "ttl_sec": 30 if action in ["LIMIT", "CLOSE", "MODIFY"] else 10,
            }
            
            # 根据 action 填充字段
            if action == "QUOTE":
                cmd["symbols"] = cmd_data.get('symbols', [])
            elif action == "MARKET":
                cmd["symbol"] = cmd_data.get('symbol')
                cmd["side"] = cmd_data.get('side')
                cmd["risk_alloc_pct"] = cmd_data.get('risk_alloc_pct')
                cmd["target_leverage"] = cmd_data.get('target_leverage')
                cmd["max_spread_points"] = cmd_data.get('max_spread_points')
                cmd["sl_points"] = cmd_data.get('sl_points')
                cmd["tp_points"] = cmd_data.get('tp_points')
            elif action == "LIMIT":
                cmd["symbol"] = cmd_data.get('symbol')
                cmd["side"] = cmd_data.get('side')
                cmd["volume"] = cmd_data.get('volume')
                cmd["price"] = cmd_data.get('price')
                cmd["sl"] = cmd_data.get('sl')
                cmd["tp"] = cmd_data.get('tp')
                cmd["max_spread_points"] = cmd_data.get('max_spread_points')
            elif action == "CLOSE":
                cmd["ticket"] = cmd_data.get('ticket')
            elif action == "MODIFY":
                cmd["ticket"] = cmd_data.get('ticket')
                cmd["sl"] = cmd_data.get('sl')
                cmd["tp"] = cmd_data.get('tp')
            
            # 幂等检查
            if cmd["id"] in self.cmd_registry:
                return self.cmd_registry[cmd["id"]]
            
            # 去重窗口检查
            dedupe_key = self.get_dedupe_key(cmd)
            now = time.time()
            window = self.dedupe_windows[account]
            
            # 清理过期窗口
            while window and now - window[0][0] > self.dedupe_window_sec:
                window.popleft()
            
            # 检查窗口内是否有相同请求
            for win_ts, win_key, win_cmd_id in window:
                if win_key == dedupe_key:
                    existing_cmd = self.cmd_registry.get(win_cmd_id)
                    if existing_cmd:
                        return existing_cmd
            
            # 新命令，加入队列和注册表
            self.queues[account].append(cmd)
            self.cmd_registry[cmd["id"]] = cmd
            window.append((now, dedupe_key, cmd["id"]))
            
            return cmd
    
    def get_commands(self, account=None, account_name=None, max_count=50):
        """获取命令（取走即删除），支持按账户ID或账户名称查找"""
        with self.lock:
            if account is None and account_name is not None:
                account = self.get_account_id(None, account_name)
            if account is None:
                return []
            queue = self.queues.get(account, deque())
            commands = []
            count = 0
            
            while queue and count < max_count:
                cmd = queue.popleft()
                # 检查是否过期
                now = int(time.time())
                if now > cmd["created_at"] + cmd.get("ttl_sec", 30):
                    continue
                commands.append(cmd)
                count += 1
            
            return commands


# 全局命令队列实例
command_queue = CommandQueue()




# 初始化数据库
init_db()


# 自定义 datetime 过滤器（东八区）
@app.template_filter('datetime')
def format_datetime(value, format="%Y-%m-%d %H:%M:%S"):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        tz = pytz.timezone('Asia/Shanghai')
        return datetime.fromtimestamp(value, tz).strftime(format)
    elif isinstance(value, str):
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return dt.strftime(format)
        except Exception:
            return value
    return str(value)


# 登录接口
@app.route('/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    if username == 'CharlesZ' and password == 'MYbt7274':
        session['logged_in'] = True
        return jsonify({"status": "success"})
    else:
        return jsonify({"status": "error", "message": "Invalid credentials"}), 401


# 信号查询接口（实盘系统拉取最新未处理信号）
@app.route('/api/signal/latest', methods=['GET'])
def get_latest_signal():
    signal = query_db(
        'SELECT * FROM signals WHERE processed = FALSE ORDER BY received_at DESC LIMIT 1',
        one=True
    )
    if signal:
        return jsonify({'status': 'success', 'signal': dict(signal)})
    else:
        return jsonify({'status': 'error', 'message': 'No unprocessed signals'}), 404


# 登出接口
@app.route('/logout', methods=['POST'])
def logout():
    session.pop('logged_in', None)
    return jsonify({"status": "success"})


# 交易信号接口（实盘系统推送信号）
@app.route('/api/signal', methods=['POST'])
def receive_signal():
    try:
        data = request.get_json()

        # 验证必要字段
        required_fields = ['trade_id', 'direction', 'entry_time', 'entry_price', 'prediction_minutes']
        if not data or not all(field in data for field in required_fields):
            return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

        # 插入数据库
        execute_db(
            '''
            INSERT INTO signals
            (trade_id, direction, entry_time, entry_price, prediction_minutes, received_at, raw_data, client_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                data['trade_id'],
                data['direction'],
                data['entry_time'],
                data['entry_price'],
                data['prediction_minutes'],
                time.time(),
                json.dumps(data, ensure_ascii=False),
                data.get('client_ip', request.remote_addr or '')
            )
        )

        return jsonify({
            'status': 'success',
            'trade_id': data['trade_id'],
            'message': 'Signal received'
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# 结果接收接口（实盘系统推送成交结果）
@app.route('/api/result', methods=['POST'])
def receive_result():
    try:
        data = request.get_json()

        # 验证必要字段
        required_fields = ['trade_id', 'direction', 'entry_time', 'entry_price',
                           'exit_time', 'exit_price', 'result', 'prediction_minutes']
        if not data or not all(field in data for field in required_fields):
            return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

        # 插入数据库
        execute_db(
            '''
            INSERT INTO results
            (trade_id, direction, entry_time, entry_price, exit_time, exit_price,
             result, prediction_minutes, received_at, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                data['trade_id'],
                data['direction'],
                data['entry_time'],
                data['entry_price'],
                data['exit_time'],
                data['exit_price'],
                data['result'],
                data['prediction_minutes'],
                time.time(),
                json.dumps(data, ensure_ascii=False)
            )
        )

        # 更新对应的信号为已处理
        execute_db(
            'UPDATE signals SET processed = TRUE WHERE trade_id = ? AND processed = FALSE',
            (data['trade_id'],)
        )

        return jsonify({
            'status': 'success',
            'trade_id': data['trade_id'],
            'message': 'Result received'
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# 清空数据接口
@app.route('/api/clear_data', methods=['POST'])
def clear_data():
    try:
        data = request.get_json(silent=True) or {}
        admin_key = data.get('admin_key')

        # 检查登录状态或密钥
        if not session.get('logged_in') and admin_key != 'MYbt7274':
            return jsonify({"status": "error", "message": "Invalid admin key or not logged in"}), 403

        # 清空两个表
        execute_db('DELETE FROM signals')
        execute_db('DELETE FROM results')
        return jsonify({"status": "success", "message": "All data cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# 网络延迟测试接口
@app.route('/api/test_latency', methods=['POST'])
def test_latency():
    try:
        data = request.get_json()
        if data and data.get('test') == 'latency':
            return jsonify({
                'status': 'success',
                'timestamp': data.get('timestamp'),
                'server_time': time.time()
            })
        return jsonify({'status': 'error', 'message': 'Invalid test request'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# 检查并删除 30 秒内相邻重复信号/结果
@app.route('/api/check_duplicates', methods=['POST'])
def check_duplicate_signals():
    try:
        # 检查登录状态
        if not session.get('logged_in'):
            return jsonify({"status": "error", "message": "Not logged in"}), 403

        deleted_count = 0
        deleted_results_count = 0

        # 第一步：检查信号表中的重复信号
        signals = query_db('SELECT * FROM signals ORDER BY received_at ASC') or []

        # 使用集合记录需要删除的trade_id
        duplicate_trade_ids = set()

        for i in range(1, len(signals)):
            prev_signal = signals[i - 1]
            current_signal = signals[i]

            # 检查时间差是否小于30秒
            if current_signal['received_at'] - prev_signal['received_at'] < 30:
                duplicate_trade_ids.add(current_signal['trade_id'])
                execute_db('DELETE FROM signals WHERE id = ?', (current_signal['id'],))
                deleted_count += 1

        # 第二步：独立检查结果表中的重复结果
        results = query_db('SELECT * FROM results ORDER BY received_at ASC') or []

        for i in range(1, len(results)):
            prev_result = results[i - 1]
            current_result = results[i]

            # 检查时间差是否小于30秒
            if current_result['received_at'] - prev_result['received_at'] < 30:
                # 删除当前结果
                execute_db('DELETE FROM results WHERE id = ?', (current_result['id'],))
                deleted_results_count += 1
                # 同时检查对应的信号是否也需要删除
                signal = query_db('SELECT * FROM signals WHERE trade_id = ?',
                                  (current_result['trade_id'],), one=True)
                if signal:
                    execute_db('DELETE FROM signals WHERE id = ?', (signal['id'],))
                    deleted_count += 1

        # 第三步：删除所有标记为重复的trade_id对应的结果
        for trade_id in duplicate_trade_ids:
            result = query_db('SELECT * FROM results WHERE trade_id = ?',
                              (trade_id,), one=True)
            if result:
                execute_db('DELETE FROM results WHERE id = ?', (result['id'],))
                deleted_results_count += 1

        return jsonify({
            "status": "success",
            "deleted_count": deleted_count,
            "deleted_results_count": deleted_results_count,
            "message": "Duplicate check completed"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# 导出数据为XLSX接口
@app.route('/api/export_data', methods=['GET'])
def export_data():
    try:
        # 获取所有数据
        signals = query_db('SELECT * FROM signals ORDER BY received_at DESC')
        results = query_db('SELECT * FROM results ORDER BY received_at DESC')

        # 转换为DataFrame
        signals_df = pd.DataFrame([dict(row) for row in signals])
        results_df = pd.DataFrame([dict(row) for row in results])

        # 创建Excel writer
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            signals_df.to_excel(writer, sheet_name='Signals', index=False)
            results_df.to_excel(writer, sheet_name='Results', index=False)

        output.seek(0)

        # 返回文件
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='trading_data.xlsx'
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ===== 新增：JSON 汇总 / 持仓 / 历史 / MT4 指令 接口，预留给前端和实盘系统 =====

@app.route('/api/summary', methods=['GET'])
def api_summary():
    """账户与当日汇总数据 JSON 接口（当前逻辑为占位 / 简单示例）"""
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)

    today_start = tz.localize(datetime(now.year, now.month, now.day, 0, 0, 0)).timestamp()
    today_end = tz.localize(datetime(now.year, now.month, now.day, 23, 59, 59)).timestamp()

    # 当日交易总数
    today_trades_row = query_db(
        'SELECT COUNT(*) AS count FROM results WHERE exit_time BETWEEN ? AND ?',
        (today_start, today_end),
        one=True
    )
    today_trades = today_trades_row['count'] if today_trades_row else 0

    # 当日盈利 / 亏损单数（基于 result 字段）
    today_wins_row = query_db(
        "SELECT COUNT(*) AS count FROM results WHERE result = '盈利' AND exit_time BETWEEN ? AND ?",
        (today_start, today_end),
        one=True
    )
    today_wins = today_wins_row['count'] if today_wins_row else 0

    today_losses = today_trades - today_wins if today_trades > 0 else 0

    # 使用原来的“净盈利单数公式”作为占位：盈利 +1，亏损 -1.25
    today_net_profit = today_wins - today_losses * 1.25
    today_profit_pct = (today_net_profit / today_trades * 100) if today_trades > 0 else 0.0

    # 当前持仓（未处理信号）数量
    open_positions_count_row = query_db(
        'SELECT COUNT(*) AS count FROM signals WHERE processed = FALSE',
        one=True
    )
    open_positions_count = open_positions_count_row['count'] if open_positions_count_row else 0

    # 当前持仓总损益、杠杆百分比：目前用 0 占位，后续可从实盘写入真实值
    open_pnl = 0.0
    leverage_pct = 0.0

    return jsonify({
        'status': 'success',
        'data': {
            'equity': BASE_CAPITAL,
            'today_net_profit': today_net_profit,
            'today_profit_pct': today_profit_pct,
            'open_pnl': open_pnl,
            'leverage_pct': leverage_pct,
            'today_trades': today_trades,
            'open_positions_count': open_positions_count,
            'server_time': time.time()
        }
    })


@app.route('/api/open_positions', methods=['GET'])
def api_open_positions():
    """当前持仓列表：用未处理的 signals 作为“持仓”占位"""
    positions = query_db(
        'SELECT * FROM signals WHERE processed = FALSE ORDER BY entry_time DESC'
    ) or []
    return jsonify({
        'status': 'success',
        'data': [dict(row) for row in positions]
    })


@app.route('/api/history', methods=['GET'])
def api_history():
    """历史交易记录列表：基于 results 表"""
    history = query_db(
        'SELECT * FROM results ORDER BY exit_time DESC LIMIT 300'
    ) or []
    return jsonify({
        'status': 'success',
        'data': [dict(row) for row in history]
    })


@app.route('/api/mt4_commands', methods=['GET', 'POST'])
def api_mt4_commands():
    """
    MT4 指令接口：
    - POST：前端/其他系统写入一条待执行指令（payload 任意 JSON）
    - GET：MT4 EA 拉取一条最新待执行指令（status = 'pending'），并立即标记为 sent
    """
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({'status': 'error', 'message': 'Empty payload'}), 400

        payload_text = json.dumps(data, ensure_ascii=False)
        cmd_id = execute_db(
            '''
            INSERT INTO mt4_commands (created_at, status, payload)
            VALUES (?, ?, ?)
            ''',
            (time.time(), 'pending', payload_text)
        )
        return jsonify({'status': 'success', 'command_id': cmd_id})

    # GET: MT4 EA 拉取一条待执行指令
    row = query_db(
        "SELECT * FROM mt4_commands WHERE status = 'pending' ORDER BY id ASC LIMIT 1",
        one=True
    )
    if not row:
        return jsonify({'status': 'empty'})

    # 标记为 sent，避免被重复拉取
    execute_db(
        "UPDATE mt4_commands SET status = 'sent' WHERE id = ?",
        (row['id'],)
    )

    return jsonify({
        'status': 'success',
        'command_id': row['id'],
        'payload': json.loads(row['payload'])
    })


@app.route('/api/mt4_quote', methods=['GET', 'POST'])
def api_mt4_quote():
    """
    MT4 报价接口：
    - POST：MT4 EA 回传某品种当前报价 {symbol, bid, ask}
    - GET：前端查询指定 symbol 最近一次报价 /api/mt4_quote?symbol=XAUUSD
    """
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        symbol = (data.get('symbol') or '').upper()
        bid = data.get('bid')
        ask = data.get('ask')
        if not symbol or bid is None or ask is None:
            return jsonify({'status': 'error', 'message': 'symbol, bid, ask required'}), 400

        # 简单 upsert：先尝试更新，没有则插入
        existing = query_db('SELECT id FROM mt4_quotes WHERE symbol = ?', (symbol,), one=True)
        now_ts = time.time()
        if existing:
            execute_db(
                'UPDATE mt4_quotes SET bid = ?, ask = ?, updated_at = ? WHERE id = ?',
                (bid, ask, now_ts, existing['id'])
            )
        else:
            execute_db(
                'INSERT INTO mt4_quotes (symbol, bid, ask, updated_at) VALUES (?, ?, ?, ?)',
                (symbol, bid, ask, now_ts)
            )
        return jsonify({'status': 'success'})

    # GET 查询
    symbol = (request.args.get('symbol') or '').upper()
    if not symbol:
        return jsonify({'status': 'error', 'message': 'symbol required'}), 400

    row = query_db(
        'SELECT symbol, bid, ask, updated_at FROM mt4_quotes WHERE symbol = ? ORDER BY updated_at DESC LIMIT 1',
        (symbol,),
        one=True
    )
    if not row:
        return jsonify({'status': 'empty'})

    return jsonify({
        'status': 'success',
        'data': {
            'symbol': row['symbol'],
            'bid': row['bid'],
            'ask': row['ask'],
            'updated_at': row['updated_at']
        }
    })


# ===== 新版前端监控面板（含 5 个选项卡 + 交易时段黑屏机制） =====

@app.route('/')
def dashboard():
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)

    # 是否在交易时间
    trading_time_flag = is_trading_time_now()

    # 今日时间范围
    today_start = tz.localize(datetime(now.year, now.month, now.day, 0, 0, 0)).timestamp()
    today_end = tz.localize(datetime(now.year, now.month, now.day, 23, 59, 59)).timestamp()

    # 当日交易统计（用于顶部“今日净收益 / 百分比”等）
    today_trades_row = query_db(
        'SELECT COUNT(*) AS count FROM results WHERE exit_time BETWEEN ? AND ?',
        (today_start, today_end),
        one=True
    )
    today_trades = today_trades_row['count'] if today_trades_row else 0

    today_wins_row = query_db(
        "SELECT COUNT(*) AS count FROM results WHERE result = '盈利' AND exit_time BETWEEN ? AND ?",
        (today_start, today_end),
        one=True
    )
    today_wins = today_wins_row['count'] if today_wins_row else 0

    today_losses = today_trades - today_wins if today_trades > 0 else 0

    today_net_profit = today_wins - today_losses * 1.25  # 占位：净盈利单数
    today_profit_pct = (today_net_profit / today_trades * 100) if today_trades > 0 else 0.0

    # 当前“持仓”（未处理信号）
    open_positions = query_db(
        'SELECT * FROM signals WHERE processed = FALSE ORDER BY entry_time DESC LIMIT 200'
    )

    # 历史交易（已完成）
    history_trades = query_db(
        'SELECT * FROM results ORDER BY exit_time DESC LIMIT 200'
    )

    # 账号汇总（占位逻辑）
    summary = {
        'equity': BASE_CAPITAL,
        'today_net_profit': today_net_profit,
        'today_profit_pct': today_profit_pct,
        'open_pnl': 0.0,         # 占位：当前持仓总损益，后续可从实盘计算
        'leverage_pct': 0.0      # 占位：杠杆百分比
    }

    current_time_str = now.strftime('%Y-%m-%d %H:%M:%S')

    return render_template_string('''
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>交易数据监控面板</title>
        <style>
            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }
            body {
                font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans","PingFang SC","Microsoft YaHei",sans-serif;
                background: #0f172a;
                color: #e5e7eb;
                padding: 24px;
                transition: background-color 0.4s ease, color 0.4s ease;
            }
            /* 非交易时段：背景黑 + 字体黑，实现“整体隐藏”效果 */
            body.after-hours {
                background: #000000;
                color: #000000;
            }
            body.after-hours * {
                color: #000000 !important;
                border-color: #000000 !important;
                box-shadow: none !important;
            }
            .container {
                max-width: 1320px;
                margin: 0 auto;
            }
            .header {
                margin-bottom: 20px;
            }
            .header-title {
                display: flex;
                justify-content: space-between;
                align-items: baseline;
            }
            .header-title h1 {
                font-size: 24px;
                font-weight: 600;
                letter-spacing: 0.05em;
            }
            .header-sub {
                margin-top: 8px;
                font-size: 14px;
                color: #9ca3af;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .header-time {
                font-variant-numeric: tabular-nums;
            }
            .summary-grid {
                display: grid;
                grid-template-columns: repeat(5, minmax(0, 1fr));
                gap: 16px;
                margin-top: 16px;
            }
            .summary-card {
                background: radial-gradient(circle at top left, #1d4ed8 0, #020617 55%);
                border-radius: 14px;
                padding: 14px 16px;
                box-shadow: 0 18px 40px rgba(15,23,42,0.8);
                border: 1px solid rgba(148,163,184,0.25);
                position: relative;
                overflow: hidden;
            }
            .summary-card:nth-child(2) {
                background: radial-gradient(circle at top left, #059669 0, #020617 55%);
            }
            .summary-card:nth-child(3) {
                background: radial-gradient(circle at top left, #a855f7 0, #020617 55%);
            }
            .summary-card:nth-child(4) {
                background: radial-gradient(circle at top left, #f97316 0, #020617 55%);
            }
            .summary-card:nth-child(5) {
                background: radial-gradient(circle at top left, #e11d48 0, #020617 55%);
            }
            .summary-label {
                font-size: 12px;
                color: #9ca3af;
                margin-bottom: 6px;
            }
            .summary-value {
                font-size: 20px;
                font-weight: 600;
                margin-bottom: 4px;
                font-variant-numeric: tabular-nums;
            }
            .summary-sub {
                font-size: 12px;
                color: #9ca3af;
            }
            .summary-positive {
                color: #4ade80;
            }
            .summary-negative {
                color: #f97373;
            }
            .tab-bar {
                margin-top: 24px;
                display: flex;
                justify-content: space-between;
                align-items: flex-end;
            }
            .tabs {
                display: flex;
                gap: 8px;
            }
            .tab {
                padding: 8px 16px;
                border-radius: 999px;
                border: 1px solid transparent;
                background: transparent;
                color: #9ca3af;
                font-size: 14px;
                cursor: pointer;
                transition: all 0.2s ease;
            }
            .tab:hover {
                background: rgba(148,163,184,0.08);
            }
            .tab.active {
                background: #e5e7eb;
                color: #020617;
                border-color: transparent;
                box-shadow: 0 8px 24px rgba(15,23,42,0.55);
            }
            .tab-underline {
                height: 1px;
                background: linear-gradient(to right, transparent, rgba(156,163,175,0.4), transparent);
                margin-top: 8px;
            }
            .content {
                margin-top: 20px;
                background: rgba(15,23,42,0.9);
                border-radius: 18px;
                padding: 18px 20px;
                border: 1px solid rgba(55,65,81,0.7);
                box-shadow: 0 24px 60px rgba(15,23,42,0.9);
                min-height: 420px;
            }
            .tab-content {
                display: none;
                height: 100%;
            }
            .tab-content.active {
                display: block;
            }

            /* 做多 / 做空 + 仓位设置 */
            .order-layout {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 20px;
            }
            .order-card {
                background: radial-gradient(circle at top left, rgba(37,99,235,0.16) 0, rgba(15,23,42,1) 55%);
                border-radius: 16px;
                padding: 18px;
                border: 1px solid rgba(55,65,81,0.9);
            }
            .order-card.short {
                background: radial-gradient(circle at top left, rgba(239,68,68,0.18) 0, rgba(15,23,42,1) 55%);
            }
            .order-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 12px;
            }
            .order-title {
                font-size: 18px;
                font-weight: 600;
            }
            .badge {
                font-size: 11px;
                padding: 2px 8px;
                border-radius: 999px;
                border: 1px solid rgba(148,163,184,0.5);
                color: #9ca3af;
            }
            .quote-status {
                font-size: 11px;
                padding: 2px 8px;
                border-radius: 999px;
                border: 1px solid rgba(248,113,113,0.8);
                color: #fecaca;
                margin-left: 8px;
            }
            .quote-status.ready {
                border-color: rgba(34,197,94,0.9);
                color: #bbf7d0;
            }
            .position-control {
                margin-top: 8px;
                padding-top: 10px;
                border-top: 1px dashed rgba(55,65,81,0.8);
            }
            .position-row {
                display: flex;
                align-items: center;
                gap: 8px;
                margin-bottom: 10px;
            }
            .position-label {
                font-size: 13px;
                color: #9ca3af;
                width: 70px;
            }
            .position-input {
                flex: 1;
                display: flex;
                gap: 8px;
                align-items: center;
            }
            .position-input input[type="number"] {
                width: 90px;
                padding: 6px 8px;
                border-radius: 8px;
                border: 1px solid rgba(55,65,81,0.9);
                background: rgba(15,23,42,0.9);
                color: #e5e7eb;
                font-size: 13px;
            }
            .position-input input[type="range"] {
                flex: 1;
            }
            .small-input {
                width: 120px;
            }
            .order-actions {
                display: flex;
                justify-content: flex-end;
                gap: 10px;
                margin-top: 12px;
            }
            .btn {
                padding: 8px 14px;
                border-radius: 999px;
                border: 1px solid transparent;
                font-size: 13px;
                cursor: pointer;
                transition: all 0.2s ease;
            }
            .btn-outline {
                background: transparent;
                border-color: rgba(156,163,175,0.6);
                color: #e5e7eb;
            }
            .btn-outline:hover {
                background: rgba(148,163,184,0.08);
            }
            .btn-primary {
                background: linear-gradient(to right, #22c55e, #22d3ee);
                color: #020617;
                box-shadow: 0 8px 24px rgba(34,197,94,0.55);
            }
            .btn-primary.short {
                background: linear-gradient(to right, #fb7185, #f97316);
                box-shadow: 0 8px 24px rgba(239,68,68,0.55);
            }
            .btn-primary:hover {
                filter: brightness(1.03);
            }

            /* 仓位设置 Tab */
            .position-settings {
                max-width: 520px;
            }
            .field {
                margin-bottom: 14px;
            }
            .field label {
                display: block;
                font-size: 13px;
                margin-bottom: 4px;
                color: #9ca3af;
            }
            .field input, .field select {
                width: 100%;
                padding: 8px 10px;
                border-radius: 10px;
                border: 1px solid rgba(55,65,81,0.9);
                background: rgba(15,23,42,0.9);
                color: #e5e7eb;
                font-size: 13px;
            }
            .field-hint {
                font-size: 12px;
                margin-top: 3px;
                color: #6b7280;
            }

            /* 列表公用样式（当前持仓 / 历史交易） */
            .list-container {
                height: 100%;
                display: flex;
                flex-direction: column;
            }
            .list-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 8px;
            }
            .list-header-title {
                font-size: 16px;
                font-weight: 500;
            }
            .list-header-meta {
                font-size: 12px;
                color: #9ca3af;
            }
            .table-wrapper {
                margin-top: 6px;
                border-radius: 12px;
                overflow: hidden;
                border: 1px solid rgba(55,65,81,0.8);
                background: radial-gradient(circle at top left, rgba(15,23,42,1) 0, rgba(3,7,18,1) 100%);
                flex: 1;
                display: flex;
                flex-direction: column;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                font-size: 13px;
            }
            thead {
                background: rgba(15,23,42,0.95);
            }
            th, td {
                padding: 8px 10px;
                text-align: left;
                border-bottom: 1px solid rgba(31,41,55,0.9);
                vertical-align: middle;
            }
            th {
                font-weight: 500;
                color: #9ca3af;
                position: sticky;
                top: 0;
                backdrop-filter: blur(10px);
                z-index: 2;
            }
            .scroll-body {
                overflow-y: auto;
                max-height: 320px;
            }
            tr.data-row {
                cursor: pointer;
                transition: background-color 0.12s ease;
            }
            tr.data-row:hover {
                background: rgba(31,41,55,0.9);
            }
            .pill {
                padding: 2px 8px;
                border-radius: 999px;
                font-size: 11px;
                display: inline-block;
            }
            .pill-long {
                background: rgba(22,163,74,0.14);
                color: #4ade80;
            }
            .pill-short {
                background: rgba(248,113,113,0.16);
                color: #fb7185;
            }
            .pill-win {
                background: rgba(34,197,94,0.16);
                color: #4ade80;
            }
            .pill-loss {
                background: rgba(248,113,113,0.16);
                color: #fecaca;
            }
            .detail-row {
                background: rgba(15,23,42,0.96);
            }
            .detail-cell {
                padding: 10px 16px 12px;
                border-top: 1px solid rgba(31,41,55,0.8);
            }
            .detail-grid {
                display: grid;
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 8px 16px;
                font-size: 12px;
                color: #9ca3af;
            }
            .detail-item-label {
                color: #6b7280;
                margin-right: 4px;
            }
            .detail-raw {
                margin-top: 8px;
                padding-top: 6px;
                border-top: 1px dashed rgba(55,65,81,0.7);
                font-family: SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;
                font-size: 11px;
                color: #6b7280;
                word-break: break-all;
            }

            @media (max-width: 960px) {
                .summary-grid {
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }
                .order-layout {
                    grid-template-columns: 1fr;
                }
                .detail-grid {
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }
            }
            @media (max-width: 640px) {
                .summary-grid {
                    grid-template-columns: 1fr;
                }
                .detail-grid {
                    grid-template-columns: 1fr;
                }
                .tab-bar {
                    flex-direction: column;
                    align-items: flex-start;
                    gap: 8px;
                }
            }
        </style>
    </head>
    <body class="{{ '' if trading_time_flag else 'after-hours' }}">
        <div class="container">
            <div class="header">
                <div class="header-title">
                    <h1>交易数据监控面板</h1>
                </div>
                <div class="header-sub">
                    <div>实盘信号中介 · Web 控制台</div>
                    <div class="header-time" id="headerTime">{{ current_time }}</div>
                </div>
                <div class="summary-grid">
                    <div class="summary-card">
                        <div class="summary-label">当前资金总额度</div>
                        <div class="summary-value">¥ {{ '%.2f'|format(summary.equity) }}</div>
                        <div class="summary-sub">基于配置 BASE_CAPITAL</div>
                    </div>
                    <div class="summary-card">
                        <div class="summary-label">今日净收益</div>
                        <div class="summary-value {{ 'summary-positive' if summary.today_net_profit >= 0 else 'summary-negative' }}">
                            {{ '%.2f'|format(summary.today_net_profit) }}
                        </div>
                        <div class="summary-sub">单位为“净盈利单数”占位逻辑</div>
                    </div>
                    <div class="summary-card">
                        <div class="summary-label">今日收益百分比</div>
                        <div class="summary-value {{ 'summary-positive' if summary.today_profit_pct >= 0 else 'summary-negative' }}">
                            {{ '%.2f'|format(summary.today_profit_pct) }}%
                        </div>
                        <div class="summary-sub">基于当日交易胜负结果估算</div>
                    </div>
                    <div class="summary-card">
                        <div class="summary-label">当前持仓总损益</div>
                        <div class="summary-value">0.00</div>
                        <div class="summary-sub">预留：后续接入实盘实时 PnL</div>
                    </div>
                    <div class="summary-card">
                        <div class="summary-label">杠杆使用率</div>
                        <div class="summary-value">0.00%</div>
                        <div class="summary-sub">预留：后续接入账户杠杆信息</div>
                    </div>
                </div>
            </div>

            <div class="tab-bar">
                <div class="tabs">
                    <button class="tab active" data-tab="long">做多</button>
                    <button class="tab" data-tab="short">做空</button>
                    <button class="tab" data-tab="settings">仓位设置</button>
                    <button class="tab" data-tab="open">当前持仓</button>
                    <button class="tab" data-tab="history">历史交易记录</button>
                </div>
            </div>
            <div class="tab-underline"></div>

            <div class="content">
                <!-- 选项1/2：做多 / 做空（中间内嵌仓位与限价、点差、止盈止损设置） -->
                <div class="tab-content active" id="tab-long">
                    <div class="order-layout">
                        <div class="order-card">
                            <div class="order-header">
                                <div class="order-title">做多开仓</div>
                                <div>
                                    <span class="badge">信号来源：实盘系统 / API</span>
                                    <span id="longQuoteStatus" class="quote-status">未确认报价</span>
                                </div>
                            </div>
                            <div class="position-control">
                                <div class="position-row">
                                    <div class="position-label">交易品种</div>
                                    <div class="position-input">
                                        <input id="longSymbol" type="text" class="small-input" value="{{ open_positions[0]['symbol'] if open_positions else 'XAUUSD' }}"
                                               onkeyup="if(event.key==='Enter'){requestQuote('long');}">
                                        <button class="btn btn-outline" style="padding:4px 10px;font-size:11px;"
                                                type="button" onclick="requestQuote('long')">
                                            报价
                                        </button>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">订单类型</div>
                                    <div class="position-input">
                                        <select id="longOrderType" class="small-input" onchange="onOrderTypeChange('long')">
                                            <option value="MARKET">市价</option>
                                            <option value="LIMIT">限价</option>
                                        </select>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">委托价格</div>
                                    <div class="position-input">
                                        <input id="longLimitPrice" type="number" step="0.01" class="small-input" placeholder="仅限价单生效" disabled>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">允许点差</div>
                                    <div class="position-input">
                                        <input id="longMaxSlippage" type="number" min="0" step="0.1" class="small-input" value="2.0">
                                        <span style="font-size:12px;color:#6b7280;">点</span>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">仓位比例</div>
                                    <div class="position-input">
                                        <input id="longPositionPct" type="range" min="0" max="100" value="20" step="1"
                                               oninput="syncSlider('long')">
                                        <input id="longPositionInput" type="number" min="0" max="100" value="20"
                                               oninput="syncInput('long')">
                                        <span style="font-size:12px;color:#6b7280;">%</span>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">预估名义</div>
                                    <div class="position-input">
                                        <span id="longNotional" style="font-size:13px;color:#e5e7eb;">—</span>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">止盈价</div>
                                    <div class="position-input">
                                        <input id="longTp" type="number" step="0.01" class="small-input" placeholder="可选">
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">止损价</div>
                                    <div class="position-input">
                                        <input id="longSl" type="number" step="0.01" class="small-input" placeholder="可选">
                                    </div>
                                </div>
                            </div>
                            <div class="order-actions">
                                <button class="btn btn-outline" onclick="previewOrder('long')">模拟预览</button>
                                <button class="btn btn-primary" onclick="submitOrder('long')">发送做多指令</button>
                            </div>
                        </div>
                        <div class="order-card short">
                            <div class="order-header">
                                <div class="order-title">做空开仓</div>
                                <div>
                                    <span class="badge">信号来源：实盘系统 / API</span>
                                    <span id="shortQuoteStatus" class="quote-status">未确认报价</span>
                                </div>
                            </div>
                            <div class="position-control">
                                <div class="position-row">
                                    <div class="position-label">交易品种</div>
                                    <div class="position-input">
                                        <input id="shortSymbol" type="text" class="small-input" value="{{ open_positions[0]['symbol'] if open_positions else 'XAUUSD' }}"
                                               onkeyup="if(event.key==='Enter'){requestQuote('short');}">
                                        <button class="btn btn-outline" style="padding:4px 10px;font-size:11px;"
                                                type="button" onclick="requestQuote('short')">
                                            报价
                                        </button>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">订单类型</div>
                                    <div class="position-input">
                                        <select id="shortOrderType" class="small-input" onchange="onOrderTypeChange('short')">
                                            <option value="MARKET">市价</option>
                                            <option value="LIMIT">限价</option>
                                        </select>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">委托价格</div>
                                    <div class="position-input">
                                        <input id="shortLimitPrice" type="number" step="0.01" class="small-input" placeholder="仅限价单生效" disabled>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">允许点差</div>
                                    <div class="position-input">
                                        <input id="shortMaxSlippage" type="number" min="0" step="0.1" class="small-input" value="2.0">
                                        <span style="font-size:12px;color:#e5e7eb;">点</span>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">仓位比例</div>
                                    <div class="position-input">
                                        <input id="shortPositionPct" type="range" min="0" max="100" value="20" step="1"
                                               oninput="syncSlider('short')">
                                        <input id="shortPositionInput" type="number" min="0" max="100" value="20"
                                               oninput="syncInput('short')">
                                        <span style="font-size:12px;color:#e5e7eb;">%</span>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">预估名义</div>
                                    <div class="position-input">
                                        <span id="shortNotional" style="font-size:13px;color:#e5e7eb;">—</span>
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">止盈价</div>
                                    <div class="position-input">
                                        <input id="shortTp" type="number" step="0.01" class="small-input" placeholder="可选">
                                    </div>
                                </div>
                                <div class="position-row">
                                    <div class="position-label">止损价</div>
                                    <div class="position-input">
                                        <input id="shortSl" type="number" step="0.01" class="small-input" placeholder="可选">
                                    </div>
                                </div>
                            </div>
                            <div class="order-actions">
                                <button class="btn btn-outline" onclick="previewOrder('short')">模拟预览</button>
                                <button class="btn btn-primary short" onclick="submitOrder('short')">发送做空指令</button>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 选项2 Tab：说明（做空主面板已与做多并排展示） -->
                <div class="tab-content" id="tab-short">
                    <p style="font-size:13px;color:#9ca3af;">做空指令面板与“做多”共享一套仓位逻辑，已在左侧 Tab 中并排展示。此处可按需扩展独立策略配置。</p>
                </div>

                <!-- 选项3：仓位设置 -->
                <div class="tab-content" id="tab-settings">
                    <div class="position-settings">
                        <div class="list-header">
                            <div class="list-header-title">仓位与风险参数</div>
                            <div class="list-header-meta">仅前端配置占位，预留 JSON 读写接口</div>
                        </div>

                        <div class="field">
                            <label>基础资金规模 (¥)</label>
                            <input id="baseCapitalInput" type="number" min="0" value="{{ '%.2f'|format(summary.equity) }}">
                            <div class="field-hint">占位字段：保存后可用于计算预估名义 / 风险敞口。</div>
                        </div>

                        <div class="field">
                            <label>单笔最大风险比例 (%)</label>
                            <input id="maxRiskInput" type="number" min="0" max="100" value="1.0">
                            <div class="field-hint">例如 1%，表示单笔最大亏损不超过总资金的 1%。</div>
                        </div>

                        <div class="field">
                            <label>默认杠杆倍数</label>
                            <input id="defaultLeverageInput" type="number" min="1" value="1">
                            <div class="field-hint">此处仅为前端展示与记录，真实杠杆由实盘系统控制。</div>
                        </div>

                        <div class="field">
                            <label>通知与风控行为</label>
                            <select>
                                <option>仅记录，不自动平仓</option>
                                <option>触及风险阈值时提醒</option>
                                <option>触及风险阈值时建议减仓</option>
                            </select>
                        </div>

                        <button class="btn btn-primary" onclick="saveSettings()">保存配置（占位）</button>
                    </div>
                </div>

                <!-- 选项4：当前持仓 -->
                <div class="tab-content" id="tab-open">
                    <div class="list-container">
                        <div class="list-header">
                            <div class="list-header-title">当前持仓列表</div>
                            <div class="list-header-meta">
                                来源：未处理信号（signals.processed = FALSE）
                                &nbsp;|&nbsp;
                                <a href="javascript:void(0)" style="color:#38bdf8;text-decoration:none;" onclick="lockAllPositions()">
                                    一键锁定全部（MT4 端要求已设置止盈止损才会锁仓）
                                </a>
                            </div>
                        </div>
                        <div class="table-wrapper">
                            <table>
                                <thead>
                                    <tr>
                                        <th>持仓ID</th>
                                        <th>方向</th>
                                        <th>入场时间</th>
                                        <th>入场价</th>
                                        <th>预测(分)</th>
                                        <th>来源IP</th>
                                        <th>操作</th>
                                    </tr>
                                </thead>
                            </table>
                            <div class="scroll-body">
                                <table>
                                    <tbody>
                                    {% for pos in open_positions %}
                                        <tr class="data-row" onclick="toggleDetail('open', {{ loop.index0 }})">
                                            <td>{{ pos['trade_id'] }}</td>
                                            <td>
                                                <span class="pill {{ 'pill-long' if pos['direction'] == '做多' else 'pill-short' }}">
                                                    {{ pos['direction'] }}
                                                </span>
                                            </td>
                                            <td>{{ pos['entry_time']|datetime }}</td>
                                            <td>{{ '%.2f'|format(pos['entry_price']) }}</td>
                                            <td>{{ pos['prediction_minutes'] }}</td>
                                            <td>{{ pos['client_ip'] }}</td>
                                            <td>
                                                <button class="btn btn-outline" style="padding:4px 10px;font-size:11px;"
                                                        onclick="event.stopPropagation();lockOnePosition('{{ pos['trade_id'] }}')">
                                                    锁仓
                                                </button>
                                            </td>
                                        </tr>
                                        <tr id="open-detail-{{ loop.index0 }}" class="detail-row" style="display:none;">
                                            <td colspan="6" class="detail-cell">
                                                <div class="detail-grid">
                                                    <div><span class="detail-item-label">Trade ID:</span>{{ pos['trade_id'] }}</div>
                                                    <div><span class="detail-item-label">Symbol:</span>{{ pos['symbol'] }}</div>
                                                    <div><span class="detail-item-label">方向:</span>{{ pos['direction'] }}</div>
                                                    <div><span class="detail-item-label">预测时长:</span>{{ pos['prediction_minutes'] }} 分钟</div>
                                                    <div><span class="detail-item-label">入场时间戳:</span>{{ pos['entry_time'] }}</div>
                                                    <div><span class="detail-item-label">接收时间戳:</span>{{ pos['received_at'] }}</div>
                                                    <div><span class="detail-item-label">处理状态:</span>{{ '未处理' if not pos['processed'] else '已处理' }}</div>
                                                    <div><span class="detail-item-label">客户端 IP:</span>{{ pos['client_ip'] }}</div>
                                                </div>
                                                <div class="detail-raw">
                                                    原始数据(raw_data): {{ pos['raw_data'] }}
                                                </div>
                                            </td>
                                        </tr>
                                    {% else %}
                                        <tr><td colspan="6" style="padding:16px 12px;color:#6b7280;">暂无未平仓持仓</td></tr>
                                    {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 选项5：历史交易记录 -->
                <div class="tab-content" id="tab-history">
                    <div class="list-container">
                        <div class="list-header">
                            <div class="list-header-title">历史交易记录</div>
                            <div class="list-header-meta">来源：results 表（最近 200 条）</div>
                        </div>
                        <div class="table-wrapper">
                            <table>
                                <thead>
                                    <tr>
                                        <th>交易ID</th>
                                        <th>方向</th>
                                        <th>结果</th>
                                        <th>入场价</th>
                                        <th>出场价</th>
                                        <th>预测(分)</th>
                                    </tr>
                                </thead>
                            </table>
                            <div class="scroll-body">
                                <table>
                                    <tbody>
                                    {% for res in history_trades %}
                                        <tr class="data-row" onclick="toggleDetail('history', {{ loop.index0 }})">
                                            <td>{{ res['trade_id'] }}</td>
                                            <td>
                                                <span class="pill {{ 'pill-long' if res['direction'] == '做多' else 'pill-short' }}">
                                                    {{ res['direction'] }}
                                                </span>
                                            </td>
                                            <td>
                                                <span class="pill {{ 'pill-win' if res['result'] == '盈利' else 'pill-loss' }}">
                                                    {{ res['result'] }}
                                                </span>
                                            </td>
                                            <td>{{ '%.2f'|format(res['entry_price']) }}</td>
                                            <td>{{ '%.2f'|format(res['exit_price']) }}</td>
                                            <td>{{ res['prediction_minutes'] }}</td>
                                        </tr>
                                        <tr id="history-detail-{{ loop.index0 }}" class="detail-row" style="display:none;">
                                            <td colspan="6" class="detail-cell">
                                                <div class="detail-grid">
                                                    <div><span class="detail-item-label">Trade ID:</span>{{ res['trade_id'] }}</div>
                                                    <div><span class="detail-item-label">Symbol:</span>{{ res['symbol'] }}</div>
                                                    <div><span class="detail-item-label">方向:</span>{{ res['direction'] }}</div>
                                                    <div><span class="detail-item-label">结果:</span>{{ res['result'] }}</div>
                                                    <div><span class="detail-item-label">入场时间戳:</span>{{ res['entry_time'] }}</div>
                                                    <div><span class="detail-item-label">出场时间戳:</span>{{ res['exit_time'] }}</div>
                                                    <div><span class="detail-item-label">预测时长:</span>{{ res['prediction_minutes'] }} 分钟</div>
                                                    <div><span class="detail-item-label">接收时间戳:</span>{{ res['received_at'] }}</div>
                                                </div>
                                                <div class="detail-raw">
                                                    原始数据(raw_data): {{ res['raw_data'] }}
                                                </div>
                                            </td>
                                        </tr>
                                    {% else %}
                                        <tr><td colspan="6" style="padding:16px 12px;color:#6b7280;">暂无历史记录</td></tr>
                                    {% endfor %}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>

            </div> <!-- /content -->
        </div> <!-- /container -->

        <script>
            // Tab 切换
            const tabs = document.querySelectorAll('.tab');
            const tabContents = document.querySelectorAll('.tab-content');

            tabs.forEach(tab => {
                tab.addEventListener('click', () => {
                    const target = tab.dataset.tab;
                    tabs.forEach(t => t.classList.remove('active'));
                    tabContents.forEach(c => c.classList.remove('active'));

                    tab.classList.add('active');
                    const el = document.getElementById('tab-' + target);
                    if (el) el.classList.add('active');
                });
            });

            // 展开 / 收起明细
            function toggleDetail(type, idx) {
                const id = type + '-detail-' + idx;
                const row = document.getElementById(id);
                if (!row) return;
                row.style.display = row.style.display === 'none' || row.style.display === '' ? 'table-row' : 'none';
            }

            // 顶部时间本地刷新
            function startClock() {
                const el = document.getElementById('headerTime');
                if (!el) return;
                setInterval(() => {
                    const now = new Date();
                    const y = now.getFullYear();
                    const m = String(now.getMonth() + 1).padStart(2, '0');
                    const d = String(now.getDate()).padStart(2, '0');
                    const hh = String(now.getHours()).padStart(2, '0');
                    const mm = String(now.getMinutes()).padStart(2, '0');
                    const ss = String(now.getSeconds()).padStart(2, '0');
                    el.textContent = `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
                }, 1000);
            }

            // 仓位滑块联动 & 预估名义（基于 BASE_CAPITAL 占位）
            const BASE_CAPITAL_JS = {{ '%.2f'|format(summary.equity) }};

            function syncSlider(side) {
                const slider = document.getElementById(side + 'PositionPct');
                const input = document.getElementById(side + 'PositionInput');
                const value = Number(slider.value);
                input.value = value;
                updateNotional(side, value);
            }

            function syncInput(side) {
                const slider = document.getElementById(side + 'PositionPct');
                const input = document.getElementById(side + 'PositionInput');
                let value = Number(input.value);
                if (isNaN(value)) value = 0;
                value = Math.min(100, Math.max(0, value));
                input.value = value;
                slider.value = value;
                updateNotional(side, value);
            }

            function updateNotional(side, pct) {
                const notionalEl = document.getElementById(side + 'Notional');
                if (!notionalEl) return;
                const notional = BASE_CAPITAL_JS * pct / 100;
                notionalEl.textContent = '≈ ¥ ' + notional.toFixed(2);
            }

            function onOrderTypeChange(side) {
                const typeEl = document.getElementById(side + 'OrderType');
                const priceEl = document.getElementById(side + 'LimitPrice');
                if (!typeEl || !priceEl) return;
                if (typeEl.value === 'LIMIT') {
                    priceEl.disabled = false;
                } else {
                    priceEl.disabled = true;
                    priceEl.value = '';
                }
            }

            function collectOrderParams(side) {
                const symbolEl   = document.getElementById(side + 'Symbol');
                const typeEl     = document.getElementById(side + 'OrderType');
                const limitEl    = document.getElementById(side + 'LimitPrice');
                const slipEl     = document.getElementById(side + 'MaxSlippage');
                const pctEl      = document.getElementById(side + 'PositionInput');
                const tpEl       = document.getElementById(side + 'Tp');
                const slEl       = document.getElementById(side + 'Sl');

                const symbol   = symbolEl ? symbolEl.value.trim() : '';
                const orderType = typeEl ? typeEl.value : 'MARKET';
                const limitPrice = limitEl && limitEl.value !== '' ? Number(limitEl.value) : 0;
                const maxSlip    = slipEl && slipEl.value !== '' ? Number(slipEl.value) : 0;
                const pct        = pctEl && pctEl.value !== '' ? Number(pctEl.value) : 0;
                const tp         = tpEl && tpEl.value !== '' ? Number(tpEl.value) : 0;
                const sl         = slEl && slEl.value !== '' ? Number(slEl.value) : 0;

                return {
                    symbol: symbol || 'XAUUSD',
                    side: side === 'long' ? 'BUY' : 'SELL',
                    order_type: orderType,
                    limit_price: limitPrice,
                    max_slippage: maxSlip,
                    position_pct: pct,
                    tp: tp,
                    sl: sl
                };
            }

            function previewOrder(side) {
                const p = collectOrderParams(side);
                console.log('预览下单参数:', p);
                alert('预览 ' + (p.side === 'BUY' ? '做多' : '做空') +
                      '\\n品种: ' + p.symbol +
                      '\\n订单类型: ' + (p.order_type === 'MARKET' ? '市价' : '限价') +
                      (p.order_type === 'LIMIT' ? '\\n委托价: ' + p.limit_price : '') +
                      '\\n仓位比例: ' + p.position_pct + '%' +
                      '\\n允许点差: ' + p.max_slippage + ' 点' +
                      '\\n止盈: ' + (p.tp || '未设置') +
                      '\\n止损: ' + (p.sl || '未设置'));
            }

            function submitOrder(side) {
                const p = collectOrderParams(side);
                if (p.position_pct <= 0) {
                    alert('请先设置仓位比例');
                    return;
                }
                if (p.order_type === 'LIMIT' && p.limit_price <= 0) {
                    alert('限价单需要填写有效的委托价格');
                    return;
                }

                const payload = {
                    action: 'OPEN',
                    side: p.side,
                    symbol: p.symbol,
                    order_type: p.order_type,     // MARKET / LIMIT
                    limit_price: p.limit_price,   // 0 表示市价
                    max_slippage: p.max_slippage, // 允许点差（点）
                    position_pct: p.position_pct,
                    tp: p.tp,
                    sl: p.sl,
                    source: 'web_panel'
                };

                fetch('/api/mt4_commands', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
                .then(r => r.json())
                .then(d => {
                    if (d.status === 'success') {
                        alert('已发送至 MT4 指令队列，Command ID: ' + d.command_id);
                    } else {
                        alert('发送指令失败: ' + (d.message || '未知错误'));
                    }
                })
                .catch(err => {
                    alert('网络错误: ' + err);
                });
            }

            function saveSettings() {
                const baseCapital = Number(document.getElementById('baseCapitalInput').value) || 0;
                const maxRisk = Number(document.getElementById('maxRiskInput').value) || 0;
                const leverage = Number(document.getElementById('defaultLeverageInput').value) || 1;

                const payload = {
                    base_capital: baseCapital,
                    max_risk_pct: maxRisk,
                    default_leverage: leverage
                    // TODO: 这里可以预留一个 /api/settings JSON 接口进行持久化
                };
                console.log('预留仓位配置 JSON payload:', payload);
                alert('当前为前端占位保存逻辑，可根据需要接入后端 /api/settings 保存配置。');
            }

            // 预留：统一管理 JSON 接口地址，后续可直接用 fetch 调用
            const API_ENDPOINTS = {
                summary: '/api/summary',
                openPositions: '/api/open_positions',
                history: '/api/history',
                mt4Commands: '/api/mt4_commands'
            };

            // 锁仓相关：单笔 / 全部（MT4 端再校验是否有止盈止损）
            function lockOnePosition(tradeId) {
                const payload = {
                    action: 'LOCK_ONE',
                    trade_id: tradeId,
                    source: 'web_panel'
                };
                fetch(API_ENDPOINTS.mt4Commands, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
                .then(r => r.json())
                .then(d => {
                    if (d.status === 'success') {
                        alert('已发送单笔锁仓指令，Trade ID: ' + tradeId);
                    } else {
                        alert('锁仓指令发送失败: ' + (d.message || '未知错误'));
                    }
                })
                .catch(err => alert('网络错误: ' + err));
            }

            function lockAllPositions() {
                if (!confirm('确认一键锁定所有符合条件的持仓？（MT4 端仅会锁定已设置止盈止损的仓位）')) {
                    return;
                }
                const payload = {
                    action: 'LOCK_ALL',
                    source: 'web_panel'
                };
                fetch(API_ENDPOINTS.mt4Commands, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
                .then(r => r.json())
                .then(d => {
                    if (d.status === 'success') {
                        alert('已发送一键锁仓指令');
                    } else {
                        alert('一键锁仓发送失败: ' + (d.message || '未知错误'));
                    }
                })
                .catch(err => alert('网络错误: ' + err));
            }

            // 请求 MT4 即时报价：发送 QUOTE_REQUEST 指令，然后轮询 /api/mt4_quote
            function requestQuote(side) {
                const symbolEl = document.getElementById(side + 'Symbol');
                const statusEl = document.getElementById(side + 'QuoteStatus');
                if (!symbolEl || !statusEl) return;
                const symbol = (symbolEl.value || '').trim().toUpperCase();
                if (!symbol) {
                    alert('请先输入交易品种');
                    return;
                }

                statusEl.classList.remove('ready');
                statusEl.textContent = '等待 MT4 报价...';

                const payload = {
                    action: 'QUOTE_REQUEST',
                    symbol: symbol,
                    source: 'web_panel'
                };
                fetch(API_ENDPOINTS.mt4Commands, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).then(() => {
                    // 轻量轮询报价，最多尝试 10 次
                    let attempts = 0;
                    const timer = setInterval(() => {
                        attempts++;
                        fetch('/api/mt4_quote?symbol=' + encodeURIComponent(symbol))
                          .then(r => r.json())
                          .then(d => {
                              if (d.status === 'success' && d.data) {
                                  clearInterval(timer);
                                  const bid = d.data.bid;
                                  const ask = d.data.ask;
                                  statusEl.classList.add('ready');
                                  statusEl.textContent = symbol + ' 报价 Bid:' +
                                      bid.toFixed(2) + ' / Ask:' + ask.toFixed(2);
                              }
                          })
                          .catch(() => {});
                        if (attempts >= 10) {
                            clearInterval(timer);
                            if (!statusEl.classList.contains('ready')) {
                                statusEl.textContent = '报价超时';
                            }
                        }
                    }, 1000);
                }).catch(err => {
                    statusEl.textContent = '发送报价请求失败';
                    console.error(err);
                });
            }

            startClock();
            // 初始化预估名义
            updateNotional('long', Number(document.getElementById('longPositionInput').value));
            updateNotional('short', Number(document.getElementById('shortPositionInput').value));
        </script>
    </body>
    </html>
    ''',
                                  trading_time_flag=trading_time_flag,
                                  current_time=current_time_str,
                                  summary=summary,
                                  open_positions=open_positions,
                                  history_trades=history_trades)


# ==================== MT4 API 接口 ====================

@app.post("/mt4/status")
def receive_status():
    """接收 MT4 状态上报"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "Empty request"}), 400
        
        # 注册账户名称映射
        if data.get('account_name'):
            command_queue.register_account_name(data['account_name'], data['account'])
        
        # 保存到数据库
        with closing(sqlite3.connect(MT4_DATABASE)) as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO mt4_status_history 
                    (account, ts, balance, equity, margin, margin_level, daily_pnl, daily_return, leverage_used, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    data.get('account'), data.get('ts'), data.get('balance'),
                    data.get('equity'), data.get('margin'), data.get('margin_level'),
                    data.get('daily_pnl'), data.get('daily_return'), data.get('leverage_used'),
                    json.dumps(data, ensure_ascii=False)
                ))
        
        return jsonify({"status": "ok", "ts": int(time.time())})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.post("/mt4/report")
def receive_report():
    """接收 MT4 执行回报"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "Empty request"}), 400
        
        # 保存到数据库
        with closing(sqlite3.connect(MT4_DATABASE)) as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO mt4_reports 
                    (account, cmd_id, ok, action, symbol, ticket, error, message, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    data.get('account'), data.get('cmd_id'), data.get('ok'),
                    data.get('action'), data.get('symbol'), data.get('ticket'),
                    data.get('error', 0), data.get('message', ''), data.get('ts')
                ))
        
        return jsonify({"status": "ok", "ts": int(time.time())})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.post("/mt4/quote")
def receive_quote():
    """接收 MT4 报价上报"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "Empty request"}), 400
        
        quotes = data.get('quotes', {})
        account = data.get('account')
        request_id = data.get('request_id')
        ts = data.get('ts')
        
        # 保存到数据库
        with closing(sqlite3.connect(MT4_DATABASE)) as conn:
            with conn:
                cursor = conn.cursor()
                for symbol, quote_data in quotes.items():
                    cursor.execute('''
                        INSERT INTO mt4_quotes 
                        (account, request_id, symbol, bid, ask, spread_points, ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        account, request_id, symbol,
                        quote_data.get('bid'), quote_data.get('ask'),
                        quote_data.get('spread_points', 0), ts
                    ))
        
        return jsonify({"status": "ok", "ts": int(time.time())})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.get("/mt4/commands")
def get_commands():
    """
    MT4 拉取命令（批量，取走即删除）
    支持 account 参数（账户ID）或 account_name 参数（账户名称）
    """
    try:
        account = request.args.get('account', type=int)
        account_name = request.args.get('account_name', type=str)
        max_count = request.args.get('max', default=50, type=int)
        
        commands = command_queue.get_commands(account, account_name, max_count)
        
        return jsonify({"commands": commands})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.post("/api/command")
def create_command():
    """网页/API 创建命令（下单等）"""
    try:
        cmd_data = request.get_json()
        if not cmd_data:
            return jsonify({"status": "error", "message": "Empty request"}), 400
        
        cmd = command_queue.add_command(cmd_data)
        return jsonify({
            "status": "success",
            "cmd_id": cmd["id"],
            "command": cmd
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/test')
def mt4_test_page():
    """MT4 交易信号测试工具页面"""
    return render_template_string('''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MT4 交易信号测试工具</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            overflow: hidden;
        }

        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }

        .header h1 {
            font-size: 28px;
            margin-bottom: 10px;
        }

        .header p {
            opacity: 0.9;
            font-size: 14px;
        }

        .content {
            padding: 30px;
        }

        .form-group {
            margin-bottom: 20px;
        }

        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #333;
            font-size: 14px;
        }

        .form-group input,
        .form-group select,
        .form-group textarea {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.3s;
        }

        .form-group input:focus,
        .form-group select:focus,
        .form-group textarea:focus {
            outline: none;
            border-color: #667eea;
        }

        .form-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }

        .form-group.required label::after {
            content: " *";
            color: #e74c3c;
        }

        .command-params {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            margin-top: 15px;
            border: 2px dashed #dee2e6;
        }

        .command-params.hidden {
            display: none;
        }

        .command-params h3 {
            color: #667eea;
            margin-bottom: 15px;
            font-size: 16px;
        }

        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 15px 30px;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            transition: transform 0.2s, box-shadow 0.2s;
        }

        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }

        .btn:active {
            transform: translateY(0);
        }

        .btn:disabled {
            background: #ccc;
            cursor: not-allowed;
            transform: none;
        }

        .result {
            margin-top: 25px;
            padding: 20px;
            border-radius: 8px;
            display: none;
        }

        .result.success {
            background: #d4edda;
            border: 2px solid #c3e6cb;
            color: #155724;
            display: block;
        }

        .result.error {
            background: #f8d7da;
            border: 2px solid #f5c6cb;
            color: #721c24;
            display: block;
        }

        .result h3 {
            margin-bottom: 10px;
            font-size: 16px;
        }

        .result pre {
            background: rgba(0,0,0,0.05);
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
            font-size: 12px;
            line-height: 1.5;
            margin-top: 10px;
        }

        .info-box {
            background: #e7f3ff;
            border-left: 4px solid #2196F3;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
            font-size: 13px;
            line-height: 1.6;
        }

        .info-box strong {
            color: #1976D2;
        }

        .symbols-input {
            font-family: monospace;
        }

        .symbols-hint {
            font-size: 12px;
            color: #666;
            margin-top: 5px;
        }

        .loading {
            display: none;
            text-align: center;
            padding: 20px;
        }

        .loading.active {
            display: block;
        }

        .spinner {
            border: 3px solid #f3f3f3;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            width: 30px;
            height: 30px;
            animation: spin 1s linear infinite;
            margin: 0 auto 10px;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .help-text {
            font-size: 12px;
            color: #666;
            margin-top: 5px;
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 MT4 交易信号测试工具</h1>
            <p>向 MT4 Trading EA 发送测试命令</p>
        </div>

        <div class="content">
            <div class="info-box">
                <strong>使用说明：</strong><br>
                1. 输入账户ID（MT4账户号码）<br>
                2. 选择命令类型，填写相应参数<br>
                3. 点击"发送命令"按钮，MT4 EA 将在下次轮询时获取并执行
            </div>

            <form id="commandForm">
                <div class="form-group">
                    <label for="accountId" class="required">账户ID</label>
                    <input type="number" id="accountId" placeholder="例如: 123456" required>
                    <div class="help-text">MT4 账户号码（EA 中 AccountID 参数）</div>
                </div>

                <div class="form-group">
                    <label for="action" class="required">命令类型</label>
                    <select id="action" required>
                        <option value="">-- 请选择命令类型 --</option>
                        <option value="QUOTE">QUOTE - 请求报价</option>
                        <option value="MARKET">MARKET - 市价单</option>
                        <option value="LIMIT">LIMIT - 限价单</option>
                        <option value="CLOSE">CLOSE - 平仓</option>
                        <option value="MODIFY">MODIFY - 修改订单</option>
                    </select>
                </div>

                <!-- QUOTE 参数 -->
                <div id="params-QUOTE" class="command-params hidden">
                    <h3>📊 QUOTE 命令参数</h3>
                    <div class="form-group">
                        <label for="quote_symbols" class="required">交易品种列表</label>
                        <textarea id="quote_symbols" rows="3" placeholder="EURUSD, XAUUSD, GBPUSD" class="symbols-input"></textarea>
                        <div class="symbols-hint">多个品种用逗号分隔，例如：EURUSD, XAUUSD, GBPUSD</div>
                    </div>
                </div>

                <!-- MARKET 参数 -->
                <div id="params-MARKET" class="command-params hidden">
                    <h3>📈 MARKET 命令参数</h3>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="market_symbol" class="required">交易品种</label>
                            <input type="text" id="market_symbol" placeholder="EURUSD" style="text-transform: uppercase;">
                        </div>
                        <div class="form-group">
                            <label for="market_side" class="required">方向</label>
                            <select id="market_side">
                                <option value="BUY">BUY - 买入</option>
                                <option value="SELL">SELL - 卖出</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="market_risk_alloc" class="required">风险分配比例 (%)</label>
                            <input type="number" id="market_risk_alloc" step="0.01" min="0.01" max="100" value="2" placeholder="2">
                            <div class="help-text">开仓占资金比例，例如 2 表示 2%</div>
                        </div>
                        <div class="form-group">
                            <label for="market_max_spread" class="required">最大点差 (points)</label>
                            <input type="number" id="market_max_spread" min="1" value="15" placeholder="15">
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="market_sl_points">止损点数 (points)</label>
                            <input type="number" id="market_sl_points" min="1" value="200" placeholder="200">
                            <div class="help-text">0 表示不设置止损</div>
                        </div>
                        <div class="form-group">
                            <label for="market_tp_points">止盈点数 (points)</label>
                            <input type="number" id="market_tp_points" min="1" value="300" placeholder="300">
                            <div class="help-text">0 表示不设置止盈</div>
                        </div>
                    </div>
                    <div class="form-group">
                        <label for="market_target_leverage">目标杠杆</label>
                        <input type="number" id="market_target_leverage" step="0.1" min="1" value="5" placeholder="5">
                        <div class="help-text">可选，EA 用仓位控制模拟</div>
                    </div>
                </div>

                <!-- LIMIT 参数 -->
                <div id="params-LIMIT" class="command-params hidden">
                    <h3>⏰ LIMIT 命令参数</h3>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="limit_symbol" class="required">交易品种</label>
                            <input type="text" id="limit_symbol" placeholder="EURUSD" style="text-transform: uppercase;">
                        </div>
                        <div class="form-group">
                            <label for="limit_side" class="required">方向</label>
                            <select id="limit_side">
                                <option value="BUY">BUY - 买入限价</option>
                                <option value="SELL">SELL - 卖出限价</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="limit_volume" class="required">手数</label>
                            <input type="number" id="limit_volume" step="0.01" min="0.01" value="0.1" placeholder="0.1">
                        </div>
                        <div class="form-group">
                            <label for="limit_price" class="required">挂单价格</label>
                            <input type="number" id="limit_price" step="0.00001" placeholder="1.09000">
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="limit_sl">止损价格</label>
                            <input type="number" id="limit_sl" step="0.00001" placeholder="1.08300">
                            <div class="help-text">0 表示不设置止损</div>
                        </div>
                        <div class="form-group">
                            <label for="limit_tp">止盈价格</label>
                            <input type="number" id="limit_tp" step="0.00001" placeholder="1.08800">
                            <div class="help-text">0 表示不设置止盈</div>
                        </div>
                    </div>
                    <div class="form-group">
                        <label for="limit_max_spread" class="required">最大点差 (points)</label>
                        <input type="number" id="limit_max_spread" min="1" value="20" placeholder="20">
                    </div>
                </div>

                <!-- CLOSE 参数 -->
                <div id="params-CLOSE" class="command-params hidden">
                    <h3>🔒 CLOSE 命令参数</h3>
                    <div class="form-group">
                        <label for="close_ticket" class="required">订单票号 (Ticket)</label>
                        <input type="number" id="close_ticket" min="1" placeholder="987654">
                        <div class="help-text">要平仓的订单票号</div>
                    </div>
                </div>

                <!-- MODIFY 参数 -->
                <div id="params-MODIFY" class="command-params hidden">
                    <h3>✏️ MODIFY 命令参数</h3>
                    <div class="form-group">
                        <label for="modify_ticket" class="required">订单票号 (Ticket)</label>
                        <input type="number" id="modify_ticket" min="1" placeholder="987654">
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label for="modify_sl">新止损价格</label>
                            <input type="number" id="modify_sl" step="0.00001" placeholder="1.08300">
                            <div class="help-text">0 表示不修改止损</div>
                        </div>
                        <div class="form-group">
                            <label for="modify_tp">新止盈价格</label>
                            <input type="number" id="modify_tp" step="0.00001" placeholder="1.08800">
                            <div class="help-text">0 表示不修改止盈</div>
                        </div>
                    </div>
                </div>

                <div class="loading" id="loading">
                    <div class="spinner"></div>
                    <div>正在发送命令...</div>
                </div>

                <button type="submit" class="btn" id="submitBtn">发送命令</button>
            </form>

            <div id="result" class="result"></div>
        </div>
    </div>

    <script>
        const actionSelect = document.getElementById('action');
        const form = document.getElementById('commandForm');
        const resultDiv = document.getElementById('result');
        const loadingDiv = document.getElementById('loading');
        const submitBtn = document.getElementById('submitBtn');

        // 切换命令参数面板
        actionSelect.addEventListener('change', function() {
            // 隐藏所有参数面板
            document.querySelectorAll('.command-params').forEach(panel => {
                panel.classList.add('hidden');
            });

            // 显示选中的参数面板
            const selectedAction = this.value;
            if (selectedAction) {
                const paramsPanel = document.getElementById(`params-${selectedAction}`);
                if (paramsPanel) {
                    paramsPanel.classList.remove('hidden');
                }
            }
        });

        // 自动转大写交易品种
        document.querySelectorAll('input[type="text"][id*="symbol"]').forEach(input => {
            input.addEventListener('input', function() {
                this.value = this.value.toUpperCase();
            });
        });

        // 表单提交
        form.addEventListener('submit', async function(e) {
            e.preventDefault();

            const accountId = parseInt(document.getElementById('accountId').value);
            const action = actionSelect.value;

            if (!accountId || !action) {
                showResult('error', '请填写所有必填字段');
                return;
            }

            // 构建命令数据
            const cmdData = {
                account: accountId,
                action: action
            };

            // 根据命令类型添加参数
            try {
                switch(action) {
                    case 'QUOTE':
                        const symbolsText = document.getElementById('quote_symbols').value.trim();
                        if (!symbolsText) {
                            showResult('error', '请填写交易品种列表');
                            return;
                        }
                        cmdData.symbols = symbolsText.split(',').map(s => s.trim()).filter(s => s);
                        break;

                    case 'MARKET':
                        cmdData.symbol = document.getElementById('market_symbol').value.trim();
                        cmdData.side = document.getElementById('market_side').value;
                        cmdData.risk_alloc_pct = parseFloat(document.getElementById('market_risk_alloc').value);
                        cmdData.max_spread_points = parseInt(document.getElementById('market_max_spread').value);
                        const slPoints = parseInt(document.getElementById('market_sl_points').value) || 0;
                        const tpPoints = parseInt(document.getElementById('market_tp_points').value) || 0;
                        if (slPoints > 0) cmdData.sl_points = slPoints;
                        if (tpPoints > 0) cmdData.tp_points = tpPoints;
                        const targetLeverage = parseFloat(document.getElementById('market_target_leverage').value);
                        if (targetLeverage > 0) cmdData.target_leverage = targetLeverage;
                        break;

                    case 'LIMIT':
                        cmdData.symbol = document.getElementById('limit_symbol').value.trim();
                        cmdData.side = document.getElementById('limit_side').value;
                        cmdData.volume = parseFloat(document.getElementById('limit_volume').value);
                        cmdData.price = parseFloat(document.getElementById('limit_price').value);
                        cmdData.max_spread_points = parseInt(document.getElementById('limit_max_spread').value);
                        const limitSl = parseFloat(document.getElementById('limit_sl').value) || 0;
                        const limitTp = parseFloat(document.getElementById('limit_tp').value) || 0;
                        if (limitSl > 0) cmdData.sl = limitSl;
                        if (limitTp > 0) cmdData.tp = limitTp;
                        break;

                    case 'CLOSE':
                        cmdData.ticket = parseInt(document.getElementById('close_ticket').value);
                        if (!cmdData.ticket) {
                            showResult('error', '请填写订单票号');
                            return;
                        }
                        break;

                    case 'MODIFY':
                        cmdData.ticket = parseInt(document.getElementById('modify_ticket').value);
                        if (!cmdData.ticket) {
                            showResult('error', '请填写订单票号');
                            return;
                        }
                        const modifySl = parseFloat(document.getElementById('modify_sl').value) || 0;
                        const modifyTp = parseFloat(document.getElementById('modify_tp').value) || 0;
                        if (modifySl > 0) cmdData.sl = modifySl;
                        if (modifyTp > 0) cmdData.tp = modifyTp;
                        break;
                }

                // 验证必填字段
                if (action === 'MARKET' && (!cmdData.symbol || !cmdData.side || !cmdData.risk_alloc_pct)) {
                    showResult('error', 'MARKET 命令缺少必填参数');
                    return;
                }
                if (action === 'LIMIT' && (!cmdData.symbol || !cmdData.side || !cmdData.volume || !cmdData.price)) {
                    showResult('error', 'LIMIT 命令缺少必填参数');
                    return;
                }

            } catch (error) {
                showResult('error', '参数格式错误: ' + error.message);
                return;
            }

            // 发送请求
            submitBtn.disabled = true;
            loadingDiv.classList.add('active');
            resultDiv.className = 'result';

            try {
                const response = await fetch('/api/command', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(cmdData)
                });

                const result = await response.json();

                if (response.ok && result.status === 'success') {
                    showResult('success', '命令发送成功！', {
                        cmd_id: result.cmd_id,
                        command: result.command,
                        full_response: result
                    });
                } else {
                    showResult('error', result.message || '命令发送失败', result);
                }
            } catch (error) {
                showResult('error', '网络错误: ' + error.message, error);
            } finally {
                submitBtn.disabled = false;
                loadingDiv.classList.remove('active');
            }
        });

        function showResult(type, message, data = null) {
            resultDiv.className = `result ${type}`;
            let html = `<h3>${type === 'success' ? '✅ 成功' : '❌ 错误'}</h3>`;
            html += `<p><strong>${message}</strong></p>`;
            
            if (data) {
                html += '<pre>' + JSON.stringify(data, null, 2) + '</pre>';
            }
            
            resultDiv.innerHTML = html;
            resultDiv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    </script>
</body>
</html>
    ''')


@app.get("/api/status/<int:account>")
def get_account_status(account):
    """查询账户最新状态"""
    try:
        limit = request.args.get('limit', default=10, type=int)
        with closing(sqlite3.connect(MT4_DATABASE)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM mt4_status_history 
                WHERE account = ? 
                ORDER BY ts DESC LIMIT ?
            ''', (account, limit))
            rows = cursor.fetchall()
            return jsonify({
                "status": "success",
                "data": [dict(row) for row in rows]
            })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.get("/api/reports/<int:account>")
def get_account_reports(account):
    """查询账户执行回报"""
    try:
        limit = request.args.get('limit', default=50, type=int)
        with closing(sqlite3.connect(MT4_DATABASE)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM mt4_reports 
                WHERE account = ? 
                ORDER BY ts DESC LIMIT ?
            ''', (account, limit))
            rows = cursor.fetchall()
            return jsonify({
                "status": "success",
                "data": [dict(row) for row in rows]
            })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.get("/api/quotes/<symbol>")
def get_latest_quote(symbol):
    """查询最新报价"""
    try:
        account = request.args.get('account', type=int)
        with closing(sqlite3.connect(MT4_DATABASE)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if account:
                cursor.execute('''
                    SELECT * FROM mt4_quotes 
                    WHERE symbol = ? AND account = ?
                    ORDER BY ts DESC LIMIT 1
                ''', (symbol.upper(), account))
            else:
                cursor.execute('''
                    SELECT * FROM mt4_quotes 
                    WHERE symbol = ?
                    ORDER BY ts DESC LIMIT 1
                ''', (symbol.upper(),))
            row = cursor.fetchone()
            if row:
                return jsonify({"status": "success", "data": dict(row)})
            else:
                return jsonify({"status": "empty"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    # 公网部署配置：host='0.0.0.0' 允许外部访问
    app.run(host='0.0.0.0', port=5000, threaded=True, debug=False)

