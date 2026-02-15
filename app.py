#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MT4 量化交易系统后端
提供命令队列、去重/幂等、数据展示等功能
"""

from flask import Flask, request, jsonify, render_template_string
from datetime import datetime, timedelta
import uuid
import hashlib
import json
import threading
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional

app = Flask(__name__)

# ==================== 数据存储（内存） ====================
# 命令队列：{account: deque([command, ...])}
command_queues: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))

# 命令状态追踪：{cmd_id: command_state}
command_states: Dict[str, dict] = {}

# 最新状态：{account: status_data}
latest_status: Dict[str, dict] = {}

# 执行回报：{account: deque([report, ...])}
reports: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))

# 报价数据：{account: deque([quote, ...])}
quotes: Dict[str, deque] = defaultdict(lambda: deque(maxlen=50))

# 持仓数据：{account: positions_data}
positions_data: Dict[str, dict] = {}

# 去重窗口：{account: {hash: (cmd_id, timestamp)}}
dedupe_cache: Dict[str, Dict[str, tuple]] = defaultdict(dict)

# 统计指标
metrics = {
    'total_commands': 0,
    'dedupe_hits': 0,
    'delivered_count': 0,
    'executed_count': 0,
    'error_count': 0,
    'last_error': None,
    'last_error_time': None,
}

# 锁
data_lock = threading.Lock()

# ==================== 工具函数 ====================

def generate_nonce() -> str:
    """生成随机 nonce"""
    return uuid.uuid4().hex[:8]

def generate_cmd_id() -> str:
    """生成命令 ID"""
    return f"cmd_{uuid.uuid4().hex[:12]}"

def compute_dedupe_hash(action: str, account: str, **kwargs) -> str:
    """计算去重哈希"""
    # 根据 action 提取关键字段
    key_parts = [action, account]
    
    if action == 'MARKET':
        key_parts.extend([
            str(kwargs.get('symbol', '')),
            str(kwargs.get('side', '')),
            str(kwargs.get('volume', kwargs.get('risk_alloc_pct', ''))),
            str(kwargs.get('sl_points', '')),
            str(kwargs.get('tp_points', '')),
        ])
    elif action == 'LIMIT':
        key_parts.extend([
            str(kwargs.get('symbol', '')),
            str(kwargs.get('side', '')),
            str(kwargs.get('price', '')),
            str(kwargs.get('volume', '')),
        ])
    elif action == 'CLOSE':
        key_parts.extend([
            str(kwargs.get('ticket', '')),
        ])
    elif action == 'QUOTE':
        key_parts.extend([
            ','.join(sorted(kwargs.get('symbols', []))),
        ])
    
    key_str = '|'.join(key_parts)
    return hashlib.md5(key_str.encode()).hexdigest()

def cleanup_expired_commands():
    """清理过期命令（后台线程）"""
    while True:
        try:
            time.sleep(5)
            now = time.time()
            with data_lock:
                for account, queue in list(command_queues.items()):
                    # 清理队列中过期命令
                    expired_indices = []
                    for i, cmd in enumerate(queue):
                        if cmd.get('created_at', 0) + cmd.get('ttl_sec', 0) < now:
                            expired_indices.append(i)
                            cmd_id = cmd.get('id')
                            if cmd_id in command_states:
                                command_states[cmd_id]['state'] = 'EXPIRED'
                    
                    # 从后往前删除，避免索引变化
                    for i in reversed(expired_indices):
                        queue.remove(queue[i])
                
                # 清理去重缓存（超过2秒的）
                for account in list(dedupe_cache.keys()):
                    cache = dedupe_cache[account]
                    expired_keys = [
                        k for k, (_, ts) in cache.items()
                        if now - ts > 2.0
                    ]
                    for k in expired_keys:
                        del cache[k]
        except Exception as e:
            print(f"Cleanup error: {e}")

# 启动清理线程
cleanup_thread = threading.Thread(target=cleanup_expired_commands, daemon=True)
cleanup_thread.start()

# ==================== MT4 接口 ====================

@app.route('/mt4/commands', methods=['GET'])
def get_commands():
    """MT4 轮询拉取命令"""
    account = request.args.get('account', '')
    max_count = int(request.args.get('max', 50))
    
    if not account:
        return jsonify({'error': 'account required'}), 400
    
    with data_lock:
        queue = command_queues.get(account, deque())
        commands = []
        delivered_ids = []
        
        # 批量取走命令
        for _ in range(min(max_count, len(queue))):
            if queue:
                cmd = queue.popleft()
                cmd_id = cmd.get('id')
                commands.append(cmd)
                delivered_ids.append(cmd_id)
                
                # 更新状态为 DELIVERED
                if cmd_id in command_states:
                    command_states[cmd_id]['state'] = 'DELIVERED'
                    command_states[cmd_id]['delivered_at'] = time.time()
                else:
                    command_states[cmd_id] = {
                        'state': 'DELIVERED',
                        'delivered_at': time.time(),
                        'created_at': cmd.get('created_at'),
                        'action': cmd.get('action'),
                        'symbol': cmd.get('symbol', ''),
                    }
        
        metrics['delivered_count'] += len(commands)
        queue_len = len(queue)
    
    return jsonify({
        'commands': commands,
        'server_ts': time.time(),
        'queue_len': queue_len,
    })

@app.route('/mt4/status', methods=['POST'])
def post_status():
    """MT4 上报状态"""
    data = request.get_json() or {}
    account = data.get('account', '')
    
    if not account:
        return jsonify({'error': 'account required'}), 400
    
    with data_lock:
        latest_status[account] = {
            **data,
            'updated_at': time.time(),
        }
    
    return jsonify({'ok': True})

@app.route('/mt4/report', methods=['POST'])
def post_report():
    """MT4 上报执行结果"""
    data = request.get_json() or {}
    account = data.get('account', '')
    cmd_id = data.get('cmd_id', '')
    nonce = data.get('nonce', '')
    
    if not account or not cmd_id:
        return jsonify({'error': 'account and cmd_id required'}), 400
    
    with data_lock:
        # 校验 cmd_id 和 nonce
        if cmd_id in command_states:
            state = command_states[cmd_id]
            
            # 获取原始命令的 nonce 进行校验
            original_nonce = ''
            if 'command' in state and 'nonce' in state['command']:
                original_nonce = state['command']['nonce']
            
            # 校验 nonce（如果提供了）
            nonce_valid = True
            if nonce and original_nonce and nonce != original_nonce:
                nonce_valid = False
                state['state'] = 'INVALID_NONCE'
                metrics['error_count'] += 1
                metrics['last_error'] = f'Nonce mismatch for cmd_id: {cmd_id}'
                metrics['last_error_time'] = time.time()
            else:
                state['state'] = 'REPORTED'
            
            state['report'] = data
            state['reported_at'] = time.time()
            
            # 计算延迟
            if 'delivered_at' in state:
                state['latency_est_ms'] = (state['reported_at'] - state['delivered_at']) * 1000
            
            ok = data.get('ok', False)
            if ok and nonce_valid:
                metrics['executed_count'] += 1
            elif not nonce_valid:
                # nonce 不匹配已在上面处理
                pass
            else:
                metrics['error_count'] += 1
                metrics['last_error'] = data.get('error', 'unknown')
                metrics['last_error_time'] = time.time()
        else:
            # 未知命令 ID
            state = {
                'state': 'INVALID_REPORT',
                'report': data,
                'reported_at': time.time(),
            }
            command_states[cmd_id] = state
            metrics['error_count'] += 1
            metrics['last_error'] = f'Unknown cmd_id: {cmd_id}'
            metrics['last_error_time'] = time.time()
        
        # 保存到回报列表
        reports[account].append({
            **data,
            'timestamp': time.time(),
        })
    
    return jsonify({'ok': True})

@app.route('/mt4/quote', methods=['POST'])
def post_quote():
    """MT4 上报报价"""
    data = request.get_json() or {}
    account = data.get('account', '')
    
    if not account:
        return jsonify({'error': 'account required'}), 400
    
    with data_lock:
        quotes[account].append({
            **data,
            'timestamp': time.time(),
        })
    
    return jsonify({'ok': True})

@app.route('/mt4/positions', methods=['POST'])
def post_positions():
    """MT4 上报持仓"""
    data = request.get_json() or {}
    account = data.get('account', '')
    
    if not account:
        return jsonify({'error': 'account required'}), 400
    
    with data_lock:
        positions_data[account] = {
            **data,
            'updated_at': time.time(),
        }
    
    return jsonify({'ok': True})

# ==================== Web API 接口 ====================

@app.route('/api/command', methods=['POST'])
def create_command():
    """创建命令（网页端调用）"""
    data = request.get_json() or {}
    account = data.get('account', '')
    action = data.get('action', '')
    
    if not account or not action:
        return jsonify({'error': 'account and action required'}), 400
    
    # 生成命令 ID 和 nonce
    cmd_id = generate_cmd_id()
    nonce = generate_nonce()
    
    # 去重检查
    dedupe_hash = compute_dedupe_hash(action, account, **data)
    deduped = False
    
    with data_lock:
        # 检查去重窗口
        cache = dedupe_cache[account]
        if dedupe_hash in cache:
            existing_cmd_id, _ = cache[dedupe_hash]
            # 如果命令还在队列中，返回已存在的 cmd_id
            if existing_cmd_id in command_states:
                state = command_states[existing_cmd_id]
                if state.get('state') in ['QUEUED', 'DELIVERED']:
                    deduped = True
                    cmd_id = existing_cmd_id
                    # 从原始命令获取 nonce
                    if 'command' in state and 'nonce' in state['command']:
                        nonce = state['command']['nonce']
                    else:
                        # 如果找不到，从队列中查找
                        for cmd in command_queues[account]:
                            if cmd.get('id') == existing_cmd_id:
                                nonce = cmd.get('nonce', '')
                                break
        
        if not deduped:
            # 创建新命令
            command = {
                'id': cmd_id,
                'nonce': nonce,
                'action': action,
                'account': account,
                'created_at': time.time(),
                'ttl_sec': data.get('ttl_sec', 10),
                **{k: v for k, v in data.items() if k not in ['account', 'action', 'ttl_sec']}
            }
            
            # 入队
            command_queues[account].append(command)
            
            # 记录状态
            command_states[cmd_id] = {
                'state': 'QUEUED',
                'created_at': command['created_at'],
                'action': action,
                'symbol': data.get('symbol', ''),
                'command': command,
            }
            
            # 更新去重缓存
            cache[dedupe_hash] = (cmd_id, time.time())
            
            metrics['total_commands'] += 1
        else:
            metrics['dedupe_hits'] += 1
    
    return jsonify({
        'ok': True,
        'id': cmd_id,
        'nonce': nonce,
        'deduped': deduped,
    })

@app.route('/api/data', methods=['GET'])
def get_data():
    """获取数据（供前端拉取）"""
    account = request.args.get('account', '')
    
    with data_lock:
        # 获取命令状态列表（最近100条）
        recent_states = sorted(
            command_states.items(),
            key=lambda x: x[1].get('created_at', 0),
            reverse=True
        )[:100]
        
        commands_list = []
        for cmd_id, state in recent_states:
            cmd_data = {
                'cmd_id': cmd_id,
                'state': state.get('state', 'UNKNOWN'),
                'action': state.get('action', ''),
                'symbol': state.get('symbol', ''),
                'created_at': state.get('created_at', 0),
                'delivered_at': state.get('delivered_at', 0),
                'reported_at': state.get('reported_at', 0),
                'latency_est_ms': state.get('latency_est_ms', 0),
            }
            if 'report' in state:
                report = state['report']
                cmd_data['ok'] = report.get('ok', False)
                cmd_data['message'] = report.get('message', '')
                cmd_data['ticket'] = report.get('ticket', '')
                cmd_data['error'] = report.get('error', '')
            commands_list.append(cmd_data)
        
        # 获取账户状态
        status = latest_status.get(account, {})
        
        # 获取回报列表
        reports_list = list(reports.get(account, deque()))[-20:]
        
        # 获取报价列表
        quotes_list = list(quotes.get(account, deque()))[-10:]
        
        # 获取持仓
        positions = positions_data.get(account, {}).get('positions', [])
        
        # 计算统计
        queue_len = len(command_queues.get(account, deque()))
        
        # 最近1分钟的命令统计
        now = time.time()
        recent_commands = [
            s for s in recent_states
            if s[1].get('created_at', 0) > now - 60
        ]
        recent_success = sum(
            1 for _, s in recent_commands
            if s.get('report', {}).get('ok', False)
        )
        recent_total = len(recent_commands)
        success_rate = (recent_success / recent_total * 100) if recent_total > 0 else 0
        
        # 平均延迟
        latencies = [
            s[1].get('latency_est_ms', 0)
            for _, s in recent_states
            if s[1].get('latency_est_ms', 0) > 0
        ]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0
    
    return jsonify({
        'status': status,
        'commands': commands_list,
        'reports': reports_list,
        'quotes': quotes_list,
        'positions': positions,
        'metrics': {
            'queue_len': queue_len,
            'total_commands': metrics['total_commands'],
            'dedupe_hits': metrics['dedupe_hits'],
            'delivered_count': metrics['delivered_count'],
            'executed_count': metrics['executed_count'],
            'error_count': metrics['error_count'],
            'last_error': metrics['last_error'],
            'last_error_time': metrics['last_error_time'],
            'recent_commands_1min': recent_total,
            'success_rate_1min': round(success_rate, 2),
            'avg_latency_ms': round(avg_latency, 2),
        },
        'server_ts': time.time(),
    })

# ==================== 可视化页面 ====================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>MT4 量化交易系统 - 可视化追踪</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Arial, sans-serif;
            background: #1a1a1a;
            color: #e0e0e0;
            padding: 20px;
        }
        .container { max-width: 1600px; margin: 0 auto; }
        h1 { color: #4CAF50; margin-bottom: 20px; }
        h2 { color: #2196F3; margin: 20px 0 10px; font-size: 18px; }
        .panel {
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 20px;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat-item {
            background: #333;
            padding: 12px;
            border-radius: 6px;
            border-left: 4px solid #4CAF50;
        }
        .stat-label { font-size: 12px; color: #aaa; }
        .stat-value { font-size: 24px; font-weight: bold; color: #4CAF50; }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        th, td {
            padding: 8px;
            text-align: left;
            border-bottom: 1px solid #444;
        }
        th {
            background: #333;
            color: #4CAF50;
            position: sticky;
            top: 0;
        }
        tr:hover { background: #333; }
        .state-QUEUED { color: #FFC107; }
        .state-DELIVERED { color: #2196F3; }
        .state-EXECUTED { color: #4CAF50; }
        .state-REPORTED { color: #4CAF50; }
        .state-EXPIRED { color: #f44336; }
        .state-INVALID_REPORT { color: #f44336; background: #3a1a1a; }
        .ok-true { color: #4CAF50; }
        .ok-false { color: #f44336; }
        .command-form {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 10px;
            margin-bottom: 15px;
        }
        input, select, button {
            padding: 8px;
            border: 1px solid #555;
            border-radius: 4px;
            background: #333;
            color: #e0e0e0;
        }
        button {
            background: #4CAF50;
            color: white;
            cursor: pointer;
            font-weight: bold;
        }
        button:hover { background: #45a049; }
        .error { color: #f44336; }
        .success { color: #4CAF50; }
        .auto-refresh { margin: 10px 0; }
        .auto-refresh label { margin-right: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 MT4 量化交易系统 - 可视化追踪面板</h1>
        
        <div class="panel">
            <div class="auto-refresh">
                <label>
                    <input type="checkbox" id="autoRefresh" checked>
                    自动刷新 (1秒)
                </label>
                <label>
                    账户: <input type="text" id="accountInput" value="123456" style="width: 100px;">
                </label>
                <button onclick="loadData()">手动刷新</button>
            </div>
        </div>
        
        <div class="panel">
            <h2>📊 统计面板</h2>
            <div class="stats-grid" id="statsGrid"></div>
        </div>
        
        <div class="panel">
            <h2>💰 账户状态</h2>
            <div id="accountStatus"></div>
        </div>
        
        <div class="panel">
            <h2>📝 命令下发表单</h2>
            <div class="command-form">
                <select id="actionSelect">
                    <option value="MARKET">MARKET - 市价单</option>
                    <option value="LIMIT">LIMIT - 限价单</option>
                    <option value="CLOSE">CLOSE - 平仓</option>
                    <option value="QUOTE">QUOTE - 询价</option>
                </select>
                <input type="text" id="symbolInput" placeholder="Symbol (EURUSD)" value="EURUSD">
                <input type="text" id="sideInput" placeholder="Side (BUY/SELL)" value="BUY">
                <input type="number" id="volumeInput" placeholder="Volume (0.01)" step="0.01" value="0.01">
                <input type="number" id="riskPctInput" placeholder="Risk % (0.02)" step="0.01" value="0.02">
                <input type="number" id="slPointsInput" placeholder="SL Points (200)" value="200">
                <input type="number" id="tpPointsInput" placeholder="TP Points (300)" value="300">
                <input type="number" id="maxSpreadInput" placeholder="Max Spread (15)" value="15">
                <input type="number" id="ticketInput" placeholder="Ticket (for CLOSE)" value="">
                <button onclick="sendCommand()">发送命令</button>
            </div>
            <div id="commandResult"></div>
        </div>
        
        <div class="panel">
            <h2>🔄 命令生命周期追踪表</h2>
            <div style="overflow-x: auto; max-height: 500px;">
                <table id="commandsTable">
                    <thead>
                        <tr>
                            <th>时间</th>
                            <th>cmd_id</th>
                            <th>nonce</th>
                            <th>action</th>
                            <th>symbol</th>
                            <th>state</th>
                            <th>ok</th>
                            <th>message</th>
                            <th>ticket</th>
                            <th>latency_ms</th>
                        </tr>
                    </thead>
                    <tbody id="commandsBody"></tbody>
                </table>
            </div>
        </div>
        
        <div class="panel">
            <h2>📈 持仓表格</h2>
            <div style="overflow-x: auto;">
                <table id="positionsTable">
                    <thead>
                        <tr>
                            <th>Ticket</th>
                            <th>Symbol</th>
                            <th>Type</th>
                            <th>Lots</th>
                            <th>Open Price</th>
                            <th>SL</th>
                            <th>TP</th>
                            <th>Profit</th>
                        </tr>
                    </thead>
                    <tbody id="positionsBody"></tbody>
                </table>
            </div>
        </div>
        
        <div class="panel">
            <h2>💹 报价表格</h2>
            <div style="overflow-x: auto;">
                <table id="quotesTable">
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Bid</th>
                            <th>Ask</th>
                            <th>Spread</th>
                            <th>Time</th>
                        </tr>
                    </thead>
                    <tbody id="quotesBody"></tbody>
                </table>
            </div>
        </div>
    </div>
    
    <script>
        let autoRefreshInterval = null;
        
        function getAccount() {
            return document.getElementById('accountInput').value || '123456';
        }
        
        function formatTime(ts) {
            if (!ts) return '-';
            return new Date(ts * 1000).toLocaleTimeString();
        }
        
        function formatDateTime(ts) {
            if (!ts) return '-';
            return new Date(ts * 1000).toLocaleString();
        }
        
        async function loadData() {
            const account = getAccount();
            try {
                const res = await fetch(`/api/data?account=${account}`);
                const data = await res.json();
                
                // 更新统计面板
                const stats = data.metrics;
                document.getElementById('statsGrid').innerHTML = `
                    <div class="stat-item">
                        <div class="stat-label">队列长度</div>
                        <div class="stat-value">${stats.queue_len}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">总命令数</div>
                        <div class="stat-value">${stats.total_commands}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">去重命中</div>
                        <div class="stat-value">${stats.dedupe_hits}</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">成功率 (1分钟)</div>
                        <div class="stat-value">${stats.success_rate_1min}%</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">平均延迟</div>
                        <div class="stat-value">${stats.avg_latency_ms}ms</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">错误数</div>
                        <div class="stat-value">${stats.error_count}</div>
                    </div>
                `;
                
                // 更新账户状态
                const status = data.status;
                document.getElementById('accountStatus').innerHTML = status.account ? `
                    <table>
                        <tr><td>账户</td><td>${status.account}</td></tr>
                        <tr><td>余额</td><td>${status.balance || '-'}</td></tr>
                        <tr><td>净值</td><td>${status.equity || '-'}</td></tr>
                        <tr><td>保证金</td><td>${status.margin || '-'}</td></tr>
                        <tr><td>保证金水平</td><td>${status.margin_level || '-'}%</td></tr>
                        <tr><td>当日PnL</td><td>${status.daily_pnl || '-'}</td></tr>
                        <tr><td>当日收益率</td><td>${status.daily_return ? (status.daily_return * 100).toFixed(2) + '%' : '-'}</td></tr>
                        <tr><td>杠杆使用</td><td>${status.leverage_used || '-'}</td></tr>
                    </table>
                ` : '<p>暂无账户状态数据</p>';
                
                // 更新命令表格
                const tbody = document.getElementById('commandsBody');
                tbody.innerHTML = data.commands.map(cmd => `
                    <tr class="state-${cmd.state}">
                        <td>${formatTime(cmd.created_at)}</td>
                        <td>${cmd.cmd_id}</td>
                        <td>-</td>
                        <td>${cmd.action}</td>
                        <td>${cmd.symbol}</td>
                        <td class="state-${cmd.state}">${cmd.state}</td>
                        <td class="ok-${cmd.ok}">${cmd.ok !== undefined ? (cmd.ok ? '✓' : '✗') : '-'}</td>
                        <td>${cmd.message || cmd.error || '-'}</td>
                        <td>${cmd.ticket || '-'}</td>
                        <td>${cmd.latency_est_ms ? cmd.latency_est_ms.toFixed(0) : '-'}</td>
                    </tr>
                `).join('');
                
                // 更新持仓表格
                const posBody = document.getElementById('positionsBody');
                if (data.positions && data.positions.length > 0) {
                    posBody.innerHTML = data.positions.map(pos => `
                        <tr>
                            <td>${pos.ticket}</td>
                            <td>${pos.symbol}</td>
                            <td>${pos.type}</td>
                            <td>${pos.lots}</td>
                            <td>${pos.open_price}</td>
                            <td>${pos.sl || '-'}</td>
                            <td>${pos.tp || '-'}</td>
                            <td>${pos.profit || '-'}</td>
                        </tr>
                    `).join('');
                } else {
                    posBody.innerHTML = '<tr><td colspan="8">暂无持仓</td></tr>';
                }
                
                // 更新报价表格
                const quoteBody = document.getElementById('quotesBody');
                if (data.quotes && data.quotes.length > 0) {
                    const latestQuote = data.quotes[data.quotes.length - 1];
                    if (latestQuote.quotes) {
                        quoteBody.innerHTML = Object.entries(latestQuote.quotes).map(([sym, q]) => `
                            <tr>
                                <td>${sym}</td>
                                <td>${q.bid}</td>
                                <td>${q.ask}</td>
                                <td>${q.spread_points || '-'}</td>
                                <td>${formatTime(latestQuote.timestamp)}</td>
                            </tr>
                        `).join('');
                    } else {
                        quoteBody.innerHTML = '<tr><td colspan="5">暂无报价数据</td></tr>';
                    }
                } else {
                    quoteBody.innerHTML = '<tr><td colspan="5">暂无报价数据</td></tr>';
                }
                
            } catch (error) {
                console.error('Load data error:', error);
            }
        }
        
        async function sendCommand() {
            const account = getAccount();
            const action = document.getElementById('actionSelect').value;
            const symbol = document.getElementById('symbolInput').value;
            const side = document.getElementById('sideInput').value;
            const volume = parseFloat(document.getElementById('volumeInput').value);
            const riskPct = parseFloat(document.getElementById('riskPctInput').value);
            const slPoints = parseInt(document.getElementById('slPointsInput').value);
            const tpPoints = parseInt(document.getElementById('tpPointsInput').value);
            const maxSpread = parseInt(document.getElementById('maxSpreadInput').value);
            const ticket = document.getElementById('ticketInput').value;
            
            const payload = {
                account: account,
                action: action,
                ttl_sec: 10,
            };
            
            if (action === 'MARKET') {
                payload.symbol = symbol;
                payload.side = side;
                if (volume > 0) payload.volume = volume;
                if (riskPct > 0) payload.risk_alloc_pct = riskPct;
                if (slPoints > 0) payload.sl_points = slPoints;
                if (tpPoints > 0) payload.tp_points = tpPoints;
                if (maxSpread > 0) payload.max_spread_points = maxSpread;
            } else if (action === 'LIMIT') {
                payload.symbol = symbol;
                payload.side = side;
                payload.volume = volume;
                payload.price = parseFloat(prompt('请输入限价价格:') || '0');
            } else if (action === 'CLOSE') {
                payload.ticket = parseInt(ticket);
            } else if (action === 'QUOTE') {
                payload.symbols = symbol.split(',').map(s => s.trim());
            }
            
            try {
                const res = await fetch('/api/command', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                const result = await res.json();
                
                const resultDiv = document.getElementById('commandResult');
                if (result.ok) {
                    resultDiv.innerHTML = `<div class="success">✓ 命令已创建: ${result.id} (deduped: ${result.deduped})</div>`;
                } else {
                    resultDiv.innerHTML = `<div class="error">✗ 错误: ${result.error}</div>`;
                }
                
                // 刷新数据
                setTimeout(loadData, 500);
            } catch (error) {
                document.getElementById('commandResult').innerHTML = `<div class="error">✗ 请求失败: ${error}</div>`;
            }
        }
        
        // 自动刷新
        document.getElementById('autoRefresh').addEventListener('change', function(e) {
            if (e.target.checked) {
                autoRefreshInterval = setInterval(loadData, 1000);
            } else {
                if (autoRefreshInterval) clearInterval(autoRefreshInterval);
            }
        });
        
        // 初始加载
        loadData();
        if (document.getElementById('autoRefresh').checked) {
            autoRefreshInterval = setInterval(loadData, 1000);
        }
    </script>
</body>
</html>
'''

@app.route('/api', methods=['GET'])
def api_page():
    """可视化页面"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/', methods=['GET'])
def index():
    """首页重定向到 /api"""
    return render_template_string('<script>window.location.href="/api";</script>')

if __name__ == '__main__':
    print("=" * 60)
    print("MT4 量化交易系统后端启动")
    print("=" * 60)
    print("访问 http://localhost:5000/api 查看可视化界面")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
