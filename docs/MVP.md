# QA 平台 MVP 与阶段二边界

## 可演示的业务闭环

```text
本机登录并通过 RBAC
→ 创建项目、套件和用例
→ 生成不可变用例快照
→ 创建计划并完成测试执行
→ 为失败结果提交、流转缺陷
→ 在缺陷下评论和上传安全附件
→ 导入/导出测试资产
→ 查看趋势、覆盖率和缺陷指标
→ 触发本地流水线或通过统一 Provider 操作已配置 CI
→ 任务入队、分配设备并由 Cron 产生任务
→ 用健康探针、日志和指标观察系统
```

## 当前包含

- 异步 FastAPI、统一响应与 Vue 3 管理后台。
- 本机用户、系统级 RBAC、服务端 Session、CSRF 与会话撤销。
- 项目、树形套件、用例/步骤、不可变快照、计划、执行与结果。
- 缺陷状态机、关联关系和追加式业务审计。
- 评论、回复、作者/moderator 权限、安全附件与图片重编码。
- CSV/XLSX 模板、Preview、SHA-256 确认、部分 create-only 导入和导出。
- 有明确分子/分母的质量汇总、日/周趋势和套件覆盖率。
- 本地流水线模拟器与 Local/Jenkins/GitLab/BK-CI Provider 适配层。
- 独立 Learning CI Lab、Provider Trigger Intent/Dispatcher、质量门禁审批、签名
  Webhook 接收和 Provider Run Artifact。
- Provider 连接/运行、任务、设备租约、Cron/Fire 的共享关系数据库持久模型。
- 一次性 migration Job、独立 Worker/Scheduler/Outbox Dispatcher、PG claim/CAS 和
  transactional task wake-up outbox 的代码边界。
- 异步 SQLAlchemy 2、Alembic、默认本机 SQLite，以及仅限内部 Compose 网络的可选 PostgreSQL 适配。
- 流水线快照复用所选数据库并在同一事务保存运行/触发键/回调事件；readiness 支持 SQLite 与受约束的 PostgreSQL。
- GitHub Actions CI、本机 Docker/Nginx、Prometheus/Alertmanager 参考环境。

## 明确不包含

- 真实公司数据库、线上 QA 数据或存量账号迁移。
- 默认开启的真实 Jenkins、GitLab 或 BK-CI 连接。
- 复制完整蓝盾、Jenkins 或 GitLab 产品。
- 企业 SSO/OIDC、MFA、项目成员制和跨租户隔离。
- 原子批量导入、更新/删除导入、异步大文件批处理。
- 病毒查杀、真实对象存储、外部缺陷系统双向同步。
- 需求覆盖/代码覆盖数据源、历史质量事实仓库和面向真实发布的生产质量门禁。
- 生产 Agent 服务、已经实测的多实例竞争与领导者/故障切换。
- SQLite→PostgreSQL 数据搬迁、双写、多实例并发验证和数据库高可用。
- 合规级不可篡改审计、高可用、灾备和生产发布体系。

## 数据策略

学习数据默认保存在本机 `backend/.data` 的 SQLite 与附件目录，测试使用临时目录。可选 PostgreSQL 只允许 `postgres_local_container + APP_ENV=local-container + postgresql+asyncpg@postgres:5432`，关系数据位于不发布端口的自建 Compose 命名卷，附件仍位于后端本机数据卷。外部 Provider 默认关闭；测试使用 Local Provider 或 Mock HTTP。真实凭据既不进入仓库，也不进入数据库业务字段。

当前机器没有 Docker，PostgreSQL/RabbitMQ 尚未做真实容器启动、migration Job、
CRUD、多进程竞争、流水线恢复和 readiness 联调；当前完成的是共享持久化、独立
进程入口、配置边界、状态机测试与 PostgreSQL 方言离线验证。Web 当前仍保持单实例，
不宣称 HA。

## 怎样判断“阶段二完成”

这里的完成标准是：七大能力都有可读模型、稳定边界、HTTP 或本机演示入口、自动化测试和生产差距说明。它不是“所有企业功能都完成”。

下一阶段重点不是继续堆页面，而是 Unit of Work、项目级授权、真实 PostgreSQL/
RabbitMQ 容器与故障注入、数据搬迁/备份恢复、自建测试环境 Provider 联调，以及
多实例与高可用验收。
