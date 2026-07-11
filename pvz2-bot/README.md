# PVZ2 代刷机器人 v3.0

植物大战僵尸2 云端代刷系统 — 完整重构的模块化版本。

## 架构

```
pvz2_bot/
├── crypto.py          # AES-CBC 加密 (MD5→key→MD5→iv)
├── config.py          # 全局配置/密钥/端点
├── database.py        # SQLite 账号存储 (Account Manager)
├── pgsql_db.py        # Postgres+Redis (整合登录)
├── account_models.py  # Pydantic 数据模型
├── account_manager.py # FastAPI 账号管理 (8000端口)
├── activate_client.py # 双平台账号激活
├── cloud_client.py    # PVZ2 云端 API 客户端
├── ios_register.py    # iOS 自动注册 V201→V203
├── invite_service.py  # Flask 邀请服务 (5000端口)
├── task_engine.py     # 任务调度引擎
├── task_models.py     # Task/RuntimeStore 模型
├── task_api.py        # Flask 任务节点 (39902端口)
└── runner.py          # 统一启动入口
```

## 部署

```bash
pip install -r requirements.txt
```

### 子系统启动

| 命令 | 端口 | 功能 |
|------|------|------|
| `python -m pvz2_bot.runner account-manager` | 8000 | 账号CRUD+原子分发 |
| `python -m pvz2_bot.runner activate` | — | 双平台账号激活 |
| `python -m pvz2_bot.runner invite` | 5000 | 邀请码填写 |
| `python -m pvz2_bot.runner ios-register` | — | iOS自动注册 |
| `python -m pvz2_bot.runner task-node` | 39902 | 任务执行节点 |
| `python -m pvz2_bot.runner all` | 全部 | 一键启动全部 |

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PVZ_ACCOUNT_API_URL` | `http://127.0.0.1:8000` | Account Manager 地址 |
| `PVZ_SCHEDULER_URL` | `http://43.156.165.72:39900` | 调度层地址 |
| `PVZ_NODE_NAME` | 主机名 | 节点标识 |
| `PVZ_NODE_PORT` | 39902 | 任务节点端口 |

## API 清单

### Account Manager (8000)
- `POST /api/accounts/upload` — 上传单个账号
- `POST /api/accounts/upload/batch` — 批量上传
- `GET /api/accounts/distribute?platform=&purpose=` — 原子分发
- `POST /api/accounts/distribute/batch` — 批量分发
- `PUT /api/accounts/{id}/status` — 更新状态
- `POST /api/accounts/batch/status` — 批量更新状态
- `GET /api/accounts/stats` — 统计
- `GET /api/accounts/list` — 列表

### 邀请服务 (5000)
- `POST /api/invite` — 填写邀请码

### 任务节点 (39902)
- `POST /api/v2/task/submit` — 提交任务
- `GET /api/v2/task/status/<id>` — 查询任务
- `GET /api/v2/status` — 节点状态

## 加密协议

```
MD5(secret + req_name) → AES key (16 bytes)
MD5(key)               → AES iv  (16 bytes)
AES-CBC + PKCS7 pad    → base64
base64                 → URL-safe (+,/,= → -/_,)
```

响应码:
- `r:0/75051` → 成功
- `r:40024` → 封禁
- `r:20024` → 需要新NS