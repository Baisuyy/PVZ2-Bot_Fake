import requests
import re
import time
import random
import json
import hashlib
import binascii
import atexit
import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad
import ddddocr
from login_failure_logger import 记录登录失败
from proxy_pool import 启用全局代理
from pvz2_common import 请求UI_SK
启用全局代理()
try:
    from PIL import Image, ImageOps, ImageFilter
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

OCR_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_OCR_ENGINES = None
_TESSERACT_BIN = shutil.which("tesseract")
DEFAULT_REQUEST_TIMEOUT = (5, 15)
DEFAULT_LOGIN_MAX_SECONDS = 120


def _parse_timeout_env(name: str, default_value: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default_value))))
    except Exception:
        return default_value


def _request_timeout():
    connect_timeout = _parse_timeout_env("PVZ2_HTTP_CONNECT_TIMEOUT", DEFAULT_REQUEST_TIMEOUT[0])
    read_timeout = _parse_timeout_env("PVZ2_HTTP_READ_TIMEOUT", DEFAULT_REQUEST_TIMEOUT[1])
    return (connect_timeout, read_timeout)


def 获取OCR引擎列表():
    global _OCR_ENGINES
    if _OCR_ENGINES is not None:
        return _OCR_ENGINES
    try:
        # 4399验证码在强制set_ranges时容易被压缩成空串/短串，这里不强制限制字符集
        _OCR_ENGINES = [
            ddddocr.DdddOcr(ocr=True, det=False, show_ad=False),
            ddddocr.DdddOcr(ocr=True, det=False, old=True, show_ad=False),
            ddddocr.DdddOcr(ocr=True, det=False, beta=True, show_ad=False),
        ]
        return _OCR_ENGINES
    except Exception as e:
        print(f"❌ OCR初始化失败: {e}")
        记录登录失败("4399", "OCR初始化", str(e))
        return []


def 清洗验证码文本(text):
    if not text:
        return None
    code = "".join(ch for ch in text.upper() if ch.isdigit() or ("A" <= ch <= "Z"))
    if len(code) >= 4:
        return code[:4]
    return None


def 生成验证码候选图(img_bytes):
    # 生成少量变体图，提升高噪声验证码的识别率
    candidates = [img_bytes]
    if not PIL_AVAILABLE:
        return candidates
    try:
        src = Image.open(BytesIO(img_bytes)).convert("RGB")
        gray = ImageOps.autocontrast(src.convert("L"))
        inv = ImageOps.invert(gray)
        sharp = gray.filter(ImageFilter.SHARPEN)
        for im in [gray, inv, sharp]:
            buff = BytesIO()
            im.save(buff, format="PNG")
            candidates.append(buff.getvalue())
    except Exception:
        pass
    return candidates


def tesseract识别(img_bytes):
    if not _TESSERACT_BIN:
        return None
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(img_bytes)
            tmp_path = f.name
        cmd = [
            _TESSERACT_BIN,
            tmp_path,
            "stdout",
            "-l",
            "eng",
            "--oem",
            "1",
            "--psm",
            "8",
            "-c",
            f"tessedit_char_whitelist={OCR_CHARSET}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
        if result.returncode != 0:
            return None
        return 清洗验证码文本(result.stdout)
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _清理OCR引擎():
    global _OCR_ENGINES
    if not _OCR_ENGINES:
        return
    for engine in _OCR_ENGINES:
        try:
            if hasattr(engine, "cleanup"):
                engine.cleanup()
        except Exception:
            pass
    _OCR_ENGINES = None


atexit.register(_清理OCR引擎)

def des_encrypt(明文: bytes, 密钥: bytes = b'TwPay001', 偏移量: bytes = b'\x01\x02\x03\x04\x05\x06\x07\x08') -> bytes:
    return DES.new(密钥, DES.MODE_CBC, 偏移量).encrypt(pad(明文, DES.block_size))

def 获取state():
    try:
        response = requests.post(
            url="https://m.4399api.com/openapiv2/oauth.html",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data='''usernames=&top_bar=1&state=&env=RUaEpU4PBRE9%2BxWdHKzcGrNagN6RdYXp0BDPGptMurehnhCHtsr5Nsgp46SJmqEl3S4mKd4pTAEK%0AwokXw1GURyaATRMLZjZNGJn%2BK4xibnqLlkOiHoFTm682Cpt2o42H4Kr7eepLBZYRaw3n2yJ7Umgg%0Az0Odey9GsDk1ZA3tFU%2BOthSBBOIDcuDNttDXv3ZzZrEtRrjQdX7b9V%2BVTg4H1D3ETBgw%2FoFDEwqQ%0AokammqLAr54UoaZl3cApPqsY84scCziyzSBG6UEvnnX%2BlXQGWWXrTl2vI6xs72RIneJF8L0xDOkm%0AhAg%2FfM1IADZ9lBIVlwgVzvKLI5R%2FcqJCWTghlpBPUP8WDK31Q9RU0c6m%2F2o0gKNPNaelUAKPKmVQ%0AWTfzW6EegKiUXak1QRi02a%2F40mYKxK2xbjXqIDzDQd%2FqSPa2jC69GHgf7wKniHvVZLn3Hkw7UXi%2B%0AR0rdHvVE%2BInesIgZtCrFAJy9BJLRPT3jNQvnMGc27kbWjAQVP9cA7K2876bfrpSuZn3s2FHTnNkC%0AVzc7nq4R%2F2wzoKRptwElhdKVi0jpvYRieWAt1hmqOfskpWdFu1EvgjVgWK5qjEtngznHRmVAYOjf%0AJRpntuB8ZyvkMyFydfBPdlYBWBQJyexPeMnM2dAf79xKjg5jaW%2FcI24TwJ8fiQHZWEAbol9ywM6V%0ARLnNHO4%2Fk3%2BQcfxTrkRywGfev7ZImiQ8vm4s1qSPMYuZf66gCj5ttCIjBeqbK%2BrJMxb0KSjF4Ia%2F%0AzrwgzNqzLj9UyYd6NFVW9VQBniv8Wg%3D%3D%0A&''',
            timeout=_request_timeout(),
        )
        return re.search(r'state=([^&]*)', response.text).group(1)
    except:
        return None

def 获取验证码(cid):
    try:
        img = requests.get(
            f"https://ptlogin.4399.com/ptlogin/captcha.do?captchaId={cid}",
            timeout=10
        ).content
        for candidate in 生成验证码候选图(img):
            for engine in 获取OCR引擎列表():
                raw = engine.classification(candidate)
                code = 清洗验证码文本(raw)
                if code:
                    return code
        return tesseract识别(img)
    except Exception:
        return None

def login_4399(username, password):
    最大重试次数 = 4
    最大耗时秒 = _parse_timeout_env("PVZ2_4399_LOGIN_MAX_SECONDS", DEFAULT_LOGIN_MAX_SECONDS)
    当前重试次数 = 0
    started = time.monotonic()
    
    while 当前重试次数 < 最大重试次数:
        if time.monotonic() - started > 最大耗时秒:
            记录登录失败("4399", "登录流程超时", f"超过最大耗时{最大耗时秒}s", {"username": username})
            break
            
        try:
            # 步骤1：获取state
            state = 获取state()
            if not state:
                记录登录失败("4399", "步骤1-获取state", "返回空state", {"username": username})
                当前重试次数 += 1
                continue
            
            # 步骤2：获取验证码
            captcha_id = f'captchaReq9dccdf4c4f{random.randint(10000000,100000000000000)}'
            验证码 = 获取验证码(captcha_id)
            if not 验证码:
                记录登录失败("4399", "步骤2-获取验证码", "验证码识别失败", {"captcha_id": captcha_id, "username": username})
                当前重试次数 += 1
                continue
            
            # 步骤3：发送登录请求
            data = {
                'password': password,
                'username': username,
                'response_type': 'TOKEN',
                'client_id': 'f37dbba82960bfff6e54e5487292be0e',
                'state': state,
                "captcha": 验证码,
                "captcha_id": captcha_id,
                'redirect_uri': 'https://m.4399api.com/openapi/oauth-callback.html?gamekey=46628'
            }
            response = requests.post(
                'https://ptlogin.4399.com/oauth2/loginAndAuthorize.do',
                data=data,
                timeout=_request_timeout(),
            )
            
            # 步骤4：检查登录结果
            if "欢迎进入游戏" in response.text:
                try:
                    return response.json()
                except Exception as e:
                    记录登录失败("4399", "步骤4-解析登录响应", str(e), {"username": username})
            else:
                记录登录失败(
                    "4399",
                    "步骤4-校验登录结果",
                    f"登录失败，HTTP={response.status_code}",
                    {"username": username, "response_preview": response.text[:200]},
                )
            当前重试次数 += 1
        except Exception as e:
            记录登录失败("4399", "登录流程异常", str(e), {"username": username, "retry_index": 当前重试次数 + 1})
            当前重试次数 += 1
    
    记录登录失败("4399", "登录结束", f"连续{最大重试次数}次失败", {"username": username})
    return None

def 执行注册(state_param, uid):
    try:
        login_info = binascii.hexlify(des_encrypt(json.dumps({"channelParam": "", "idfa": "3ae7b872-599a-4ac2-8fb4-e575e3dcea43", "imei": "80e0b33b2a25115a", "userId": uid, "requestIp": "0000", "oauthKey": state_param}, separators=(',', ':')).encode())).decode().upper()
        head = "C2AADAA23B2D1DB9B12B7CEBA3789C9205391713B11DA45DD1B0F0B166B47E96AE3DA372F2D9364761D4BDF278B2C1B8C320DFEC425F1271B36FAFE8BB510846E8C38999DB5985D8A0C7CB98749BDF5C425D9AE923AE5674A73F6D1B4197FA5B8376C6A3432F548F4E2A0B4120E84AAFFD1C62AC57432314"
        login_response = requests.post("http://payment2.talkyun.com.cn/payment-provider/api/user/login", headers={"Content-Type": "application/x-www-form-urlencoded"}, data=f"head={head}&loginInfo={login_info}&md5={hashlib.md5(f'{head}{login_info}b0b29851-b8a1-4df5-abcb-a8ea158bea20'.encode()).hexdigest()}", timeout=15)
        if login_response.status_code != 200:
            记录登录失败("4399", "注册-登录请求", f"HTTP状态码异常: {login_response.status_code}", {"uid": uid})
            return {"error": "登录请求失败"}
        try:
            login_data = login_response.json()
        except Exception as error:
            记录登录失败("4399", "注册-解析响应JSON", str(error), {"uid": uid})
            return {"error": "登录响应解析失败"}
        if "content" not in login_data:
            记录登录失败("4399", "注册-检查content", "登录响应缺少content字段", {"uid": uid})
            return {"error": "登录响应缺少content字段"}
        try:
            content_json = json.loads(login_data["content"].replace("\\", ""))
        except Exception as error:
            记录登录失败("4399", "注册-解析content", str(error), {"uid": uid})
            return {"error": "content解析失败"}
        user_info = content_json["channelUserInfo"]
        user_id, login_token = user_info["channelUserId"], user_info["loginToken"]
        响应数据 = 请求UI_SK(channel_key="4399", account_id=user_id, token=login_token)
        return 响应数据
    except Exception as e:
        记录登录失败("4399", "注册流程异常", str(e), {"uid": uid})
        return {"error": f"执行注册异常: {str(e)}"}

def main():
    print("=== 4399账号登录工具 ===")
    print("示例账号：cga5086910,密码：aa123456")
    username = input("请输入4399账号: ").strip()
    password = input("请输入4399密码: ").strip()
    
    if not username or not password:
        print("错误：账号和密码不能为空！")
        return
    
    login_result = login_4399(username, password)
    
    if not login_result:
        print("登录失败，请检查账号/密码/验证码逻辑！")
        return
    
    if "result" in login_result and "state" in login_result["result"] and "uid" in login_result["result"]:
        result = 执行注册(login_result["result"]["state"], login_result["result"]["uid"])
        if "error" in result:
            print(f"执行注册失败: {result['error']}")
        else:
            print("注册成功！")
            print(f"UI: {result['ui']}")
            print(f"SK: {result['sk']}")
    else:
        print("登录响应格式错误，无法执行注册流程！")

if __name__ == '__main__':
    main()
