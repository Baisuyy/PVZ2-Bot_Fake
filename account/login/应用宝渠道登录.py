import base64
import json
import time
import uuid
import re
from urllib.parse import parse_qs, urlsplit

import requests

from login_failure_logger import 记录登录失败
from proxy_pool import 启用全局代理
from pvz2_common import 请求UI_SK

启用全局代理()


def hash33(qrsig: str) -> int:
    value = 0
    mod = 0x100000000
    for char in qrsig:
        value = (value * 33 + ord(char)) % mod
    return value & 0x7FFFFFFF


def 提取参数(原始链接: str):
    参数部分 = 原始链接.split("#")[-1]
    参数字典 = parse_qs(参数部分)
    用户标识 = 参数字典.get("openid", [""])[0]
    访问令牌 = 参数字典.get("access_token", [""])[0]
    return 用户标识, 访问令牌


def 获取QQ动态凭证():
    try:
        response = requests.get(
            "https://xui.ptlogin2.qq.com/ssl/ptqrshow?s=8&e=0&appid=716027609&type=0"
            "&t=0.39260692876683234&u1=http%3A%2F%2Fconnect.qq.com&daid=381&pt_3rd_aid=1105136054",
            timeout=10,
        )
        response.raise_for_status()
        qrsig = response.cookies.get("qrsig", "")
        if not qrsig:
            raise ValueError("响应中缺少 qrsig")
        return {"qrsig": qrsig, "ptqrtoken": hash33(qrsig), "qr_bytes": response.content}
    except Exception as error:
        记录登录失败("应用宝", "QQ二维码创建", str(error))
        raise


def 轮询QQ登录状态(cookies):
    while True:
        try:
            url = (
                "https://xui.ptlogin2.qq.com/ssl/ptqrlogin?u1=http%3A%2F%2Fconnect.qq.com&from_ui=1&type=1"
                f"&ptlang=2052&ptqrtoken={cookies['ptqrtoken']}&daid=381&aid=716027609&pt_3rd_aid=1105136054"
                "&pt_openlogin_data=pt_enable_pwd%3D1%26appid%3D716027609%26pt_3rd_aid%3D1105136054%26daid%3D381"
                "%26pt_skey_valid%3D0%26style%3D35%26force_qr%3D1%26autorefresh%3D1%26s_url%3Dhttp%253A%252F%252Fconnect.qq.com"
                "%26refer_cgi%3Dm_authorize%26ucheck%3D1%26fall_to_wv%3D1%26status_os%3D12%26redirect_uri%3Dauth%253A%252F%252Ftauth.qq.com%252F"
                "%26client_id%3D1105136054%26pf%3Dopenmobile_android%26response_type%3Dtoken%26scope%3Dall%26sdkp%3Da%26sdkv%3D3.5.14.lite"
                "%26sign%3D88d506116f63445df82cfe18e2180dd8%26status_machine%3D23116PN5BC%26switch%3D1%26time%3D1748408062"
                "%26show_download_ui%3Dtrue%26h5sig%3DYsvrHt1UeXZLtrUWzItT7vidRA6FdTSnuWTJmVdcEV8%26loginty%3D6%26pt_flex%3D1"
                "%26loginfrom%3D%26h5sig%3DYsvrHt1UeXZLtrUWzItT7vidRA6FdTSnuWTJmVdcEV8%26loginty%3D6%26"
                "&device=2&ptopt=1&pt_uistyle=35&jsver=9fce2a54&aegis_uid=2e737f000001efdb-ca7fdf68ca8d8c08-4099"
                "&r=0.15062276381071327"
            )
            response = requests.get(url, cookies={"qrsig": cookies["qrsig"]}, timeout=10)
            response.raise_for_status()

            if "ptuiCB('65'" in response.text:
                print("二维码已失效，重新生成...")
                cookies.update(获取QQ动态凭证())
                continue

            if "登录成功" in response.text:
                print("登录成功！")
                return response.text

            print(response.text)
            time.sleep(5)
        except Exception as error:
            记录登录失败("应用宝", "QQ二维码轮询", str(error))
            print(f"请求异常：{error}")
            time.sleep(5)


def 提取登录回调链接(raw_text: str):
    match = re.search(r"'(https?://[^']+)'", raw_text)
    if not match:
        return ""
    return match.group(1).replace("\\/", "/")


def 用QQ登录(raw_text: str):
    callback_url = 提取登录回调链接(raw_text)
    if not callback_url:
        记录登录失败("应用宝", "解析QQ回调链接", "无法从登录结果提取回调链接")
        raise ValueError("无法从登录结果提取回调链接")
    account_id, access_token = 提取参数(callback_url)
    if not account_id or not access_token:
        记录登录失败("应用宝", "解析QQ回调参数", "回调链接缺少 openid/access_token")
        raise ValueError("回调链接缺少 openid/access_token")
    return 请求UI_SK(channel_key="应用宝", account_id=account_id, token=f"{access_token}|QQ")


def login_应用宝QQ():
    动态凭证 = 获取QQ动态凭证()
    登录结果 = 轮询QQ登录状态(动态凭证)
    account_id, access_token = 提取参数(登录结果)
    return 请求UI_SK(channel_key="应用宝", account_id=account_id, token=f"{access_token}|QQ")


# 微信登录相关
_WX_SCAN_CODE_LOGIN_URL = "https://ysdk.qq.com/auth/wx_scan_code_login"
_WX_SCAN_CODE_LOGIN_HEADERS = {
    "Host": "ysdk.qq.com",
    "Accept-Charset": "UTF-8",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; 23116PN5BC Build/c7565ea.0)",
    "Connection": "Keep-Alive",
    "Accept-Encoding": "gzip",
}

_WX_SCAN_CODE_LOGIN_PARAMS = {
    "openappid": "1105136054",
    "scope": "snsapi_userinfo,snsapi_friend,snsapi_message",
    "noncestr": "-5269609101065074559",
    "traceid": "0bc80e82-774a-4906-b0b0-394b56f3dff1",
    "yyb_install": "1",
    "wx_appid": "wx51a3b2f19ba0d320",
    "orientation": "1",
    "source_channel": "10095285",
    "app_version": "3.7.1",
    "openid": "",
    "loginplatform": "2",
    "yyb_vercode": "8962130",
    "version": "2.2.3",
    "resolution": "3200*3200",
    "sig": "D697A37116685616C520755E46D077C5",
    "app_name": "植物大战僵尸2",
    "qq_appid": "1105136054",
    "yyb_vername": "8.9.6",
    "anti_hope_switch": "1",
    "ysdk_plugin_version": "101",
    "pay_os": "32",
    "is_cloud_env": "0",
    "appid": "wx51a3b2f19ba0d320",
    "offerid": "1105136054",
    "yyb_app": "应用宝",
    "channel_id": "10095285",
    "timestamp": "1748407737112",
    "sk": "default",
    "sf": "589672E63E5D86F086731BC7D5E2337A5494D7E3529F433B69F980F3CA5586AFDF0216E476BF395CC0B25E6FCDFE89572C3FD6A64864F1296CCA058771DC16E22B64B0049DA7325B14D7F2D27015B17D4C57A7E94F58A8B6AA2718AF59377FF0C1F4A4148C91811FFAB874D03C861E2867B243EB841DD7FCC2952071A91A7537218295753583133270F99F2AE9B3DA63D18201DC898B9E1CE6748CDF2BAE76778F839531A50C1D1CBC5F33737CBBC29FAB3BCA0A1A70085A73DF23839BB24F347AAFF91BA7CA4BC18ABFE6A974BCB7A457B3450C3975DECBA835C78C728592DD48CCE14CE264ADA455FE58C66DA90AEB5BAE0B5538DA413602B0B9AF42672709",
}

_WX_VERIFY_URL = "https://ysdk.qq.com/auth/wx_verify_code"

_WX_VERIFY_BASE_PARAMS = {
    "channel": "10095285",
    "offerid": "1105136054",
    "loginType": "1",
    "platform": "desktop_m_wechat",
    "client_hope_switch": "1",
    "yyb_install": "1",
    "wx_appid": "wx51a3b2f19ba0d320",
    "orientation": "2",
    "source_channel": "10095285",
    "app_version": "3.7.1",
    "openid": "",
    "loginplatform": "2",
    "yyb_vercode": "8962130",
    "version": "2.2.3",
    "resolution": "4096*1920",
    "sig": "6B82BD681F742E9B8E15873CFD473553",
    "app_name": "植物大战僵尸2",
    "qq_appid": "1105136054",
    "yyb_vername": "8.9.6",
    "anti_hope_switch": "1",
    "ysdk_plugin_version": "101",
    "pay_os": "32",
    "is_cloud_env": "0",
    "appid": "wx51a3b2f19ba0d320",
    "yyb_app": "应用宝",
    "channel_id": "10095285",
    "sk": "default",
    "timestamp": "1746703130799",
    "traceid": "1b355d07-852d-41cb-8c4d-d330485fcc4e",
    "sf": "95C5326AEC9CFC46EF0265B434C8FE339ED95625DE30C778BA395F7FF1E44C23B4C937C00F36C1D248B9CCB97761215EF0D8CAECDF47490BEF47407488A46CD9E33C3BC84F6A51F81C33B446503C8A9E0E0BF3E66CE76D6DB2CEB5569D321F8DE4993272BB288B6A96763966BF47A03413B1919169F6AC496B5EAC283CA32299BED751699E049F79DFB6F9D982303AF9522685FCFAE6F8EF19FE62B6AE577CA21DDD13DC3BD774BE14FB66CFE627341EDAD7A1B522C3DC5FCABCB55C00EF60865C7235F65BA812B1A1347F1F94EF1A973D2386D0972FE9450779C042535F7DA9",
}


def _normalize_base64(raw: str) -> str:
    text = (raw or "").strip().replace("\n", "")
    if not text:
        return ""
    text += "=" * ((4 - len(text) % 4) % 4)
    return text


def _pick_first_non_empty(mapping: dict, keys: list[str]) -> str:
    for key in keys:
        value = mapping.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_token_from_url(raw_url: str) -> tuple[str, str]:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return "", ""
    text = raw_url.strip()
    parts = []
    try:
        split = urlsplit(text)
        if split.query:
            parts.append(split.query)
        if split.fragment:
            parts.append(split.fragment)
    except Exception:
        if "?" in text:
            parts.append(text.split("?", 1)[1])
        if "#" in text:
            parts.append(text.split("#", 1)[1])
    for part in parts:
        parsed = parse_qs(part, keep_blank_values=True)
        openid = (parsed.get("openid", [""])[0] or parsed.get("open_id", [""])[0]).strip()
        token = (
            parsed.get("atk", [""])[0]
            or parsed.get("access_token", [""])[0]
            or parsed.get("token", [""])[0]
            or parsed.get("accessToken", [""])[0]
        ).strip()
        if openid and token:
            return openid, token
    return "", ""


def _extract_openid_atk(payload: dict) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    nodes = [payload]
    for key in ("data", "result", "d"):
        child = payload.get(key)
        if isinstance(child, dict):
            nodes.append(child)
        elif isinstance(child, str) and child.strip().startswith("{"):
            try:
                maybe_dict = json.loads(child)
                if isinstance(maybe_dict, dict):
                    nodes.append(maybe_dict)
            except Exception:
                pass

    openid_keys = ["openid", "open_id", "wx_openid", "uid", "user_openid"]
    token_keys = ["atk", "access_token", "accessToken", "token", "login_token", "atoken"]
    for node in nodes:
        openid = _pick_first_non_empty(node, openid_keys)
        token = _pick_first_non_empty(node, token_keys)
        if openid and token:
            return openid, token

    url_like_keys = ["url", "redirect_url", "callback", "callback_url", "raw"]
    for node in nodes:
        for key in url_like_keys:
            openid, token = _extract_token_from_url(node.get(key, ""))
            if openid and token:
                return openid, token
    return "", ""


def _call_wx_verify(wx_code: str, dynamic: bool) -> tuple[dict | None, str]:
    verify_params = dict(_WX_VERIFY_BASE_PARAMS)
    verify_params["code"] = wx_code
    if dynamic:
        verify_params["traceid"] = str(uuid.uuid4())
        verify_params["timestamp"] = str(int(time.time() * 1000))
    verify_resp = requests.get(_WX_VERIFY_URL, params=verify_params, timeout=15)
    verify_resp.raise_for_status()
    verify_text = verify_resp.text or ""
    try:
        verify_data = verify_resp.json()
    except Exception:
        return None, verify_text
    if not isinstance(verify_data, dict):
        return None, verify_text
    return verify_data, verify_text


def 获取微信二维码():
    try:
        scan_params = dict(_WX_SCAN_CODE_LOGIN_PARAMS)
        param_resp = requests.get(
            _WX_SCAN_CODE_LOGIN_URL,
            params=scan_params,
            headers=_WX_SCAN_CODE_LOGIN_HEADERS,
            timeout=15,
        )
        param_resp.raise_for_status()
        qr_params = param_resp.json()
        if not isinstance(qr_params, dict):
            raise ValueError("wx_scan_code_login 返回格式异常")
        if int(qr_params.get("ret", -1)) != 0:
            raise ValueError(f"wx_scan_code_login 失败: ret={qr_params.get('ret')} msg={qr_params.get('msg')}")

        qr_resp = requests.get(
            "https://open.weixin.qq.com/connect/sdk/qrconnect",
            params=qr_params,
            timeout=15,
        )
        qr_resp.raise_for_status()
        qr_data = qr_resp.json()
        if not isinstance(qr_data, dict):
            raise ValueError("qrconnect 返回格式异常")
        if int(qr_data.get("errcode", -1)) != 0:
            raise ValueError(f"qrconnect 失败: errcode={qr_data.get('errcode')} errmsg={qr_data.get('errmsg')}")

        wx_uuid = (qr_data.get("uuid") or "").strip()
        qrcode_node = qr_data.get("qrcode") or {}
        raw_base64 = ""
        if isinstance(qrcode_node, dict):
            raw_base64 = qrcode_node.get("qrcodebase64") or qrcode_node.get("base64") or ""
        elif isinstance(qrcode_node, str):
            raw_base64 = qrcode_node
        qr_base64 = _normalize_base64(raw_base64)
        if not wx_uuid or not qr_base64:
            raise ValueError("未获取到微信二维码数据")

        base64.b64decode(qr_base64)

        return {
            "uuid": wx_uuid,
            "qr_base64": qr_base64,
            "raw": qr_data,
        }
    except Exception as error:
        记录登录失败("应用宝", "微信二维码创建", str(error))
        raise


def 检查微信登录状态(wx_uuid: str):
    try:
        resp = requests.get(
            "https://long.open.weixin.qq.com/connect/l/qrconnect",
            params={"f": "json", "uuid": wx_uuid, "last": "404"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        wx_errcode = int(data.get("wx_errcode", -1))

        if wx_errcode == 404:
            return {"status": "waiting", "raw": data}
        if wx_errcode == 408:
            return {"status": "expired", "raw": data}
        if wx_errcode != 405:
            return {"status": "error", "error": f"未知微信状态码: {wx_errcode}", "raw": data}

        wx_code = (data.get("wx_code") or "").strip()
        if not wx_code:
            return {"status": "waiting", "raw": data}

        verify_attempts: list[tuple[dict, str]] = []
        last_non_json = ""

        for use_dynamic in (False, True):
            verify_data, verify_text = _call_wx_verify(wx_code, dynamic=use_dynamic)
            if verify_data is None:
                last_non_json = verify_text
                continue
            verify_attempts.append((verify_data, "dynamic" if use_dynamic else "fixed"))
            openid, atk = _extract_openid_atk(verify_data)
            if openid and atk:
                return {
                    "status": "success",
                    "openid": openid,
                    "atk": atk,
                    "raw": verify_data,
                }

        if last_non_json and not verify_attempts:
            return {
                "status": "error",
                "error": f"wx_verify_code 非JSON响应: {last_non_json[:160]}",
                "raw": last_non_json[:1200],
            }

        if verify_attempts:
            merged = []
            for item, mode in verify_attempts:
                merged.append(
                    {
                        "mode": mode,
                        "ret": item.get("ret"),
                        "msg": item.get("msg") or item.get("errmsg") or "",
                        "keys": sorted([str(k) for k in item.keys()]),
                    }
                )
            return {"status": "error", "error": f"wx_verify_code 未返回 openid/atk; attempts={merged}", "raw": verify_attempts[0][0]}
        return {"status": "error", "error": "wx_verify_code 无有效响应", "raw": {}}
    except Exception as error:
        记录登录失败("应用宝", "微信二维码轮询", str(error), {"uuid": wx_uuid})
        raise


def 用微信登录(openid: str, atk: str):
    if not openid or not atk:
        raise ValueError("缺少 openid 或 atk")
    return 请求UI_SK(channel_key="应用宝", account_id=openid, token=f"{atk}|WX")


def main():
    result = login_应用宝QQ()
    print(f"UI: {result['ui']}")
    print(f"SK: {result['sk']}")


if __name__ == "__main__":
    main()