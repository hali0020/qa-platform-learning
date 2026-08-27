# 架构说明

## 设计目标

平台采用模块化单体起步。模块化单体让 HTTP、领域规则、事务和故障更容易学习；只有当独立部署、伸缩、资源隔离或团队所有权真正需要时，才拆成网络服务。

## 分层与依赖方向

```text
Vue 管理后台 / API Client
             ↓
FastAPI Router ── 认证、权限、参数、HTTP 状态
             ↓
Application Service ── 用例编排、幂等与一致性边界
             ↓
Domain ── QA 状态机、任务/设备/Cron 规则
             ↓
Repository / Provider / Object Storage / Identity / Secret Store Port
             ↓
SQLite（默认）/ PostgreSQL（可选内部容器）/ 本机文件（默认）/ SeaweedFS S3（可选内部容器）/ Local Provider / 独立 Learning CI Lab / 自建产品 CI API
```

领域和应用服务不应知道 Learning CI、Jenkins、GitLab、BK-CI 或某个 S3 实现的具体 URL，也不应直接操作上传目录。供应商协议由 `PipelineProvider` 隔离，对象内容由 Storage Port 隔离，数据库由 Repository 隔离。SQLite 与 PostgreSQL 共用异步 SQLAlchemy 会话、Repository 和 Alembic metadata；流水线快照也通过同一数据库适配器持久化。关系数据库继续保存附件归属、大小、摘要和对象键等元数据，对象存储只保存不可信的二进制内容。

## 当前模块

- Identity：用户、内置系统角色、权限、Session 与 CSRF。
- QA Core：项目、测试设计、计划、执行和结果。
- Defect/Audit：缺陷状态机、业务关联与追加式事件。
- Collaboration：评论、回复、附件元数据与本机安全存储。
- Data Transfer：CSV/XLSX 模板、解析、预检、部分创建与导出。
- Quality：通过率、覆盖率、趋势和套件维度的只读计算。
- Pipeline：本地流水线，以及 Local/Learning CI/Jenkins/GitLab/BK-CI Provider。
- CI Lab：独立 FastAPI/SQLite 控制面，拥有固定 Definition 和 Run 状态事实；不共享 QA 数据库，不提供 Shell、动态插件、Git clone 或任意 URL。
- Automation Runtime：持久任务、设备租约、调度和 Provider 运行映射。
- Broker/Worker：固定 RabbitMQ 唤醒提示、数据库权威租约、固定本机 Handler 与优雅退出。
- Object Storage：默认本机文件适配器，以及只允许内部 `seaweedfs:8333` 的 S3 兼容适配器；Bucket 固定为 `qa-artifacts`。
- External Identity：默认关闭的 Keycloak OIDC 适配，固定 public issuer、内部 token/JWKS transport、PKCE/nonce/state 与管理员显式 subject 绑定。
- Secret Store：默认环境适配器，以及只允许内部 `vault:8200`、两个精确 KV-v2 文档和 token file 的 Vault 适配器；当前业务接线仅覆盖 Provider Secret。
- Observability：存活/就绪、结构化日志、Request ID 与 Prometheus 指标。

## 身份边界

当前是单租户、系统级 RBAC。浏览器使用服务端 Session Cookie；数据库只存 Session Token 摘要。写请求同时校验 CSRF Cookie 和 Header。Router 执行粗粒度 permission gate，Service 执行作者、资源状态、跨项目关系等业务级授权。

阶段五保留本机账号为默认值，并增加只能显式选择的自建 Keycloak 模式。浏览器经 `127.0.0.1:23010/identity` 完成 Authorization Code + S256 PKCE；后端 token/JWKS 请求只到内部 `keycloak:8080`。两条路径共享固定 issuer。OIDC 不做 JIT 用户创建，也不按 username/email 自动匹配，更不信任 realm/client role。切换前，拥有 `users.manage` 的本地管理员必须通过受 Session + CSRF 保护的 API 把 Keycloak 稳定 `(issuer, sub)` 显式绑定到一个已启用的本地用户；登录只认该绑定，权限仍来自本机 RBAC。Keycloak core 位于第二层网络，gateway 不暴露 master/Admin/management。

如果以后增加项目成员，应当把“系统管理员”与“项目内角色”分开，并在 Service 查询资源归属后做对象级授权，不能只靠前端隐藏菜单。

## Provider 与自动化边界

`PipelineProvider` 统一 trigger/get/cancel 语义。Local Provider 是无网络测试替身；默认 `local_lab` 在构造 HTTP 客户端前拒绝所有网络 Provider。阶段六 A 增加一个更窄的 `ci_lab_local`：它只能构造 `learning_ci`，宿主机固定访问 `127.0.0.1:23020`，容器固定访问 `172.30.60.2:8080/32`，页面、数据库和通用 Host/CIDR/Port 变量都不能改写目标。Learning CI 拥有独立数据库，并以 Bearer + `Idempotency-Key` 提供真实异步 HTTP 边界。

Jenkins/GitLab/BK-CI 仍只允许显式 `self_hosted_lab`，并要求自有环境确认、连接记录、Secret 引用与出站 allowlist。宿主机仅允许环回目标，私网仅允许隔离的 `local-container` 内部容器拓扑；测试使用 MockTransport，不把 `APP_ENV=test` 当作网络许可。三个模式彼此不能顺带启用其他 Provider，也没有 external/public 逃生开关。

自动化任务与设备都采用有期限租约。Worker/Agent 获得明文租约 Token，数据库只存摘要；过期后可回收。Cron 将计算决策和执行任务分离，fire key 用于防止同一计划时刻重复入队。

运行时 Secret 默认来自本机进程环境。可选 Vault 模式的 core 位于 `secrets-core`，应用只能经过 `vault` gateway GET `runtime` 与 `providers` 两个 KV-v2 data path；同样的精确边界还由 Vault ACL policy 重复约束。backend 只读取 Compose secret token 文件，root token 与 unseal key 没有应用挂载路径。本轮真正接线的是 Provider 在执行操作时异步读取 `providers` 中已精确 allowlist 的 `QA_PROVIDER_SECRET_*`；sealed、403、超时或 schema 错误令该操作失败关闭。`runtime` 文档只是启动引导接口的教学预留，数据库、Broker、S3 仍在同步构造阶段从现有 Settings/`.env` 读取，尚未由 Vault 启动注入。

这些模型可持久化，独立 Worker 进程骨架和内部 RabbitMQ profile 已加入；Scheduler、迁移 Job、outbox 及真实多进程故障演练仍未完成。

## 当前一致性边界

| 场景 | 当前语义 | 生产演进 |
| --- | --- | --- |
| 单 Repository 写入 | 单个 SQLAlchemy 事务 | 保持 |
| 业务变更 + 审计 | 多次 Repository 提交，进程锁串行 | Unit of Work + 同事务审计/outbox |
| 批量导入 | 默认 clean gate 零写入；允许部分时逐行提交，非原子 | 原子批事务或可恢复批任务二选一 |
| 质量报表 | 多 Repository 读取后内存聚合 | 同一读事务、SQL 聚合、事实表 |
| 本地流水线 | 完整快照在同一数据库事务提交，单进程写协调 | 按 Run 增量更新、版本/CAS |
| QA → Learning CI 触发 | CI Lab 以全局幂等键去重外部副作用；QA 端当前在关系库事务内完成 HTTP 后再保存映射 | transactional outbox/trigger intent、短事务、可恢复对账与补偿 |
| 任务与设备租约 | PostgreSQL 为 claim 权威来源；任务/设备行锁、统一数据库时钟与 active-device 唯一索引；RabbitMQ 仅唤醒 | 容器并发验证、事务 outbox、Handler 业务幂等与容量治理 |
| 附件元数据 + 对象内容 | 数据库与文件/S3 是两个资源，不能伪装成一个原子事务；失败必须可识别、可补偿 | pending 状态、outbox、孤儿对象回收和校验任务 |
| Scheduler | 持久 next-run/fire 语义 | 领导者选举或数据库抢占 |

`asyncio.Lock` 只能协调一个 Python 进程，不能保护多 Worker 或多实例。切换到 PostgreSQL 不会自动改变这条边界，Docker 示例因此仍固定一个 Uvicorn Worker。

## 运行拓扑

本机源码模式由 Vue 开发服务器代理 `/api/v1` 到 FastAPI，默认使用本机 SQLite、`local_filesystem`、本机账号、环境 Secret 与无网络 Local Provider。Compose 模式由 Nginx 提供静态页面并反代后端，业务端口仅发布到 `127.0.0.1`；Prometheus、Alertmanager、PostgreSQL、Worker、SeaweedFS、Keycloak、Vault 与 CI Lab 使用可选 profile。`worker` profile 强制 `postgres_local_container + rabbitmq_local_container`，RabbitMQ 只发送固定无业务数据的 wake-up hint，Worker 仍从 PostgreSQL claim。`object-storage` profile 提供单节点 SeaweedFS，但只有后端同时显式选择 `s3_local_container` 才会使用它。`identity-secrets` 提供路径受限的 Keycloak/Vault 双层网络；只有后端同时选择对应严格运行模式才会发起内部 HTTP。`ci-lab` profile 在独立 internal 网络以固定 `172.30.60.2` 运行，仅把观察端口 `23020` 绑定到宿主机环回；QA frontend 不代理 Lab。数据库、AMQP、S3、OIDC core、Vault core 和其他内部组件端口都不发布到宿主机。

当前机器没有 Docker，因此 PostgreSQL/RabbitMQ/SeaweedFS/Keycloak/Vault/CI Lab/多 Worker 拓扑只完成配置边界、共享持久化代码、单元测试、ORM/迁移方言和探针分支的静态/自动化验证，尚未真实运行容器。CI Lab 的 Python 契约和 Provider→ASGI 全链路已验证，但固定 IP 上的真实 socket、容器重启与故障注入仍待补做。SQLite→PostgreSQL 数据搬迁、对象数据搬迁、realm import、PKCE+TOTP、Vault init/unseal、命名卷权限、多实例并发、迁移互斥和故障切换也尚未完成。

生产目标至少应拆分为入口网关、Web API、迁移 Job、Worker、Scheduler、关系数据库、消息代理、对象存储、Secret Manager 和监控系统，并补齐 TLS、备份、资源限制、灰度和回滚。
