# QA Platform Learning

一个围绕质量工程流程构建的全栈实践项目。项目以 Python 异步服务为核心，将测试资产、执行计划、缺陷协作、质量度量、流水线控制、任务调度和设备管理组织在同一套可运行的系统中。

项目重点不是复刻某个现有平台，而是把常见 QA 能力拆成边界清晰、可以测试和持续演进的模块，并记录每个阶段的实现范围与限制。

## 已实现能力

| 领域 | 主要内容 |
| --- | --- |
| 测试资产 | 项目、测试套件、测试用例、不可变快照、测试计划与执行结果 |
| 缺陷协作 | 缺陷状态流转、评论、附件、审计记录与归档约束 |
| 数据处理 | CSV/XLSX 预检与提交、逐行结果、模板导出与输入安全校验 |
| 质量度量 | 通过率、自动化覆盖率、执行触达率、缺陷关联率及趋势统计 |
| 异步 HTTP | 基于 `httpx` 的异步请求、超时控制、状态轮询、错误映射与响应边界 |
| CI 控制 | 本地 CI Lab、触发 Intent、质量门禁审批、持久 Webhook Outbox/签名主动投递、Run Artifact，以及 Jenkins/GitLab/BK-CI 的默认关闭适配与 Mock 契约测试 |
| 自动化调度 | 任务队列、独立 Worker/Scheduler、租约与心跳、重试与死信、设备分配、Cron claim/CAS 和任务唤醒 outbox |
| 数据持久化 | SQLAlchemy、Alembic、SQLite，以及可选的 PostgreSQL 运行适配 |
| 身份与安全 | Session、RBAC、CSRF 防护，以及隔离的 OIDC 与 Secret Store 学习边界 |
| 可观测性 | 健康检查、结构化日志、基础指标和运行状态展示 |

## 主要流程

```text
项目
  └─ 测试套件
      └─ 测试用例与快照
          └─ 测试计划与执行
              ├─ 缺陷与协作记录
              ├─ 质量统计与趋势
              └─ 流水线、任务与测试设备
```

## 架构概览

```text
Vue 3 管理端
      │
FastAPI 异步应用
      │
应用服务与领域规则
      ├─ Repository / SQLAlchemy / Alembic
      ├─ Pipeline Provider / Learning CI Lab / Provider Dispatcher
      ├─ Task / Worker / Device / Scheduler / Outbox Dispatcher
      └─ Storage / Identity / Observability
```

后端按路由、应用服务、领域模型、仓储和外部适配层拆分。流水线、存储、消息代理和身份服务通过窄接口接入，业务流程不直接依赖具体产品协议。
Compose 中 schema 只由一次性 migration Job 修改；Web、Worker、Scheduler 和 QA 侧两个
Dispatcher 只验证 schema。Web 只写数据库事务 outbox，不持有 RabbitMQ 连接或凭据。
CI Lab 另有一个独立 Webhook Worker，从 Lab 自有 SQLite 领取持久投递记录；
这是单机教学拓扑，不是多实例高可用架构。

## 技术栈

- 后端：Python 3.10、FastAPI、Pydantic、HTTPX、SQLAlchemy、Alembic
- 测试：pytest、pytest-asyncio、ASGITransport、MockTransport
- 前端：Vue 3、TypeScript、Vite
- 数据：SQLite；可选 PostgreSQL
- 自动化：GitHub Actions、任务 Worker、Cron、RabbitMQ 适配
- 扩展边界：S3 兼容对象存储、OIDC、Secret Store、Prometheus 指标

## 项目结构

```text
qa-platform-learning/
├── backend/      # FastAPI 应用、领域逻辑、数据访问、Worker 与自动化测试
├── frontend/     # Vue 3 管理端
├── docs/         # 架构、数据、CI、调度和安全边界说明
├── infra/        # 隔离运行环境的参考配置
├── scripts/      # 开发与验证脚本
└── .github/      # 持续集成质量门禁
```

## 本地准备

先创建唯一由你手工维护的本机私有配置文件；它已被 Git 忽略：

```powershell
Copy-Item .env.example .env
```

以后人工维护、需要复用的本机连接配置和普通实验凭据只放在根目录 `.env`，不要
创建前端专用环境文件，也不要把私有值写进源码、测试、文档或命令行。平台账号与
QA 数据仍保存在被忽略的 `.data` 数据库；Vault 初始化材料和短生命周期 CI Token
使用各自受保护的本地文件或内存，不要为了“集中”而复制进 `.env`。

后端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m alembic upgrade head
```

前端：

```powershell
cd frontend
pnpm install
pnpm dev
```

## 质量验证

```powershell
python -m pytest
cd frontend
pnpm type-check
pnpm build
```

持续集成会分别执行后端测试、Python 源码编译、前端类型检查和生产构建。

## 最近完成的扩展

- 将任务执行从进程内示例扩展为独立 Worker 骨架，并加入任务心跳、租约续期、取消和失败恢复。
- 增加 PostgreSQL 与 RabbitMQ 的隔离运行适配，明确数据库是任务状态的权威来源。
- 增加 S3 兼容对象存储边界，统一附件的本地文件与对象存储接口。
- 增加 OIDC、MFA 和 Secret Store 的隔离实验，保留本地账号作为默认认证方式。
- 增加独立 Learning CI Lab，通过真实异步 HTTP 练习触发、轮询、取消、幂等和服务重启恢复。
- 增加不可绕过的 CI 质量门禁、独立审批权限与防止触发人自批；Run 可保存经摘要校验、可补偿和可审计的测试报告/Artifact。
- 增加独立 HMAC Webhook 接收与 CI Lab 主动投递：Connection/Correlation 签名绑定、持久 Outbox、单 Run 递增 sequence、租约/CAS、指数退避、死信和手工重试，并用快照 watermark 完成轮询对账。
- 将 Provider HTTP 触发拆为持久 trigger intent、租约 claim、事务外 HTTP 和 CAS 结算，并提供独立 Provider Dispatcher。
- 增加一次性 Alembic migration Job、PostgreSQL claim/CAS Scheduler，以及只发布无业务内容 RabbitMQ 唤醒提示的 transactional outbox/独立 Dispatcher。

## 公开边界

- 仓库不包含真实用户数据、业务数据、账号、Token、API Key 或私有连接信息。
- 示例配置只保留通用开关；连接信息和凭据必须在未跟踪的本地环境中设置。
- Jenkins、GitLab 和蓝盾适配器使用 Mock 契约或自建隔离服务验证，不连接任何真实组织系统。
- 数据库、上传文件、构建产物、依赖目录和运行日志均被排除在版本控制之外。
- 当前机器没有 Docker；真实 PostgreSQL/RabbitMQ、容器进程启动顺序、CI Lab 双向 HTTP、多实例竞争、
  Worker 崩溃、Broker/数据库中断和故障注入尚未实机验证。
- Web 当前保持单实例；CI Lab 是一个 API 进程加一个 Webhook Worker 共享本机 SQLite 的教学服务。项目不宣称生产级高可用、
  容量治理或合规审计能力。
