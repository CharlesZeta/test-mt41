from flask import Flask, request, jsonify, render_template_string, redirect, url_for
from datetime import datetime

app = Flask(__name__)


# 直接把前端页面模板嵌入到此文件中，方便单文件部署 / 提交 GitHub
INDEX_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>MT4 远程交易监控与下单面板</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --bg: #0f172a;
            --bg-elevated: #111827;
            --accent: #3b82f6;
            --accent-soft: rgba(59, 130, 246, 0.1);
            --border: #1f2937;
            --text: #e5e7eb;
            --text-soft: #9ca3af;
            --danger: #ef4444;
            --success: #22c55e;
            --warning: #f59e0b;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: radial-gradient(circle at top, #1d283a 0, #020617 55%, #000 100%);
            color: var(--text);
        }
        .page {
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        header {
            padding: 16px 24px;
            border-bottom: 1px solid rgba(148, 163, 184, 0.2);
            display: flex;
            align-items: center;
            justify-content: space-between;
            backdrop-filter: blur(16px);
            background: linear-gradient(to right, rgba(15, 23, 42, 0.9), rgba(15, 23, 42, 0.7));
            position: sticky;
            top: 0;
            z-index: 10;
        }
        .logo {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .logo-icon {
            width: 32px;
            height: 32px;
            border-radius: 10px;
            background: conic-gradient(from 210deg, #3b82f6, #22c55e, #a855f7, #3b82f6);
            position: relative;
            overflow: hidden;
        }
        .logo-icon::after {
            content: "";
            position: absolute;
            inset: 2px;
            border-radius: 8px;
            background: radial-gradient(circle at 30% 20%, rgba(148, 163, 184, 0.7), transparent 55%);
        }
        .logo-text-main {
            font-weight: 600;
            letter-spacing: 0.03em;
        }
        .logo-text-sub {
            font-size: 12px;
            color: var(--text-soft);
        }
        main {
            flex: 1;
            padding: 24px;
            max-width: 1200px;
            margin: 0 auto;
            width: 100%;
        }
        .grid {
            display: grid;
            grid-template-columns: 2.1fr 1.4fr;
            gap: 20px;
        }
        @media (max-width: 960px) {
            .grid {
                grid-template-columns: 1fr;
            }
        }
        .card {
            background: radial-gradient(circle at top left, rgba(148, 163, 184, 0.16), transparent 55%), var(--bg-elevated);
            border-radius: 16px;
            padding: 18px 18px 16px;
            border: 1px solid rgba(31, 41, 55, 0.9);
            box-shadow: 0 18px 40px rgba(15, 23, 42, 0.7);
        }
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .card-title {
            font-size: 15px;
            font-weight: 600;
        }
        .card-subtitle {
            font-size: 12px;
            color: var(--text-soft);
        }
        .pill {
            padding: 3px 10px;
            border-radius: 999px;
            font-size: 11px;
            border: 1px solid rgba(148, 163, 184, 0.35);
            color: var(--text-soft);
        }
        .pill-success {
            border-color: rgba(34, 197, 94, 0.55);
            color: var(--success);
            background: rgba(22, 163, 74, 0.08);
        }
        .pill-warning {
            border-color: rgba(245, 158, 11, 0.55);
            color: var(--warning);
            background: rgba(245, 158, 11, 0.06);
        }
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            margin-top: 8px;
        }
        @media (max-width: 960px) {
            .metrics-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }
        }
        .metric {
            padding: 8px 9px;
            border-radius: 12px;
            border: 1px solid rgba(31, 41, 55, 0.9);
            background: radial-gradient(circle at top, rgba(15, 23, 42, 0.95), rgba(15, 23, 42, 0.96));
        }
        .metric-label {
            font-size: 11px;
            color: var(--text-soft);
        }
        .metric-value {
            margin-top: 3px;
            font-size: 15px;
            font-weight: 500;
        }
        .metric-value.positive {
            color: var(--success);
        }
        .metric-value.negative {
            color: var(--danger);
        }
        .section-title {
            margin-top: 16px;
            margin-bottom: 6px;
            font-size: 12px;
            color: var(--text-soft);
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .table-like {
            border-radius: 12px;
            border: 1px solid rgba(31, 41, 55, 0.9);
            overflow: hidden;
            font-size: 12px;
        }
        .table-row {
            display: grid;
            grid-template-columns: 150px 1fr;
            padding: 7px 10px;
            border-bottom: 1px solid rgba(31, 41, 55, 0.9);
        }
        .table-row:nth-child(even) {
            background: rgba(15, 23, 42, 0.96);
        }
        .table-key {
            color: var(--text-soft);
        }
        .table-value {
            word-break: break-all;
        }
        .table-row:last-child {
            border-bottom: none;
        }
        form {
            margin-top: 6px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .field-group {
            display: flex;
            gap: 8px;
        }
        .field {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        label {
            font-size: 12px;
            color: var(--text-soft);
        }
        input, select, textarea {
            border-radius: 10px;
            border: 1px solid rgba(31, 41, 55, 0.9);
            background: rgba(15, 23, 42, 0.96);
            color: var(--text);
            font-size: 13px;
            padding: 7px 9px;
            outline: none;
            transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
        }
        input:focus, select:focus, textarea:focus {
            border-color: rgba(59, 130, 246, 0.9);
            box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.6);
            background: rgba(15, 23, 42, 0.98);
        }
        button {
            border-radius: 999px;
            border: none;
            padding: 8px 14px;
            font-size: 13px;
            font-weight: 500;
            background: linear-gradient(135deg, #2563eb, #0ea5e9);
            color: white;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            box-shadow: 0 8px 18px rgba(37, 99, 235, 0.45);
            transition: transform 0.08s ease, box-shadow 0.08s ease, filter 0.08s ease;
        }
        button:hover {
            transform: translateY(-1px);
            filter: brightness(1.05);
            box-shadow: 0 12px 30px rgba(37, 99, 235, 0.65);
        }
        button:active {
            transform: translateY(0);
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.9);
        }
        .btn-secondary {
            background: rgba(15, 23, 42, 1);
            border: 1px solid rgba(31, 41, 55, 0.9);
            box-shadow: none;
        }
        .btn-secondary:hover {
            filter: none;
            background: rgba(15, 23, 42, 1);
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.85);
        }
        .status-line {
            margin-top: 8px;
            font-size: 12px;
            min-height: 18px;
        }
        .status-success { color: var(--success); }
        .status-error { color: var(--danger); }
        footer {
            padding: 10px 24px 18px;
            font-size: 11px;
            color: rgba(148, 163, 184, 0.8);
            text-align: right;
        }
        code {
            font-size: 11px;
            background: rgba(15, 23, 42, 0.96);
            padding: 2px 4px;
            border-radius: 4px;
        }
    </style>
</head>
<body>
<div class="page">
    <header>
        <div class="logo">
            <div class="logo-icon"></div>
            <div>
                <div class="logo-text-main">MT4 Remote Trade Hub</div>
                <div class="logo-text-sub">Flask 网关 · 账户监控 & 指令下发</div>
            </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
            <span class="pill">
                上次心跳：
                {% if report.received_at %}
                    {{ report.received_at }}
                {% else %}
                    暂无
                {% endif %}
            </span>
        </div>
    </header>
    <main>
        <div class="grid">
            <!-- 左侧：账户与风控信息 -->
            <section class="card">
                <div class="card-header">
                    <div>
                        <div class="card-title">账户概览</div>
                        <div class="card-subtitle">展示最近一次 MT4 上报的净值、保证金、风险水平等</div>
                    </div>
                    {% if report.body and report.body.account %}
                        <div class="pill-success">
                            账号：{{ report.body.account }} · {{ report.body.server }}
                        </div>
                    {% else %}
                        <div class="pill-warning">
                            等待 MT4 调用 <code>/web/api/echo</code> 上报数据
                        </div>
                    {% endif %}
                </div>

                {% if report.body and report.body.account %}
                    {% set b = report.body %}
                    <div class="metrics-grid">
                        <div class="metric">
                            <div class="metric-label">Balance / Equity</div>
                            <div class="metric-value">
                                {{ "%.2f"|format(b.balance) }} / {{ "%.2f"|format(b.equity) }}
                            </div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Free Margin</div>
                            <div class="metric-value">{{ "%.2f"|format(b.free_margin) }}</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Used Margin</div>
                            <div class="metric-value">{{ "%.2f"|format(b.margin) }}</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">Margin Level</div>
                            <div class="metric-value {% if b.margin_level < 150 %}negative{% elif b.margin_level < 300 %}warning{% else %}positive{% endif %}">
                                {{ "%.1f"|format(b.margin_level) }}%
                            </div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">浮动盈亏</div>
                            <div class="metric-value {% if b.floating_pnl >= 0 %}positive{% else %}negative{% endif %}">
                                {{ "%.2f"|format(b.floating_pnl) }}
                            </div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">当日盈亏</div>
                            <div class="metric-value {% if b.daily_pnl >= 0 %}positive{% else %}negative{% endif %}">
                                {{ "%.2f"|format(b.daily_pnl) }}
                            </div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">当日收益率</div>
                            <div class="metric-value {% if b.daily_return >= 0 %}positive{% else %}negative{% endif %}">
                                {{ "%.3f"|format(b.daily_return * 100) }}%
                            </div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">杠杆 / 敞口</div>
                            <div class="metric-value">
                                {{ "%.1f"|format(b.leverage_used) }} ×
                            </div>
                        </div>
                    </div>

                    <div class="section-title">风险标记 & 性能指标</div>
                    <div class="table-like">
                        <div class="table-row">
                            <div class="table-key">风险标记</div>
                            <div class="table-value">{{ b.risk_flags or "-" }}</div>
                        </div>
                        <div class="table-row">
                            <div class="table-key">最近 HTTP 状态</div>
                            <div class="table-value">
                                {% if b.metrics %}
                                    {{ b.metrics.last_http_code }} · {{ b.metrics.last_error }}
                                {% else %}
                                    -
                                {% endif %}
                            </div>
                        </div>
                        <div class="table-row">
                            <div class="table-key">最近轮询延迟</div>
                            <div class="table-value">
                                {% if b.metrics %}
                                    {{ "%.0f"|format(b.metrics.poll_latency_ms) }} ms
                                {% else %}
                                    -
                                {% endif %}
                            </div>
                        </div>
                        <div class="table-row">
                            <div class="table-key">暴露名义本金</div>
                            <div class="table-value">
                                {{ "%.0f"|format(b.exposure_notional) }}
                            </div>
                        </div>
                    </div>
                {% else %}
                    <p style="font-size:13px;color:var(--text-soft);margin-top:8px;">
                        暂无账户数据。请在 MT4 EA 中向
                        <code>POST /web/api/echo</code>
                        发送你示例中的 JSON，以便在这里查看实时风控信息。
                    </p>
                {% endif %}

                <div class="section-title">原始 Headers（调试用）</div>
                <div class="table-like" style="max-height:140px;overflow:auto;">
                    {% if report.headers %}
                        {% for k, v in report.headers.items() %}
                            <div class="table-row">
                                <div class="table-key">{{ k }}</div>
                                <div class="table-value">{{ v }}</div>
                            </div>
                        {% endfor %}
                    {% else %}
                        <div class="table-row">
                            <div class="table-key">提示</div>
                            <div class="table-value">尚未收到任何请求</div>
                        </div>
                    {% endif %}
                </div>

                <div class="section-title">原始 Body JSON（数据浏览）</div>
                <div class="table-like" style="max-height:200px;overflow:auto;">
                    {% if report.body %}
                        <pre style="margin:0;padding:8px 10px;white-space:pre;overflow:auto;
                                   font-family:SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;
                                   font-size:11px;line-height:1.4;">
{{ report.body | tojson(indent=2) }}
                        </pre>
                    {% else %}
                        <div class="table-row">
                            <div class="table-key">提示</div>
                            <div class="table-value">尚未收到任何 Body 数据</div>
                        </div>
                    {% endif %}
                </div>

                <div class="section-title">原始 Raw JSON 字符串（完整请求体）</div>
                <div class="table-like" style="max-height:200px;overflow:auto;">
                    {% if report.raw_body %}
                        <pre style="margin:0;padding:8px 10px;white-space:pre-wrap;word-break:break-all;overflow:auto;
                                   font-family:SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace;
                                   font-size:11px;line-height:1.4;">
{{ report.raw_body }}
                        </pre>
                    {% else %}
                        <div class="table-row">
                            <div class="table-key">提示</div>
                            <div class="table-value">尚未收到任何 Raw JSON 数据</div>
                        </div>
                    {% endif %}
                </div>
            </section>

            <!-- 右侧：下单面板 -->
            <section class="card">
                <div class="card-header">
                    <div>
                        <div class="card-title">下单指令面板</div>
                        <div class="card-subtitle">通过 Web 下发指令，由 MT4 远程执行</div>
                    </div>
                    <button class="btn-secondary" type="button" onclick="resetForm()">
                        重置
                    </button>
                </div>

                <form id="trade-form">
                    <div class="field-group">
                        <div class="field">
                            <label for="symbol">交易品种 Symbol</label>
                            <input id="symbol" name="symbol" placeholder="例如：EURUSD、XAUUSD" required>
                        </div>
                        <div class="field">
                            <label for="order_type">方向</label>
                            <select id="order_type" name="order_type" required>
                                <option value="BUY">BUY（做多）</option>
                                <option value="SELL">SELL（做空）</option>
                            </select>
                        </div>
                    </div>

                    <div class="field-group">
                        <div class="field">
                            <label for="volume">手数 Volume</label>
                            <input id="volume" name="volume" type="number" step="0.01" min="0.01" value="0.10" required>
                        </div>
                        <div class="field">
                            <label for="price">价格（可选，留空为市价）</label>
                            <input id="price" name="price" type="number" step="0.00001" placeholder="市价可留空">
                        </div>
                    </div>

                    <div class="field-group">
                        <div class="field">
                            <label for="sl">止损 SL（可选）</label>
                            <input id="sl" name="sl" type="number" step="0.00001">
                        </div>
                        <div class="field">
                            <label for="tp">止盈 TP（可选）</label>
                            <input id="tp" name="tp" type="number" step="0.00001">
                        </div>
                    </div>

                    <div class="field">
                        <label for="comment">订单备注（可选）</label>
                        <textarea id="comment" name="comment" rows="2" placeholder="例如：来自 Web 面板的信号、策略名等"></textarea>
                    </div>

                    <div style="display:flex;align-items:center;justify-content:space-between;margin-top:2px;">
                        <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-soft);">
                            <span style="width:6px;height:6px;border-radius:999px;background:#22c55e;"></span>
                            <span>建议由 MT4 EA 轮询一个 <code>/api/trade</code> 等待队列来执行这些指令</span>
                        </div>
                        <button type="submit">
                            发送下单指令
                        </button>
                    </div>
                </form>

                <div id="status" class="status-line"></div>

                <div class="section-title">如何与 MT4 集成（思路）</div>
                <ul style="margin:0 0 4px 18px;padding:0;font-size:11px;color:var(--text-soft);line-height:1.5;">
                    <li>EA 端：定时向 <code>/web/api/echo</code> 上报账户状态（你给的 JSON）</li>
                    <li>EA 端：每隔 N 秒调用一个（你自定义）<code>/api/next_order</code> 接口取未执行指令</li>
                    <li>服务器端：在 <code>/api/trade</code> 中把指令写入数据库/队列，<code>/api/next_order</code> 负责派发</li>
                </ul>
            </section>
        </div>
    </main>
    <footer>
        运行方式：<code>python app.py</code> &nbsp;·&nbsp; 默认监听 <code>http://127.0.0.1:5000/</code>
    </footer>
</div>

<script>
    function resetForm() {
        document.getElementById('trade-form').reset();
        setStatus('', '');
    }

    function setStatus(message, type) {
        const el = document.getElementById('status');
        el.textContent = message || '';
        el.className = 'status-line' + (type ? ' status-' + type : '');
    }

    document.getElementById('trade-form').addEventListener('submit', async function (e) {
        e.preventDefault();
        setStatus('正在提交指令...', '');

        const formData = new FormData(this);

        try {
            const res = await fetch('/api/trade', {
                method: 'POST',
                body: formData
            });
            const data = await res.json();
            if (!res.ok || data.status !== 'ok') {
                const msg = (data && data.errors) ? data.errors.join('；') : '未知错误';
                setStatus('下单失败：' + msg, 'error');
            } else {
                setStatus('下单指令已接受：' + JSON.stringify(data.order), 'success');
            }
        } catch (err) {
            setStatus('网络错误或服务器异常：' + err, 'error');
        }
    });
</script>
</body>
</html>
"""


# 内存中简单保存最近一次 MT4 上报的数据
latest_report = {
    "headers": {},
    "body": {},
    "raw_body": "",
    "received_at": None,
}

# 内存中保存待执行的下单指令队列（真实环境建议改为数据库/Redis 等）
pending_orders = []


@app.route("/")
def index():
    """
    网页端首页：
    - 展示最近一次 MT4 上报的账户/风险数据
    - 提供下单表单，发送到 /api/trade
    """
    return render_template_string(INDEX_HTML, report=latest_report)


@app.route("/web/api/echo", methods=["POST"])
def mt4_echo():
    """
    供 MT4 远程执行模块调用的上报接口
    按你提供的示例：
    - header 在 request.headers
    - body 为 JSON（或 x-www-form-urlencoded 里只有一个 JSON 字符串也能兼容）
    """
    global latest_report

    # 记录 headers（转成普通 dict 方便在模板中展示）
    headers_dict = {k: v for k, v in request.headers.items()}

    # 原始请求体（字节 -> 字符串），用于页面上展示“raw JSON”
    raw_body_text = request.get_data(as_text=True) or ""

    # 兼容两种 body 格式：
    # 1) Content-Type: application/json  => request.get_json()
    # 2) Content-Type: application/x-www-form-urlencoded 且只有一个 JSON 字符串字段
    body_data = None
    if request.is_json:
        body_data = request.get_json(silent=True)
    else:
        # 尝试从 form 里取第一个字段并当作 JSON
        try:
            from json import loads

            if request.form:
                # 取第一个 key 的 value
                first_key = next(iter(request.form.keys()))
                body_data = loads(first_key)
            else:
                body_data = {}
        except Exception:
            body_data = {}

    latest_report = {
        "headers": headers_dict,
        "body": body_data or {},
        "raw_body": raw_body_text,
        "received_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    # 这里简单回显，真实环境你可以按 MT4 EA 的协议返回内容
    return jsonify(
        {
            "status": "ok",
            "message": "report received",
            "data": latest_report["body"],
        }
    )


@app.route("/api/trade", methods=["POST"])
def trade_api():
    """
    网页端提交下单指令到此接口。
    返回 JSON，供前端显示“下单成功/失败”。
    实际对接 MT4 时，你可以：
    - 把指令写到数据库/队列，由 EA 定时拉取并执行
    - 或直接让 EA 调用这个接口，接收指令后立即执行
    """
    data = request.form or request.json or {}

    symbol = data.get("symbol")
    order_type = data.get("order_type")  # BUY / SELL
    volume = data.get("volume")
    price = data.get("price")  # 可选：市价可以不填
    sl = data.get("sl")  # 止损
    tp = data.get("tp")  # 止盈
    comment = data.get("comment", "")

    # 这里只做演示性的校验和 echo
    errors = []
    if not symbol:
        errors.append("交易品种(symbol)不能为空")
    if order_type not in ("BUY", "SELL"):
        errors.append("订单方向(order_type)必须为 BUY 或 SELL")
    try:
        volume_val = float(volume)
        if volume_val <= 0:
            errors.append("手数(volume)必须大于 0")
    except (TypeError, ValueError):
        errors.append("手数(volume)必须为数字")

    if errors:
        return jsonify({"status": "error", "errors": errors}), 400

    trade_order = {
        "symbol": symbol,
        "order_type": order_type,
        "volume": volume_val,
        "price": price,
        "sl": sl,
        "tp": tp,
        "comment": comment,
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    # 简单写入内存队列，供 MT4 EA 轮询 /api/next_order 拉取执行
    pending_orders.append(trade_order)

    return jsonify({"status": "ok", "order": trade_order})


@app.route("/api/next_order", methods=["GET"])
def next_order():
    """
    供 MT4 EA 轮询调用：
    - 如果有尚未执行的订单，则弹出（pop）一条返回
    - 如果没有，则返回 status=no_order
    真实环境建议增加鉴权（token）、账户绑定等逻辑
    """
    if not pending_orders:
        return jsonify({"status": "no_order"})

    order = pending_orders.pop(0)
    return jsonify({"status": "ok", "order": order})


if __name__ == "__main__":
    # 开发环境运行，生产可以用 gunicorn / waitress 等 WSGI 服务器
    app.run(host="0.0.0.0", port=5000, debug=True)

