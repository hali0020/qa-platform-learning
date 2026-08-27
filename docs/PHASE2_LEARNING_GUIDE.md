# 阶段二学习说明：把 QA MVP 扩展成平台骨架

## 1. 这一阶段解决什么问题

第一阶段回答“怎样用异步 HTTP 完成项目、用例、执行、缺陷和流水线闭环”。第二阶段继续回答七个更接近真实 QA 平台的问题：谁能操作、怎样批量搬运数据、怎样围绕业务对象协作、怎样解释质量、怎样对接 CI、怎样分配执行资源，以及怎样运行和观察平台。

本阶段仍坚持三个边界：

1. 业务数据默认落在本机 SQLite；可选自建 PostgreSQL 只允许 `postgres_local_container + APP_ENV=local-container + postgresql+asyncpg@postgres:5432` 的 Compose 内部网络。附件仍位于受约束的本机/容器数据目录。
2. 真实外部 Provider 默认关闭，测试使用 Local Provider 或 HTTP Mock。
3. 能在单机演示不等于能上生产；文档会明确指出尚缺的多实例、事务、密钥和运维能力。

推荐学习顺序不是按页面顺序，而是按依赖关系：

```text
身份与权限
  ↓
QA 数据与协作 ──→ 质量指标
  ↓                  ↓
Provider 适配 ──→ 任务/设备/Cron
                     ↓
              Docker 与可观测性
```

## 2. 用户登录、角色与权限

### 已实现的模型

- 本机用户、启用/禁用状态和一个系统角色；当前不是项目成员制，一个用户一次只绑定一个系统角色。
- 内置 `system_admin`、`qa_lead`、`tester`、`developer`、`viewer` 角色，以及细分到 QA、缺陷、协作、导入、报表、集成、设备、调度等领域的权限码。
- 首个管理员一次性初始化；项目没有默认用户名和默认密码。
- Argon2 密码哈希、失败登录计数与临时锁定、改密和管理员重置密码后的会话撤销。
- 服务端 Session：浏览器只持有不透明 Cookie，数据库保存 Token 摘要而不是明文 Token。
- 写请求使用双提交 CSRF 校验：可读的 CSRF Cookie 与 `X-CSRF-Token` 请求头必须相同；Session Cookie 为 HttpOnly，Cookie 使用 Strict SameSite。
- 路由依赖根据 HTTP 读写方法检查权限，审计 actor 来自当前登录主体。

为什么使用服务端 Session，而不是先上 JWT：本机管理后台可以即时撤销会话，角色变化后旧会话也能失效，学习“认证”和“授权”两件不同的事更直接。JWT 并不会自动解决撤销、密钥轮换、浏览器存储和 CSRF 问题。

### 仍不是生产身份系统

- 没有企业 SSO/OIDC、MFA、密码找回邮件、验证码或设备可信度。
- 角色是系统级，不支持“在 A 项目是负责人、在 B 项目是只读成员”。
- 锁定与限速状态位于当前应用数据层，没有网关级或分布式防护。
- 本地审计不是不可篡改、不可抵赖的合规审计。

### 练习 1：观察认证与授权

1. 执行 Alembic 迁移，确认没有用户时 `/auth/status` 表示需要初始化。
2. 通过本机登录页或 `python -m app.cli.bootstrap` 创建首个管理员。
3. 在浏览器开发工具中确认 Session Cookie 不能被 JavaScript 读取，而 CSRF Cookie 可被前端读取并送入请求头。
4. 创建 `viewer` 用户，分别尝试 GET 和 POST 业务接口，记录 `401 未认证` 与 `403 无权限` 的区别。
5. 禁用用户或修改角色，验证旧会话被撤销。

复盘问题：认证主体是谁？权限在 Router 还是 Service 检查？如果以后增加项目成员表，现有系统角色怎样与项目角色组合？

## 3. CSV/XLSX 两阶段导入与导出

### 为什么不能“上传后直接写库”

批量文件常同时包含格式错误、跨对象引用错误和业务规则错误。如果收到文件就逐行写库，用户直到中途才知道问题，重试还可能重复创建。因此当前流程分成两步：

```text
下载模板 → 填写文件 → Preview 解析/校验 → 用户确认摘要 → Commit 再解析/再校验 → 逐行结果
```

Preview 返回文件 SHA-256、有效/无效行数和逐行问题。Commit 必须带回预检摘要，服务会重新解析相同字节并核对摘要，避免用户确认的文件与实际提交文件不一致。默认 clean gate 会在任一行存在错误时做到零写入。

### 必须诚实理解的“非原子边界”

当前导入模式是 `partial_create_only`，不是批量原子事务：

- 只创建用例或缺陷，不执行更新和删除。
- 关闭 clean gate 后，每行独立调用现有领域服务；前面的成功行不会因后面的失败行自动回滚。
- 结果明确区分 created、failed、skipped，并显式返回 `atomic=false`。
- 文件内 `row_key` 只用于关联主行和步骤、定位错误，不是数据库幂等键。

真实企业导入可按需求选择“全部成功或全部回滚”的 Unit of Work，或选择可恢复的批任务、幂等键和错误文件下载。两种方案不能混在一起对用户宣称“原子”。

### 文件安全边界

- 只接受 `.csv` 和 `.xlsx`，不接受旧 `.xls`、含宏的 `.xlsm` 或任意压缩包。
- 上传上限 5 MiB，主记录最多 5,000 行，总行数最多 100,000。
- XLSX 检查路径穿越、宏、外部链接、嵌入对象、解压体积和异常压缩比。
- 导出时对以 `= + - @` 等字符开头的文本加安全前缀，防止表格公式注入。
- 问题响应只回显有长度上限的单元格内容，不回显整个文件。

### 练习 2：验证两阶段语义

1. 下载用例 CSV 模板，构造一行合法数据和一行非法数据。
2. Preview 后记录 SHA-256 和逐行问题。
3. 保持默认 clean gate 提交，确认没有任何行被创建。
4. 在隔离学习数据上允许部分提交，确认合法行创建、非法行跳过。
5. 修改一个字节后继续使用旧摘要，确认 Commit 拒绝文件。
6. 导出标题以 `=` 开头的虚构数据，确认在 Excel 中不会变成可执行公式。

复盘问题：为什么 Preview 不能代替 Commit 时的再次校验？如果重试部分成功的文件，怎样避免重复数据？

## 4. 评论、附件与图片

### 协作对象与权限

评论和附件可以关联项目、套件、用例、快照、计划、执行或缺陷。服务会解析真实目标并核对 `project_id`，防止把附件挂到另一个项目。已归档项目不能继续新增或修改协作内容。

评论支持回复、编辑和软删除。普通用户只能修改自己的评论；具备 moderation 权限的角色可以管理他人内容。附件同样执行上传者或管理员规则，元数据与审计留在数据库中。

### 文件为什么不能沿用原文件名保存

原文件名只作为展示元数据；实际存储键由 UUID 生成，并被限制在数据库目录下的上传根目录。这样可以避免 `../`、Windows 保留名、同名覆盖和用户控制磁盘路径。

附件还会执行：

- 大小上限、空文件、扩展名与声明 MIME 双重检查；
- JPEG/PNG/WEBP 解码验证、像素上限、EXIF 方向处理和重新编码；
- PDF 文件签名、UTF-8 文本、JSON 内容、XLSX ZIP 结构检查；
- SHA-256 记录、下载 `nosniff`、内联图片沙箱策略；
- 删除时先移入本机 trash，再写软删除元数据，写入失败会尝试恢复。

这些检查降低风险，但不等于病毒查杀或内容审核。生产环境还需要恶意软件扫描、对象存储隔离、访问签名、生命周期策略和备份。

### 练习 3：建立协作证据链

1. 在一个缺陷下用两个用户回复评论，观察作者快照与父评论关系。
2. 用非作者账号编辑评论，确认被拒绝；再用管理员验证 moderation 权限。
3. 上传一张带 EXIF 方向的图片，下载后比较方向和文件摘要。
4. 尝试上传伪装成 PNG 的文本、含路径字符的文件名和超限图片。
5. 归档项目后尝试新增评论或附件。

复盘问题：附件二进制与元数据怎样保持一致？如果改成对象存储，哪些接口属于 Storage Port？

## 5. 质量报表、趋势与覆盖率

### 指标口径比图表更重要

当前报表只读取已完成执行，因为运行中结果仍会变化；时间区间按 `[date_from 00:00, date_to + 1 天 00:00)` 计算，当前只支持 UTC 和 Asia/Shanghai，最长查询 366 天。所有比率同时返回分子、分母和百分比；分母为 0 时百分比为空，而不是伪造为 0%。

| 指标 | 当前口径 |
| --- | --- |
| 自动化覆盖率 | 启用的自动化用例数 / 启用用例数 |
| 执行触达率 | 区间内已完成执行中实际运行过的启用用例去重数 / 当前启用用例数 |
| 完成率 | 非 `not_run` 结果数 / 所有结果数 |
| 通过率 | passed / (passed + failed)；blocked、skipped、not_run 不进入该分母 |
| 失败缺陷关联率 | 有关联缺陷的 failed/blocked 执行-用例对 / 全部 failed/blocked 执行-用例对 |
| 未解决缺陷 | 当前状态为 open、in_progress 或 reopened 的缺陷数 |
| 高严重度未关闭 | 当前 blocker/critical 且状态不是 closed 的缺陷数 |

趋势按日或周聚合完成执行结果，以及缺陷创建、解决、关闭、重新打开事件。套件覆盖率按当前启用用例归属聚合，未归套件用例单独展示。

### 当前统计的限制

- 当前是对 Repository 数据做确定性读取计算，不是面向大数据量的 SQL 聚合仓库。
- “当前用例集合”与“历史完成执行”并非同一时点快照，长期趋势需要事实表或快照口径。
- 暂无需求覆盖、代码覆盖、风险权重、逃逸缺陷、MTTR、版本/迭代维度。
- 跨 Repository 读取不是一个数据库一致性快照。

### 练习 4：自己算一遍指标

1. 创建 4 个启用用例，其中 2 个自动化，先手算自动化覆盖率。
2. 完成一次包含 passed、failed、blocked、not_run 的执行，预测完成率与通过率。
3. 为 failed 和 blocked 中的一项创建关联缺陷，预测失败缺陷关联率。
4. 切换日期、时区和日/周粒度，核对边界日期落在哪个桶。
5. 让某个分母为 0，确认前端展示“无数据”而不是“0%”。

复盘问题：一个质量指标能否被团队稳定使用，取决于图表样式还是可复算的口径？

## 6. Pipeline Provider：Local、Jenkins、GitLab 与 BK-CI

### Provider 模式解决什么

QA 平台关心的是“触发一条已配置的流水线、查询状态、取消运行”，不应该让缺陷或执行服务直接拼 Jenkins/GitLab/BK-CI URL。统一 `PipelineProvider` 将业务意图与供应商协议隔开：

```text
QA 应用服务 → PipelineProvider → Local / Jenkins / GitLab / BK-CI
```

Local Provider 不访问网络，用于教学、状态机测试和 CI。三个自建实验室协议适配器负责各自的认证头、路径、请求体和状态映射。`/integrations/connections` 已接入连接、连通性测试、触发、运行查询和取消的 HTTP 入口；连接元数据和运行映射持久化到当前选择的关系数据库，但 Token 不入库：数据库只保存 `secret_env_var`，运行时再从进程环境读取对应值。

### 默认关闭与出站防护

自建实验室调用必须同时满足显式启用和安全配置：

- `PROVIDER_RUNTIME_MODE=local_lab` 是默认值，网络 Provider 会在读取 Secret、DNS 或 HTTP 之前被拒绝；
- 只允许切换为 `self_hosted_lab`，不存在 external/public 模式，并且必须设置 `PROVIDER_SELF_HOSTED_OWNERSHIP_ACKNOWLEDGED=true` 确认目标是我们自己搭建的；
- 除 `APP_ENV=local-container` 外一律只允许 localhost/环回 IP 与环回 CIDR；RFC1918/ULA 私网只能位于内部容器网络，测试也不能通过 `APP_ENV=test` 绕过；
- 主机必须精确列入 allowlist，端口也必须列入 allowlist；
- 默认要求 HTTPS，只有显式开启时才允许环回地址 HTTP；
- DNS 解析后的每一个 IP 都必须是 RFC1918/IPv6 ULA/环回地址并落入显式窄 CIDR（IPv4 至少 `/24`、IPv6 至少 `/64`，单机优先 `/32` 或 `/128`），公网、链路本地和保留地址一律拒绝；
- Base URL 不能包含凭据、查询或片段，请求路径不能跳出基址；
- 不跟随重定向，设置连接/读写/连接池超时与响应体上限；
- 触发变量中拒绝看起来像 Token、密码或凭据的键。
- Secret 引用必须使用 `QA_PROVIDER_SECRET_` 前缀，且变量名必须列入 `PROVIDER_SECRET_ENV_ALLOWLIST`；即使系统环境中存在 `PATH` 等变量，也不能把它们当作凭据读取。

这套代码不会复制“完整蓝盾”。蓝盾是包含流水线编排、插件生态、Agent、制品、权限、审批和运维能力的一整套产品；这里实现的是 QA 平台最常用的一层 BK-CI API 适配器。不同版本的 BK-CI 网关前缀、认证方式与字段都可能不同，自建实验室启用前必须针对所部署版本核对。

### 练习 5：先 Mock，再考虑真实连接

1. 用 Local Provider 触发、查询、取消一次运行，观察统一状态。
2. 在测试中用 Mock HTTP 返回 Jenkins、GitLab、BK-CI 的典型响应，验证供应商状态怎样归一化。
3. 构造非 allowlist 主机、重定向、私网 DNS 结果和超大响应，确认调用在发出或读取阶段被阻止。
4. 查看连接记录，确认只能看到 Secret 环境变量名称及“是否已配置”，不能读到 Secret 值。
5. 只有在自己搭建测试实例和测试账号后，才切换 `self_hosted_lab`，在启动进程的本机环境或 Secret Manager 中设置 Secret，并同时完成所有权确认与四类 allowlist。

复盘问题：Provider 层负责协议差异，应用层还必须负责哪些幂等、审计、重试和状态同步规则？

## 7. 持久任务、设备租约与 Cron

### 任务不是在 HTTP 请求里睡一会儿

长时间自动化任务需要独立生命周期。当前任务模型包含 queued、running、retry_wait、succeeded、failed、cancelled、dead_letter 状态，以及优先级、队列、最大尝试次数、可执行时间和幂等键。

Worker claim 任务后得到一次性租约 Token；数据库只保存 Token 摘要。Worker 必须用 Token 续租、完成或报告失败。租约过期后任务可以被回收，重试使用有上限的退避并最终进入 dead letter。取消运行中任务是协作式 `cancel_requested`，不是强杀线程。

### 设备为什么也需要租约

设备记录包含 agent、平台、能力标签、心跳和 offline/idle/reserved/busy/maintenance 状态。任务按能力选择设备并取得带过期时间的租约；一个设备同一时刻只能有一个活动租约。申请和续租设备时必须同时证明持有对应任务租约，设备租约有效期也不会超过任务租约。Agent 标识不是授权凭据，生产 Agent 仍需 mTLS 或短期身份令牌。

### Cron 不是只有一个表达式

调度记录除五段 Cron 和 IANA 时区外，还需要：

- misfire：服务停机期间错过执行时选择 skip、fire_once 或 bounded catch-up；
- overlap：上一任务未完成时选择 forbid、allow 或 replace；
- fire key：确保同一计划时刻不会重复入队；
- `next_run_at` 与 fire 历史：支持恢复、审计和手动触发。

`/automation/tasks`、`/automation/devices` 和 `/automation/schedules` 已接入上述持久模型，便于从 HTTP 手工驱动 Worker、Agent 和 tick 练习。当前实现仍用于单机学习；生产环境必须把 Web、Worker 和 Scheduler 拆成独立进程，并解决多 Scheduler 领导者选举、数据库锁/CAS、事务 outbox、消息代理和 Handler 幂等。

### 练习 6：模拟故障恢复

1. 使用同一个幂等键入队两次相同任务，再用同键不同 payload 入队，比较 replay 与 conflict。
2. Claim 任务但不续租，推进测试时钟后回收，再由另一个 Worker Claim。
3. 连续报告可重试失败，观察 retry_wait、退避和 dead_letter。
4. 注册两台能力不同的设备，按能力租用并验证互斥；停止心跳后观察 offline。
5. 为同一 Cron 分别使用三种 misfire 和 overlap 策略，记录产生的 task/fire 历史。

复盘问题：at-least-once 队列为什么要求 Handler 幂等？设备租约与任务租约能否在一个事务里分配？

## 8. Docker、Prometheus、Alertmanager 与生产差距

### 当前本机参考拓扑

Compose 的阶段二默认仍提供单 Web 进程、Nginx/Vue、本机数据卷，以及可选的 PostgreSQL、Prometheus 和 Alertmanager profile。阶段三另加入显式 `worker` profile：它强制内部 PostgreSQL 与 RabbitMQ，支持 `--scale worker=N`；数据库、AMQP 和管理端口都不发布到宿主机。唯一业务入口绑定 `127.0.0.1`；Alertmanager 使用空接收器，不会向邮件或机器人发送消息。

后端暴露：

- `/health/live`：只说明进程和事件循环可响应；
- `/health/ready`：对通过安全边界的 SQLite 或 PostgreSQL 执行有超时的 `SELECT 1`；失败时应摘流而非一定重启；
- `/metrics`：HTTP 延迟/计数、任务、设备和 Provider 的低基数指标；
- JSON 请求日志和 `X-Request-ID`，不记录请求体、Cookie、Authorization 或原始查询串。

Prometheus 标签禁止放用户 ID、任务 ID、设备 ID、完整 URL、文件名和异常正文，否则会造成高基数和隐私泄漏。示例告警用于学习服务不可用、错误率和延迟等信号，不代表已经调优。

### PostgreSQL 当前验证到哪里

QA Repository、自动化运行时和流水线快照已共用异步 SQLAlchemy 与 Alembic；流水线一次 checkpoint 的运行、触发键和回调事件在同一事务提交。配置测试会拒绝 PostgreSQL 的错误环境、驱动、主机、端口和 URL 参数，迁移与 ORM 已通过 PostgreSQL 方言离线生成/编译。

当前机器没有 Docker，所以尚未真实启动 PostgreSQL 容器，也未完成容器内迁移、完整 CRUD、流水线重启恢复和 readiness 联调。学习时必须把“代码/方言已适配”和“真实 PostgreSQL 已验证”分开记录。

### 为什么还不能叫生产部署

1. 默认 SQLite、进程内锁和部分跨 Repository 多次提交不支持多 Worker/多实例一致性；可选 PostgreSQL 适配也尚未完成多实例并发验证。
2. Web 进程仍承担部分本地后台行为；生产应拆 Worker、Scheduler、迁移 Job。
3. 已有内部 RabbitMQ 唤醒提示与 Worker 骨架，但没有 SQLite→PostgreSQL 数据搬迁、高可用数据库、对象存储、事务 outbox、Secret Manager、备份恢复和灾难演练。
4. 没有企业入口层的 TLS/WAF/限流、网络 egress policy 和统一身份。
5. 没有镜像签名、SBOM、漏洞扫描、资源配额、滚动/蓝绿发布和自动回滚。
6. 现有审计、日志留存与告警通知不满足合规要求。

### 练习 7：运行但不假装上线

1. 用 Compose 启动本机业务 profile，确认只有 `127.0.0.1:23010` 可访问。
2. 启动 observability profile，在 Prometheus 中查询请求计数和延迟。
3. 在具备 Docker 的隔离机器上，把可选 PostgreSQL 作为待完成练习：先从空卷迁移，再验证 CRUD、流水线重启恢复和 readiness；不要搬入 SQLite 旧数据。
4. 停止后端或所选数据库，观察 readiness、Prometheus target 和告警状态。
5. 检查日志，确认登录密码、数据库密码、Cookie、Token 和上传内容没有出现。
6. 画出生产目标拓扑，并标注数据库、消息队列、对象存储、Secret Manager、Worker、Scheduler 和入口网关。

## 9. 建议的提交式学习节奏

每一课都按“读测试 → 手工复现 → 修改一个规则 → 补测试 → 恢复或提交”推进：

1. 身份：新增一个项目级角色设计文档，不急着写代码。
2. 数据：为导入增加真正幂等的业务外部键，并比较 partial 与 atomic。
3. 协作：抽象本机存储 Port，再写一个不联网的 Fake Object Storage。
4. 指标：为每个指标添加一组可手算 fixture 和口径说明。
5. Provider：只用 Mock 新增一个供应商适配器，先写契约测试。
6. 调度：写两个幂等 Handler，演示进程崩溃后重复投递。
7. 交付：把迁移、Web、Worker 和 Scheduler 拆成部署单元，再设计回滚。

完成一个练习的标准：本机可复现、有测试、没有真实凭据或公司连接，并且能解释故障时会发生什么。

## 10. 阶段三延伸：独立 Worker 与 RabbitMQ

当前已新增只允许 `postgres_local_container + rabbitmq_local_container` 的独立 Worker 进程骨架。消息只包含固定唤醒提示，数据库 claim 才授予租约；没有消息时仍周期轮询数据库。四种 Handler 都是固定、确定性的本机模拟，不允许 Payload 指定 import、命令、子进程或 URL。详细实现、故障语义与容器命令见 [PHASE3_WORKER_AND_BROKER.md](PHASE3_WORKER_AND_BROKER.md) 和 [DEPLOYMENT_PHASE3.md](../infra/DEPLOYMENT_PHASE3.md)。

当前机器没有 Docker，因此多 Worker、RabbitMQ 断线和真实 PostgreSQL 并发尚未容器实跑；现阶段只能声明代码与静态拓扑已准备，不能声明分布式生产能力已经验证。
