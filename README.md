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
| CI 控制 | 本地 CI Lab，以及 Jenkins、GitLab、蓝盾（BK-CI）的协议适配与 Mock 契约测试 |
| 自动化调度 | 任务队列、Worker、租约与心跳、重试与死信、设备分配和 Cron 调度 |
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
      ├─ Pipeline Provider / Learning CI Lab
      ├─ Task / Worker / Device / Scheduler
      └─ Storage / Identity / Observability
```

后端按路由、应用服务、领域模型、仓储和外部适配层拆分。流水线、存储、消息代理和身份服务通过窄接口接入，业务流程不直接依赖具体产品协议。

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

## 公开边界

- 仓库不包含真实用户数据、业务数据、账号、Token、API Key 或私有连接信息。
- 示例配置只保留通用开关；连接信息和凭据必须在未跟踪的本地环境中设置。
- Jenkins、GitLab 和蓝盾适配器使用 Mock 契约或自建隔离服务验证，不连接任何真实组织系统。
- 数据库、上传文件、构建产物、依赖目录和运行日志均被排除在版本控制之外。
- 当前实现用于单机学习与功能验证，不代表已经具备生产级高可用、容量治理或合规审计能力。
