from flask import Flask, request, jsonify, render_template, redirect, url_for
from datetime import datetime

app = Flask(__name__)


# 内存中简单保存最近一次 MT4 上报的数据
latest_report = {
    "headers": {},
    "body": {},
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
    return render_template("index.html", report=latest_report)


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

