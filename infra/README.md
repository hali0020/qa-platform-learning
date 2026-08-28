# Infrastructure

这里保存本机教学基础设施：

- SQLite：最早期关系数据练习
- PostgreSQL：内部网络中的可选迁移实验容器
- RabbitMQ：内部网络中的固定唤醒提示队列
- SeaweedFS：可选 `object-storage` profile 中的单节点 S3 兼容学习服务，并由双层内部网络上的 S3-only 网关隔离其管理面
- Keycloak：可选 `identity-secrets` profile 中的自建 OIDC/PKCE/TOTP realm，并由双层内部网络隔离 master/Admin/management 面
- Vault：可选 `identity-secrets` profile 中的持久 sealed 单节点 Secret 教学服务，应用网关只放行两个只读 KV-v2 path
- Learning CI：可选 `ci-lab` profile 中由本仓库实现的异步 CI HTTP 模拟服务，固定在独立 `172.30.60.0/28` 内部网络
- migration Job：Compose 中唯一执行 Alembic upgrade 的一次性进程
- Worker：数据库权威 claim、租约心跳和固定本机 Handler
- Scheduler：PostgreSQL `SKIP LOCKED` claim、事务外 Cron 计算和 CAS finalize
- Provider Dispatcher：claim 持久 Trigger Intent，在数据库事务外执行 CI HTTP
- Outbox Dispatcher：发布 transactional outbox 中固定、无业务内容的 RabbitMQ 唤醒提示；Web 不持有 Broker
- 本地流水线模拟器
- 本地文件制品库
- Docker Compose
- 日志和指标

默认不配置任何公司内网或线上服务。PostgreSQL 不发布宿主机端口，密码由被
Git 忽略的本机 `.env` 提供并以 Compose secret 文件挂载给数据库。应用默认使用
SQLite；启用 profile 并显式更改同一 `.env` 后才会切换到内部 PostgreSQL。具体边界与命令见
[DEPLOYMENT_PHASE2.md](DEPLOYMENT_PHASE2.md)。PostgreSQL + RabbitMQ + 多 Worker
实验见 [DEPLOYMENT_PHASE3.md](DEPLOYMENT_PHASE3.md)。对象存储默认仍是本机文件系统；
显式启用的 SeaweedFS 拓扑、镜像固定与凭据边界见
[DEPLOYMENT_PHASE4.md](DEPLOYMENT_PHASE4.md)。
阶段五 Keycloak + Vault 的初始化、最小权限 token、人工 unseal 和故障练习见
[DEPLOYMENT_PHASE5.md](DEPLOYMENT_PHASE5.md)。两者默认均不启动，核心服务不发布
宿主机端口，真实凭据只允许来自被 Git 忽略的 `.env`、`.data/secrets` 或 Compose
secret；root token 与 unseal key 永远不挂载给应用。

第六阶段 Learning CI 不是公司 Jenkins、GitLab 或蓝盾的替身连接，也不会发现或
导入任何现有服务。它只在显式 `ci-lab` profile 下启动；QA backend 的出站目标由
代码固定为专网中的单个 `/32` 地址，宿主机教学入口只发布到
`127.0.0.1:23020`。启动、连接、幂等触发和故障练习见
[DEPLOYMENT_PHASE6_CI_LAB.md](DEPLOYMENT_PHASE6_CI_LAB.md)。

当前机器没有 Docker。上述 migration/进程入口、PostgreSQL SQL 形状、outbox 和状态机
已有自动化验证，但真实 PostgreSQL/RabbitMQ、多 Worker/Scheduler/Dispatcher、重复
消息、进程崩溃、Broker/数据库中断和备份恢复尚未实跑。Web 当前保持单实例，CI Lab
只支持单 API + 单 Webhook Worker 共享 SQLite；不宣称 HA。持久主动 Webhook
Outbox/Worker 已有自动化验证，但固定 IP 双向 HTTP、崩溃/租约/死信恢复尚未
容器实测。Jenkins/GitLab/BK-CI 和公司系统仍关闭。
