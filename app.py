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

# ==================== 工具函数：日志记录 ====================

def log_request(route_name):
    """记录请求信息"""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        remote_addr = request.remote_addr or 'unknown'
        method = request.method
        path = request.path
        headers = dict(request.headers)
        
        # 获取请求体（前500字节）
        try:
            body_data = request.get_data(as_text=True)
            if body_data:
                body_preview = body_data[:500]
            else:
                body_preview = "(empty)"
        except:
            body_preview = "(无法读取)"
        
        print(f"[{timestamp}] [{route_name}] {method} {path}")
        print(f"  Remote: {remote_addr}")
        print(f"  Headers: {headers}")
        print(f"  Body (first 500 bytes): {body_preview}")
    except Exception as log_err:
        print(f"[LOG ERROR] Failed to log request: {log_err}")

def safe_json_response(ok, data=None, error=None, trace=None, status_code=200):
    """安全创建 JSON 响应"""
    try:
        response_data = {'ok': ok}
        if data:
            response_data.update(data)
        if error:
            response_data['error'] = error
        if trace:
            response_data['trace'] = trace
        
        response = jsonify(response_data)
        response.headers['Content-Type'] = 'application/json'
        return response, status_code
    except Exception as e:
        # 如果连 JSON 响应都无法创建，返回最简单的响应
        print(f"[CRITICAL] Cannot create JSON response: {e}")
        from flask import Response
        return Response(
            '{"ok":false,"error":"Internal error"}',
            status=200,
            mimetype='application/json'
        )

# ==================== 全局错误处理 ====================

@app.errorhandler(404)
def not_found(error):
    """404错误返回JSON"""
    return safe_json_response(False, error='Not found', status_code=404)

@app.errorhandler(405)
def method_not_allowed(error):
    """405错误返回JSON"""
    return safe_json_response(False, error='Method not allowed', status_code=405)

@app.errorhandler(Exception)
def handle_exception(e):
    """捕获所有未处理的异常，返回JSON（状态码200避免MT4异常）"""
    import traceback
    error_msg = str(e)
    error_type = e.__class__.__name__
    traceback_str = traceback.format_exc()
    
    # 打印完整 traceback 到控制台
    print(f"[GLOBAL ERROR HANDLER] {error_type}: {error_msg}")
    print(traceback_str)
    
    # 返回 JSON 错误结构，状态码 200（避免 MT4 逻辑异常）
    return safe_json_response(
        ok=False,
        error=error_msg,
        trace=error_type
    )

# ==================== MT4 API 接口（仅JSON，路径：/web/api/mt4/...）===================

@app.route('/web/api/mt4/commands', methods=['GET', 'POST'])
def get_commands():
    """MT4 轮询拉取命令 - 支持GET和POST"""
    log_request('get_commands')
    try:
        # 优先从POST JSON获取，其次从GET参数获取
        try:
            if request.method == 'POST':
                data = request.get_json() or {}
                account = data.get('account', '') or request.args.get('account', '')
                # 安全转换 max_count
                try:
                    max_val = data.get('max') or request.args.get('max', 50)
                    max_count = int(max_val) if max_val else 50
                except (ValueError, TypeError):
                    max_count = 50
            else:
                account = request.args.get('account', '')
                try:
                    max_count = int(request.args.get('max', 50))
                except (ValueError, TypeError):
                    max_count = 50
        except Exception as parse_err:
            print(f"[MT4 Commands] Parse error: {parse_err}")
            account = request.args.get('account', '')
            max_count = 50
        
        # 调试日志
        print(f"[MT4 Commands] Method: {request.method}, Account: {account}, Max: {max_count}")
        print(f"[MT4 Commands] GET args: {dict(request.args)}")
        if request.method == 'POST':
            try:
                print(f"[MT4 Commands] POST data: {request.get_json()}")
            except:
                print(f"[MT4 Commands] POST data: (无法解析)")
        
        if not account:
            response = jsonify({'error': 'account required'})
            response.headers['Content-Type'] = 'application/json'
            return response, 400
        
        try:
            with data_lock:
                queue = command_queues.get(account, deque())
                commands = []
                delivered_ids = []
                
                # 批量取走命令
                max_to_take = min(max_count, len(queue))
                for _ in range(max_to_take):
                    if queue:
                        try:
                            cmd = queue.popleft()
                            cmd_id = cmd.get('id', '')
                            
                            if not cmd_id:
                                continue
                            
                            # 创建命令副本，确保JSON可序列化
                            clean_cmd = {}
                            try:
                                for key, value in cmd.items():
                                    # 跳过None值，转换数据类型
                                    if value is None:
                                        continue
                                    elif isinstance(value, float):
                                        # 对于浮点数，如果是整数部分，转换为int
                                        if key in ['created_at', 'ttl_sec']:
                                            try:
                                                clean_cmd[key] = int(value)
                                            except (ValueError, OverflowError):
                                                clean_cmd[key] = 0
                                        else:
                                            # 检查是否为 NaN 或 Inf
                                            if not (value != value or value == float('inf') or value == float('-inf')):
                                                clean_cmd[key] = value
                                    elif isinstance(value, (str, int, bool)):
                                        clean_cmd[key] = value
                                    elif isinstance(value, (list, dict)):
                                        # 递归清理嵌套结构
                                        try:
                                            json.dumps(value)  # 测试是否可序列化
                                            clean_cmd[key] = value
                                        except (TypeError, ValueError):
                                            clean_cmd[key] = str(value)
                                    else:
                                        # 其他类型转换为字符串
                                        clean_cmd[key] = str(value)
                            except Exception as clean_err:
                                print(f"[MT4 Commands] Clean cmd error: {clean_err}")
                                # 如果清理失败，至少保留基本字段
                                clean_cmd = {
                                    'id': cmd_id,
                                    'action': cmd.get('action', 'UNKNOWN'),
                                    'account': cmd.get('account', account),
                                }
                            
                            commands.append(clean_cmd)
                            delivered_ids.append(cmd_id)
                            
                            # 更新状态为 DELIVERED
                            try:
                                if cmd_id in command_states:
                                    command_states[cmd_id]['state'] = 'DELIVERED'
                                    command_states[cmd_id]['delivered_at'] = time.time()
                                else:
                                    command_states[cmd_id] = {
                                        'state': 'DELIVERED',
                                        'delivered_at': time.time(),
                                        'created_at': clean_cmd.get('created_at', time.time()),
                                        'action': clean_cmd.get('action', 'UNKNOWN'),
                                        'symbol': clean_cmd.get('symbol', ''),
                                    }
                            except Exception as state_err:
                                print(f"[MT4 Commands] State update error: {state_err}")
                        except Exception as cmd_err:
                            print(f"[MT4 Commands] Process cmd error: {cmd_err}")
                            continue
                
                metrics['delivered_count'] += len(commands)
                queue_len = len(queue)
        except Exception as lock_err:
            print(f"[MT4 Commands] Lock error: {lock_err}")
            import traceback
            print(traceback.format_exc())
            commands = []
            queue_len = 0
        
        # 确保响应数据可序列化
        try:
            response_data = {
                'commands': commands,
                'server_ts': int(time.time()),
                'queue_len': queue_len,
            }
            # 测试序列化
            json.dumps(response_data)
        except Exception as json_err:
            print(f"[MT4 Commands] JSON serialization error: {json_err}")
            # 如果序列化失败，返回空命令列表
            response_data = {
                'commands': [],
                'server_ts': int(time.time()),
                'queue_len': 0,
                'error': 'Serialization error',
            }
        
        response = jsonify(response_data)
        response.headers['Content-Type'] = 'application/json'
        return response
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[MT4 Commands] Fatal error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(
            ok=False,
            data={
                'commands': [],
                'server_ts': int(time.time()),
                'queue_len': 0,
            },
            error=error_msg,
            trace=error_type
        )

@app.route('/web/api/mt4/status', methods=['POST'])
def post_status():
    """MT4 上报状态"""
    log_request('post_status')
    try:
        data = request.get_json(silent=True)
        if not data:
            return safe_json_response(ok=False, error='invalid json')
        
        account = data.get('account', '')
        
        if not account:
            return safe_json_response(ok=False, error='account required')
        
        with data_lock:
            if account not in latest_status:
                latest_status[account] = {}
            latest_status[account].update({
                **data,
                'updated_at': time.time(),
            })
        
        return safe_json_response(ok=True)
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[MT4 Status] Error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(ok=False, error=error_msg, trace=error_type)

@app.route('/web/api/mt4/report', methods=['POST'])
def post_report():
    """MT4 上报执行结果"""
    log_request('post_report')
    try:
        data = request.get_json(silent=True)
        if not data:
            return safe_json_response(ok=False, error='invalid json')
        
        account = data.get('account', '')
        cmd_id = data.get('cmd_id', '')
        nonce = data.get('nonce', '')
        
        if not account or not cmd_id:
            return safe_json_response(ok=False, error='account and cmd_id required')
        
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
            
            # 保存到回报列表（使用 .get() 安全访问）
            if account not in reports:
                reports[account] = deque(maxlen=100)
            reports[account].append({
                **data,
                'timestamp': time.time(),
            })
        
        return safe_json_response(ok=True)
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[MT4 Report] Error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(ok=False, error=error_msg, trace=error_type)

@app.route('/web/api/mt4/quote', methods=['POST'])
def post_quote():
    """MT4 上报报价"""
    log_request('post_quote')
    try:
        data = request.get_json(silent=True)
        if not data:
            return safe_json_response(ok=False, error='invalid json')
        
        account = data.get('account', '')
        
        if not account:
            return safe_json_response(ok=False, error='account required')
        
        with data_lock:
            if account not in quotes:
                quotes[account] = deque(maxlen=50)
            quotes[account].append({
                **data,
                'timestamp': time.time(),
            })
        
        return safe_json_response(ok=True)
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[MT4 Quote] Error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(ok=False, error=error_msg, trace=error_type)

@app.route('/web/api/mt4/positions', methods=['POST'])
def post_positions():
    """MT4 上报持仓"""
    log_request('post_positions')
    try:
        data = request.get_json(silent=True)
        if not data:
            return safe_json_response(ok=False, error='invalid json')
        
        account = data.get('account', '')
        
        if not account:
            return safe_json_response(ok=False, error='account required')
        
        with data_lock:
            positions_data[account] = {
                **data,
                'updated_at': time.time(),
            }
        
        return safe_json_response(ok=True)
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[MT4 Positions] Error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(ok=False, error=error_msg, trace=error_type)

# ==================== 前端 Web API 接口（仅JSON，路径：/web/api/...）===================

@app.route('/web/api/command', methods=['POST'])
def create_command():
    """创建命令（网页端调用）"""
    log_request('create_command')
    try:
        # 检查Content-Type
        if not request.is_json:
            return safe_json_response(ok=False, error='Content-Type must be application/json')
        
        data = request.get_json(silent=True)
        if not data:
            return safe_json_response(ok=False, error='invalid json')
        
        account = data.get('account', '')
        action = data.get('action', '')
        
        if not account or not action:
            return safe_json_response(ok=False, error='account and action required')
        
        # 生成命令 ID 和 nonce
        cmd_id = generate_cmd_id()
        nonce = generate_nonce()
        
        # 去重检查（排除 account 和 action，因为它们已经作为位置参数传递）
        dedupe_data = {k: v for k, v in data.items() if k not in ['account', 'action']}
        dedupe_hash = compute_dedupe_hash(action, account, **dedupe_data)
        deduped = False
        
        with data_lock:
            # 检查去重窗口（使用 .get() 安全访问）
            if account not in dedupe_cache:
                dedupe_cache[account] = {}
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
                            # 如果找不到，从队列中查找（使用 .get() 安全访问）
                            queue = command_queues.get(account, deque())
                            for cmd in queue:
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
                
                # 入队（使用 .get() 安全访问）
                if account not in command_queues:
                    command_queues[account] = deque(maxlen=1000)
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
        
        return safe_json_response(ok=True, data={
            'id': cmd_id,
            'nonce': nonce,
            'deduped': deduped,
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[Create Command] Error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(ok=False, error=error_msg, trace=error_type)

@app.route('/web/api/data', methods=['GET'])
def get_data():
    """获取数据（供前端拉取）"""
    log_request('get_data')
    try:
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
            
            # 获取账户状态（使用 .get() 安全访问）
            status = latest_status.get(account, {})
            
            # 获取回报列表（使用 .get() 安全访问）
            reports_list = list(reports.get(account, deque()))[-20:]
            
            # 获取报价列表（使用 .get() 安全访问）
            quotes_list = list(quotes.get(account, deque()))[-10:]
            
            # 获取持仓（使用 .get() 安全访问）
            positions = positions_data.get(account, {}).get('positions', [])
            
            # 计算统计（使用 .get() 安全访问）
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
        
        return safe_json_response(ok=True, data={
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
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[Get Data] Error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(ok=False, error=error_msg, trace=error_type)

# ==================== 健康检查和调试接口 ====================

@app.route('/web/api/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    try:
        instance_id = str(uuid.uuid4())[:8]
        return safe_json_response(ok=True, data={
            'server_time': datetime.now().isoformat(),
            'instance_id': instance_id,
            'timestamp': time.time(),
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[Health Check] Error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(ok=False, error=error_msg, trace=error_type)

@app.route('/web/api/debug/queues', methods=['GET'])
def debug_queues():
    """调试接口：查看队列状态"""
    log_request('debug_queues')
    try:
        with data_lock:
            # 所有账户队列长度
            queue_info = {}
            for account, queue in command_queues.items():
                queue_info[account] = {
                    'queue_len': len(queue),
                    'queue_items': [cmd.get('id', 'unknown') for cmd in list(queue)[:10]]  # 最近10条
                }
            
            # 最近命令列表（最近20条）
            recent_commands = []
            sorted_states = sorted(
                command_states.items(),
                key=lambda x: x[1].get('created_at', 0),
                reverse=True
            )[:20]
            for cmd_id, state in sorted_states:
                recent_commands.append({
                    'cmd_id': cmd_id,
                    'state': state.get('state', 'UNKNOWN'),
                    'action': state.get('action', ''),
                    'created_at': state.get('created_at', 0),
                })
            
            # 最近 report 列表（最近20条）
            recent_reports = []
            for account, report_queue in reports.items():
                for report in list(report_queue)[-10:]:  # 每个账户最近10条
                    recent_reports.append({
                        'account': account,
                        'cmd_id': report.get('cmd_id', ''),
                        'ok': report.get('ok', False),
                        'timestamp': report.get('timestamp', 0),
                    })
            recent_reports = sorted(recent_reports, key=lambda x: x.get('timestamp', 0), reverse=True)[:20]
        
        return safe_json_response(ok=True, data={
            'queues': queue_info,
            'recent_commands': recent_commands,
            'recent_reports': recent_reports,
            'total_accounts': len(command_queues),
            'total_command_states': len(command_states),
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = e.__class__.__name__
        traceback_str = traceback.format_exc()
        print(f"[Debug Queues] Error: {error_type}: {error_msg}")
        print(traceback_str)
        return safe_json_response(ok=False, error=error_msg, trace=error_type)

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
                    账户: <input type="text" id="accountInput" value="833711" style="width: 100px;">
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
            return document.getElementById('accountInput').value || '833711';
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
                const res = await fetch(`/web/api/data?account=${account}`);
                
                // 检查响应内容类型
                const contentType = res.headers.get('content-type');
                let data;
                
                if (contentType && contentType.includes('application/json')) {
                    data = await res.json();
                } else {
                    // 如果不是JSON，读取文本内容
                    const text = await res.text();
                    console.error('非JSON响应:', text.substring(0, 200));
                    throw new Error(`服务器返回非JSON响应 (HTTP ${res.status}): ${text.substring(0, 100)}`);
                }
                
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
                const res = await fetch('/web/api/command', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                
                // 检查响应内容类型
                const contentType = res.headers.get('content-type');
                let result;
                
                if (contentType && contentType.includes('application/json')) {
                    result = await res.json();
                } else {
                    // 如果不是JSON，读取文本内容
                    const text = await res.text();
                    console.error('非JSON响应:', text.substring(0, 200));
                    throw new Error(`服务器返回非JSON响应 (HTTP ${res.status}): ${text.substring(0, 100)}`);
                }
                
                const resultDiv = document.getElementById('commandResult');
                if (res.ok && result.ok) {
                    resultDiv.innerHTML = `<div class="success">✓ 命令已创建: ${result.id} (deduped: ${result.deduped})</div>`;
                } else {
                    const errorMsg = result.error || result.message || `HTTP ${res.status}`;
                    resultDiv.innerHTML = `<div class="error">✗ 错误: ${errorMsg}</div>`;
                }
                
                // 刷新数据
                setTimeout(loadData, 500);
            } catch (error) {
                console.error('发送命令错误:', error);
                const errorMsg = error.message || String(error);
                document.getElementById('commandResult').innerHTML = `<div class="error">✗ 请求失败: ${errorMsg}</div>`;
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

# ==================== 展示页面（HTML，路径：/web 或 /）===================

@app.route('/web', methods=['GET'])
def web_page():
    """可视化展示页面（HTML）"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/', methods=['GET'])
def index():
    """首页重定向到 /web"""
    return render_template_string('<script>window.location.href="/web";</script>')

if __name__ == '__main__':
    print("=" * 60)
    print("MT4 量化交易系统后端启动")
    print("=" * 60)
    print("展示页面: http://localhost:5000/web 或 http://localhost:5000/")
    print("MT4 API: /web/api/mt4/... (仅JSON)")
    print("前端API: /web/api/... (仅JSON)")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
