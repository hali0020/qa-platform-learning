# 第二阶段 Docker 与可观测性边界

本目录提供本机容器化、可选 PostgreSQL 运行时与监控的参考拓扑，不代表应用已经达到多实例生产级别。默认 Compose 使用 SQLite 和 `PROVIDER_RUNTIME_MODE=local_lab`，不包含 Jenkins、GitLab 或蓝盾地址和凭据；所有发布到宿主机的端口都绑定在 `127.0.0.1`。PostgreSQL 不发布任何宿主机端口。

## 本机启动

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f infra/compose.phase2.yaml up --build
```

这组默认值是 `COMPOSE_DATABASE_RUNTIME_MODE=sqlite_local` 和容器内 SQLite URL。Compose 不在版本化的 YAML 中暗藏数据库回退值；它要求从被 Git 忽略的仓库根目录 `.env` 显式注入两个值。这样切换数据库时可以同时审查“运行模式 + 精确 URL”，不会因只改一项而误连。

浏览器访问 `http://127.0.0.1:23010`。后端不直接发布宿主机端口，由 Nginx 通过内部网络转发 `/api/`。容器内 Uvicorn 必须监听 `0.0.0.0`，所以 Compose 设置 `LOCAL_ONLY=false`；真正的本机隔离依赖 `127.0.0.1:23010:8080` 这一条端口映射，不能擅自改成 `23010:8080`。

Compose 的默认网络设置为 `internal: true`。运行中的后端、前端、PostgreSQL、Prometheus 和 Alertmanager 只能互相通信，没有默认公网出口；镜像和依赖下载发生在构建阶段。以后加入我们自建的 CI、消息队列和对象存储时，也必须加入这一内部网络，不能删除该硬边界。源码模式下自建 Provider 只允许环回地址；容器私网模式仍需精确 Host/Port/CIDR/Secret 白名单。

## 可选 PostgreSQL 实验 profile

代码层已接入 PostgreSQL：FastAPI 和 SQLite 共用同一套异步 SQLAlchemy Session/Repository，驱动固定为 `asyncpg==0.31.0`；Alembic 已做 PostgreSQL 方言的离线 upgrade/downgrade SQL 验证；流水线运行快照也已转为同一异步数据库事务适配。后端启动时会通过 Alembic 将所选数据库升到 head。

安全默认仍然是 SQLite。只有同时将 `.env` 中的 `COMPOSE_DATABASE_RUNTIME_MODE` 改为 `postgres_local_container`、将 `COMPOSE_DATABASE_URL` 改为精确的 `postgres:5432` URL，并在启动命令中加入 `--profile postgres`，应用才会连接该容器。后端还会校验 `APP_ENV=local-container`、`postgresql+asyncpg` 驱动以及精确主机/端口，复制一条公司或公网数据库 URL 会在建立连接前被拒绝。

当前开发机没有可用的 Docker 运行时，所以本阶段只完成了 Compose/依赖静态校验和非容器代码测试，**没有声称已运行 PostgreSQL 容器或已完成真实在线迁移验证**。真实容器启动、在线 upgrade/downgrade 和数据回滚是后续实操步骤。

镜像固定为 `postgres:17.11-alpine3.23`，不会跟随 `latest` 或浮动 major 自动升级。数据写入命名卷 `postgres-data`，挂载到 PostgreSQL 17 官方镜像的 `/var/lib/postgresql/data`。容器没有 `ports` 配置，只在 Compose 内部网络声明 `5432`；宿主机上的数据库客户端无法直接连接它。

### 1. 在本机准备密码

从仓库根目录复制示例配置：

```powershell
Copy-Item .env.example .env
```

然后用密码管理器生成一个仅供本实验使用的长随机密码。为让同一值既能作为 PostgreSQL 原始密码，又能安全插入 SQLAlchemy URL，这里限定字符集为 `A-Z`/`a-z`/`0-9`/`_`/`-`。在 `.env` 中填写 `POSTGRES_PASSWORD`，将已有的 `COMPOSE_DATABASE_RUNTIME_MODE=sqlite_local` 替换为 `postgres_local_container`，并将 SQLite `COMPOSE_DATABASE_URL` 替换为示例中的 PostgreSQL URL。URL 中的密码与 `POSTGRES_PASSWORD` 必须是同一个值。示例文件故意保留空密码，仓库也通过 `.gitignore` 忽略 `.env`。

Compose 把宿主机 `POSTGRES_PASSWORD` 转换为 `postgres_password` secret；PostgreSQL 容器只获得只读文件 `/run/secrets/postgres_password`，使用官方镜像的 `POSTGRES_PASSWORD_FILE` 读取。数据库容器不会出现 `POSTGRES_PASSWORD` 环境变量，但教学后端仍通过 `DATABASE_URL` 环境变量获得密码，因此不要分享会展开变量的 `docker compose config` 输出。这不等同于生产 Secret Manager。

### 2. 使用 PostgreSQL 启动应用

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile postgres up --build
```

这里显式传入仓库根目录的 `.env`。默认 SQLite 启动时可选依赖不阻断后端；启用 profile 后，后端会等待 PostgreSQL 健康再启动。

健康检查使用容器内的 `pg_isready`。不需要、也不应该为排查问题临时增加 `5432:5432`。可以通过容器内命令检查状态和版本：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile postgres ps
docker compose --env-file .env -f infra/compose.phase2.yaml --profile postgres exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version();"'
```

PostgreSQL 模式下，容器内后端的数据库主机名必须为 `postgres`、端口必须为 `5432`，而不是 `127.0.0.1`；宿主机源码模式无法访问这个未发布端口。阶段五的生产安全改造应让后端通过 secret file 或 Vault/Secret Manager 读取密码，不再注入完整 URL。

### 3. 停止与数据边界

普通停止不会删除命名卷：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile postgres down
```

`down --volumes` 会永久删除 PostgreSQL、SQLite 和监控命名卷中的本机学习数据，因此本说明不把它作为普通清理命令。需要重置时应先确认项目名和卷名，并按“可丢弃的本机实验数据”处理。

## 监控 profile

需要观察指标和告警规则时，可启用监控 profile：

```powershell
docker compose -f infra/compose.phase2.yaml --profile observability up --build
```

- Prometheus：`http://127.0.0.1:23090`
- Alertmanager：`http://127.0.0.1:23093`
- Alertmanager 默认使用空接收器，不会发送邮件或机器人消息。

建议指标名已经写入 `prometheus/alerts.yml`。当前 HTTP 指标使用路由模板与完整状态码标签；其他标签只使用任务状态、Provider 类型、操作和结果等固定低基数枚举，不得使用用户 ID、设备 ID、运行 ID、完整 URL 或异常正文。

## 健康检查约定

- `/health/live`：只检查进程与事件循环，不访问数据库或 Provider。
- `/health/ready`：对当前选中的本机 SQLite 或自建 PostgreSQL 执行有超时的 `SELECT 1`；外部 Provider 不阻断 readiness。
- `/metrics`：仅供内部网络抓取，不经前端 Nginx 暴露。

当前 Compose 已使用 `/health/ready` 作为容器健康检查。

## 上生产前的硬门槛

1. 在真实 PostgreSQL 容器上完成在线 upgrade/downgrade、SQLite 数据搬迁、回滚恢复与并发压测，并将剩余进程内锁收敛为支持多实例事务/CAS 的实现。
2. 数据库迁移作为独立部署 Job 执行，Web 副本不并发自动迁移。
3. 任务 Worker 与 Scheduler 使用独立进程；任务采用 at-least-once、租约、心跳和幂等 Handler。
4. 凭据来自 Secret Manager 或只读 secret 文件，不能放进 Compose、镜像、数据库明文字段或日志。
5. Provider 出站只能经过域名/CIDR allowlist 与网络层 egress policy，TLS 校验不可关闭。
6. 增加 HTTPS、认证授权、备份恢复演练、资源限制、镜像摘要/SBOM/漏洞扫描和发布回滚。
7. 多副本前先把现有流水线“整表 JSON checkpoint”改为按 Run 增量更新并增加版本号。

Provider API 契约依据：Jenkins Remote Access API、GitLab Pipelines API，以及 BK-CI 开源仓库中的 `UserBuildResource`。不同版本的蓝盾网关前缀和认证方式可能不同，启用前必须按目标部署重新核对；当前代码默认关闭真实 HTTP。
