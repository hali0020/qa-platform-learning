# 下次开发必读交接

> 这份文档只用于继续写代码时的交接。按现有教程学习、启动和操作平台时，
> 不要求先阅读本页。

## 开发恢复规则

下次开始开发前，先阅读本页，再查看 `git status`、最近提交和
[阶段六 A 说明](PHASE6_CI_LAB.md)。不要重新连接或探测任何公司 Jenkins、
GitLab、蓝盾、数据库、IdP、Vault、S3 或其他线上系统。

不可改变的边界：

- 默认保持 `local_lab + sqlite_local + disabled_local broker + local_filesystem + local_accounts + env_local`。
- `ci_lab_local` 只允许仓库自有 `learning_ci`，固定宿主机
  `127.0.0.1:23020` 或容器 `172.30.60.2:8080/32`。
- Jenkins/GitLab/BK-CI 真实联调继续关闭；以后也只连接我们自己安装的测试实例。
- 只提交本仓库自行编写的内容；`.env`、Token、数据库、日志、构建产物和依赖目录不得提交。

## 已编码并通过本机自动化验证

- 阶段六 A 独立 Learning CI Lab：独立 FastAPI 进程、独立 SQLite、固定不可变
  Definition、Bearer 机器身份、16 KiB 请求上限、幂等 trigger/get/cancel、确定性
  状态推进和重启恢复。
- `learning_ci` Provider 与 `ci_lab_local` 固定目标模式；通用 Host/CIDR/Port、页面
  和数据库都不能覆盖目标。
- HTTP 客户端禁环境代理、重定向和压缩响应，按原始字节限制响应大小。
- Compose `ci-lab` profile、独立 internal 网络、固定 IP、非 root/read-only 镜像、
  本机环回发布和临时机器 Secret 启动脚本。
- 无 Docker 源码脚本使用专用 `.data/ci-lab-source` 数据库，强制关闭其他网络实验
  模式；Token 位于固定本地磁盘的当前用户 `LocalApplicationData` 随机目录，关闭
  ACL 继承并在退出时精确清理。
- 旧流水线快照兼容：新触发只接受字符串变量，但历史 Run 仍可读取数字、布尔和
  对象变量。
- 阶段六 B 质量门禁：`local-quality-gate` 持久等待审批；审批事件幂等，触发人
  不能审批自己，`pipeline.approve` 只授予 `system_admin`/`qa_lead`，Webhook 也不能
  绕过等待状态。
- Provider Run Artifact：复用 Storage Port，显式
  `pending → ready/failed → deleted`，保存大小与 SHA-256，上传/删除失败有补偿，
  JSON/JUnit XML 输入有界校验并记录审计。
- 独立签名 Webhook 收发：独立于浏览器 Session/CSRF，使用专用 Secret、
  16 KiB 原始请求上限、固定五分钟时间窗、常量时间 HMAC、事件唯一键、重放/内容冲突、
  乱序/序列缺口/终态回退处理。签名 body 绑定 Connection/correlation，不匹配的
  本地 Run 不消费收据。
- CI Lab 持久主动 Webhook Outbox/Worker：可见状态与不可变 body 同事务，
  每 Run sequence 顺序、租约 token 摘要/version 结算、有界退避/死信、安全列表/手工
  retry。Worker 主动物化非终态 Run，只能投递到固定环回或 Compose
  `172.30.60.3:23100`；无任意 URL 配置。轮询用 `webhook_sequence` watermark
  对账。
- QA→CI 触发改为持久 trigger intent；Web 事务只写 Run/Intent，独立 Provider
  Dispatcher 以租约 claim，在事务外执行 HTTP，再以幂等键和 CAS 结算未知结果。
- 阶段六 C 一次性 Alembic migration Job；Compose 中 Web/Worker/Scheduler/
  Dispatcher 使用 verify-only schema 模式并等待 Job 成功。
- 独立 Scheduler 进程与 PostgreSQL `FOR UPDATE SKIP LOCKED` claim、数据库时钟、
  事务外 Cron 计算、版本/租约 token CAS 结算；SQLite 只保留单进程教学入口。
- 任务入队与无业务内容 RabbitMQ wake-up outbox 同事务；独立 Outbox Dispatcher
  claim 后在事务外发布固定提示并 CAS 结算。Web 不持有 Broker 连接或凭据，Worker
  仍以 PostgreSQL claim 为唯一执行授权。

## 尚未完成，按此顺序继续

### 1. 真实容器与故障验收

- 当前机器没有 Docker。在个人隔离 Docker 环境从空卷验证 migration →
  Web/Worker/Scheduler/Dispatcher 的启动顺序、真实 PostgreSQL/RabbitMQ、CI Lab
  固定 IP 双向 HTTP、Webhook Worker、health、停止/重启、Secret 轮换和命名卷恢复。
- 对多 Worker/多 Scheduler/多 Dispatcher 做并发 claim；注入 Worker/Dispatcher
  崩溃、租约过期、重复 wake-up、RabbitMQ 中断、数据库中断和恢复，核对没有重复
  授权、任务仍能由数据库轮询兜底、未知 Provider 触发可对账。
- 不要把“YAML/单测通过”写成“容器已运行通过”。
- 复核 `scripts/start-ci-lab-source.ps1` 的 Windows ACL、端口占用拒绝和 Ctrl+C 清理。

### 2. 备份、恢复与高可用设计

- 分别设计并实测 PostgreSQL、对象存储、CI Lab SQLite 和 Secret 材料的备份、
  成对恢复、RPO/RTO 与回滚；补 SQLite→PostgreSQL 数据搬迁方案。
- 在真实并发与恢复验收前，Web 保持单实例，CI Lab 保持单 API + 单
  Webhook Worker；不宣称 HA。
- 若未来要多 Web，再审计剩余进程锁、跨 Repository 事务、会话、文件存储、缓存和
  入口摘流，设计数据库/对象存储/消息代理/Secret Manager 的高可用拓扑。

### 3. 自建产品未来联调

- Jenkins/GitLab/BK-CI 和所有公司系统继续关闭，也不要探测。
- 只在我们自己安装、拥有和隔离的测试实例上，一次联调一个 Provider；按所安装
  版本的官方契约补 Webhook 发送、Artifact 拉取、权限、Secret 轮换与故障测试。
- 若继续完善 Learning CI，下一步是在具备 Docker 的个人隔离机器实测固定
  `172.30.60.4 → 172.30.60.3:23100` 投递、API/Worker 独立崩溃、租约过期、
  重试/死信/手工恢复、旧 sequence 阻塞与 watermark 对账；不要把单测写成
  容器故障验收。

## 下次开发的最小检查

```powershell
git status --short
.\backend\.venv\Scripts\python.exe -m pytest backend\tests -q
.\backend\.venv\Scripts\python.exe -m compileall -q backend\app backend\tests
.\backend\.venv\Scripts\python.exe -m pip check
cd frontend
pnpm run type-check
pnpm run build
```

随后检查 `git diff --check`、Compose YAML、PowerShell AST 和敏感信息，再形成一个
边界清晰的小提交。
