# CI/CD 与 QA 平台集成

## 先区分两件事

仓库自己的 CI 与 QA 平台连接外部 CI 产品是两条不同链路：

- 仓库 CI：验证本项目的后端、前端和构建产物。
- 平台 Provider：QA 平台根据业务操作触发或查询仓库自有的 Learning CI Lab；Jenkins、GitLab CI、BK-CI 目前只保留默认关闭的协议适配，未来也只能连接我们自己安装的实验室。

两者都叫“流水线”，但权限、失败语义和安全边界不同。

## 当前仓库 CI

`.github/workflows/ci.yml` 在推送到 `main` 或 Pull Request 时执行后端与前端质量门禁：

```text
Backend                         Frontend
Python 3.10 + 固定依赖          Node 22 + pnpm
        ↓                              ↓
pytest / compile                TypeScript 类型检查
                                       ↓
                                Vite 生产构建
                                       ↓
                                上传 frontend-dist 制品
```

这属于 CI，不会部署服务器，也不会连接 Jenkins、GitLab 或蓝盾。

## 本地流水线与统一 Provider

本地流水线模拟器用于学习 Pipeline/Stage/Job、幂等触发、取消、回调去重和重启恢复。Provider 适配层进一步统一自建实验室产品的 trigger/get/cancel：

- Local：完全不访问网络，适合开发和测试。
- Learning CI：仓库自有的独立 FastAPI/SQLite 服务，以真实本机 HTTP 练习机器鉴权、幂等、轮询、取消和重启恢复。
- Jenkins：Remote Access API、Job 与 queue/build 状态映射；当前只做 Mock 契约测试。
- GitLab：Pipelines API、项目和 ref/variables 语义；当前只做 Mock 契约测试。
- BK-CI：针对已配置项目/流水线的触发、查询与停止适配；当前只做 Mock 契约测试。

Provider 连接只保存供应商类型、已绑定的定义标识、非敏感配置和 Secret 环境变量名称；不会把 Token 值保存到所选关系数据库。默认 `local_lab` 会硬拒绝所有网络 Provider。单独的 `ci_lab_local` 只能启用 `learning_ci`，其地址不能入库或由页面配置：源码固定 `127.0.0.1:23020`，容器固定 `172.30.60.2:8080/32`，Secret 名固定 `QA_PROVIDER_SECRET_CI_LAB`。`self_hosted_lab` 才用于我们自己安装的 Jenkins/GitLab/BK-CI，并继续要求所有权确认、精确 host/port/CIDR/Secret allowlist、HTTPS、DNS 结果校验、禁重定向、超时和响应大小限制。三个模式互斥，没有 external/public 模式。

Learning CI 的运行事实位于独立 `ci-lab.db`，QA 数据库保存连接、归一化 Run、
Trigger Intent、审批、Artifact 元数据和 Webhook 收据。Web 触发只提交 Run/Intent；
独立 Dispatcher claim 后在数据库事务外调用 CI，并把稳定 correlation ID 转为
`Idempotency-Key`。相同键/相同输入重放得到同一个 Run，相同键/不同输入冲突。
状态依据固定 Definition 的时间线物化；`local-quality-gate` 会持久等待 QA 的非触发
人审批，不能由前端或成功 Webhook 绕过。它不拉代码、不执行 Shell，也不是构建
Executor。

Run Artifact 复用 Storage Port，显式记录 `pending/ready/failed/deleted`、大小、
SHA-256、补偿和审计。独立机器 Webhook 接收端使用专用 HMAC Secret、五分钟时间窗、
事件唯一键与 sequence reducer，处理重放、乱序、缺口和终态回退；CI Lab 目前没有
主动 delivery Worker，所以当前只验证接收与轮询对账。

流水线模拟器的 checkpoint 与 QA 数据复用异步 SQLAlchemy 和 Alembic。默认落到 SQLite；可选 PostgreSQL 只允许 `postgres_local_container + APP_ENV=local-container + postgresql+asyncpg@postgres:5432` 的内部网络。一次 checkpoint 的运行、触发幂等键和回调事件在同一数据库事务提交，readiness 也支持这两种受约束的 backend。

## “学习蓝盾”不等于“复制蓝盾”

完整蓝盾包含流水线编排 UI、插件市场、Agent、制品、权限、审批、触发器、环境和运维体系，复制它相当于重新开发一个大型 DevOps 产品。本项目只实现 QA 平台需要的 Provider 边界：把“触发/查询/取消已存在流水线”映射到统一模型。

这仍然能学到可迁移的核心知识：

- Pipeline、Stage、Job、Step 与 Artifact 的区别；
- 触发幂等、异步状态同步、Webhook/轮询和终态；
- Secret、最小权限、回调签名和审计；
- 供应商字段到平台状态的适配；
- QA 结果怎样成为质量门禁或发布依据。

BK-CI 不同版本的网关前缀、认证方式、项目/流水线标识可能不同，自建实验室接入必须以所部署版本的文档为准，不能把本项目示例当作通用生产配置。

## 建议学习顺序

1. 在 Local Provider 中理解统一状态机，确认它完全不访问网络。
2. 显式启动 Learning CI，用两个本机进程练习 Bearer、异步 trigger/get/cancel、幂等冲突、超时、停机和恢复。
3. 在 Learning CI 上练习已经实现的签名 Webhook 接收/轮询对账、审批、质量门禁、
   Trigger Intent 和 Artifact，不先绑定供应商语法。
4. 观察 migration Job、Provider Dispatcher、Scheduler、task wake-up outbox/
   Dispatcher 的短事务边界；RabbitMQ 只作无内容唤醒，Web 不持有 Broker。
5. 用 Mock HTTP 为 Jenkins/GitLab/BK-CI 维护契约测试，不发真实产品请求。
6. 学习 Jenkinsfile、`.gitlab-ci.yml` 和 BK-CI 流水线变量，但不把供应商语法塞入 QA 领域服务。
7. 自己安装一个产品的测试实例和测试账号后，一次只联调一个 Provider，并以实际版本官方契约为准。
8. 最后练习发布审批、环境差异、回滚、审计和生产交付门槛。

## CD 与生产差距

当前 Compose 只提供本机容器演示，不执行自动部署。一次性 migration Job、
verify-only Web/Worker/Scheduler/Dispatcher、PostgreSQL、RabbitMQ 与 `ci-lab` profile
已接线；Web 不持有 Broker。当前机器没有 Docker，因此尚未做真实容器迁移、固定
IP socket、多进程竞争、流水线恢复和 readiness 验证；现有检查属于安全边界、
方言、ASGI 契约和静态/自动化验证。

正式 CD 仍需要环境分层、Secret Manager 启动注入、镜像签名与扫描、部署审批、
滚动/蓝绿/灰度、健康门禁、数据库兼容迁移和一键回滚。当前也没有
SQLite→PostgreSQL 数据搬迁、真实多实例/故障注入、备份恢复或 HA 验收；Web 保持
单实例。真实 Jenkins/GitLab/BK-CI 和公司系统仍关闭。

不要让“Pipeline 成功”成为唯一发布依据；还应结合测试质量、变更风险、监控信号和人工审批。
