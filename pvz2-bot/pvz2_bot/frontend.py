"""
控制台前端 — Flask 5555 端口
CDK 卡密验证 + 任务提交 + 节点状态 + 查询
"""
import json, logging, os, time, hashlib, secrets, string
import requests
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
from functools import wraps
from .cdk_manager import cdk_bp, init_cdk_db
from .config import UNIFIED_PASSWORD

logger = logging.getLogger("frontend")

SCHEDULER_URL = os.getenv("PVZ_SCHEDULER_URL", "http://127.0.0.1:39900").rstrip("/")
API_TOKEN = os.getenv("API_TOKEN", UNIFIED_PASSWORD)

FAILED_LIMIT = 5
LOCKOUT_SECONDS = 600
ADMIN_PASSWORD = "www555"

app = Flask(__name__, template_folder=None)
app.secret_key = "frontend_secret_2024_v1"
app.config["SESSION_COOKIE_NAME"] = "pvz2_admin_session"
app.register_blueprint(cdk_bp, url_prefix="/api")
init_cdk_db()

ip_records = {}
task_progress: dict[str, dict] = {}  # cdk -> {task_id, node_url, status, done, total}
req_session = requests.Session()

def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else request.remote_addr

def _check_ip(ip):
    now = time.time()
    rec = ip_records.get(ip)
    if rec and now < rec["lock_until"]:
        return False, round(rec["lock_until"] - now)
    return True, 0

def _fail_ip(ip):
    now = time.time()
    rec = ip_records.get(ip, {"count": 0, "lock_until": 0})
    rec["count"] += 1
    if rec["count"] >= FAILED_LIMIT:
        rec["lock_until"] = now + LOCKOUT_SECONDS
        logger.warning("IP %s 已封禁 %ds (连续失败%d次)", ip, LOCKOUT_SECONDS, rec["count"])
    ip_records[ip] = rec

def _reset_ip(ip):
    ip_records.pop(ip, None)

# ════════════════════════════════════════════════
#  API
# ════════════════════════════════════════════════

@app.route("/api/verify", methods=["POST"])
def api_verify():
    """验证卡密是否存在且未使用"""
    ip = _client_ip()
    ok, wait = _check_ip(ip)
    if not ok:
        return jsonify({"status":"error","message":f"操作过频，请{wait}秒后重试"}),429

    data = request.json or {}
    cdk = data.get("cdk_code","").strip()
    if not cdk:
        return jsonify({"status":"error","message":"请输入CDK"}),400

    from .cdk_manager import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT cdk_code,amount,is_used,used_time,used_for_level FROM cdk_list WHERE cdk_code=?",
            (cdk,)
        ).fetchone()
        if not row:
            _fail_ip(ip)
            return jsonify({"status":"error","message":"卡密不存在"}),404
        is_used = row["is_used"] == 1
        return jsonify({
            "status":"success" if not is_used else "used",
            "data":{
                "cdk_code":row["cdk_code"],
                "amount":row["amount"],
                "is_used":is_used,
                "used_time":row["used_time"],
                "used_for_level":row["used_for_level"],
            }
        })
    finally:
        conn.close()

@app.route("/api/submit", methods=["POST"])
def api_submit():
    """验证 + 核销卡密 → 转发调度层"""
    ip = _client_ip()
    ok, wait = _check_ip(ip)
    if not ok:
        return jsonify({"status":"error","message":f"操作过频，请{wait}秒后重试"}),429

    data = request.json or {}
    cdk = data.get("cdk_code","").strip()
    platform = data.get("platform","").lower()
    level_id = data.get("level_id","").strip()
    task_type = data.get("type","like")

    if not all([cdk, platform, level_id]):
        return jsonify({"status":"error","message":"参数不完整"}),400
    if platform not in ("android","ios"):
        return jsonify({"status":"error","message":"平台仅支持android/ios"}),400

    from .cdk_manager import get_conn
    conn = get_conn()
    try:
        row = conn.execute("SELECT amount,is_used FROM cdk_list WHERE cdk_code=?",(cdk,)).fetchone()
        if not row:
            _fail_ip(ip)
            return jsonify({"status":"error","message":"卡密不存在"}),404
        if row["is_used"] == 1:
            return jsonify({"status":"error","message":"该卡密已被使用"}),400

        amount = row["amount"]
        target_info = f"{platform}|{level_id}"
        conn.execute(
            "UPDATE cdk_list SET is_used=1,used_time=datetime('now','localtime'),used_for_level=? WHERE cdk_code=?",
            (target_info, cdk)
        )
        conn.commit()
        _reset_ip(ip)

        # 转发调度层
        payload = {
            "platform": platform, "level_id": level_id,
            "count": amount, "type": task_type,
        }
        headers = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type":"application/json"}
        try:
            resp = req_session.post(
                f"{SCHEDULER_URL}/api/tasks",
                json=payload, headers=headers, timeout=15
            )
            if resp.status_code == 200:
                res = resp.json()
                node_name = (res.get("node") or {}).get("name","") or res.get("node_assigned","未知")
                node_ip = (res.get("node") or {}).get("public_ip","127.0.0.1")
                task_id = res.get("task_id", "")
                # 存进度追踪
                if task_id:
                    task_progress[cdk] = {
                        "task_id": task_id,
                        "node_ip": node_ip,
                        "status": "queued",
                        "done": 0,
                        "total": amount,
                        "level_id": level_id,
                    }
                logger.info("任务OK ip=%s cdk=%s node=%s task=%s", ip, cdk[:8]+"****", node_name, task_id)
                return jsonify({"status":"success","message":f"任务已接单！节点: {node_name}","task_id":task_id})
            raise Exception(f"调度返回 {resp.status_code}")
        except requests.Timeout:
            logger.error("调度超时 ip=%s", ip)
            return jsonify({"status":"error","message":"系统繁忙，卡密已保留请稍后重试"}),504
        except Exception as e:
            logger.error("调度失败 ip=%s err=%s", ip, e)
            return jsonify({"status":"error","message":f"节点异常: {str(e)[:50]}"}),502
    finally:
        conn.close()

@app.route("/api/query", methods=["GET"])
def api_query():
    """查询CDK状态"""
    cdk = request.args.get("cdk_code","").strip()
    if not cdk:
        return jsonify({"status":"error","message":"请输入CDK"}),400
    from .cdk_manager import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT cdk_code,amount,is_used,used_time,used_for_level FROM cdk_list WHERE cdk_code=?",
            (cdk,)
        ).fetchone()
        if not row:
            return jsonify({"status":"error","message":"CDK不存在"}),404
        return jsonify({
            "status":"success",
            "data":{
                "cdk_code":row["cdk_code"],
                "amount":row["amount"],
                "is_used":bool(row["is_used"]),
                "used_time":row["used_time"],
                "used_for_level":row["used_for_level"],
            }
        })
    finally:
        conn.close()

@app.route("/api/nodes", methods=["GET"])
def api_nodes():
    """代理查询调度层节点列表，保底返回本地节点"""
    try:
        resp = req_session.get(f"{SCHEDULER_URL}/nodes/list", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("nodes") and len(data["nodes"]) > 0:
                return data
    except Exception:
        pass
    # 保底本地节点
    return jsonify({
        "nodes": [{
            "name": "local-node",
            "public_ip": "127.0.0.1",
            "port": 39902,
            "runtime_mode": "active",
            "task_count": 0,
            "seconds_since_heartbeat": 0,
        }],
        "total": 1,
    })

@app.route("/api/query/progress", methods=["GET"])
def api_query_progress():
    """查询CDK对应的任务进度"""
    cdk = request.args.get("cdk_code","").strip()
    if not cdk:
        return jsonify({"status":"error","message":"请输入CDK"}),400
    info = task_progress.get(cdk)
    if not info:
        return jsonify({"status":"unknown","message":"暂无进度信息"}),404
    task_id = info["task_id"]
    node_ip = info["node_ip"]
    try:
        resp = req_session.get(f"http://{node_ip}:39902/api/v2/task/status/{task_id}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            info["status"] = data.get("status", info["status"])
            info["done"] = data.get("completed", info["done"])
            info["total"] = data.get("total", info["total"])
    except Exception:
        pass
    return jsonify({
        "status":"success",
        "data":{
            "task_id": task_id,
            "state": info["status"],
            "done": info["done"],
            "total": info["total"],
            "level_id": info.get("level_id",""),
        }
    })


# ════════════════════════════════════════════════
#  管理员登录
# ════════════════════════════════════════════════


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import session
        if not session.get("admin_logged_in"):
            from flask import redirect, url_for
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated








@app.route("/api/admin/logs", methods=["GET"])
@admin_required
def api_admin_logs():
    """返回最近80行日志"""
    log_file = "/tmp/pvz2-frontend.log"
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
        lines = lines[-80:]
        result = []
        for line in lines:
            line = line.rstrip("\n")
            if len(line) > 500:
                line = line[:500] + "..."
            result.append(line)
        return jsonify({"lines": result, "total": len(result)})
    except Exception as e:
        return jsonify({"lines": [f"读取日志失败: {e}"], "total": 0})


@app.route("/api/admin/accounts/list", methods=["GET"])
@admin_required
def api_admin_accounts_list():
    """查询账号列表（代理到账号管理API）"""
    platform = request.args.get("platform", "android")
    limit = min(int(request.args.get("limit", 20)), 100)
    status = request.args.get("status", "inactive")
    try:
        resp = req_session.get(
            "http://localhost:8000/api/accounts/list",
            params={"platform": platform, "status": status, "limit": limit},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            return jsonify({"accounts": data[:limit], "total": len(data)})
        return jsonify({"error": "API return %d" % resp.status_code}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/admin/db/info", methods=["GET"])
@admin_required
def api_admin_db_info():
    """数据库信息"""
    import os
    dbs = {}
    for name, path in [
        ("accounts", "/opt/pvz2-bot/accounts.db"),
        ("cdk", "/opt/pvz2-bot/cdk_data.db"),
        ("invite", "/opt/pvz2-bot/invite_cdk_data.db"),
    ]:
        try:
            st = os.stat(path)
            dbs[name] = {
                "path": path,
                "size_bytes": st.st_size,
                "size_mb": round(st.st_size / 1024 / 1024, 2),
            }
        except:
            dbs[name] = {"path": path, "error": "not found"}
    
    from .cdk_manager import get_conn
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM cdk_list").fetchone()["c"]
        unused = conn.execute("SELECT COUNT(*) AS c FROM cdk_list WHERE is_used=0").fetchone()["c"]
        used = conn.execute("SELECT COUNT(*) AS c FROM cdk_list WHERE is_used=1").fetchone()["c"]
        dbs["cdk"]["records"] = {"total": total, "unused": unused, "used": used}
    except:
        pass
    finally:
        conn.close()
    
    return jsonify(dbs)


@app.route("/api/admin/tasks", methods=["GET"])
@admin_required
def api_admin_tasks():
    """管理员查看所有活跃任务"""
    tasks = []
    for cdk, info in list(task_progress.items()):
        tasks.append({
            "cdk": cdk[:8]+"****",
            "task_id": info.get("task_id",""),
            "status": info.get("status",""),
            "done": info.get("done",0),
            "total": info.get("total",0),
            "level_id": info.get("level_id",""),
            "node_ip": info.get("node_ip",""),
        })
    return jsonify({"tasks": tasks, "total": len(tasks)})


def _gen_captcha():
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(chars) for _ in range(4))


@app.route("/captcha-image")
def captcha_image():
    """生成验证码图片（PIL图片/SVG fallback）"""
    import io, random
    
    code = _gen_captcha()
    session["captcha"] = code
    
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        
        width, height = 160, 60
        img = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        
        for _ in range(6):
            x1 = random.randint(0, width); y1 = random.randint(0, height)
            x2 = random.randint(0, width); y2 = random.randint(0, height)
            draw.line([(x1, y1), (x2, y2)], fill=(200,200,200), width=2)
        for _ in range(80):
            draw.point((random.randint(0,width), random.randint(0,height)), fill=(180,180,180))
        
        font = ImageFont.load_default()
        for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"]:
            try:
                font = ImageFont.truetype(fp, 32)
                break
            except:
                continue
        
        char_w = 32
        for i, ch in enumerate(code):
            ch_img = Image.new("RGBA", (char_w, height), (255,255,255,0))
            cd = ImageDraw.Draw(ch_img)
            cd.text((4, 5), ch, fill=(random.randint(30,100),random.randint(30,100),random.randint(30,100)), font=font)
            ch_img = ch_img.rotate(random.randint(-25, 25), expand=1, fillcolor=(255,255,255,0))
            img.paste(ch_img, (i * char_w + 10, 5), ch_img)
        
        img = img.filter(ImageFilter.SMOOTH)
        
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        from flask import Response
        return Response(buf.getvalue(), mimetype="image/png")
    except Exception:
        # Fallback: SVG captcha if PIL fails
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="60">'
        svg += '<rect width="160" height="60" fill="#f5f5f5" rx="6"/>'
        colors = ["#e74c3c","#3498db","#2ecc71","#f39c12","#9b59b6","#1abc9c"]
        for i, ch in enumerate(code):
            rot = random.randint(-20, 20)
            x = 10 + i * 35
            y = random.randint(10, 25)
            color = random.choice(colors)
            svg += f'<text x="{x}" y="{y+20}" font-size="28" font-weight="bold" font-family="monospace" fill="{color}" transform="rotate({rot},{x+10},{y+15})">{ch}</text>'
        for _ in range(5):
            x1 = random.randint(0, 160); y1 = random.randint(0, 60)
            x2 = random.randint(0, 160); y2 = random.randint(0, 60)
            svg += f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#ddd" stroke-width="1.5"/>'
        svg += '</svg>'
        from flask import Response
        return Response(svg, mimetype="image/svg+xml")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        captcha_input = (request.form.get("captcha") or "").strip().upper()
        expected = session.pop("captcha", "")
        if not expected or captcha_input != expected:
            return render_template_string(LOGIN_HTML, error="验证码错误")
        if pw == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = True
            return redirect(url_for("admin"))
        return render_template_string(LOGIN_HTML, error="密码错误")
    return render_template_string(LOGIN_HTML, error="")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))


LOGIN_HTML = """<!DOCTYPE html>
<html lang=zh-CN>
<head>
<meta charset=UTF-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>管理员登录</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
.card{background:rgba(255,255,255,.95);backdrop-filter:blur(20px);border-radius:20px;padding:36px 32px;width:380px;max-width:92vw;box-shadow:0 20px 60px rgba(0,0,0,.3),0 0 0 1px rgba(255,255,255,.1);text-align:center}
h1{font-size:22px;color:#1a1a2e;margin-bottom:24px;letter-spacing:1px}
.input-group{position:relative;margin-bottom:16px;text-align:left}
.input-group label{display:block;font-size:12px;font-weight:600;color:#888;margin-bottom:4px;text-transform:uppercase;letter-spacing:1px}
input[type=text],input[type=password]{width:100%;padding:12px 16px;border:2px solid #e8e8e8;border-radius:12px;font-size:14px;outline:none;box-sizing:border-box;transition:all .25s ease;background:#fafafa}
input:focus{border-color:#667eea;box-shadow:0 0 0 4px rgba(102,126,234,.15);background:#fff}
.btn{width:100%;padding:13px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;border:none;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;transition:all .25s ease;letter-spacing:2px;margin-top:4px}
.btn:hover{transform:translateY(-1px);box-shadow:0 8px 25px rgba(102,126,234,.4)}
.btn:active{transform:translateY(0)}
.error{color:#e74c3c;font-size:13px;margin-bottom:12px;padding:8px;background:#fff5f5;border-radius:8px;border:1px solid #ffe0e0}
.captcha-wrap{display:flex;flex-direction:column;align-items:center;gap:8px;margin-bottom:16px}
.captcha-wrap img{border-radius:10px;border:2px solid #e8e8e8;transition:opacity .2s}
.captcha-wrap img:hover{opacity:.85}
.captcha-hint{font-size:11px;color:#bbb;margin-top:2px}

.qResult{padding:12px;border-radius:8px;margin-top:12px;font-size:14px;line-height:1.6;display:none}
.qResult.error{background:#fff2f0;border:1px solid #ffccc7;color:#cf1322;display:block}
.qResult.success{background:#f6ffed;border:1px solid #b7eb8f;color:#389e0d;display:block}
.qResult.info{background:#e6f7ff;border:1px solid #91d5ff;color:#096dd9;display:block}

</style>
</head>
<body>
<div class=card>
<h1>🔐 管理面板</h1>
{% if error %}<div class=error>{{ error }}</div>{% endif %}
<form method=POST>
<div class=captcha-wrap>
<img src=/captcha-image alt=验证码 style="border-radius:10px;border:2px solid #e8e8e8;cursor:pointer" onclick="this.src='/captcha-image?'+Date.now()" title="点击刷新验证码">
<div class=captcha-hint>点击图片刷新验证码</div>
</div>
<div class=input-group>
<label>验证码</label>
<input type=text name=captcha placeholder="输入图片中的字符" maxlength=4 autocomplete=off style="text-align:center;letter-spacing:6px;font-weight:700">
</div>
<div class=input-group>
<label>密码</label>
<input type=password name=password placeholder="管理员密码">
</div>
<button class=btn type=submit>登 录</button>
</form>
</div>
</body>
</html>"""

# ════════════════════════════════════════════════
#  页面
# ════════════════════════════════════════════════

INDEX_HTML = """<!DOCTYPE html>
<html lang=zh-CN>
<head>
<meta charset=UTF-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PVZ2 任务平台</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:linear-gradient(135deg,#f5af19 0%,#f12711 50%,#f5af19 100%);min-height:100vh;display:flex;justify-content:center;align-items:center;flex-direction:column;gap:16px;padding:20px}
.card{background:rgba(255,255,255,.95);backdrop-filter:blur(10px);border-radius:20px;padding:32px;width:420px;max-width:94vw;box-shadow:0 8px 32px rgba(0,0,0,.15),0 0 0 1px rgba(255,255,255,.2)}
h1{font-size:22px;margin-bottom:8px;color:#1a1a2e;text-align:center}
.sub{color:#666;font-size:13px;text-align:center;margin-bottom:24px}
.form-group{margin-bottom:16px}
label{display:block;font-size:13px;font-weight:600;color:#444;margin-bottom:4px}
input,select{width:100%;padding:10px 14px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;outline:none;transition:border .2s}
input:focus,select:focus{border-color:#f5af19;box-shadow:0 0 0 3px rgba(245,175,25,.2)}
select{appearance:none;background:#fff}
.btn{width:100%;padding:12px;background:#4a90d9;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;transition:background .2s}
.btn:disabled{background:#ccc;cursor:not-allowed;transform:none;box-shadow:none}
#result{margin-top:16px;padding:12px;border-radius:8px;display:none;font-size:14px;line-height:1.5}
#result.error{background:#fff2f0;border:1px solid #ffccc7;color:#cf1322;display:block}
#result.success{background:#f6ffed;border:1px solid #b7eb8f;color:#389e0d;display:block}
#result.info{background:#e6f7ff;border:1px solid #91d5ff;color:#096dd9;display:block}
.loading{text-align:center;color:#999;font-size:13px;margin-top:12px;display:none}
.admin-link{text-align:center;margin-top:16px;font-size:13px}
.admin-link a{color:#4a90d9;text-decoration:none}
.result-box{margin-top:12px;padding:12px;border-radius:8px;display:none;font-size:14px;line-height:1.5}
.qResult{padding:12px;border-radius:8px;display:none;font-size:14px;line-height:1.5}
.qResult.error{background:#fff2f0;border:1px solid #ffccc7;color:#cf1322;display:block}
.qResult.success{background:#f6ffed;border:1px solid #b7eb8f;color:#389e0d;display:block}
.qResult.info{background:#e6f7ff;border:1px solid #91d5ff;color:#096dd9;display:block}
</style>
</head>
<body>
<div class=card>
<h1>🌻 向日葵庭院</h1>
<p class=sub>任务提交平台 — 输入卡密开始</p>
<div class=form-group>
<label>卡密 (CDK)</label>
<input id=cdk placeholder="输入12位卡密" value="{{prefill_cdk}}">
</div>
<div class=form-group>
<label>平台</label>
<select id=platform><option value=android>安卓</option><option value=ios>iOS</option></select>
</div>
<div class=form-group>
<label>关卡/目标 (level_id)</label>
<input id=level_id placeholder="如: 1-8-5">
</div>
<div class=form-group>
<label>任务类型</label>
<select id=type><option value=like>点赞</option><option value=play>游玩</option><option value=both>点赞+游玩</option></select>
</div>
<button class=btn id=submitBtn onclick=submitTask()>提 交 任 务</button>
<div id=result></div>
<div class=loading id=loading>⏳ 处理中...</div>
<div class=admin-link><a href=/admin target=_blank>管理面板 →</a></div>
</div>
<div class=card style=margin-top:16px>
<h2 style=font-size:16px;margin-bottom:12px;color:#333>📋 查询进度</h2>
<div class=form-group>
<label>卡密 (CDK)</label>
<input id=qCdk placeholder="输入卡密" style=margin-bottom:0>
</div>
<button class=btn id=queryBtn onclick=queryProgress()>查 询 进 度</button>
<div id=queryResult class=result-box></div>
</div>
</div>
<script>
function q(id){return document.getElementById(id)}
function showResult(msg,type){var r=q('result');r.className=type;r.textContent=msg;r.style.display='block'}
function queryProgress(){
  var cdk=q('qCdk').value.trim(),qr=q('queryResult');
  if(!cdk){qr.className='qResult error';qr.textContent='请输入卡密';qr.style.display='block';return}
  qr.style.display='none';q('queryBtn').disabled=true
  fetch('/api/query/progress?cdk_code='+encodeURIComponent(cdk))
  .then(function(r){return r.json()}).then(function(d){
    if(d.status==='unknown'){qr.className='qResult info';qr.textContent=d.message;qr.style.display='block';return}
    if(d.status==='error'){qr.className='qResult error';qr.textContent=d.message;qr.style.display='block';return}
    var s=d.data,state=s.state||'queued',done=s.done||0,total=s.total||0,pct=total>0?Math.round(done/total*100):0
    var stateText={'queued':'⏳ 排队中','processing':'⚙️ 执行中','completed':'✅ 已完成','failed':'❌ 失败','paused':'⏸ 已暂停'}[state]||state
    qr.className='qResult '+(state==='completed'?'success':'info')
    qr.innerHTML='任务: '+s.task_id+'<br>状态: '+stateText+'<br>进度: '+done+'/'+total+' ('+pct+'%)'
    qr.style.display='block'
  }).catch(function(){qr.className='qResult error';qr.textContent='查询失败';qr.style.display='block'})
  .finally(function(){q('queryBtn').disabled=false})
}

function submitTask(){
  var cdk=q('cdk').value.trim(),platform=q('platform').value,lv=q('level_id').value.trim(),type=q('type').value;
  if(!cdk){showResult('请输入卡密','error');return}
  if(!lv){showResult('请输入关卡ID','error');return}
  q('submitBtn').disabled=true;q('loading').style.display='block';q('result').style.display='none'
  fetch('/api/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cdk_code:cdk,platform:platform,level_id:lv,type:type})})
  .then(function(r){return r.json()})
  .then(function(d){showResult(d.message||d.status,d.status==='success'?'success':'error')})
  .catch(function(){showResult('网络错误','error')})
  .finally(function(){q('submitBtn').disabled=false;q('loading').style.display='none'})
}
document.addEventListener('DOMContentLoaded',function(){
  if(q('cdk').value){q('level_id').focus()}
  q('cdk').addEventListener('keydown',function(e){if(e.key==='Enter')submitTask()})
  q('level_id').addEventListener('keydown',function(e){if(e.key==='Enter')submitTask()})
})
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang=zh-CN>
<head>
<meta charset=UTF-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>管理面板 - PVZ2 任务平台</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;padding:0}
.wrapper{display:flex;min-height:100vh}
.sidebar{width:220px;background:#1a1a2e;color:#fff;flex-shrink:0;display:flex;flex-direction:column}
.sidebar .brand{padding:20px;font-size:16px;font-weight:700;border-bottom:1px solid rgba(255,255,255,.1);text-align:center}
.sidebar .nav{flex:1;padding:8px 0}
.sidebar .nav-item{padding:12px 20px;cursor:pointer;font-size:14px;color:rgba(255,255,255,.7);display:flex;align-items:center;gap:10px;border-left:3px solid transparent;transition:all .2s}
.sidebar .nav-item:hover{color:#fff;background:rgba(255,255,255,.05)}
.sidebar .nav-item.active{color:#fff;background:rgba(102,126,234,.2);border-left-color:#667eea;font-weight:600}
.sidebar .nav-footer{margin-top:auto;border-top:1px solid rgba(255,255,255,.1);padding:8px 0}
.main{flex:1;padding:24px;overflow-y:auto;background:#f0f2f5}
.tab-content{display:none}
.tab-content.active{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.stat-card{background:#fff;padding:16px;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
.stat-card .num{font-size:28px;font-weight:700;color:#4a90d9}
.stat-card .label{font-size:12px;color:#888;margin-top:4px}
.section{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
.section h2{font-size:16px;margin-bottom:12px;color:#333}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.row input,.row select{flex:1;min-width:100px;padding:8px 12px;border:1px solid #d9d9d9;border-radius:6px;font-size:13px}
.btn{padding:8px 20px;background:#4a90d9;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px}
.btn:hover{background:#357abd}
.btn-green{background:#52c41a}
.btn-green:hover{background:#389e0d}
.btn-red{background:#ff4d4f}
.btn-red:hover{background:#cf1322}
table{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #f0f0f0;word-break:break-word;overflow-wrap:break-word}
th{background:#fafafa;font-weight:600;color:#666}
tr:hover{background:#fafafa}
.badge{padding:2px 6px;border-radius:4px;font-size:11px}
.badge-green{background:#f6ffed;color:#389e0d}
.badge-red{background:#fff2f0;color:#cf1322}
.badge-gray{background:#fafafa;color:#999}
.log-box{background:#1a1a2e;color:#0f0;font-family:"Courier New",monospace;font-size:12px;padding:12px;border-radius:6px;max-height:500px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.5}
.db-card{background:#fff;border-radius:10px;padding:14px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.db-card .name{font-size:14px;font-weight:600;color:#333}
.db-card .info{font-size:12px;color:#888;margin-top:4px}

.show{padding:10px 14px;border-radius:8px;margin-top:8px;font-size:13px}
.show.error{background:#fff2f0;border:1px solid #ffccc7;color:#cf1322}
</style>
</head>
<body>
<div class=wrapper>
<div class=sidebar>
<div class=brand>🌻 向日葵庭院</div>
<div class=nav>
<div class="nav-item active" onclick=switchTab(0)>📊 仪表盘</div>
<div class=nav-item onclick=switchTab(1)>🤖 账号管理</div>
<div class=nav-item onclick=switchTab(2)>📋 日志</div>
<div class=nav-item onclick=switchTab(3)>🗄️ 数据库</div>
</div>
<div class=nav-footer>
<div class=nav-item onclick="location.href='/admin/logout'" style=color:rgba(255,100,100,0.8)>🔒 退出登录</div>
</div>
</div>
<div class=main>
<div id=tab0 class="tab-content active">
<div class=grid id=statsGrid></div>
<div class=section>
<h2>📱 运行节点</h2>
<div id=nodeList><p style=color:#999>加载中...</p></div>
</div>
<div class=section>
<h2>🎴 CDK 管理</h2>
<div class=row>
<input id=cdkCount placeholder="数量" value=10>
<input id=cdkAmount placeholder="次数" value=200>
<input id=cdkPrefix placeholder="前缀(选填)">
<button class="btn btn-green" onclick=batchCdk()>批量生成</button>
</div>
<button class=btn onclick=refreshDashboard()>🔄 刷新</button>
<div id=result></div>
</div>
<div class=section>
<h2>📄 CDK 列表</h2>
<div class=row>
<select id=filterUsed><option value="">全部</option><option value=0>未使用</option><option value=1>已使用</option></select>
<input id=searchCdk placeholder="搜索卡密">
<button class=btn onclick=loadCdks()>查询</button>
</div>
<table><thead><tr><th>卡密</th><th>次数</th><th>状态</th><th>使用时间</th><th>关卡</th></tr></thead><tbody id=cdkList></tbody></table>
</div>
<div class=section>
<h2>🤖 机器人账号</h2>
<div class=grid id=accountStatGrid></div>
</div>
<div class=section>
<h2>📊 任务管理</h2>
<div id=taskMgmt><p style=color:#999>加载中...</p></div>
</div>
</div>
<div id=tab1 class=tab-content>
<div class=section>
<h2>🤖 账号管理</h2>
<div class=row>
<select id=acctPlatform><option value=android>安卓</option><option value=ios>iOS</option></select>
<select id=acctStatus><option value=inactive>未激活</option><option value=activated>已激活</option><option value=used>已使用</option></select>
<button class=btn onclick=loadAccounts()>查询账号</button>
</div>
<table><thead><tr><th>ID</th><th>平台</th><th>状态</th><th>UI(末8位)</th><th>用户名</th><th>创建时间</th></tr></thead><tbody id=acctList></tbody></table>
</div>
</div>
<div id=tab2 class=tab-content>
<div class=section>
<h2>📋 运行日志</h2>
<div class=row><button class=btn onclick=loadLogs()>🔄 刷新</button><span id=logCount style=font-size:12px;color:#999;margin-left:8px></span></div>
<div class=log-box id=logContent>加载中...</div>
</div>
</div>
<div id=tab3 class=tab-content>
<div class=section>
<h2>🗄️ 数据库状态</h2>
<div id=dbInfo><p style=color:#999>加载中...</p></div>
</div>
</div>
</div>
</div>
<script>
function q(id){return document.getElementById(id)}
function showMsg(msg,type){var e=q('result');e.textContent=msg;e.className='show'+(type==='error'?' error':'');setTimeout(function(){e.className=''},5000)}
function switchTab(idx){
  document.querySelectorAll('.nav-item').forEach(function(t,i){if(t.classList)t.className='nav-item'+(i===idx?' active':'')});
  document.querySelectorAll('.tab-content').forEach(function(t,i){t.className='tab-content'+(i===idx?' active':'')});
  if(idx===1)loadAccounts();
  if(idx===2)loadLogs();
  if(idx===3)loadDbInfo();
}
function loadAccounts(){
  var p=q('acctPlatform').value,s=q('acctStatus').value;
  fetch('/api/admin/accounts/list?platform='+p+'&status='+s).then(function(r){return r.json()}).then(function(d){
    var html='';
    if(d.accounts&&d.accounts.length){d.accounts.forEach(function(a){
      html+='<tr><td>'+a.id+'</td><td>'+a.platform+'</td><td><span class="badge badge-'+((a.status==='activated'||a.status==='used')?'red':'green')+'">'+a.status+'</span></td><td>'+(a.ui||'').slice(-8)+'</td><td>'+(a.username||'')+'</td><td>'+(a.created_at||'')+'</td></tr>';
    })}else{html='<tr><td colspan=6 style="text-align:center;color:#999">暂无数据</td></tr>'}
    q('acctList').innerHTML=html;
  }).catch(function(){q('acctList').innerHTML='<tr><td colspan=6 style="color:red">加载失败</td></tr>'})
}
function loadLogs(){
  fetch('/api/admin/logs').then(function(r){return r.json()}).then(function(d){
    q('logContent').textContent=d.lines.join("\n")||'(空)';
    q('logCount').textContent='共'+d.total+'行';
  }).catch(function(){q('logContent').textContent='加载失败'})
}
function loadDbInfo(){
  fetch('/api/admin/db/info').then(function(r){return r.json()}).then(function(d){
    var html='';
    Object.keys(d).forEach(function(k){
      var db=d[k];
      html+='<div class=db-card><div class=name>'+k+'.db</div><div class=info>路径: '+(db.path||'')+'</div>';
      if(db.size_mb!==undefined)html+='<div class=info>大小: '+db.size_mb+' MB</div>';
      if(db.records)html+='<div class=info>记录: 共'+db.records.total+' 未用'+db.records.unused+' 已用'+db.records.used+'</div>';
      if(db.error)html+='<div class=info style=color:red>'+db.error+'</div>';
      html+='</div>';
    });
    q('dbInfo').innerHTML=html;
  }).catch(function(){q('dbInfo').innerHTML='<p style=color:red>加载失败</p>'})
}
function refreshDashboard(){
  Promise.all([
    fetch('/api/cdk/stats').then(function(r){return r.json()}),
    fetch('/api/nodes').then(function(r){return r.json()})
  ]).then(function(dataArr){
    var stats=dataArr[0],nodes=dataArr[1];
    q('statsGrid').innerHTML=
      '<div class=stat-card><div class=num>'+stats.total+'</div><div class=label>总卡密</div></div>'+
      '<div class=stat-card><div class=num>'+stats.unused+'</div><div class=label>未使用</div></div>'+
      '<div class=stat-card><div class=num>'+stats.used+'</div><div class=label>已使用</div></div>'+
      '<div class=stat-card><div class=num>'+stats.unused_amount+'</div><div class=label>剩余次数</div></div>'+
      '<div class=stat-card><div class=num>'+(nodes.nodes?nodes.nodes.length:0)+'</div><div class=label>活跃节点</div></div>';
    var html='';
    if(nodes.nodes&&nodes.nodes.length){
      html='<table><thead><tr><th>节点</th><th>IP</th><th>任务数</th><th>模式</th><th>心跳</th></tr></thead><tbody>';
      nodes.nodes.forEach(function(n){
        var sec=n.seconds_since_heartbeat||0,color=sec<10?'green':'red';
        html+='<tr><td>'+n.name+'</td><td>'+n.public_ip+':'+n.port+'</td><td>'+n.task_count+'</td><td><span class="badge badge-'+color+'">'+(n.runtime_mode||'unknown')+'</span></td><td>'+(sec<60?sec+'s':Math.round(sec/60)+'m')+'</td></tr>';
      });
      html+='</tbody></table>';
    }else{html='<p style=color:#999>暂无活跃节点</p>'}
    q('nodeList').innerHTML=html;
  }).catch(function(){q('statsGrid').innerHTML='<p style=color:red>加载失败</p>'})
}
function batchCdk(){
  var c=parseInt(q('cdkCount').value)||10,a=parseInt(q('cdkAmount').value)||200,p=q('cdkPrefix').value.trim();
  fetch('/api/cdk/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:c,amount:a,prefix:p})})
  .then(function(r){return r.json()}).then(function(d){
    showMsg('生成了 '+d.created+' 张卡密','ok');if(d.created)loadCdks()
  }).catch(function(){showMsg('失败','error')})
}
function loadCdks(){
  var filter=q('filterUsed').value,search=q('searchCdk').value.trim();
  var url='/api/cdk/list?limit=50'+(filter?'&is_used='+filter:'')+(search?'&search='+encodeURIComponent(search):'');
  fetch(url).then(function(r){return r.json()}).then(function(d){
    var html='';
    if(!d.cdks||!d.cdks.length){html='<tr><td colspan=5 style="text-align:center;color:#999">暂无数据</td></tr>'}
    else{d.cdks.forEach(function(c){
      var used=c.is_used?'<span class="badge badge-red">已用</span>':'<span class="badge badge-green">未用</span>';
      html+='<tr><td>'+c.cdk_code+'</td><td>'+c.amount+'</td><td>'+used+'</td><td>'+(c.used_time||'')+'</td><td>'+(c.used_for_level||'')+'</td></tr>';
    })}
    q('cdkList').innerHTML=html;
  }).catch(function(){q('cdkList').innerHTML='<tr><td colspan=5 style="color:red">加载失败</td></tr>'})
}
refreshDashboard();loadCdks();
loadAccounts();loadLogs();loadDbInfo();
setInterval(refreshDashboard,30000);
</script>
</body>
</html>"""
@app.route("/")
@app.route("/<url_cdk>")
def index(url_cdk: str = ""):
    if url_cdk == "favicon.ico":
        return "", 404
    return render_template_string(INDEX_HTML, prefill_cdk=url_cdk)

@app.route("/admin")
@admin_required
def admin():
    return render_template_string(ADMIN_HTML)

# ════════════════════════════════════════════════
#  启动
# ════════════════════════════════════════════════

def run(port: int = 5555):
    logger.info("控制台前端启动 → 端口 %d", port)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
