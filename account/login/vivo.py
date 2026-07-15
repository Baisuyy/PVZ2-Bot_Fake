import json
import sys
from login_failure_logger import 记录登录失败
from pvz2_common import 请求UI_SK

def extract_vivo_info(data_input: str) -> dict:
    """提取Vivo账号信息"""
    try:
        result = json.loads(data_input)
        if "data" not in result or "subAccounts" not in result["data"]:
            记录登录失败("vivo", "解析账号信息", "输入数据格式不符合要求，缺少必要字段")
            return {"error": "输入数据格式不符合要求，缺少必要字段"}
        
        sub_open_ids = []
        open_token = None
        
        for account in result["data"]["subAccounts"]:
            sub_open_id = account.get("subOpenId")
            if sub_open_id:
                sub_open_ids.append(sub_open_id)
            
            current_open_token = account.get("openToken")
            if current_open_token:
                if open_token is None:
                    open_token = current_open_token
                else:
                    记录登录失败("vivo", "解析账号信息", "检测到多个openToken，不支持此场景")
                    return {"error": "检测到多个openToken，不支持此场景"}
        
        return {
            "sub_open_ids": sub_open_ids,
            "open_token": open_token
        }
    
    except json.JSONDecodeError:
        记录登录失败("vivo", "解析账号信息", "输入的内容不是有效的JSON格式")
        return {"error": "输入的内容不是有效的JSON格式"}
    except Exception as e:
        记录登录失败("vivo", "解析账号信息", str(e))
        return {"error": f"处理过程中发生异常: {str(e)}"}

def 发送游戏请求(oi, t):
    try:
        响应 = 请求UI_SK(channel_key="vivo", account_id=oi, token=t)
        return {'status': 'success', 'ui': 响应["ui"], 'sk': 响应["sk"]}
    except Exception as e:
        记录登录失败("vivo", "请求V202", str(e), {"sub_open_id": oi})
        return {'status': 'error', 'message': f'游戏请求失败: {str(e)}'}

def load_json_from_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"读取文件失败: {str(e)}")
        return None

def display_help():
    print("=" * 60)
    print("Vivo账号信息提取工具 - 终端版")
    print("=" * 60)
    print("\n使用方式:")
    print("1. python script.py <JSON字符串>")
    print("2. python script.py -f <JSON文件路径>")
    print("3. python script.py (从剪贴板读取)")
    print("\n获取JSON数据的步骤:")
    print("1. 访问 https://passport.vivo.com.cn/#/login?lang=zh_CN 登录")
    print("2. 访问 https://joint.vivo.com.cn/h5/union/get?gamePackage=com.popcap.pvz2cthdbbg")
    print("3. 复制页面显示的JSON数据")
    print("\n示例:")
    print('python script.py \'{"data": {"subAccounts": [{"subOpenId": "123", "openToken": "abc"}]}}\'')
    print("=" * 60)

def main():
    if len(sys.argv) > 1:
        if sys.argv[1] in ['-h', '--help']:
            display_help()
            return
        
        if sys.argv[1] == '-f' and len(sys.argv) > 2:
            json_data = load_json_from_file(sys.argv[2])
            if not json_data:
                return
        else:
            json_data = sys.argv[1]
    else:
        print("请输入JSON数据（输入完成后按以下方式结束）:")
        print("  Windows: Ctrl+Z 然后按Enter")
        print("  macOS/Linux: Ctrl+D")
        print("  或输入单独的一行'END'结束输入")
        print("=" * 60)
        lines = []
        try:
            while True:
                line = input()
                if line.strip() == 'END':
                    break
                lines.append(line)
        except EOFError:
            pass
        except KeyboardInterrupt:
            print("\n\n👋 用户中断，程序退出")
            return
            
        json_data = '\n'.join(lines)
    
    if not json_data or json_data.strip() == '':
        display_help()
        return
    
    result = extract_vivo_info(json_data)
    
    if "error" in result:
        print(f"\n❌ 错误: {result['error']}")
        return
    
    sub_open_ids = result["sub_open_ids"]
    open_token = result["open_token"]
    
    print(f"\n✅ 提取成功!")
    print(f"📊 找到 {len(sub_open_ids)} 个subOpenId")
    print(f"🔑 openToken: {open_token}")
    
    if not open_token:
        print("\n❌ 错误: 未找到openToken，无法调用游戏请求")
        return
    
    all_results = []
    successful_count = 0
    
    for i, oi in enumerate(sub_open_ids, 1):
        print(f"\n[{i}/{len(sub_open_ids)}] 处理 subOpenId: {oi}")
        print("-" * 40)
        
        game_result = 发送游戏请求(oi, open_token)
        
        if game_result["status"] == "success":
            print(f"✅ 成功获取:")
            print(f"   UI: {game_result['ui']}")
            print(f"   SK: {game_result['sk']}")
            successful_count += 1
        else:
            print(f"❌ 失败: {game_result.get('message', '未知错误')}")
        
        all_results.append({
            "sub_open_id": oi,
            "ui": game_result.get("ui", ""),
            "sk": game_result.get("sk", ""),
            "status": game_result["status"]
        })
    
    print("\n" + "=" * 60)
    print("处理完成!")
    print("=" * 60)
    
    print(f"\n📊 统计:")
    print(f"   总账号数: {len(sub_open_ids)}")
    print(f"   成功数: {successful_count}")
    print(f"   失败数: {len(sub_open_ids) - successful_count}")
    
    output_file = "vivo_accounts_results.json"
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                "open_token": open_token,
                "results": all_results,
                "summary": {
                    "total": len(sub_open_ids),
                    "successful": successful_count,
                    "failed": len(sub_open_ids) - successful_count
                }
            }, f, ensure_ascii=False, indent=2)
        print(f"\n💾 结果已保存到: {output_file}")
    except Exception as e:
        print(f"\n⚠️  保存结果文件失败: {str(e)}")
    
    print("\n" + "=" * 60)
    print("可用的UI/SK组合:")
    print("=" * 60)
    
    for result in all_results:
        if result["status"] == "success":
            print(f"\nsubOpenId: {result['sub_open_id']}")
            print(f"UI: {result['ui']}")
            print(f"SK: {result['sk']}")
            print("-" * 40)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 用户中断，程序退出")
    except Exception as e:
        print(f"\n❌ 程序运行出错: {str(e)}")
