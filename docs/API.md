# API 概览

业务接口统一使用 `/api/v1` 前缀，默认地址为 `http://127.0.0.1:23100`。健康探针和指标是根路径。字段、枚举、请求示例和当前状态码以运行中的 `/docs` OpenAPI 为准；本文只维护稳定的资源与语义概览。

普通 JSON 接口使用统一响应包：

```json
{"code": 0, "message": "ok", "data": {}}
```

下载接口直接返回文件流。批量导入使用 `multipart/form-data`。

## 认证、Session 与 CSRF

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/auth/status` | 查询认证方式、是否需要初始化及当前会话状态 |
| POST | `/auth/setup` | 仅本机、空用户库时创建首个管理员 |
| POST | `/auth/login` | 登录并设置 Session/CSRF Cookie |
| GET | `/auth/me` | 查询当前主体、角色与权限 |
| POST | `/auth/logout` | 撤销当前 Session 并清 Cookie |
| POST | `/auth/change-password` | 修改自己的密码并要求重新登录 |
| GET | `/auth/oidc/start` | 开始固定本机 Keycloak 的 state/nonce/S256 PKCE 事务 |
| GET | `/auth/oidc/callback` | 原子消费登录事务、验证 ID token 并签发平台 Session |
| GET | `/roles` | 查询内置系统角色 |
| GET/POST | `/users` | 查询或创建用户 |
| PATCH | `/users/{user_id}` | 修改显示名、角色或启用状态 |
| POST | `/users/{user_id}/reset-password` | 管理员重置密码并撤销会话 |
| POST | `/users/{user_id}/revoke-sessions` | 管理员撤销指定用户会话 |
| POST | `/users/{user_id}/oidc-binding` | 管理员显式绑定固定 issuer 下的稳定 Keycloak subject |

除状态、初始化和登录外，业务 API 默认要求 Session。POST/PUT/PATCH/DELETE 还要求 `X-CSRF-Token` 与 `qa_csrf` Cookie 匹配。浏览器客户端应携带 Cookie；不能通过关闭前端菜单替代后端 RBAC。

`authentication_method=oidc` 时，密码登录和网页初始化被关闭。OIDC 不会按用户名/邮箱自动 JIT 绑定，也不采信 Keycloak role；必须先由具有 `users.manage` 权限的本地管理员将 `(issuer, sub)` 显式绑到已有用户，权限仍来自平台本地 RBAC。

## QA 核心资源

| 资源前缀 | 主要操作 |
| --- | --- |
| `/projects` | 创建、列表、详情、修改、状态流转、安全删除 |
| `/test-suites` | 树形套件 CRUD、排序/归属、启用与归档 |
| `/test-cases` | 用例与步骤 CRUD、筛选、启用与禁用 |
| `/test-case-snapshots` | 按项目或套件创建、查询不可变快照；不提供修改/删除 |
| `/test-plans` | 计划 CRUD 与状态流转 |
| `/executions` | 发起/查询执行、状态流转、写入单用例结果 |
| `/defects` | 提交、筛选、修改和状态流转；不物理删除 |
| `/audit-events` | 按项目、实体和动作只读查询业务事件 |

GET 通常需要读权限，写方法需要对应管理权限。Service 还会检查同项目关联、实体状态和引用完整性。

## 评论与附件

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET/POST | `/comments` | 按协作目标查询或创建评论/回复 |
| PATCH/DELETE | `/comments/{comment_id}` | 作者或 moderator 编辑、软删除 |
| GET/POST | `/attachments` | 按目标查询或上传安全附件 |
| GET | `/attachments/{attachment_id}/content` | 下载；仅验证后的图片允许 inline |
| DELETE | `/attachments/{attachment_id}` | 上传者或 moderator 软删除 |

协作目标可为项目、套件、用例、快照、计划、执行或缺陷。上传采用 MIME/扩展名白名单和内容验证，接口不会暴露磁盘存储键。

## CSV/XLSX 数据交换

`{entity}` 当前为用例或缺陷，`format` 为 `csv` 或 `xlsx`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/data-transfer/templates/{entity}` | 下载导入模板 |
| POST | `/data-transfer/imports/{entity}/preview` | 解析、校验并返回摘要和逐行问题 |
| POST | `/data-transfer/imports/{entity}` | 携带预检 SHA-256 再校验并提交 |
| GET | `/data-transfer/exports/{entity}` | 按项目导出数据 |

提交默认要求干净预检，因此有任一错误时零写入。显式允许部分提交后是逐行 create-only、非原子语义；响应会分别报告 created、failed、skipped，不应把它解释为批量事务。

## 质量报表

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/quality/report` | 返回汇总、趋势和套件覆盖的完整报表 |
| GET | `/quality/summary` | 仅返回汇总指标 |
| GET | `/quality/trends` | 按日或周返回趋势 |
| GET | `/quality/coverage` | 返回套件/未归套件覆盖率 |

查询需要项目、起止日期，可选择时区和日/周粒度。比率带分子和分母；具体口径见 [PHASE2_LEARNING_GUIDE.md](PHASE2_LEARNING_GUIDE.md)。

## 流水线与外部集成

原有 `/pipelines` 是不访问网络的本地流水线模拟器：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST/GET | `/pipelines` | 幂等触发或查询本地运行 |
| GET | `/pipelines/{run_id}` | 查询阶段和 Job 状态 |
| POST | `/pipelines/{run_id}/cancel` | 取消运行 |
| POST | `/pipelines/{run_id}/callbacks` | 学习回调去重和状态同步 |

阶段二的 Provider 连接接口位于 `/integrations/connections`：

- 读取 `/runtime-status`，确认当前是 `local_lab`、`ci_lab_local` 还是 `self_hosted_lab`，以及目标范围是环回还是内部容器网络；
- 对连接执行列表、创建、详情、修改和删除；
- 测试连接，或触发一个已配置的流水线定义；
- 查询该连接的运行记录、刷新/读取运行并取消。

连接 API 不接受任意 Secret 值，数据库只保存使用 `QA_PROVIDER_SECRET_` 前缀且进入显式 allowlist 的引用名称。默认 `env_local` 从本进程环境读取；显式选择 `vault_local_container` 后只能异步 GET 自建 `vault:8200` 的固定 `qa-platform/providers` 文档字段。`local_lab` 模式下 Local 连接不访问网络，所有其他 Provider 被硬拒绝。`ci_lab_local` 只允许 `learning_ci`，其连接必须使用空 `base_url`、空 config、固定定义引用和 `QA_PROVIDER_SECRET_CI_LAB`；后端根据运行环境选择代码固定的环回或容器 IP。`self_hosted_lab` 只面向我们自建的 Jenkins/GitLab/BK-CI 实例，并要求所有权确认、连接启用以及精确 host/port/CIDR/Secret allowlist 同时满足。不存在任意 external/public 模式。

### 独立 Learning CI Lab 机器 API

CI Lab 是 `127.0.0.1:23020`（容器内固定 `172.30.60.2:8080`）上的另一进程，不属于 QA API Session/CSRF 接口，也不使用上面的统一响应包。除 liveness 外只接受机器 Bearer Token：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health/live` | 进程存活；唯一匿名接口 |
| GET | `/api/v1/definitions` | 查询源码内固定、不可修改的教学 Definition |
| POST | `/api/v1/definitions/{definition}/runs` | 携带 `Idempotency-Key` 触发运行 |
| GET | `/api/v1/runs/{run_id}` | 物化并读取运行/Stage/Job 状态 |
| POST | `/api/v1/runs/{run_id}/cancel` | 幂等取消非终态运行 |

该 API 不提供 OpenAPI UI、任意 URL、Shell、动态 Definition 或凭据回显。相同幂等键与相同输入返回同一 Run；同一个键配不同输入返回 `409`。详细契约见 [PHASE6_CI_LAB.md](PHASE6_CI_LAB.md)。

## 任务、设备与定时调度

| 前缀 | 主要语义 |
| --- | --- |
| `/automation/tasks` | 列表、入队、Claim、心跳、完成、失败、取消、重试和死信 |
| `/automation/devices` | 注册/查询/修改设备、Agent 心跳、按能力租用、开始/续租/释放 |
| `/automation/schedules` | CRUD、手动执行、计算一次 due tick、查看 fire 历史 |

Worker 和 Agent 操作使用一次性租约 Token，响应中的 Token 只应保存在调用方内存，数据库存摘要。设备申请和续租还必须提交对应任务租约 Token，且设备租约不会超过任务租约有效期。`tick` 是教学用显式驱动端点；当前没有把 Web 进程伪装成高可用生产 Scheduler。

## 健康检查与指标

| 方法 | 根路径 | 用途 |
| --- | --- | --- |
| GET | `/health/live` | 进程/事件循环存活，不访问数据库 |
| GET | `/health/ready` | 当前受限关系数据库就绪检查，失败返回 503 |
| GET | `/metrics` | Prometheus 文本指标，可由配置关闭 |

兼容接口 `/api/v1/health` 仍可用于查看本机模式。生产环境不应把 `/metrics` 暴露到公共网络。

## HTTP 错误语义

- `400/422`：文件、参数或业务输入无效。
- `401`：没有有效 Session。
- `403`：已登录但缺少权限、CSRF 不匹配或操作他人资源。
- `404`：资源不存在，或已软删除内容不可读取。
- `409`：状态、唯一约束、幂等键或版本冲突。
- `503`：依赖或显式关闭的能力当前不可用。

自动化客户端必须按状态码和稳定错误码处理，不能只匹配中文 message。
