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

def extract_latest_details(record):
    """从记录中提取详细字段，用于模板展示"""
    if not record or not record.get('parsed'):
        return None
    p = record['parsed']
    # 安全提取metrics
    metrics = p.get('metrics', {})
    return {
        'account': p.get('account'),
        'server': p.get('server'),
        'ts': p.get('ts'),
        'balance': p.get('balance'),
        'equity': p.get('equity'),
        'margin': p.get('margin'),
        'free_margin': p.get('free_margin'),
        'margin_level': p.get('margin_level'),
        'floating_pnl': p.get('floating_pnl'),
        'day_start_equity': p.get('day_start_equity'),
        'daily_pnl': p.get('daily_pnl'),
        'daily_return': p.get('daily_return'),
        'poll_latency_ms': metrics.get('poll_latency_ms'),
        'last_http_code': metrics.get('last_http_code'),
        'last_error': metrics.get('last_error'),
        # 保留原始解析对象以备扩展
        '_raw_parsed': p
    }

# ==================== 路由：主页 ====================
@app.route('/')
def index():
    """显示控制面板：最近上报数据、指令队列、发单表单"""
    with history_lock:
        hist_list = list(reversed(history))  # 最新的在前
        latest_record = hist_list[0] if hist_list else None
        latest_detail = extract_latest_details(latest_record)
    with commands_lock:
        cmds_copy = commands.copy()
    return render_template_string(
        HTML_TEMPLATE,
        history=hist_list,
        latest=latest_detail,
        latest_raw=latest_record,  # 传递原始记录用于原始预览等
        commands=cmds_copy,
        MAX_HISTORY=MAX_HISTORY
    )

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
        'body_raw': raw_body[:500] + ('...' if len(raw_body) > 500 else ''),
        'parsed': parsed_json,
        # 提取常用字段方便快速展示（表格中用）
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

    if not symbol or direction not in ['BUY', 'SELL'] or not volume:
        return redirect(url_for('index'))

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
    <title>MT4 远程交易执行面板 · 专业版</title>
    <!-- Bootstrap 5 CDN + 字体图标 -->
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
    </style>
</head>
<body>
    <div class="container">
        <h1 class="mb-3"><i class="bi bi-cpu"></i> MT4 远程交易执行 · 专业监控</h1>
        
        <!-- 接口说明信息框 -->
        <div class="info-box d-flex justify-content-between align-items-center">
            <div>
                <i class="bi bi-info-circle-fill me-2"></i>
                <strong>MT4上报接口：</strong> <code>POST /web/api/echo</code> 
                <span class="badge bg-secondary ms-2">等待指令返回</span>
                <span class="ms-3"><i class="bi bi-arrow-return-right"></i> 响应格式：纯文本，每行一条指令（如 <code>BUY,EURUSD,0.1,1.1050,1.1100</code>），无指令返回 <code>NOCOMMAND</code></span>
            </div>
            <span class="text-muted small">队列指令将在下次上报时被取走</span>
        </div>

        <!-- 最新上报详细数据卡片 (解析后) -->
        <div class="card mb-4 shadow-sm">
            <div class="card-header bg-primary text-white bg-gradient">
                <i class="bi bi-graph-up-arrow"></i> 最新账户状态 (解析数据)
            </div>
            <div class="card-body">
                {% if latest %}
                    <div class="stat-grid">
                        <!-- 基础信息 -->
                        <div class="stat-item"><span class="stat-label">账户</span><div class="stat-value">{{ latest.account or 'N/A' }}</div></div>
                        <div class="stat-item"><span class="stat-label">服务器</span><div class="stat-value">{{ latest.server or 'N/A' }}</div></div>
                        <div class="stat-item"><span class="stat-label">时间戳(ts)</span><div class="stat-value">{{ latest.ts or 'N/A' }}</div></div>
                        <!-- 资金数据 -->
                        <div class="stat-item"><span class="stat-label">余额(Balance)</span><div class="stat-value">{{ "%.2f"|format(latest.balance) if latest.balance is number else latest.balance }}</div></div>
                        <div class="stat-item"><span class="stat-label">净值(Equity)</span><div class="stat-value">{{ "%.2f"|format(latest.equity) if latest.equity is number else latest.equity }}</div></div>
                        <div class="stat-item"><span class="stat-label">已用预付款(Margin)</span><div class="stat-value">{{ "%.2f"|format(latest.margin) if latest.margin is number else latest.margin }}</div></div>
                        <div class="stat-item"><span class="stat-label">可用预付款(Free)</span><div class="stat-value">{{ "%.2f"|format(latest.free_margin) if latest.free_margin is number else latest.free_margin }}</div></div>
                        <div class="stat-item"><span class="stat-label">预付款比例(Margin Level)</span><div class="stat-value">{{ "%.2f"|format(latest.margin_level) if latest.margin_level is number else latest.margin_level }}%</div></div>
                        <div class="stat-item"><span class="stat-label">浮动盈亏</span><div class="stat-value">{{ "%.2f"|format(latest.floating_pnl) if latest.floating_pnl is number else latest.floating_pnl }}</div></div>
                        <div class="stat-item"><span class="stat-label">日初始净值</span><div class="stat-value">{{ "%.2f"|format(latest.day_start_equity) if latest.day_start_equity is number else latest.day_start_equity }}</div></div>
                        <div class="stat-item"><span class="stat-label">日盈亏</span><div class="stat-value">{{ "%.2f"|format(latest.daily_pnl) if latest.daily_pnl is number else latest.daily_pnl }}</div></div>
                        <div class="stat-item"><span class="stat-label">日盈亏率</span><div class="stat-value">{{ "%.5f"|format(latest.daily_return) if latest.daily_return is number else latest.daily_return }}</div></div>
                        <!-- 网络指标 -->
                        <div class="stat-item"><span class="stat-label">网络延迟(ms)</span><div class="stat-value">{{ "%.0f"|format(latest.poll_latency_ms) if latest.poll_latency_ms is number else latest.poll_latency_ms }}</div></div>
                        <div class="stat-item"><span class="stat-label">上次HTTP代码</span><div class="stat-value">{{ latest.last_http_code or 'N/A' }}</div></div>
                        {% if latest.last_error %}
                        <div class="stat-item"><span class="stat-label">错误信息</span><div class="stat-value text-danger" style="font-size:0.9rem;">{{ latest.last_error }}</div></div>
                        {% endif %}
                    </div>
                    <!-- 原始JSON预览（可折叠） -->
                    <div class="mt-3">
                        <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#rawJsonPreview" aria-expanded="false">
                            <i class="bi bi-code-slash"></i> 查看原始JSON
                        </button>
                        <div class="collapse mt-2" id="rawJsonPreview">
                            <pre class="bg-light p-3 rounded" style="font-size:0.75rem;">{{ latest_raw.body_raw if latest_raw else '无原始数据' }}</pre>
                        </div>
                    </div>
                {% else %}
                    <p class="text-muted"><i class="bi bi-exclamation-circle"></i> 尚未收到任何MT4上报数据，请等待终端上报。</p>
                {% endif %}
            </div>
        </div>

        <!-- 两列布局：左侧历史记录，右侧指令管理 -->
        <div class="row">
            <!-- 左侧：历史记录表 -->
            <div class="col-lg-7 mb-4">
                <div class="card shadow-sm h-100">
                    <div class="card-header bg-secondary text-white">
                        <i class="bi bi-clock-history"></i> 最近上报历史 ({{ history|length }}/{{ MAX_HISTORY }})
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
                <div class="card shadow-sm mb-4">
                    <div class="card-header bg-success text-white d-flex justify-content-between align-items-center">
                        <span><i class="bi bi-list-check"></i> 待发送指令队列 ({{ commands|length }})</span>
                        <form method="post" action="{{ url_for('clear_commands') }}" style="display:inline;">
                            <button type="submit" class="btn btn-sm btn-light" onclick="return confirm('确定清空所有指令？')"><i class="bi bi-trash"></i> 清空</button>
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
                                    <br><small class="text-muted"><i class="bi bi-clock"></i> {{ cmd.timestamp }}</small>
                                </div>
                                <form method="post" action="{{ url_for('delete_command', index=loop.index0) }}" style="margin:0;">
                                    <button type="submit" class="btn btn-sm btn-outline-danger" onclick="return confirm('删除该指令？')"><i class="bi bi-x"></i></button>
                                </form>
                            </div>
                            {% endfor %}
                        {% else %}
                            <p class="text-muted mb-0"><i class="bi bi-inbox"></i> 队列为空，暂无待发指令。</p>
                        {% endif %}
                    </div>
                </div>

                <!-- 发单表单卡片 -->
                <div class="card shadow-sm">
                    <div class="card-header bg-warning">
                        <i class="bi bi-pencil-square"></i> 下达新交易指令
                    </div>
                    <div class="card-body">
                        <form method="post" action="{{ url_for('send_command') }}">
                            <div class="mb-2">
                                <label class="form-label">品种 <span class="text-muted">(如 EURUSD)</span></label>
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
                                    <label class="form-label">止损 (SL)</label>
                                    <input type="number" step="0.00001" name="sl" class="form-control form-control-sm" placeholder="可选">
                                </div>
                                <div class="col mb-2">
                                    <label class="form-label">止盈 (TP)</label>
                                    <input type="number" step="0.00001" name="tp" class="form-control form-control-sm" placeholder="可选">
                                </div>
                            </div>
                            <button type="submit" class="btn btn-primary w-100 mt-2"><i class="bi bi-send"></i> 加入指令队列</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Bootstrap JS -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# ==================== 启动 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)  # 生产环境建议关闭debug
