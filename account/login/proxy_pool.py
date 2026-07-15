import os
import time
from itertools import cycle
from threading import Lock
from urllib.parse import urlparse

import requests


代理节点列表 = [
    {"name": "服务器01", "ip": "111.230.238.197"},
    {"name": "服务器02", "ip": "43.138.240.153"},
    {"name": "服务器03", "ip": "43.139.94.85"},
    {"name": "服务器04", "ip": "119.29.142.146"},
    {"name": "服务器05", "ip": "43.139.87.138"},
    {"name": "服务器06", "ip": "43.139.141.139"},
    {"name": "服务器07", "ip": "43.138.249.7"},
    {"name": "服务器08", "ip": "43.139.11.231"},
    {"name": "服务器09", "ip": "43.139.145.56"},
    {"name": "服务器10", "ip": "119.29.90.54"},
    {"name": "服务器11", "ip": "119.91.95.189"},
    {"name": "服务器12", "ip": "43.139.109.7"},
    {"name": "服务器13", "ip": "119.29.13.154"},
    {"name": "服务器14", "ip": "43.136.119.66"},
    {"name": "服务器15", "ip": "106.52.19.152"},
    {"name": "服务器16", "ip": "106.52.242.17"},
    {"name": "服务器17", "ip": "43.139.128.209"},
    {"name": "服务器18", "ip": "43.139.26.140"},
    {"name": "服务器19", "ip": "119.29.89.35"},
    {"name": "服务器20", "ip": "129.204.203.91"},
    {"name": "服务器21", "ip": "106.53.43.251"},
    {"name": "服务器22", "ip": "119.29.117.138"},
    {"name": "服务器23", "ip": "119.29.216.172"},
    {"name": "服务器24", "ip": "111.230.98.248"},
    {"name": "服务器25", "ip": "114.132.85.168"},
    {"name": "服务器26", "ip": "101.33.227.96"},
    {"name": "服务器27", "ip": "106.55.44.158"},
    {"name": "服务器28", "ip": "159.75.179.214"},
    {"name": "服务器29", "ip": "129.204.145.73"},
    {"name": "服务器30", "ip": "129.204.149.158"},
    {"name": "服务器31", "ip": "106.53.1.25"},
    {"name": "服务器32", "ip": "175.178.52.13"},
    {"name": "服务器33", "ip": "129.204.60.155"},
    {"name": "服务器34", "ip": "1.14.161.53"},
    {"name": "服务器35", "ip": "129.204.154.171"},
    {"name": "服务器36", "ip": "1.12.37.192"},
    {"name": "服务器37", "ip": "42.193.130.114"},
    {"name": "服务器38", "ip": "119.91.145.72"},
    {"name": "服务器39", "ip": "43.139.27.183"},
    {"name": "服务器40", "ip": "203.195.240.154"},
]
代理IP列表 = [item["ip"] for item in 代理节点列表]

_启用锁 = Lock()
_轮询锁 = Lock()
_故障锁 = Lock()
_已启用 = False
_原始请求方法 = requests.sessions.Session.request
_代理URL列表 = []
_代理循环 = None
_故障截止时间 = {}


def _读取代理池():
    raw = os.getenv("PVZ2_PROXY_HOSTS", "").strip()
    if raw:
        hosts = [item.strip() for item in raw.split(",") if item.strip()]
        if hosts:
            return hosts
    return 代理IP列表


def 获取代理节点列表():
    raw = os.getenv("PVZ2_PROXY_HOSTS", "").strip()
    if raw:
        hosts = [item.strip() for item in raw.split(",") if item.strip()]
        return [{"name": f"代理{i + 1:02d}", "ip": host} for i, host in enumerate(hosts)]
    return list(代理节点列表)


def 获取代理端口() -> int:
    try:
        return int(os.getenv("PVZ2_PROXY_PORT", "39001"))
    except Exception:
        return 39001


def _构建代理URL(ip: str) -> str:
    user = os.getenv("PVZ2_PROXY_USER", "proxyuser")
    password = os.getenv("PVZ2_PROXY_PASS", "proxypass")
    port = os.getenv("PVZ2_PROXY_PORT", "39001")
    return f"http://{user}:{password}@{ip}:{port}"


_代理URL列表 = [_构建代理URL(ip) for ip in _读取代理池()]
_代理循环 = cycle(_代理URL列表)


def _下一个代理() -> str:
    with _轮询锁:
        return next(_代理循环)


def _是否本地地址(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _是否应直连(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    # 机器人 OpenAPI 必须使用应用白名单来源 IP，避免被代理改写出口
    direct_hosts = (
        "api.sgroup.qq.com",
        "sandbox.api.sgroup.qq.com",
        "bots.qq.com",
    )
    return host.endswith(direct_hosts)


def _代理主机(proxy_url: str) -> str:
    return (urlparse(proxy_url).hostname or proxy_url).lower()


def _熔断秒数() -> int:
    try:
        return max(1, int(os.getenv("PVZ2_PROXY_COOLDOWN_SECONDS", "120")))
    except Exception:
        return 120


def _最大尝试次数() -> int:
    try:
        default = max(1, len(_代理URL列表))
        return max(1, min(len(_代理URL列表), int(os.getenv("PVZ2_PROXY_MAX_RETRIES", str(default)))))
    except Exception:
        return max(1, len(_代理URL列表))


def _标记代理失败(proxy_url: str) -> None:
    with _故障锁:
        _故障截止时间[_代理主机(proxy_url)] = time.time() + _熔断秒数()


def _代理是否可用(proxy_url: str) -> bool:
    with _故障锁:
        ts = _故障截止时间.get(_代理主机(proxy_url), 0)
    return time.time() >= ts


def _下一个可用代理() -> str:
    if not _代理URL列表:
        raise RuntimeError("代理池为空")
    for _ in range(len(_代理URL列表)):
        proxy_url = _下一个代理()
        if _代理是否可用(proxy_url):
            return proxy_url
    # 全部都在熔断窗口里时，仍然返回一个继续尝试，避免完全卡死
    return _下一个代理()


def 启用全局代理() -> None:
    global _已启用
    if _已启用:
        return
    with _启用锁:
        if _已启用:
            return

        def _代理请求(session, method, url, **kwargs):
            if _是否应直连(url) or kwargs.get("proxies"):
                return _原始请求方法(session, method, url, **kwargs)

            session.trust_env = False
            last_error = None
            max_tries = _最大尝试次数()
            for _ in range(max_tries):
                proxy_url = _下一个可用代理()
                kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
                try:
                    return _原始请求方法(session, method, url, **kwargs)
                except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
                    _标记代理失败(proxy_url)
                    last_error = error
                    continue
            if last_error is not None:
                raise last_error
            return _原始请求方法(session, method, url, **kwargs)

        requests.sessions.Session.request = _代理请求
        _已启用 = True
