# 数据库、迁移与本机容器实验

## 数据位置与所有权

应用通过同一个 SQLAlchemy 2 异步 `Database`/`AsyncSession` 访问 QA、身份、协作、自动化运行时和流水线表，持久结构统一由 Alembic 管理。默认模式是 `sqlite_local`，使用 `aiosqlite`，持久文件为：

```text
backend/.data/qa.db
```

Compose 可显式选择自建 PostgreSQL，但只允许下面这一种隔离拓扑：

| 模式 | 环境 | URL 边界 | 数据位置 |
| --- | --- | --- | --- |
| `sqlite_local`（默认） | 本机源码或 Compose | `sqlite+aiosqlite` 本机文件 | `backend/.data/qa.db` 或 Compose `qa-data` 卷 |
| `postgres_local_container`（可选） | 仅 `APP_ENV=local-container` | `postgresql+asyncpg://...@postgres:5432/...`，无查询参数 | 内部网络的 `postgres-data` 卷，不发布数据库端口 |

附件仍保存在受约束的本机/容器数据目录：SQLite 模式位于数据库文件目录下；PostgreSQL 模式由 `LOCAL_DATA_ROOT` 定义独立的本机文件边界，Compose 固定为可写的 `/data`，默认附件因此落到 `/data/uploads` 的 `qa-data` 卷。宿主机数据库、WAL/SHM、上传、临时文件和测试库被 Git 忽略；Compose 命名卷不属于仓库内容。

持久数据库的结构唯一由 Alembic 管理。内存 SQLite 只用于隔离测试，因为它不能通过另一个 Alembic 连接保留 schema，测试应用才会在自身连接上创建 metadata。

## 安全边界

- `sqlite_local` 只接受 `sqlite+aiosqlite` 本机文件，拒绝网络 SQLite URI 和 UNC 路径。
- `postgres_local_container` 必须同时满足 `APP_ENV=local-container`、`postgresql+asyncpg`、主机精确为 `postgres`、端口精确为 `5432`，并提供非空用户名、密码和数据库名；不接受 URL 查询参数。
- PostgreSQL 的 `localhost`、环回 IP、其他私网/公网/公司主机、其他端口和其他驱动都会在连接前被拒绝；MySQL 等其他数据库同样不支持。
- `LOCAL_ONLY=false` 只改变 HTTP 监听边界，不能解除数据库限制。
- 不读取公司项目的连接串、连接池或迁移历史。
- 测试使用 `tmp_path` 或内存库，每个用例相互隔离。
- Provider Token 不入库；`provider_connections.secret_env_var` 只保存环境变量名称。
- Provider/Webhook Secret 分离，连接表只保存两个引用名称；Webhook 收据不保存原始
  body 或签名。
- Worker、设备、Scheduler、Provider Dispatcher 和 Outbox Dispatcher 的租约
  Token 只保存 SHA-256 摘要。

选择数据库应发生在创建学习数据之前。当前没有 SQLite 与 PostgreSQL 之间的数据导入、双写或在线搬迁工具，不能通过修改 URL 假装旧数据会自动迁移。

## 迁移历史

| 版本 | 主要内容 |
| --- | --- |
| `0001` | 项目、用例、计划、执行及本地流水线基础表 |
| `0002` | 套件层级、用例归属与不可变快照 |
| `0003` | 缺陷和追加式审计 |
| `0004` | 角色/权限、用户、Session、评论、附件及审计 actor |
| `0005` | Provider 连接/运行、任务、设备/租约、Schedule/Fire |
| `0006` | PostgreSQL 活动设备租约唯一防线 |
| `0007` | 附件 Storage backend/namespace 路由 |
| `0008` | 本机 Keycloak OIDC 登录事务与显式 subject 绑定 |
| `0009` | Learning CI Provider 兼容列与约束 |
| `0010` | Provider trigger intent、审批、Run Artifact、签名 Webhook 收据与 Run 编排字段 |
| `0011` | Scheduler claim 字段与 transactional task wake-up outbox |

数据交换和质量报表不另建事实表：前者复用用例/缺陷写服务，后者从现有 QA Repository 只读计算。

流水线持久化也复用这套异步 SQLAlchemy 会话和 Alembic 表，不再为 PostgreSQL 走 SQLite 专用旁路。一次流水线 checkpoint 会在所选数据库的一个事务中替换运行、触发幂等键和回调事件快照；这只保证该快照内部一致，不等于所有 QA Service 已经拥有跨 Repository 的 Unit of Work。

## 初始化与常用命令

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m alembic upgrade head
python -m alembic current
python -m alembic history
```

本地练习数据可以回退一个版本，但先备份需要保留的文件：

```powershell
python -m alembic downgrade -1
python -m alembic upgrade head
```

若把 `0010` 降回 `0009`，尚未获得 Provider external ID 的本地 pending Run 会被
确定性回填为 `local-downgrade-pending-<run-id>`，以满足旧版非空/唯一约束并保留
可识别性；这个占位值不是 Provider 事实，重新升级也不会凭空还原远端 ID。

不要未经审阅直接接受 `--autogenerate`。重点检查外键删除策略、唯一约束、CheckConstraint、索引、非空列的数据回填和 downgrade 顺序。

## 引擎行为与当前验证状态

SQLite 连接启用 foreign keys、WAL 和 busy timeout，降低单进程学习时常见的约束遗漏和短暂锁冲突。PostgreSQL 使用 `asyncpg` 与连接前探活，并由相同 Repository、流水线适配器和 Alembic revision 驱动。

Compose 已提供一次性 `python -m app.migrations.main` Job。它是唯一 schema writer；
Web、Worker、Scheduler 与 Dispatcher 使用 `DATABASE_SCHEMA_MODE=verify`，只有在数据库
已经位于仓库 Alembic head 时才启动。源码脚本仍先显式执行 Alembic，再启动
verify-only Web。

当前机器没有 Docker，因而尚未真实运行 PostgreSQL 容器，也没有完成容器内 Job
启动顺序、`upgrade head`、CRUD、流水线重启恢复和 readiness 的实机联调。现有验证
覆盖 migration Job 边界、配置拒绝、PostgreSQL 方言下的 ORM 编译和 Alembic
upgrade/downgrade DDL 离线生成；这不能替代真实 PostgreSQL 集成测试。

## 当前事务与并发边界

- 一个 Repository 写入通常是一个独立 SQLAlchemy 事务。
- 业务变更与审计事件可能分两次提交；审计失败不能自动回滚业务记录。
- 批量导入允许 partial 时逐行提交，明确 `atomic=false`。
- 质量服务跨多个 Repository 读取，不是同一个数据库读快照。
- 本地流水线完整快照在同一数据库事务内保存，但写协调仍采用单进程模型。
- runtime 表保存版本、租约和幂等数据；特定任务/设备/Scheduler/Provider Intent/
  outbox 路径已实现数据库 claim/CAS，但换成 PostgreSQL 并不会自动让所有业务支持
  多 Web。
- 任务与 wake-up outbox 在同一事务创建；RabbitMQ 只收到无业务内容提示，独立
  Dispatcher 的 publish 和数据库结算之间仍允许 at-least-once 重复。
- 进程内 `asyncio.Lock` 不能协调多个进程或主机。

生产演进仍需要更广泛的 Unit of Work、剩余按记录增量更新、Handler 幂等、备份
恢复，以及经过真实负载验证的锁/CAS。当前实现的 migration Job、独立进程、
Scheduler claim 和任务 outbox 只是可测试代码边界，不代表多实例并发、故障切换或
高可用已经完成。

## 迁移验收

1. 空临时 SQLite 能升级到 `head`；PostgreSQL 方言能离线生成完整 upgrade/downgrade DDL。
2. 能降级到 base 后再次升级。
3. ORM metadata 与 Alembic head 没有结构漂移。
4. 用户、QA 数据、协作内容、Provider 元数据、任务/设备/调度能跨应用重启恢复。
5. 上传二进制与附件元数据在失败路径下不会静默失配。
6. 两个临时数据库不会相互污染。
7. 非法数据库模式、驱动、环境、主机、端口或 URL 参数在尝试连接前就被拒绝。
8. readiness 对通过安全校验的 SQLite 与 PostgreSQL 都执行有超时的 `SELECT 1`，且不泄露连接信息。

源码启动流程先显式升级再让应用校验；Compose 持久数据库只由独立 migration Job
升级，其他进程 verify-only。真实 PostgreSQL 验收、SQLite→PostgreSQL 数据搬迁、
备份恢复和多实例并发/故障测试仍是后续工作，Web 当前保持单实例。
