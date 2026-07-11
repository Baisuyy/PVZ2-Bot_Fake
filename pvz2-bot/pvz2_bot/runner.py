"""
统一启动入口 — 按需启动各子系统

用法:
  python -m pvz2_bot.runner account-manager   # 账号管理 API (8000端口)
  python -m pvz2_bot.runner activate           # 双平台激活客户端
  python -m pvz2_bot.runner invite             # 邀请服务 (5000端口)
  python -m pvz2_bot.runner ios-register       # iOS 自动注册
  python -m pvz2_bot.runner task-node          # 任务执行节点 (39902端口)
  python -m pvz2_bot.runner all                # 全部启动
"""
import argparse
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("runner")


def main():
    parser = argparse.ArgumentParser(description="PVZ2 代刷机器人")
    parser.add_argument("command", nargs="?", default="all",
                       choices=["account-manager", "activate", "invite",
                                "ios-register", "task-node", "frontend", "frontend-bg", "scheduler", "all"],
                       help="启动哪个子系统")
    parser.add_argument("--port", type=int, default=0,
                       help="覆盖默认端口")
    parser.add_argument("--total", type=int, default=1000000,
                       help="iOS注册目标数量")
    parser.add_argument("--sleep", type=float, default=0.5,
                       help="iOS注册间隔(秒)")
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════╗
║        PVZ2 代刷机器人 v3.0         ║
╚══════════════════════════════════════╝
    """)

    if args.command == "account-manager":
        from .account_manager import run
        port = args.port or 8000
        print(f"📦 账号管理服务 → 端口 {port}")
        run(port)

    elif args.command == "activate":
        from .activate_client import run as activate_run
        print("🔑 双平台激活客户端")
        activate_run()

    elif args.command == "invite":
        from .invite_service import run as invite_run
        port = args.port or 5000
        print(f"📨 邀请服务 → 端口 {port}")
        invite_run(port)

    elif args.command == "ios-register":
        from .ios_register import run as ios_run
        print(f"📱 iOS 注册 → 目标 {args.total} 个")
        ios_run(total=args.total, sleep_seconds=args.sleep)

    elif args.command == "task-node":
        from .task_api import run as task_run
        port = args.port or 39902
        print(f"⚙️ 任务执行节点 → 端口 {port}")
        task_run(port)

    elif args.command == "frontend":
        from .frontend import run as fe_run
        port = args.port or 5555
        print(f"🖥️ 控制台前端 → 端口 {port}")
        fe_run(port)

    elif args.command == "frontend-bg":
        import threading
        from .frontend import run as fe_run
        port = args.port or 5555
        t = threading.Thread(target=fe_run, args=(port,), name="Frontend", daemon=True)
        t.start()
        print(f"🖥️ 控制台前端 → :{port} (后台)")

    elif args.command == "scheduler":
        from .scheduler import run as sch_run
        port = args.port or 39900
        print(f"📡 调度层 → 端口 {port}")
        sch_run(port)

    elif args.command == "all":
        import threading
        import time

        threads = []

        # 账号管理 (必需先启动)
        from .account_manager import run as am_run
        t_am = threading.Thread(target=am_run, args=(8000,), name="AccountMgr", daemon=True)
        t_am.start()
        threads.append(t_am)
        time.sleep(1)

        # 激活客户端
        from .activate_client import run as ac_run
        t_ac = threading.Thread(target=ac_run, name="Activator", daemon=True)
        t_ac.start()
        threads.append(t_ac)

        # 邀请服务
        from .invite_service import run as iv_run
        t_iv = threading.Thread(target=iv_run, args=(5000,), name="InviteSvc", daemon=True)
        t_iv.start()
        threads.append(t_iv)

        # 任务节点
        from .task_api import run as tn_run
        t_tn = threading.Thread(target=tn_run, args=(39902,), name="TaskNode", daemon=True)
        t_tn.start()
        threads.append(t_tn)

        # 调度层
        from .scheduler import run as sch_run
        t_sch = threading.Thread(target=sch_run, args=(39900,), name="Scheduler", daemon=True)
        t_sch.start()
        threads.append(t_sch)
        time.sleep(2)

        # 控制台前端
        from .frontend import run as fe_run
        t_fe = threading.Thread(target=fe_run, args=(5555,), name="Frontend", daemon=True)
        t_fe.start()
        threads.append(t_fe)

        print("✅ 全部子系统已启动:")
        print("  📡 调度层       → :39900")
        print("  📦 账号管理     → :8000")
        print("  🖥️ 控制台前端   → :5555")
        print("  🔑 激活客户端   → 后台运行")
        print("  📨 邀请服务     → :5000")
        print("  ⚙️ 任务节点     → :39902")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 收到退出信号，正在停止...")


if __name__ == "__main__":
    main()