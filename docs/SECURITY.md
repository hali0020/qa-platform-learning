# 本地教学安全规则

## 永远遵守

1. `.env` 永不提交，只提交没有 Secret 值的 `.env.example`。
2. 禁止把真实密码、Token、Cookie、Webhook Secret、Access Key 和完整连接串写进源码、数据库、日志、截图或测试 fixture。
3. 示例数据只使用虚构账号、项目、主机和地址。
4. 提交前检查 Git 变更、生成物和敏感信息；只提交本项目自行编写的内容。

## 本机数据隔离

- 默认 `LOCAL_ONLY=true`，HTTP 只绑定环回地址，CORS 只接受环回来源。
- 数据库默认使用 `sqlite_local` 与本机 `sqlite+aiosqlite` 文件。可选 PostgreSQL 只允许 `DATABASE_RUNTIME_MODE=postgres_local_container`、`APP_ENV=local-container`、`postgresql+asyncpg` 驱动以及内部服务 `postgres:5432` 的组合；宿主机名、IP、其他端口、查询参数和其他 PostgreSQL 驱动都会在连接前被拒绝。
- 关闭 HTTP 的 `LOCAL_ONLY` 不能解除数据库边界，也没有连接任意远程、公司或公网数据库的开关。
- SQLite 数据库/WAL、上传和临时文件位于本机忽略目录；Compose PostgreSQL 数据只位于自建内部网络的命名卷，`5432` 不发布到宿主机。流水线快照与 QA 数据复用所选关系数据库。
- `UPLOAD_ROOT` 必须位于所选模式的本机存储边界内：SQLite 使用数据库目录，PostgreSQL 容器使用显式 `LOCAL_DATA_ROOT=/data` 的可写本机卷；两者拒绝 UNC/URI，解析后的 `UPLOAD_ROOT` 不得越出该边界。
- `AUTH_ENABLED=false` 只允许隔离测试环境，不能作为本机开发捷径。

## 私有配置与连接清单

- 根目录 `.env` 是唯一由人维护、需要复用的本机连接配置与普通实验凭据文件；它被 `.gitignore` 的 `.env*` 规则忽略，仓库只保留无私有值的 `.env.example`。前端使用同源 API，不维护第二份环境文件。
- 平台账号、密码哈希和 QA 数据仍写入被忽略的 `.data` 数据库；Vault init/unseal/root 材料必须留在 ACL 保护的 `.data/secrets`，CI Lab 一次性 Bearer/HMAC Secret 留在内存或 owner-only 临时文件。不要为了集中配置而把这些运行数据或高敏感、短生命周期材料复制到 `.env`。
- 当前仓库不包含任何指向公司或外部系统的域名、API Base URL、数据库/消息队列连接串、共享盘路径、Token 文件或机器专属绝对路径；测试拒绝样例只使用不可解析的 `.invalid`、保留的 `.test` 名称或明确标注的 `untrusted` 路径。
- 运行代码和部署文件中允许出现的目标只有：`127.0.0.1`/`localhost`、
  项目 CI 专网的固定 CI Lab `172.30.60.2:8080`、QA backend
  `172.30.60.3:23100` 和 Webhook Worker `172.30.60.4`，以及 Compose 内由本仓库
  创建的 `postgres`、`rabbitmq`、`seaweedfs`、`keycloak`、`vault`、`backend`
  服务。不能为了通过清单测试而加入公司或公网目标。
- 文档中的公网网址只指向所用开源组件的官方说明、发布页或包仓库，不是应用运行时 API；应用不会自动请求这些文档网址。下载依赖或镜像是人工执行的构建动作。
- Jenkins、GitLab 与 BK-CI 适配器是无预置地址的通用教学代码。默认模式在 Secret、DNS 和 HTTP 之前拒绝它们；日后也只允许连接我们自己安装的实验实例。
- `backend/tests/test_connection_inventory.py` 会扫描提交的运行代码与部署配置中的常见 URL/连接串形式。新增任何不在精确本地清单中的匹配项都会使测试失败；它是运行时连接回归门禁，不替代 Secret/PII 扫描，也不要通过扩充清单来接入现有公司服务。

## 登录与权限

- 没有默认账号或默认密码，首次管理员只能在本机、空用户库时创建。
- 密码使用 Argon2；生产参数必须按目标硬件基准测试。
- Session Cookie 为 HttpOnly，数据库只存 Token 摘要；CSRF 使用 Cookie/Header 双提交校验。
- 禁用用户、变更角色、重置密码和改密都会撤销相关会话。
- 前端隐藏按钮不是授权；Router 和 Service 必须独立检查权限及资源关系。
- 可选 `keycloak_local_container` 只接受固定环回 issuer 和内部 `keycloak:8080` token/JWKS 地址；默认 `local_accounts` 不构造 OIDC 客户端。
- OIDC 使用 state、nonce、S256 PKCE、一次性数据库事务与浏览器绑定 Cookie；只接受 RS256 并校验 issuer/audience/azp/时间/nonce。
- Keycloak TOTP Secret 不进入平台数据库。账号只能由 `users.manage` 管理员显式绑定稳定 `(issuer, sub)`；禁止按用户名/邮箱 JIT，忽略 Keycloak role。
- 当前仍是系统级 RBAC；本机 Keycloak+TOTP 实验不等于项目级隔离、企业 SSO 或合规身份治理。

## 文件与表格

- CSV/XLSX 导入有体积、行数和压缩体积上限；拒绝宏、外链、嵌入对象及危险 ZIP 路径。
- 表格导出中可能触发公式的文本会被中和。
- 附件使用 MIME/扩展名白名单，图片解码验证后重编码；实际存储键不使用用户文件名。
- `nosniff` 和下载沙箱不能代替病毒扫描。生产附件还需要扫描、隔离、对象存储策略和签名访问。

## 自建实验室 Provider

- 默认 `PROVIDER_RUNTIME_MODE=local_lab`；Learning CI、Jenkins、GitLab 和 BK-CI 会在读取 Secret、DNS 或 HTTP 之前被拒绝。
- 只存在 `local_lab`、固定目标的 `ci_lab_local` 与通用自建产品实验室 `self_hosted_lab` 三种模式，不提供连接任意 external/public 或公司系统的模式。
- `ci_lab_local` 只能在 `local`/`local-container`/`test` 使用 `learning_ci`，出站
  Bearer 只允许固定 Secret 名 `QA_PROVIDER_SECRET_CI_LAB`，回调 HMAC 只允许
  `QA_PROVIDER_SECRET_CI_LAB_WEBHOOK`。QA→CI 宿主机目标固定为
  `127.0.0.1:23020`，容器目标固定为 `172.30.60.2:8080/32`；CI→QA 宿主机
  目标固定为 `127.0.0.1:23100`，容器只允许
  `172.30.60.4 → 172.30.60.3:23100`。连接表必须把 `base_url` 和 config 留空，
  通用 Host/Port/CIDR/HTTP 开关必须保持默认空值。
- 开启回调的触发必须成对提交 Connection UUID 和 correlation ID，且后者必须
  与 `Idempotency-Key` 一致。签名 body 重复携带该绑定；QA 必须核对路径
  Connection、已启用类型/固定 Secret 引用和本地 Run correlation。找不到 Run 时不得
  消费事件收据，否则合法重试会被误判为 duplicate。
- Webhook Worker 不允许任意 URL 环境变量，关闭环境代理和重定向，并限制
  超时/响应大小。它只挂载 HMAC Secret 文件；CI Lab API 只挂载 Bearer Token
  文件。列表/手工 retry 机器 API 不返回 body、签名、目标、Secret、摘要或租约
  token。
- Learning CI 的私网明文 HTTP 例外只接受上述精确 IP literal；不能借此给域名、Jenkins、GitLab 或 BK-CI 开 HTTP。CI Lab 机器 Token 必须为 32–512 位可见 ASCII，Authorization 头有硬上限并使用 bytes 常量时间比较；所有 HTTP 方法的请求体均限制为 16 KiB。
- `self_hosted_lab` 必须通过 `PROVIDER_SELF_HOSTED_OWNERSHIP_ACKNOWLEDGED=true` 确认目标由我们自己搭建，并同时配置精确主机、端口、私网/环回 CIDR 和 Secret 名称 allowlist。
- `APP_ENV=local`、`test` 等非容器模式即使完成确认也只允许 `localhost`/环回 IP 与环回 CIDR；RFC1918/ULA 私网只允许 `APP_ENV=local-container` 的内部容器网络。测试中的私网地址只能配合 MockTransport，不会获得真实网络许可。
- DNS 的每一个结果必须既属于 RFC1918/IPv6 ULA/环回范围又命中 CIDR allowlist；IPv4 CIDR 不得宽于 `/24`、IPv6 不得宽于 `/64`，单机优先使用 `/32` 或 `/128`；公网、链路本地、保留、多播和未指定地址一律拒绝。
- 通用自建产品默认要求 HTTPS；HTTP 只允许显式开启后的环回地址。Learning CI 的固定私网 IP 例外不使用该通用开关。
- 禁止通配主机、URL 内嵌凭据、跟随重定向和任意用户输入 URL。
- DNS 解析结果也要校验，防止域名解析到环回、链路本地或未授权私网。
- 数据库只保存 Secret 引用名称；名称必须使用 `QA_PROVIDER_SECRET_` 前缀并进入 `PROVIDER_SECRET_ENV_ALLOWLIST`，API 不返回 Secret 值，触发变量拒绝敏感键。
- 可选 Vault 只允许 `APP_ENV=local-container` 下的固定 `http://vault:8200`、KV-v2 mount 和 token 文件；客户端禁代理/跳转/压缩响应，固定两个只读文档并限制大小、并发、超时和重试。本阶段应用已将 Provider Secret 读取接入 Vault；DB/Broker/S3 启动凭据仍使用现有 Settings 流程。
- Jenkins/GitLab/BK-CI 契约测试只使用 Mock HTTP；Learning CI 另有 Provider→独立 ASGI 应用的全链路测试，但不会打开公网 socket。后续产品联调也只使用我们自建的测试实例和测试账号。
- 应用层 DNS 校验不能消除 DNS-check/connect 竞态；运行自建实验室时还必须用容器或主机 egress policy 限制目标私网 CIDR。

## 日志、指标与部署

- 日志不记录请求体、Cookie、Authorization、查询串、上传内容和完整连接串。启动命令关闭 Uvicorn 原始 access log，OIDC 网关对含 code/state/nonce 的路径也关闭 access log，只保留不含 query 的 route-template JSON 日志。
- Prometheus 标签不得包含用户/任务/设备/项目 ID、URL、文件名或异常正文。
- `/metrics` 在生产必须只对监控网络开放。
- 非本机 HTTP 必须使用 Secure Cookie 和 TLS；容器的 `0.0.0.0` 监听只能通过宿主机环回端口或受控入口发布。
- Compose Web 当前固定单实例。任务、Scheduler、Provider Intent 和 task wake-up
  outbox 的特定路径已有数据库 claim/CAS，但进程内业务锁、本机附件与跨 Repository
  提交仍未完成多 Web 审计。
- CI Lab 使用独立 SQLite、单 API 进程与单 Webhook Worker，固定 Definition 不执行
  用户代码；它的
  Bearer/幂等/质量门禁不等于 TLS、分布式 Executor 或高可用。QA 端已用持久 Trigger
  Intent + 独立 Dispatcher 把 Provider HTTP 移出数据库事务，并以未知状态/轮询对账
  收敛，但真实进程崩溃窗尚未容器验收。
- CI Lab 的出站 Bearer 与入站 Webhook HMAC Secret 分离。独立 Webhook 接收端不使用
  浏览器 Session/CSRF，先限制 16 KiB 原始 body，再做五分钟时间窗、常量时间 HMAC、
  事件唯一键和 sequence reducer。CI Lab 把状态与不可变 body 同事务写入持久
  Outbox，独立 Worker 主动物化、按 Run sequence 租约 claim、事务外 HMAC 投递、
  version/token 摘要结算、退避与死信。轮询快照的 `webhook_sequence` watermark
  用于对账缺失回调；该机制不承诺恰好一次。
- Web 不持有 RabbitMQ 连接或凭据。任务和 wake-up outbox 同事务提交，独立
  Dispatcher 只发布无业务内容提示，数据库 claim 才能授予 Worker 执行权。
- 当前机器没有 Docker，因此 PostgreSQL 容器与 CI Lab 固定 IP 双向 HTTP 尚未做
  真实启动、迁移、重启恢复和 readiness 联调；Webhook Worker 崩溃、租约过期、
  重试/死信/手工恢复也未实机验收。已有的是配置边界、快照/Outbox/Worker 契约、
  PostgreSQL 方言迁移生成与 ORM 编译等离线/自动化验证。
- 尚无 SQLite 与 PostgreSQL 之间的数据搬迁工具；一次性 migration Job 已编码，但
  没有真实多实例、备份恢复或故障切换保证。不能因为已有 Compose profile 就宣称
  已生产化或 HA。

## 上生产前的安全门槛

企业 OIDC 联调、项目级授权、DB/Broker/S3 启动 Secret 注入、TLS/WAF/限流、网络 egress policy、集中审计、恶意文件扫描、备份恢复、镜像/SBOM/漏洞扫描、资源配额、密钥轮换和应急演练都仍需完成。本机 Keycloak/Vault 是学习环境，不是生产安全背书。
