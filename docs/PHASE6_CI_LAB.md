# 第六阶段：独立 Learning CI Lab 与 QA 编排

## 目标

这一阶段不复制 Jenkins、GitLab CI 或蓝盾，也不连接公司系统。
我们先自己实现一个最小 CI 控制面，让 QA 平台通过真实的异步 HTTP
学习这些可迁移能力：

1. Provider 中立触发模型如何转换为 CI 协议；
2. 机器身份、固定出站地址、超时和响应上限；
3. `correlation_id` / `Idempotency-Key` 如何防止重复触发；
4. `queued → running → succeeded/failed/cancelled` 与质量门禁状态机；
5. 轮询、签名 Webhook 接收、取消、重启恢复和错误脱敏；
6. trigger intent、独立 Dispatcher、Artifact 和 QA/CI 之间的一致性边界。

容器启动与故障练习见
[DEPLOYMENT_PHASE6_CI_LAB.md](../infra/DEPLOYMENT_PHASE6_CI_LAB.md)。

## 架构

```text
Vue / QA API :23010
        │
        │ LearningCiPipelineProvider
        │ Bearer + Idempotency-Key + bounded JSON
        ▼
Provider Dispatcher ── HTTP ──> Learning CI Lab :23020 / 172.30.60.2:8080
        │
        ├── fixed immutable definitions
        ├── deterministic run / quality-gate state machine
        └── independent ci-lab.db
```

QA 平台数据库只保存 Provider Connection 和归一化的 Provider Run。
CI Lab 拥有自己的运行事实，不共享 QA 数据库。这样可以观察真正的
网络失败、超时、协议校验和服务重启，而不是在同一进程中直接调用
Python 函数。

## 为什么不先复制三家 API

Jenkins 有 Queue/Build 两段语义，GitLab 有 Project/Pipeline/Job 资源，
蓝盾的网关前缀、认证和接口会随版本/部署变化。一次伪造三套“完整
兼容 API”会学到错误细节。本阶段使用自己定义的窄协议，先把控制面
不变的部分做对。

以后学习产品协议时，每次只增加一个明确标注的“契约模拟层”：

- GitLab Pipeline API 子集：资源模型直观，适合第一个练习；
- Jenkins：重点学 Queue ID 到 Build Number 的转换；
- 蓝盾 BK-CI：最后根据当时使用的开源版本核对契约。

契约模拟通过不等于真实产品已安装，更不等于已连接公司系统。

## 当前 HTTP 契约

CI Lab 仅开放固定接口：

```text
GET  /health/live
GET  /api/v1/definitions
POST /api/v1/definitions/{definition}/runs
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/cancel
POST /api/v1/runs/{run_id}/gate-decisions
```

除 liveness 外全部要求 Bearer 机器 Token。触发还必须提供安全的
`Idempotency-Key`。相同键与相同请求返回同一 Run；相同键搭配不同请求
返回冲突。门禁决策接口只对处于 `waiting_approval` 的质量门禁运行生效，并以事件
ID 幂等；QA 平台在调用它之前执行 `pipeline.approve` 与禁止触发人自批规则。

请求不能提供命令、模块、镜像、URL 或文件路径。变量只允许有界的
大写 ASCII 键和普通字符串，凭据类键名、URL、父目录路径和控制字符
会被拒绝。请求体上限为 16 KiB，Provider 响应也有硬上限并拒绝压缩
内容，防止解压放大。

## 内置定义

| Definition | 用途 | 结果 |
| --- | --- | --- |
| `local-quality-gate` | 输入校验 → 确定性测试 → 持久等待审批 | 批准后成功，拒绝后失败 |
| `local-failure-demo` | 输入校验 → 固定失败 → 下游取消 | 失败 |

定义是源码中的不可变对象，不存在“把用户文本当 Shell 执行”的
字段。Run 保存 definition revision，轮询时根据持久化 `created_at` 与固定
时间线物化状态。这个简化设计可以先学控制面，不引入任意代码执行。

质量门禁由 CI Lab 自己持久化，不能靠 QA 页面隐藏按钮实现。QA 平台另存审批
事件和归一化状态；只有具有 `pipeline.approve` 的 `system_admin`/`qa_lead` 能决策，
触发人不能审批自己，同一事件 ID 的相同决策可幂等重放，冲突内容会被拒绝。

## 两种本机运行方式

### 方式一：源码双进程

运行：

```powershell
.\scripts\start-ci-lab-source.ps1
```

脚本只在当前用户固定本地磁盘的 `LocalApplicationData` 下创建随机直接子目录，
关闭 ACL 继承并只授予当前用户，再写入临时机器 Token 文件；独立 Webhook Secret
只保留在本轮进程环境。随后它以隐藏后台进程
启动 `app.ci_lab.main:app` 并绑定 `127.0.0.1:23020`，再在前台迁移并启动
`127.0.0.1:23100` 的 QA backend。它只在本次进程设置 `ci_lab_local`、固定
Secret allowlist、机器 Token 与独立 Webhook Secret；不会修改 `.env`、不会输出
Secret。按 Ctrl+C
会停止脚本创建的 Lab 子进程并删除本轮 Token 文件。前端仍在另一个终端使用
`.\scripts\start-frontend.ps1`。

源码脚本不继承调用者的数据库或其他实验服务：QA 数据固定写入
`.data/ci-lab-source/qa.db`，CI 数据固定写入
`.data/ci-lab-source/ci-lab.db`，Broker、S3、OIDC 与 Vault 都被强制为关闭/本机
适配。`23020` 或 `23100` 已被占用时脚本会在迁移前失败，不会复用未知进程。

`base_url` 不能保存到 Connection；宿主机目标由代码固定为
`http://127.0.0.1:23020`。

### 方式二：隔离 Compose profile

使用：

```powershell
.\scripts\start-ci-lab.ps1
```

脚本只对本次进程设置 `ci_lab_local`，并将容器目标固定为
`http://172.30.60.2:8080`。平台不读取任意 Host/CIDR/Port 配置。

## 学习顺序

1. 在 `local_lab` 下触发 Learning CI，观察它在 Secret/DNS/socket 之前失败。
2. 启动 CI Lab，查看固定 Definition，但不修改协议或关闭机器鉴权。
3. 在 QA 平台创建 `learning_ci` Connection，URL 留空，出站 Secret 固定为
   `QA_PROVIDER_SECRET_CI_LAB`；需要练习接收 Webhook 时另配独立
   `QA_PROVIDER_SECRET_CI_LAB_WEBHOOK`，两者不能复用。
4. 触发会先提交 Run/Intent，不在 Web 数据库事务内请求 CI；源码模式可手工分发，
   Compose PostgreSQL 模式由独立 Provider Dispatcher claim 并执行 HTTP。
5. 触发 `local-quality-gate`，观察 `waiting_approval`，分别练习自批拒绝、幂等批准
   与拒绝终态；确认伪造成功 Webhook 不能越过门禁。
6. 用相同 correlation ID 重放相同请求，再修改参数重放，比较 replay 与 conflict。
7. 上传 JSON/JUnit XML 测试报告或普通 Artifact，观察
   `pending → ready/failed → deleted`、摘要和审计。
8. 用测试客户端向独立机器 Webhook 路由发送签名事件，练习 duplicate、stale、gap
   与 terminal regression，再用轮询对账。CI Lab 当前不会主动投递这些事件。
9. 在终态前取消，再尝试对成功/失败终态取消。
10. 停止 CI Lab，观察有界超时和脱敏错误；恢复后继续查询原 Run。
11. 切回 `local_lab`，即使 Lab 仍运行，也要确认 QA 平台不再打开 socket。

## 安全不变式

- 默认 `local_lab`：Learning CI、Jenkins、GitLab、BK-CI 全部无网络。
- `ci_lab_local` 只能激活 `learning_ci`，不会顺便激活其他 Provider。
- Lab 目标是代码常量与精确 `/32`，页面、数据库和自由环境变量不能覆盖。
- HTTP 关闭环境代理和重定向，每次请求都检查解析地址。
- 容器中的明文 HTTP 例外只适用于固定私网 IP，且网络为 `internal: true`。
- Token 只作为机器凭据传递，不存入 Connection/Run，不记日志，不进入 URL。
- 出站 Bearer Token 与入站 Webhook HMAC Secret 相互独立；Webhook 不使用浏览器
  Session/CSRF，验签前按原始字节限制为 16 KiB。
- CI Lab 不提供 Shell、子进程、Git clone、Docker socket、任意 URL 或动态插件。

## 六 B/六 C 已编码范围

- Provider trigger intent 与独立 Dispatcher：写意图、claim、事务外 HTTP、CAS 结算。
- 质量门禁审批、触发人自批防护、审批幂等和 Webhook 防绕过。
- Provider Run Artifact 的 Storage Port、摘要、pending、补偿、删除恢复和审计。
- 独立签名 Webhook 接收、事件收据、重放/乱序/缺口/终态回退与对账标记。
- 一次性 migration Job、独立 PostgreSQL Scheduler claim/CAS，以及与任务同事务的
  content-free Rabbit wake-up outbox/独立 Dispatcher；Web 不持有 Broker。

编排状态机、三段事务和练习顺序见
[PHASE6B_6C_ORCHESTRATION.md](PHASE6B_6C_ORCHESTRATION.md)。

## 当前验证边界

已完成的是代码、SQLite/替身测试、PostgreSQL SQL 方言和进程边界的自动化验证，
不是生产 CI 或高可用部署。CI Lab 目前没有主动 webhook delivery Worker，只有 QA
平台的独立签名接收链路；真实 Jenkins、GitLab、BK-CI 和公司系统始终关闭。

当前机器没有 Docker，因此源码双进程、Python 状态机、契约和隔离边界可以在
宿主机验证，但镜像构建、固定容器 IP、容器间真实 HTTP、容器停机和恢复演练
仍需在个人隔离 Docker 环境补做。真实 PostgreSQL/RabbitMQ、多实例 Scheduler/
Worker/Dispatcher、重复消息、崩溃/租约过期、Broker/数据库中断、备份恢复和故障
注入均未验收。Web 当前保持单实例，CI Lab 也是单实例 SQLite，不宣称 HA。
