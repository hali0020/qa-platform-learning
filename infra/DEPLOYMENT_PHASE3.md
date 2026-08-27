# 第三阶段本机 Worker 拓扑

本说明只描述自建的 Docker Compose 学习环境。RabbitMQ、PostgreSQL 和 Worker 都位于 `qa-platform-learning-internal` 内部网络，不发布 `5672`、`15672` 或 `5432` 到宿主机。

## 准备本机配置

复制示例文件：

```powershell
Copy-Item .env.example .env
```

在被 Git 忽略的 `.env` 中完成以下修改：

```dotenv
COMPOSE_DATABASE_RUNTIME_MODE=postgres_local_container
COMPOSE_DATABASE_URL=postgresql+asyncpg://qa_platform_learning:<本机实验密码>@postgres:5432/qa_platform_learning
POSTGRES_PASSWORD=<与URL相同的本机实验密码>

COMPOSE_BROKER_RUNTIME_MODE=rabbitmq_local_container
COMPOSE_BROKER_URL=amqp://qa_platform_learning:<RabbitMQ本机实验密码>@rabbitmq:5672/qa_platform_learning
RABBITMQ_DEFAULT_USER=qa_platform_learning
RABBITMQ_DEFAULT_PASS=<与Broker URL相同的RabbitMQ本机实验密码>
```

两个密码都使用密码管理器生成，只使用 URL 安全的字母、数字、`_`、`-`，并且互不复用。不要把填写后的 `.env`、展开后的 `docker compose config` 或容器 inspect 输出提交、截图或分享。

仓库根 `.dockerignore` 与两个 Dockerfile 专用 ignore 文件都采用 deny-first 规则，只把构建所需源码加入上下文；`.env`、`.data`、`.git`、虚拟环境和 `node_modules` 不会发送给 Docker builder。新增 Dockerfile 时也必须延续这一规则。

RabbitMQ 官方镜像不再支持默认用户密码的 `_FILE` 变量，本阶段只能从忽略的 `.env` 注入 `RABBITMQ_DEFAULT_PASS`。该值会出现在 Compose 展开配置和容器环境中；这是本机教学限制，不是生产 Secret 方案。

## 启动和扩容

先用一个 Worker 验证：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile worker up --build --scale worker=1
```

验证稳定后扩到三个进程：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile worker up --build --scale worker=3
```

不要添加 `container_name`，否则 Compose 无法扩容。Worker 没有 HTTP 端口；管理 UI `15672` 也没有宿主机映射。排查应使用受控的容器内命令和脱敏日志，而不是临时增加端口映射。

Worker 的默认参数：

| 变量 | 默认值 | 含义 |
| --- | ---: | --- |
| `WORKER_QUEUES` | `default` | 订阅的数据库队列，逗号分隔 |
| `WORKER_LEASE_SECONDS` | `30` | 数据库租约长度 |
| `WORKER_HEARTBEAT_SECONDS` | `10` | 续租间隔，必须小于租约 |
| `WORKER_POLL_SECONDS` | `5` | 没有消息时的数据库兜底轮询 |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `30` | SIGTERM 后等待当前任务的时间，范围 0.1–30 秒 |

Compose 给 Worker 容器设置 40 秒停止宽限，长于应用允许的最大 30 秒，确保应用有机会记录可重试退出。RabbitMQ 启动失败时 Worker 会继续数据库轮询，并按最高 30 秒的指数退避重试同一 Broker Source；日志只记录异常类型，不记录可能含 URL 或凭据的异常正文。

## 静态和运行验证

Python 侧可在宿主机执行：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_worker_backend.py backend/tests/test_worker_handlers.py backend/tests/test_worker_main.py backend/tests/test_worker_runner.py -q
```

拥有 Docker 的机器还应执行：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile worker config --quiet
docker compose --env-file .env -f infra/compose.phase2.yaml --profile worker ps
```

当前开发机未安装 Docker，因此仓库维护者尚未执行容器启动、RabbitMQ 健康检查、多 Worker 并发或断线演练。文档和 YAML 通过不等于容器已验证。

## 停止

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile worker down
```

普通 `down` 保留数据库和 RabbitMQ 命名卷。本说明不提供 `down --volumes` 作为常规命令，因为它会不可恢复地删除本机学习数据。
