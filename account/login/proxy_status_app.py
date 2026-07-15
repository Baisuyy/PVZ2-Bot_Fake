import concurrent.futures
import os
import socket
import time

from flask import Flask, jsonify, render_template, request

from proxy_pool import 获取代理节点列表, 获取代理端口

app = Flask(__name__)


def _ok(data):
    return jsonify({"ok": True, **data})


def _check_proxy_node(node: dict, port: int, timeout: float = 2.0) -> dict:
    ip = (node.get("ip") or "").strip()
    name = (node.get("name") or "").strip()
    started = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            latency_ms = int((time.perf_counter() - started) * 1000)
            return {
                "name": name,
                "ip": ip,
                "port": port,
                "online": True,
                "latency_ms": latency_ms,
                "reason": "",
            }
    except Exception as error:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "name": name,
            "ip": ip,
            "port": port,
            "online": False,
            "latency_ms": latency_ms,
            "reason": str(error),
        }


def _collect_proxy_status(timeout: float = 2.0) -> dict:
    nodes = 获取代理节点列表()
    port = 获取代理端口()
    max_workers = min(20, max(1, len(nodes)))
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_check_proxy_node, node, port, timeout) for node in nodes]
        for future in concurrent.futures.as_completed(futures):
            rows.append(future.result())

    rows.sort(key=lambda x: x["name"])
    online = sum(1 for item in rows if item["online"])
    total = len(rows)
    return {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": total,
        "online": online,
        "offline": total - online,
        "rows": rows,
    }


@app.route("/", methods=["GET"])
def proxy_status_page():
    return render_template("proxy_status.html")


@app.route("/api/proxy/status", methods=["GET"])
def api_proxy_status():
    try:
        timeout = float(request.args.get("timeout", "2.0"))
    except Exception:
        timeout = 2.0
    timeout = max(0.3, min(timeout, 10.0))
    return _ok(_collect_proxy_status(timeout=timeout))


if __name__ == "__main__":
    debug_mode = os.getenv("PVZ2_PROXY_DEBUG", "0") == "1"
    port = int(os.getenv("PVZ2_PROXY_STATUS_PORT", "7861"))
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
