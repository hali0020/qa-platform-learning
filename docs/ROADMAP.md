# 学习路线

## 学习原则

每一课遵循：概念 → 最小代码 → 本机运行 → 自动化测试 → 故障练习 → 复盘 → 小提交。先用本机替身证明业务边界，再在明确授权、最小权限和安全出站条件下连接自有测试系统。

## 已完成：异步 HTTP 与 QA 核心闭环

- ASGI、FastAPI、Pydantic、统一错误与异步接口测试。
- Repository、依赖注入、SQLAlchemy 2 异步会话、SQLite 与 Alembic。
- 项目、套件、用例、不可变快照、计划、执行与结果。
- 缺陷状态机、关联关系和追加式业务审计。
- 本地异步流水线、幂等触发、回调去重与重启恢复。
- Vue 3 管理后台和 GitHub Actions CI。

## 阶段二：平台通用能力

| 顺序 | 能力 | 当前学习落点 | 下一步深化 |
| --- | --- | --- | --- |
| 1 | 身份与权限 | 本机账号/RBAC，以及默认关闭的自建 OIDC/PKCE/TOTP | 项目成员、策略授权、SSO 退出与恢复流程 |
| 2 | 数据交换 | CSV/XLSX 模板、预检、摘要确认、部分导入、导出 | 幂等业务键、异步批任务、原子 Unit of Work |
| 3 | 协作 | 评论、回复、安全附件、图片重编码、作者权限和本机对象存储适配 | 病毒扫描、通知与外部缺陷同步 |
| 4 | 质量 | 指标口径、日/周趋势、套件覆盖 | 版本基线、历史事实表、需求/代码覆盖与质量门禁 |
| 5 | CI Provider | Local、独立 Learning CI Lab，以及 Jenkins/GitLab/BK-CI 的默认关闭适配 | 审批、质量门禁、Artifact、签名 Webhook 与自建产品沙箱联调 |
| 6 | 自动化资源 | 持久任务、租约/心跳、设备、Cron/misfire/overlap | 独立 Worker/Scheduler、消息代理、CAS/outbox、Agent 身份 |
| 7 | 交付运维 | Docker/Nginx/监控，可选 PostgreSQL、Vault 与内部网关边界 | 高可用、TLS、启动 Secret 注入、备份恢复与回滚 |

详细练习见 [PHASE2_LEARNING_GUIDE.md](PHASE2_LEARNING_GUIDE.md)。

## 阶段三：从单机教学到团队环境

1. 引入 Unit of Work，让业务写入、审计与 outbox 共享事务。
2. 已加入受限的内部 PostgreSQL 运行模式和方言迁移测试；容器实跑与迁移演练待执行。
3. 已拆出独立 Worker 进程骨架，并为任务/设备租约加入 PostgreSQL 行锁、统一数据库时钟和数据库唯一防线；Scheduler 与迁移 Job 仍待拆分。
4. 已加入 RabbitMQ 固定唤醒提示、数据库轮询兜底与固定 Handler Registry；outbox 和真实并发故障演练待完成。
5. 已加入默认关闭的本机 Keycloak OIDC/PKCE/TOTP、管理员显式 subject 绑定边界与双层网络；项目成员模型和资源级授权仍待深化。
6. 在自有测试实例上完成一个 Provider 的最小权限真实联调。
7. 对象存储已进入阶段四的本机隔离学习；备份恢复、容量测试和可回滚发布仍待完成。

Worker/Broker 的详细语义见 [PHASE3_WORKER_AND_BROKER.md](PHASE3_WORKER_AND_BROKER.md)。

## 阶段四：对象存储与 Artifact

1. 保留 `local_filesystem` 为默认值，先用同一 Storage Port 证明业务层不依赖具体 S3 产品。
2. 增加只允许 `APP_ENV=local-container`、精确 `seaweedfs:8333` 和固定 `qa-artifacts` Bucket 的 S3 运行模式。
3. Compose 通过 `object-storage` profile 启动单节点 SeaweedFS；镜像固定精确版本和 manifest index digest，不使用 `latest`。
4. 已练习有界暂存、5 MiB 顺序 multipart、取消 abort、异步流式下载、摘要校验、超时、连接池回收与重复删除语义；并行 part、断点续传与上传状态表留作容量深化。
5. 明确数据库元数据和对象内容不能原子提交，后续通过 pending/outbox、补偿和孤儿回收收敛。
6. 阶段五已加入本机 Vault/Secret Manager 边界；病毒扫描、对象生命周期、备份恢复和真实容器故障演练仍待完成。

详细边界与练习见 [PHASE4_OBJECT_STORAGE.md](PHASE4_OBJECT_STORAGE.md)。

## 阶段五：本机身份与 Secret Manager

1. 保留 `local_accounts + env_local` 为默认值，任何 OIDC/Vault 变量残留都 fail closed。
2. 增加自建 Keycloak 的 Authorization Code + S256 PKCE、state/nonce、固定 audience、短 JWKS cache 和管理员显式稳定 subject 绑定；不做 username/email 自动匹配、JIT 用户或角色信任。
3. 用可审计 realm JSON 强制新用户配置 TOTP，并关闭自注册、外部 IdP、LDAP 与 SMTP。
4. 用前端 `/identity` 和内部 `keycloak:8080` 分离 browser/backchannel transport；Keycloak core 的 master/Admin/management 留在第二层网络。
5. 使用 persistent sealed Vault file-storage 教学模式，手工 init/unseal，应用 token 只经 Compose secret file 注入。
6. Vault policy 与 HTTP gateway 都只放行 `runtime/providers` 两个精确 KV-v2 GET，不把 root/unseal 交给应用；本轮业务消费仅接通 Provider Secret。
7. 后续把 DB/Broker/S3 的同步启动构造改成经审计的 Vault 启动引导，并在有 Docker 的机器完成 realm import、PKCE+TOTP、seal/expiry/rotation、备份恢复和故障演练，再演进 PostgreSQL Keycloak、Vault integrated storage、TLS/auto-unseal/HA。

详细边界见 [PHASE5_IDENTITY_AND_SECRETS.md](PHASE5_IDENTITY_AND_SECRETS.md)。

## 阶段六：独立 CI 控制面与高可用演进

### 六 A：已完成的 Learning CI Lab

1. 新增仓库自有的独立 FastAPI 服务和独立 SQLite 数据库，不与 QA API 共享进程或运行事实。
2. 新增 `learning_ci` Provider 与显式 `ci_lab_local` 模式；宿主机/容器目标、端口、精确 `/32` 和 Secret 名均由代码固定。
3. 实现 Bearer 机器身份、16 KiB 请求上限、固定 Definition、全局幂等键、确定性状态推进、轮询、取消和重启恢复。
4. Provider 客户端禁代理、重定向和压缩响应，每次调用校验解析地址并限制响应体；默认 `local_lab` 仍在 Secret/DNS/socket 前失败关闭。
5. Compose `ci-lab` profile 使用独立 internal 网络、固定 IP、只读文件系统、非 root 用户和独立数据卷；当前机器没有 Docker，所以只完成静态与 Python 自动化验证。

### 六 B：下一步

1. 为 CI Run 增加审批记录与不可绕过的质量门禁状态机。
2. 将测试报告/制品元数据接入现有 Storage Port，并明确上传补偿、摘要与审计语义。
3. 增加独立签名 Webhook：时间窗、常量时间验签、事件唯一键、重放保护和轮询对账。
4. 保持 Jenkins/GitLab/BK-CI 真实环境关闭；需要产品练习时只安装我们自己的测试实例，并逐个核对版本契约。

### 六 C：随后完成

1. 拆出独立 Alembic migration Job 和 Scheduler 进程。
2. 用 PostgreSQL claim/CAS、outbox 与并发测试消除单 Web 进程假设。
3. 验证多 Worker 崩溃恢复、重复消息、租约过期、Broker 中断与数据库恢复；在这些验证前不宣称高可用。

详细边界与练习见 [PHASE6_CI_LAB.md](PHASE6_CI_LAB.md)。

## 暂不追求

- 复制完整 Jenkins、GitLab 或蓝盾产品。
- 连接公司生产数据库、生产 CI、真实设备池或存量账号。
- 把已停止维护的 MinIO CE 镜像作为默认对象存储，或连接任何公司 S3 Bucket。
- 在 SQLite 和单进程锁之上宣称多实例生产可用。
- 为了页面数量堆功能，而不定义状态机、指标口径和故障语义。
