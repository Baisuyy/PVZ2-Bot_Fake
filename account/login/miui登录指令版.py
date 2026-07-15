from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import base64
import json
import requests
from urllib.parse import urlparse, parse_qs
from login_failure_logger import 记录登录失败
from proxy_pool import 启用全局代理
from pvz2_common import 请求UI_SK

启用全局代理()

授权链接 = (
    "https://account.xiaomi.com/oauth2/authorize"
    "?client_id=2882303761517516898"
    "&response_type=code"
    "&scope=1%203"
    "&redirect_uri=http://game.xiaomi.com/oauthcallback/mioauth"
    "&state=05dacf23d8eb09c7"
)

def 加密函数(明文, 密钥):
    加密器 = AES.new(密钥.encode('utf-8'), AES.MODE_ECB)
    填充后的数据 = pad(明文.encode('utf-8'), AES.block_size)
    密文 = 加密器.encrypt(填充后的数据)
    return base64.urlsafe_b64encode(密文).decode('utf-8')

def 解密函数(密文, 密钥):
    解密器 = AES.new(密钥.encode('utf-8'), AES.MODE_ECB)
    解码数据 = base64.urlsafe_b64decode(密文)
    解密数据 = 解密器.decrypt(解码数据)
    去除填充 = unpad(解密数据, AES.block_size)
    return 去除填充.decode('utf-8')

def 处理授权流程(授权码):
    try:
        # 第一阶段：获取UUID和会话令牌
        请求参数 = (
            f"accountType=4&code={授权码}&isSaveSt=true&appid=2000202"
            f"&channelId=meng_100_1_android&sdkVersion=SDK_MI_SP_3.4.6"
            f"&devAppid=2882303761517212602&oaid=ced2258b-2f1a-47a3-b9b1-217c1261c87d"
            f"&ua=Xiaomi|23116PN5BC|12_stable_913|V417IR release-keys|32|shennong"
        )
        加密参数 = 加密函数(请求参数, "migc_game_sdkkey").replace('-', '%2B').replace('_', '%2F')
        
        响应结果 = requests.get(
            url=f"https://account.migc.g.mi.com/misdk/v2/oauth?p={加密参数}",
            headers={
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; 23116PN5BC Build/c7565ea.0)",
                "Host": "account.migc.g.mi.com",
                "Connection": "close"
            },
            timeout=15
        )
        
        解密结果 = 解密函数(响应结果.text, "migc_game_sdkkey")
        解析数据 = json.loads(解密结果)
        
        if 'uuid' not in 解析数据 or 'st' not in 解析数据:
            raise ValueError("第一阶段响应缺少关键字段uuid或st")

        阶段1状态码 = 解析数据.get("code", 0)
        用户标识 = 解析数据['uuid']
        会话令牌 = 解析数据['st']
        if 阶段1状态码 != 0 or not 用户标识 or not 会话令牌:
            raise ValueError("MIUI授权码无效或已过期，请重新在登录页获取新的code")

        # 第二阶段：获取长期会话
        请求体 = (
            f"fuid={用户标识}&devAppId=2882303761517212602&toke={会话令牌}"
            f"&imei=&sdkVersion=SDK_MI_SP_3.4.6&channel=meng_100_1_android"
            f"&ua=Xiaomi|23116PN5BC|12_stable_913|V417IR release-keys|32|shennong"
            f"&currentChannel=meng_100_1_android&imeiMd5=&firstChannel=&oaid="
            f"&needRealNameInfo=false&needServiceToken=true&needRiskControl=true"
        )
        
        加密器 = AES.new(b"migc_game_sdkkey", AES.MODE_ECB)
        加密数据 = 加密器.encrypt(pad(请求体.encode(), AES.block_size))
        
        响应 = requests.post(
            url="https://account.migc.g.mi.com/migc-sdk-account/getLoginAppAccount_v2",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; 23116PN5BC Build/c7565ea.0)"
            },
            data={"p": base64.b64encode(加密数据).decode()},
            timeout=15
        )
        
        解密响应 = unpad(加密器.decrypt(base64.b64decode(响应.text)), AES.block_size).decode()
        会话数据 = json.loads(解密响应)
        
        if 'appAccountId' not in 会话数据 or 'session' not in 会话数据:
            raise ValueError("第二阶段响应缺少关键字段appAccountId或session")

        app_account_id = 会话数据.get("appAccountId")
        session_token = 会话数据.get("session")
        ret_code = 会话数据.get("retCode")
        if ret_code not in (0, 200) or not app_account_id or not session_token:
            raise ValueError("MIUI登录失败，请重新获取code后重试")

        # 第三阶段：请求V202
        游戏结果 = 请求UI_SK(
            channel_key="miui",
            account_id=app_account_id,
            token=session_token,
        )
        
        return {
            "ui": 游戏结果["ui"],
            "sk": 游戏结果["sk"]
        }
    
    except Exception as e:
        记录登录失败("miui", "处理授权流程", str(e), {"auth_code": 授权码})
        raise

def 自动获取授权码(超时秒数=240):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError("未检测到 Playwright。请先执行: pip install playwright && playwright install chromium") from e

    with sync_playwright() as p:
        浏览器 = p.chromium.launch(headless=False)
        上下文 = 浏览器.new_context()
        页面 = 上下文.new_page()
        页面.goto(授权链接, wait_until="domcontentloaded")

        剩余等待毫秒 = 超时秒数 * 1000
        间隔毫秒 = 500
        try:
            while 剩余等待毫秒 > 0:
                当前地址 = 页面.url
                查询参数 = parse_qs(urlparse(当前地址).query)
                code_list = 查询参数.get("code")
                if code_list and code_list[0]:
                    return code_list[0]

                页面.wait_for_timeout(间隔毫秒)
                剩余等待毫秒 -= 间隔毫秒

            raise TimeoutError(f"等待授权码超时（{超时秒数}秒）")
        finally:
            浏览器.close()

def 主函数():
    print("MIUI账号登录工具 - 指令版")
    print("授权链接:")
    print(f"   {授权链接}")
    
    while True:
        try:
            方式 = input("\n请选择获取方式 [1自动/2手动/q退出]: ").strip().lower()
            
            if 方式 == 'q':
                break

            if 方式 == '1':
                try:
                    授权码 = 自动获取授权码()
                except Exception as e:
                    print(f"自动获取授权码失败: {str(e)}")
                    授权码 = input("请输入授权码: ").strip()
            elif 方式 == '2':
                授权码 = input("请输入授权码: ").strip()
            else:
                print("无效选项，请输入 1 / 2 / q")
                continue
            
            if not 授权码:
                print("授权码不能为空，请重新输入")
                continue
            
            结果 = 处理授权流程(授权码)
            
            save_choice = input("\n是否将结果保存到文件? (y/n): ").strip().lower()
            if save_choice == 'y':
                with open('miui_login_result.txt', 'w', encoding='utf-8') as f:
                    f.write(f"UI: {结果['ui']}\n")
                    f.write(f"SK: {结果['sk']}\n")
                print("结果已保存到 miui_login_result.txt")
            
            continue_choice = input("\n是否继续登录? (y/n): ").strip().lower()
            if continue_choice != 'y':
                break
                
        except KeyboardInterrupt:
            print("\n用户中断操作")
            break
        except Exception as e:
            print(f"登录失败: {str(e)}")
            continue

if __name__ == '__main__':
    主函数()
