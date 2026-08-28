# 阶段六 B/C：CI 编排教学说明

本页只解释已经编码的状态机与一致性做法。它用于本机学习，不表示真实
PostgreSQL、RabbitMQ、多实例或高可用已经验收；当前机器没有 Docker。Jenkins、
GitLab、BK-CI 和公司系统保持关闭，未来也只允许连接我们自己安装的隔离实例。

## 1. 五组状态机

```text
Provider Trigger Intent
pending ──claim──> claimed ──远端确认──> succeeded
   ▲                  ├─可重试──> retry_wait ─┘
   │                  ├─结果不确定──> unknown + reconcile_required
   └──────────────────┴─明确拒绝──> failed

Quality Gate
evaluating ──> waiting_approval ──approve──> approved / run succeeded
                              └─reject───> rejected / run failed

Run Artifact
pending ──存储与摘要确认──> ready ──quarantine + 元数据提交──> deleted
   └─上传/结算失败──> failed ──幂等清理────────────────────> deleted

Webhook Receipt
新事件 ──> applied | stale | ignored | reconcile_required
同 event_id + 同摘要 ──> duplicate
同 event_id + 不同摘要 ──> conflict

Task Wake-up Outbox
pending/retry_wait ──claim──> claimed ──publish confirm──> published
                                      └─失败──> retry_wait
```

质量门禁是后端与 CI Lab 的持久规则，不是前端按钮。`pipeline.approve` 只给
`system_admin`/`qa_lead`，触发人不能审批自己；签名 Webhook 也不能把等待审批的
运行直接推进为成功。

## 2. 可复用的三段事务

外部 HTTP、RabbitMQ publish 和 Cron 计算都不放在长数据库事务中：

```text
T1：写事实/意图，或 claim 一条记录，提交租约
                 ↓
事务外：执行 Provider HTTP、Rabbit publish 或 Cron 计算
                 ↓
T2：按 owner + token hash + version 做 CAS 结算
```

完整业务通常表现为三个短事务阶段：

1. 创建：Provider Run + Trigger Intent，或 Task + Wake-up Outbox，同事务提交；
2. claim：Dispatcher/Scheduler 用数据库时钟和租约取得唯一处理权后立即提交；
3. finalize：事务外动作结束后，凭 owner、token 摘要和 version 结算；PostgreSQL
   CAS 用 UPDATE 执行当刻的数据库时钟核验租约，避免行锁等待跨过到期时间；丢失
   租约时不能覆盖新处理者的结果。

这个模式不承诺“恰好一次”。Provider 重试复用相同 `Idempotency-Key`；RabbitMQ
只收到固定、无任务 ID/参数/凭据的 wake-up hint；Worker 仍必须向 PostgreSQL
claim，Handler 仍须幂等。消息丢失由数据库轮询兜底，publish 成功但结算前崩溃
可能产生无害重复提示。

## 3. Web 与独立进程的责任

| 进程 | 负责 | 不负责 |
| --- | --- | --- |
| migration Job | 一次性执行 Alembic upgrade | 提供 HTTP、处理任务 |
| Web | 认证/RBAC、业务 API、写 Run/Intent/Task/Outbox、签名 Webhook 接收 | 持有 RabbitMQ 连接、运行持续 Scheduler、在事务内调用 Provider |
| Provider Dispatcher | claim Trigger Intent、事务外请求 CI、CAS 结算 | 接收浏览器请求 |
| Scheduler | PostgreSQL `SKIP LOCKED` claim、事务外算 Cron、CAS 写 Fire/Task | 依赖进程锁协调副本 |
| Outbox Dispatcher | claim wake-up outbox、发布固定 RabbitMQ 提示、CAS 结算 | 发布任务 Payload |
| Worker | 从 PostgreSQL claim、心跳续租、运行固定幂等 Handler | 把 RabbitMQ 消息当执行授权 |

Compose 的目标顺序是 `migration → Web/Worker/Scheduler/Dispatcher`。Web、Worker、
Scheduler 和 Dispatcher 使用 verify-only schema 模式，不能并发修改结构。源码
SQLite 模式保留手工 tick/dispatch 教学入口，但不能据此推导多进程安全。

## 4. Webhook 与 Artifact 边界

Webhook 路由不使用浏览器 Session/CSRF。它先按原始字节执行 16 KiB 上限，再用
独立 Secret 校验固定五分钟时间窗与 HMAC；事件唯一键、body SHA-256、sequence 和
occurred-at 用于识别重放、内容冲突、旧事件、缺口和时间回退。任何缺口或非法状态
推进只标记 `reconciliation_required`，由后续轮询读取 CI 权威快照。

CI Lab 当前没有主动 webhook delivery Worker，所以现阶段用自动化测试或本机测试
客户端练习接收端，不应写成“CI Lab 已主动推送”。

Artifact 元数据和对象内容不能由一个 SQL 事务原子提交。因此先写 `pending`，对象
保存和摘要确认后才转 `ready`；失败转 `failed` 并尽力清理。删除先 quarantine，
元数据提交失败则 restore。下载再次核对大小/SHA-256并使用安全响应头。

## 5. 建议练习顺序

1. 用默认 SQLite/Local 模式触发，确认 Run/Intent 已提交但 Provider 尚未被调用；
   手工 dispatch 后观察 intent 与 run 的状态变化。
2. 用同一 correlation ID 重放相同/不同输入，比较 replay 与 conflict；模拟可重试
   失败，观察 `retry_wait/unknown/reconcile_required`。
3. 触发 `local-quality-gate`，验证触发人自批失败、第二位审批人可幂等批准/拒绝，
   以及成功 Webhook 不能绕过门禁。
4. 上传有效 JSON/JUnit XML 和损坏 XML，观察 Artifact 状态、SHA-256、审计、下载
   安全头与删除补偿。
5. 生成独立签名事件，依次测试正常、重复、同 ID 不同内容、stale、sequence gap
   和终态回退，再用轮询消除对账标记。
6. 用 SQLite 单进程测试 Scheduler claim/CAS 和 wake-up outbox 状态机，明确这只
   验证算法与 SQL 形状。
7. 在有 Docker 的个人隔离机器，从空卷启动真实 PostgreSQL/RabbitMQ，再做多实例
   claim、重复提示、Worker/Dispatcher 强杀、租约过期、Broker/数据库中断恢复。
8. 最后设计备份/成对恢复、RPO/RTO 和回滚；全部实测前保持 Web 单实例且不宣称
   高可用。

容器练习前先看 [NEXT_DEVELOPMENT.md](NEXT_DEVELOPMENT.md) 的未完成清单和
[DEPLOYMENT_PHASE3.md](../infra/DEPLOYMENT_PHASE3.md)、
[DEPLOYMENT_PHASE6_CI_LAB.md](../infra/DEPLOYMENT_PHASE6_CI_LAB.md) 的安全边界。
