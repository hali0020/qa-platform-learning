# 开发手册

继续写代码前先读 [NEXT_DEVELOPMENT.md](NEXT_DEVELOPMENT.md)；只按教程学习和操作
现有平台时可以跳过该交接页。

## 环境与安全前提

- Python 3.10
- Node.js 22 与 pnpm
- PowerShell（本仓库提供 `.ps1` 启动脚本）
- Docker（仅 Compose 与可选 PostgreSQL 练习需要）
- 所有业务数据和服务默认位于本机

不要复用公司项目的 `.env`、数据库 URL、Cookie 或 CI Token。根目录 `.env.example` 只有安全默认值；如需本机覆盖，复制为不会提交的 `.env`。

## 初始化后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m alembic upgrade head
```

首次管理员没有默认密码。可以启动前端后完成一次性本机初始化，也可以在 `backend` 目录交互式创建：

```powershell
python -m app.cli.bootstrap --username admin --display-name "Platform Admin"
```

密码通过隐藏输入读取，不要把密码作为 shell 参数或环境示例写进文档。

## 启动源码模式

从仓库根目录打开两个终端：

```powershell
.\scripts\start-backend.ps1
```

```powershell
.\scripts\start-frontend.ps1
```

后端脚本先执行 Alembic，再以 `127.0.0.1:23100` 启动；前端位于 `127.0.0.1:5173`。应用 lifespan 也会幂等升级持久数据库，但手工迁移更适合学习和提前发现问题。

## 后端验证

完整验证：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest
.\backend\.venv\Scripts\python.exe -m compileall -q backend\app backend\tests
.\backend\.venv\Scripts\python.exe -m pip check
```

按学习模块定向运行：

```powershell
# 身份、权限、评论与附件
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_auth_api.py backend\tests\test_collaboration_api.py -q

# 表格和质量报表
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_data_transfer_core.py backend\tests\test_quality_core.py backend\tests\test_data_quality_api_integration.py -q

# Provider、安全出站、任务、设备、Cron
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_pipeline_providers.py backend\tests\test_provider_security.py backend\tests\test_automation_tasks.py backend\tests\test_automation_devices.py backend\tests\test_automation_scheduler.py -q

# 独立 Learning CI、Provider 契约、固定运行边界与端到端 ASGI 调用
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_ci_lab_api.py backend\tests\test_ci_lab_offline_boundary.py backend\tests\test_learning_ci_provider.py backend\tests\test_learning_ci_runtime.py -q

# 指标、日志与探针
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_observability.py -q

# 数据库安全边界、共享持久化与 PostgreSQL 方言离线迁移
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_config.py backend\tests\test_database_runtime.py backend\tests\test_pipeline_persistence.py backend\tests\test_postgres_migrations.py -q
```

测试必须使用临时 SQLite、内存替身、PostgreSQL 方言离线编译或 Mock HTTP，不应连接外网 Provider/数据库。只有测试可显式使用 `APP_ENV=test` 与 `AUTH_ENABLED=false`；`APP_ENV=test` 不能启用真实 PostgreSQL 连接。

## 前端验证

```powershell
cd frontend
pnpm install
pnpm type-check
pnpm build
pnpm dev --host 127.0.0.1
```

前端请求必须设置 credentials，让浏览器携带 Session Cookie；写请求还要把 CSRF Cookie 值放入 `X-CSRF-Token`。不要把 Token 保存到 localStorage。

## Docker 本机演示

```powershell
docker compose -f infra/compose.phase2.yaml up --build
```

业务入口是 `http://127.0.0.1:23010`。启动监控 profile：

```powershell
docker compose -f infra/compose.phase2.yaml --profile observability up --build
```

独立 Learning CI 默认不启动。没有 Docker 时，可以在一个终端启动 Lab 与 QA
backend，再在另一个终端启动前端：

```powershell
.\scripts\start-ci-lab-source.ps1
.\scripts\start-frontend.ps1
```

源码脚本使用 owner-only 随机临时 Token、固定环回地址和独立
`.data/ci-lab-source` 数据库；它强制关闭 Broker/S3/OIDC/Vault，不会继承 `.env`
里的其他实验连接。Ctrl+C 时只清理它自己启动的 Lab 进程与临时 Token 文件。
要练习 Compose 隔离网络，则保持 `.env` 中
`COMPOSE_PROVIDER_RUNTIME_MODE=local_lab` 的安全默认值，再使用专用脚本为本次
Compose 调用生成临时机器 Token、显式选择 `ci_lab_local` 并重建两个消费端：

```powershell
.\scripts\start-ci-lab.ps1
```

CI Lab 观察地址是 `http://127.0.0.1:23020/health/live`，QA 入口仍是
`http://127.0.0.1:23010/`。脚本不输出 Token；不要运行会展开环境或容器 metadata
的诊断命令。完整边界和故障练习见
[DEPLOYMENT_PHASE6_CI_LAB.md](../infra/DEPLOYMENT_PHASE6_CI_LAB.md)。

默认 Compose 仍使用单 Worker 与 SQLite。可选自建 PostgreSQL 练习必须在被 Git 忽略的 `.env` 中把 `COMPOSE_DATABASE_RUNTIME_MODE` 改为 `postgres_local_container`，把 URL 改为 `.env.example` 中的 `postgresql+asyncpg://...@postgres:5432/...` 内部地址，并提供本机教学密码，然后显式启动 profile：

```powershell
docker compose -f infra/compose.phase2.yaml --profile postgres up --build
```

后端容器固定 `APP_ENV=local-container` 和 `LOCAL_DATA_ROOT=/data`；关系数据进入 `postgres-data` 卷，附件进入可写的 `qa-data:/data` 卷。PostgreSQL 只在内部网络暴露 `5432`，不映射到宿主机。不要改成 `localhost`、IP、其他端口或任意远程主机，也不要把 `.env`、真实数据库密码或 Provider Secret 提交到 Git。

当前机器没有 Docker，上述 PostgreSQL profile 尚未实机运行验证。现有测试只证明配置边界、共享 SQLAlchemy 持久化逻辑、Alembic/ORM 的 PostgreSQL 方言和 readiness 分支；具备 Docker 后仍要从空卷补做迁移、CRUD、流水线重启恢复和探针故障测试。无论选择哪个数据库，当前拓扑都仍是单 Worker。

## 修改数据库

1. 修改 ORM 模型。
2. 新增有序 Alembic 迁移并人工检查约束、索引、默认值和 downgrade。
3. 从空库执行 upgrade，执行 downgrade 后再次 upgrade。
4. 同时检查 SQLite 与 PostgreSQL 方言，验证旧数据升级、应用重启恢复和 metadata 漂移。
5. 若改动 PostgreSQL 路径，在具备 Docker 的隔离环境补真实迁移、事务回滚和 readiness；离线 DDL 不能代替集成测试。

不要依赖应用启动时 `create_all` 管理持久数据库；Alembic 是两种 backend 的唯一结构所有者。当前没有 SQLite→PostgreSQL 数据搬迁工具，也没有多实例迁移互斥；不要通过切换 URL 搬运已有数据。详细说明见 [DATABASE.md](DATABASE.md)。

## 提交前检查

```powershell
git status --short
git diff --check
git diff -- .env.example docs backend frontend infra
```

确认没有 `.env`、数据库、上传文件、Token、日志、`dist`、`node_modules`、`.venv`、缓存或公司项目内容。只提交本项目自行编写、且属于当前学习步骤的变更。

## 完成定义

一个步骤只有同时满足以下条件才算完成：

1. 能在本机复现并能解释正常路径。
2. 有自动化验证或清晰的手工验证步骤。
3. 有至少一个失败/重试/权限/边界测试。
4. 不包含真实密钥、公司连接或线上数据。
5. 文档说明当前一致性与生产差距。

提交前缀可使用 `feat:`、`fix:`、`docs:`、`test:`、`refactor:` 和 `chore:`；每个学习步骤尽量形成小而可回退的提交。
