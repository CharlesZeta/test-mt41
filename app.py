import os
import json
import time
import threading
from datetime import datetime
from collections import deque
from flask import Flask, request, render_template_string, redirect, url_for

app = Flask(__name__)

# ==================== 全局数据结构 ====================
# 存储最近收到的MT4请求（最多50条）
MAX_HISTORY = 50
history = deque(maxlen=MAX_HISTORY)
history_lock = threading.Lock()

# 待发送给MT4的指令队列
# 每个指令为字典： { 'id': 索引, 'symbol':, 'direction':, 'volume':, 'sl':, 'tp':, 'timestamp': }
commands = []
commands_lock = threading.Lock()
cmd_counter = 0  # 简单自增ID

# ==================== 工具函数 ====================
def format_command(cmd):
    """将指令字典格式化为字符串，供MT4解析"""
    base = f"{cmd['direction']},{cmd['symbol']},{cmd['volume']}"
    if cmd['sl'] is not None and cmd['tp'] is not None:
        return f"{base},{cmd['sl']},{cmd['tp']}"
    elif cmd['sl'] is not None:
        return f"{base},{cmd['sl']},0"
    elif cmd['tp'] is not None:
        return f"{base},0,{cmd['tp']}"
    else:
        return base

def get_client_ip():
    """尝试从请求头获取真实IP"""
    return request.headers.get('X-Real-Ip') or request.headers.get('X-Forwarded-For', request.remote_addr)

# ==================== 路由：主页 ====================
@app.route('/')
def index():
    """显示控制面板：最近上报数据、指令队列、发单表单"""
    with history_lock:
        # 最新的在前
        hist_list = list(reversed(history))
    with commands_lock:
        # 复制一份以免在模板遍历时被修改
        cmds_copy = commands.copy()
    return render_template_string(HTML_TEMPLATE, history=hist_list, commands=cmds_copy)

# ==================== 路由：MT4数据上报接口 ====================
@app.route('/web/api/echo', methods=['POST'])
def mt4_webhook():
    """
    接收MT4的POST请求，保存数据到历史，并返回待执行的交易指令。
    请求体可能是JSON格式（尽管Content-Type可能是x-www-form-urlencoded）。
    响应格式：纯文本，每行一条指令，无指令时返回"NOCOMMAND"
    """
    # 获取原始请求数据
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)

    # 解析JSON（如果可能）
    parsed_json = None
    try:
        parsed_json = json.loads(raw_body)
    except json.JSONDecodeError:
        pass  # 保留为None

    # 构建历史记录
    record = {
        'received_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ip': client_ip,
        'method': request.method,
        'path': request.path,
        'headers': headers_dict,
        'body_raw': raw_body[:500] + ('...' if len(raw_body) > 500 else ''),  # 避免页面过长
        'parsed': parsed_json,
        # 提取常用字段方便展示
        'account': parsed_json.get('account') if parsed_json else None,
        'server': parsed_json.get('server') if parsed_json else None,
        'balance': parsed_json.get('balance') if parsed_json else None,
        'equity': parsed_json.get('equity') if parsed_json else None,
        'floating_pnl': parsed_json.get('floating_pnl') if parsed_json else None,
    }

    with history_lock:
        history.appendleft(record)  # 最新放左边便于展示

    # 检查是否有待发送指令，如果有则取出所有并清空队列
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

# ==================== 路由：网页发单 ====================
@app.route('/send_command', methods=['POST'])
def send_command():
    """从网页表单接收指令，加入队列"""
    global cmd_counter
    symbol = request.form.get('symbol', '').strip().upper()
    direction = request.form.get('direction', '').strip().upper()
    volume = request.form.get('volume', '').strip()
    sl = request.form.get('sl', '').strip()
    tp = request.form.get('tp', '').strip()

    # 简单校验
    if not symbol or direction not in ['BUY', 'SELL'] or not volume:
        return redirect(url_for('index'))  # 忽略错误，实际可加flash消息，为简化直接返回

    try:
        volume = float(volume)
        sl = float(sl) if sl else None
        tp = float(tp) if tp else None
    except ValueError:
        return redirect(url_for('index'))

    cmd = {
        'id': cmd_counter,
        'symbol': symbol,
        'direction': direction,
        'volume': volume,
        'sl': sl,
        'tp': tp,
        'timestamp': datetime.now().strftime('%H:%M:%S')
    }
    with commands_lock:
        commands.append(cmd)
        cmd_counter += 1

    return redirect(url_for('index'))

# ==================== 路由：删除单条指令 ====================
@app.route('/delete_command/<int:index>', methods=['POST'])
def delete_command(index):
    """按索引删除指令（从0开始）"""
    with commands_lock:
        if 0 <= index < len(commands):
            commands.pop(index)
    return redirect(url_for('index'))

# ==================== 路由：清空所有指令 ====================
@app.route('/clear_commands', methods=['POST'])
def clear_commands():
    with commands_lock:
        commands.clear()
    return redirect(url_for('index'))

# ==================== 完整HTML模板（内嵌） ====================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MT4 远程交易执行面板</title>
    <!-- Bootstrap 5 CDN (简洁主题) -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { padding-top: 20px; background-color: #f8f9fa; }
        .card-header { font-weight: bold; }
        .history-table td { font-size: 0.9rem; vertical-align: middle; }
        .badge-ip { font-family: monospace; }
        .raw-preview { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .command-item { background: #e9ecef; padding: 8px 12px; border-radius: 6px; margin-bottom: 6px; }
    </style>
</head>
<body>
    <div class="container">
        <h1 class="mb-4">📊 MT4 远程交易执行</h1>
        
        <!-- 最新上报数据卡片 -->
        <div class="card mb-4">
            <div class="card-header bg-primary text-white">📨 最新接收的数据</div>
            <div class="card-body">
                {% if history %}
                    {% set last = history[0] %}
                    <div class="row">
                        <div class="col-md-3"><strong>账户:</strong> {{ last.account or 'N/A' }}</div>
                        <div class="col-md-3"><strong>服务器:</strong> {{ last.server or 'N/A' }}</div>
                        <div class="col-md-2"><strong>余额:</strong> {{ last.balance or 'N/A' }}</div>
                        <div class="col-md-2"><strong>净值:</strong> {{ last.equity or 'N/A' }}</div>
                        <div class="col-md-2"><strong>浮动盈亏:</strong> {{ last.floating_pnl or 'N/A' }}</div>
                    </div>
                    <div class="row mt-2">
                        <div class="col-12">
                            <strong>时间/IP:</strong> {{ last.received_at }} 来自 {{ last.ip }}
                            <span class="badge bg-secondary">{{ last.method }} {{ last.path }}</span>
                        </div>
                    </div>
                    <div class="row mt-2">
                        <div class="col-12">
                            <strong>原始Body预览:</strong>
                            <pre class="bg-light p-2 rounded" style="font-size:0.8rem;">{{ last.body_raw }}</pre>
                        </div>
                    </div>
                {% else %}
                    <p class="text-muted">尚未收到任何MT4上报数据。</p>
                {% endif %}
            </div>
        </div>

        <!-- 两列布局：左侧历史记录，右侧指令管理 -->
        <div class="row">
            <!-- 左侧：历史记录表 -->
            <div class="col-lg-7 mb-4">
                <div class="card h-100">
                    <div class="card-header bg-secondary text-white">📜 最近上报历史 (最多{{ history|length }}/{{ MAX_HISTORY }})</div>
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
                                        <th>原始数据</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for rec in history %}
                                    <tr>
                                        <td>{{ rec.received_at.split(' ')[1] }}</td>
                                        <td>{{ rec.account or '-' }}</td>
                                        <td>{{ rec.balance or '-' }}</td>
                                        <td>{{ rec.equity or '-' }}</td>
                                        <td>{{ rec.floating_pnl or '-' }}</td>
                                        <td><span class="badge-ip">{{ rec.ip }}</span></td>
                                        <td>
                                            <button class="btn btn-sm btn-outline-info" type="button" 
                                                    data-bs-toggle="collapse" data-bs-target="#raw-{{ loop.index }}" 
                                                    aria-expanded="false">预览</button>
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

            <!-- 右侧：指令队列 + 发单表单 -->
            <div class="col-lg-5 mb-4">
                <!-- 指令队列卡片 -->
                <div class="card mb-4">
                    <div class="card-header bg-success text-white d-flex justify-content-between align-items-center">
                        <span>⏳ 待发送指令队列 ({{ commands|length }})</span>
                        <form method="post" action="{{ url_for('clear_commands') }}" style="display:inline;">
                            <button type="submit" class="btn btn-sm btn-light" onclick="return confirm('确定清空所有指令？')">清空全部</button>
                        </form>
                    </div>
                    <div class="card-body">
                        {% if commands %}
                            {% for cmd in commands %}
                            <div class="command-item d-flex justify-content-between align-items-center">
                                <div>
                                    <strong>{{ cmd.direction }}</strong> {{ cmd.symbol }}  {{ cmd.volume }} 手
                                    {% if cmd.sl %} SL:{{ cmd.sl }}{% endif %}
                                    {% if cmd.tp %} TP:{{ cmd.tp }}{% endif %}
                                    <br><small class="text-muted">添加于 {{ cmd.timestamp }}</small>
                                </div>
                                <form method="post" action="{{ url_for('delete_command', index=loop.index0) }}" style="margin:0;">
                                    <button type="submit" class="btn btn-sm btn-outline-danger" onclick="return confirm('删除该指令？')">✖</button>
                                </form>
                            </div>
                            {% endfor %}
                        {% else %}
                            <p class="text-muted mb-0">队列为空，暂无待发指令。</p>
                        {% endif %}
                    </div>
                </div>

                <!-- 发单表单卡片 -->
                <div class="card">
                    <div class="card-header bg-warning">✍️ 下达新交易指令</div>
                    <div class="card-body">
                        <form method="post" action="{{ url_for('send_command') }}">
                            <div class="mb-2">
                                <label class="form-label">品种</label>
                                <input type="text" name="symbol" class="form-control form-control-sm" placeholder="EURUSD" required>
                            </div>
                            <div class="mb-2">
                                <label class="form-label">方向</label>
                                <select name="direction" class="form-select form-select-sm" required>
                                    <option value="BUY">买入 (BUY)</option>
                                    <option value="SELL">卖出 (SELL)</option>
                                </select>
                            </div>
                            <div class="mb-2">
                                <label class="form-label">手数</label>
                                <input type="number" step="0.01" min="0.01" name="volume" class="form-control form-control-sm" value="0.1" required>
                            </div>
                            <div class="row">
                                <div class="col mb-2">
                                    <label class="form-label">止损 (SL, 可选)</label>
                                    <input type="number" step="0.00001" name="sl" class="form-control form-control-sm" placeholder="例如 1.1050">
                                </div>
                                <div class="col mb-2">
                                    <label class="form-label">止盈 (TP, 可选)</label>
                                    <input type="number" step="0.00001" name="tp" class="form-control form-control-sm" placeholder="例如 1.1100">
                                </div>
                            </div>
                            <button type="submit" class="btn btn-primary w-100 mt-2">➡️ 加入指令队列</button>
                        </form>
                        <hr>
                        <p class="small text-muted mb-0">
                            * 指令将在下一次MT4上报时被取走。<br>
                            * 队列支持多条指令，会一次性全部返回（每行一条）。
                        </p>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Bootstrap JS (用于折叠组件) -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# ==================== 启动 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)  # 生产环境建议关闭debug
