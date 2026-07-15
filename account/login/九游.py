import base64
import json

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from login_failure_logger import 记录登录失败
from proxy_pool import 启用全局代理
from pvz2_common import 请求UI_SK
启用全局代理()

安全配置 = {
    "密钥": base64.b64decode("osVbNnKnJ23J/yJFujQcbw=="),
    "初始向量": base64.b64decode("0oi7xv84EQrHJZS8M1FhhA=="),
}

九游登录URL = "http://sdk-account.9game.cn/ng/client/unifiedAccount.loginByMobilePassword?ver=0&df=adat&os=android"
九游公共参数 = {
    "i": "NiZwNt1QUEZLg+27uZRL9n0ifI/uiKu/lAuyniLA2p9GfRKHD+iLuc842dnTA+a8TJCMVKOASbuitarafEZ1Ng==",
    "k": "oamzzeFKJYKEYVGqErEyxVf4dT5rRTr4Ckp5KE7hrv+5DWyDqGPsJDaj68j1V33I1h///RduofK9bFAPC9SyzQ==",
    "v": 5,
}


def 加密文本(加密内容):
    公钥_bytes = base64.b64decode("MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAMFxR3NxXU5Mf3U6wykuXYOU+OgXZgr/VyfNaXw4vOGImx0f7a9s9ASrHQAGsev8nqn7C5uLzTo8tiHWd5Cb/YsCAwEAAQ==")
    公钥 = serialization.load_der_public_key(公钥_bytes, backend=default_backend())
    加密结果 = 公钥.encrypt(加密内容.encode(), padding.PKCS1v15())
    return base64.b64encode(加密结果).decode()


def aes加密(数据):
    密码器 = AES.new(安全配置["密钥"], AES.MODE_CBC, 安全配置["初始向量"])
    填充数据 = pad(数据.encode("utf-8"), AES.block_size)
    return base64.b64encode(密码器.encrypt(填充数据)).decode("utf-8")


def 解密AES_CBC(加密数据):
    密码器 = AES.new(安全配置["密钥"], AES.MODE_CBC, 安全配置["初始向量"])
    解密数据 = 密码器.decrypt(加密数据)
    return unpad(解密数据, AES.block_size).decode("utf-8")


def _九游请求(payload: dict):
    body = dict(九游公共参数)
    body["d"] = aes加密(json.dumps(payload, separators=(",", ":")))
    try:
        resp = requests.post(九游登录URL, json=body, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        decrypted = json.loads(解密AES_CBC(base64.b64decode(raw["d"])))
        return decrypted
    except Exception as error:
        action = payload.get("data", {})
        step = "请求账号列表" if "serviceTicket" not in action else "请求SID"
        记录登录失败("九游", step, str(error))
        raise


def 获取九游账号列表(手机号: str, 密码: str):
    请求数据 = {
        "id": 1746528361062,
        "data": {
            "mobile": 手机号,
            "password": "5|" + 加密文本(f"41d8cd98f00b204e||{密码}"),
            "encrypt": 1,
        },
        "game": {"gameId": 516373},
        "client": {"ve": "9.8.0.0"},
    }
    decrypted = _九游请求(请求数据)
    data = decrypted.get("data", {})
    service_ticket = data.get("serviceTicket", "")
    account_list = data.get("accountList", [])
    accounts = [str(item.get("accountId", "")) for item in account_list if item.get("accountId")]
    if not service_ticket:
        记录登录失败("九游", "解析账号列表", "响应数据中缺少 serviceTicket", {"mobile": 手机号})
        raise ValueError("响应数据中缺少 serviceTicket")
    if not accounts:
        记录登录失败("九游", "解析账号列表", "响应数据中缺少 accountList", {"mobile": 手机号})
        raise ValueError("响应数据中缺少 accountList")
    return {"service_ticket": service_ticket, "accounts": accounts, "raw": decrypted}


def 获取九游SID(service_ticket: str, account_id: str):
    请求数据2 = {
        "id": "1746528361062",
        "data": {
            "mobile": "***********",
            "password": "***********",
            "encrypt": "***",
            "serviceTicket": service_ticket,
            "accountId": account_id,
        },
        "game": {"gameId": 516373},
        "client": {"ve": "9.8.0.0"},
    }
    decrypted = _九游请求(请求数据2)
    sid = decrypted.get("data", {}).get("sid", "")
    if not sid:
        记录登录失败("九游", "解析SID", "获取 sid 失败", {"account_id": account_id})
        raise ValueError("获取 sid 失败")
    return sid


def 登录九游指定账号(service_ticket: str, account_id: str):
    sid = 获取九游SID(service_ticket, account_id)
    return 请求UI_SK(channel_key="九游", account_id=account_id, token=sid)


def login_九游(手机号, 密码):
    try:
        列表结果 = 获取九游账号列表(手机号, 密码)
        最终结果 = []
        for account_id in 列表结果["accounts"]:
            最终响应 = 登录九游指定账号(列表结果["service_ticket"], account_id)
            最终结果.append(
                {
                    "account_id": account_id,
                    "ui": 最终响应["ui"],
                    "sk": 最终响应["sk"],
                    "response": 最终响应["raw"],
                }
            )
        return 最终结果
    except Exception as e:
        记录登录失败("九游", "登录流程异常", str(e), {"mobile": 手机号})
        return None

def main():
    print("=== 九游账号登录工具 ===")
    手机号 = input("请输入手机号: ").strip()
    密码 = input("请输入密码: ").strip()
    
    if not 手机号 or not 密码:
        print("错误：手机号和密码不能为空！")
        return
    
    结果 = login_九游(手机号, 密码)
    
    if not 结果:
        print("登录失败，请检查手机号/密码！")
        return
    
    print("\n=== 登录结果 ===")
    for i, 账号信息 in enumerate(结果):
        print(f"\n账号 {i+1}:")
        print(f"  account_id: {账号信息['account_id']}")
        print(f"  响应状态: 成功")

if __name__ == '__main__':
    main()
