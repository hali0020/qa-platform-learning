# 第三阶段：PostgreSQL、RabbitMQ 与多 Worker

这一阶段把“HTTP 请求进程顺手执行任务”拆成独立 Worker，但仍坚持本机、自建、默认关闭的学习边界。Worker 只允许在 `APP_ENV=local-container` 下同时连接精确的 `postgres:5432` 和 `rabbitmq:5672/qa_platform_learning`，不能连接宿主机、公司内网或公网服务。

## 1. 为什么消息不是任务本身

RabbitMQ 消息固定为一个不含任务 ID、参数、凭据或命令的提示：数据库里可能有任务可领取。Worker 收到提示后仍需调用数据库 claim，取得带随机 Token 的租约才有执行权：

```text
API 提交事务
     │
     └── PostgreSQL 同事务保存任务 + wake-up outbox
                                      │
Outbox Dispatcher claim ──事务外发布固定 hint──> RabbitMQ
                                                  │
Worker 收到提示 ──────────────────────────────────┘
     │
     ├── 向 PostgreSQL claim
     ├── 没抢到：提示作废，继续等待
     └── 抢到：执行固定 Handler，并定期续租
```

Web 不连接 RabbitMQ，也不做“事务提交后顺手 publish”。任务与 wake-up outbox 由
一个数据库事务创建；独立 Outbox Dispatcher claim 记录、提交租约、在事务外发布，
再以 owner/token/version CAS 结算 `published` 或 `retry_wait`。publish confirm 后、
数据库结算前崩溃可能重复发送，但提示不含任务 ID、参数、凭据或命令。

消息丢失不会永久丢任务，因为 Worker 每隔 `WORKER_POLL_SECONDS` 主动查询数据库。重复消息也不会重复授予执行权，因为 PostgreSQL claim 才是权威边界。RabbitMQ 首次连接失败时，同一个 Source 对象会在后台按有上限的指数退避重试，数据库轮询不会暂停；连接恢复后自动重新使用消息唤醒。这是 at-least-once 系统常用的“Broker 加速、数据库兜底”设计。

## 2. Worker 当前能做什么

`app.worker` 只注册四个源码中固定的本机教学 Handler：

- `qa.import.validate`：校验最多 1000 行的内联字典，不打开文件或 URL；
- `qa.quality.generate`：根据已聚合的通过/失败/跳过数量计算摘要；
- `qa.pipeline.poll`：只归一化传入的模拟状态，不访问 Jenkins、GitLab 或蓝盾；
- `qa.device.execute`：只验证固定动作词表，不启动 adb、命令或子进程。

任务类型只能精确命中固定 Registry。Payload 不能指定模块、函数、Shell、可执行文件或 URL；未知类型会以 `worker_unknown_task_type` 非重试失败。第三方工具调用应在未来加入经过审计的专用 Adapter，不能演变为“从数据库动态 import/执行”。

## 3. 租约、心跳与退出

一个 Worker 进程同时处理一个任务，通过 Compose `--scale worker=N` 增加进程数。每个任务执行期间有独立心跳协程：

- 心跳延长数据库租约；
- 心跳发现 `cancel_requested` 时取消本机 Handler，再调用 complete，让数据库记录 authoritative `cancelled`；
- 心跳失败代表租约归属不确定，Worker 停止 Handler，不再写完成/失败，等待租约过期恢复；
- 普通异常只保存固定错误码，不把异常正文或 Payload 写进日志；
- SIGINT/SIGTERM 后停止领取新任务，给当前 Handler 最多 30 秒 grace period；超时则取消并以可重试 `worker_shutdown` 结束。Compose 的 40 秒停止宽限必须始终大于应用值。

Handler 必须幂等。数据库租约能防止正常竞争，但不能证明外部副作用恰好执行一次；进程可能在副作用完成后、数据库 complete 前崩溃。

PostgreSQL 模式下，任务租约使用数据库 `clock_timestamp()` 作为统一时钟，避免不同 Worker 主机的系统时间偏差提前回收任务。设备抢占采用 `task → device → lease` 的固定加锁顺序，候选设备使用 `FOR UPDATE SKIP LOCKED`；数据库还有“每台设备最多一条 active lease”的部分唯一索引作为最后防线。SQLite 仍只是单进程教学模式，不能据此扩展多个 Worker。

迁移 `20260827_0006` 如果发现历史数据里同一设备已有多条 active lease，会主动失败，要求先审计和修复冲突数据，不会静默删除租约历史。阶段六 C 的独立 Scheduler 使用 PostgreSQL `FOR UPDATE SKIP LOCKED`、数据库时钟、claim 租约和版本/token CAS；Cron 计算在事务外完成。SQLite 仍只有单进程教学语义。

## 4. 安全边界

- Worker 启动时拒绝 SQLite、`disabled_local` Broker 和非容器环境；
- Web 的 Broker 模式固定关闭；只有 Worker 和 Outbox Dispatcher 获得 RabbitMQ 配置；
- RabbitMQ 消息体是固定常量，不承载业务 Payload；
- RabbitMQ 和 PostgreSQL 都不映射宿主机端口；
- Compose 网络为 `internal: true`，容器没有默认公网出口；
- RabbitMQ 使用独立 vhost 和实验账号，密码只放在被忽略的 `.env`；
- `aio-pika==9.6.2` 是兼容本项目 Python 3.10.11 的固定版本；10.x 要求 Python 3.11，不能安装；
- RabbitMQ 固定镜像 `rabbitmq:4.3.5-management-alpine`，不使用 `latest`。

RabbitMQ 3.9 之后的官方镜像不支持 `RABBITMQ_DEFAULT_PASS_FILE`。当前 Compose 只能把 `.env` 中的密码注入容器环境，因此 `docker compose config/inspect` 可看到该值。这比把密码提交进 YAML 安全，但不是生产 Secret Manager；第五阶段再迁移到 Vault/Kubernetes Secret 或受控启动脚本。

## 5. 仍未宣称完成的内容

当前开发机没有 Docker，所以只完成 Python 单元测试、配置静态检查和 Compose 文档，尚未实际启动 PostgreSQL、RabbitMQ 或多个 Worker。容器实跑时还需要验证：

1. migration Job 从空卷成功后，Web/Worker/Scheduler/Dispatcher 才通过 schema verify；
2. 多 Worker/多 Scheduler/多 Outbox Dispatcher 并发 claim 只有一个获胜；
3. RabbitMQ 停止时 outbox 进入 retry_wait，Worker 数据库轮询仍能继续；
4. Worker/Dispatcher 被强杀后租约到期并安全重试；
5. publish-confirm/结算崩溃窗产生的重复提示不重复授权；
6. 数据库中断与恢复、断线重连和优雅退出。

上述进程、claim/outbox 与 migration Job 已编码并有本机自动化测试，但当前机器没有
Docker，真实 PostgreSQL/RabbitMQ、多实例竞争和故障注入仍未执行，不能宣称 HA。

具体环境变量与启动命令见 [第三阶段部署说明](../infra/DEPLOYMENT_PHASE3.md)。
