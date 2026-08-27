# 可观测性：指标、日志、健康检查与告警

这一模块的目标不是把本地教学项目伪装成生产平台，而是先建立一套能继续接线、边界诚实的可观测性基础。实现位于 `backend/app/observability/`，默认只服务当前本机进程，不会主动连接 Prometheus、告警平台、Jenkins、GitLab 或蓝盾。

## 已实现的能力

### 健康检查

| 路径 | 用途 | 是否访问数据库 | 失败语义 |
| --- | --- | --- | --- |
| `/health/live` | 判断 Python 进程和 ASGI 事件循环是否还活着 | 否 | 进程无法响应时由部署平台重启 |
| `/health/ready` | 判断实例是否可以接收业务流量 | 是，对已选择且通过边界校验的 SQLite 或 PostgreSQL 执行 `SELECT 1` | 返回 HTTP 503，应从流量入口摘除但不一定重启 |

readiness 复用应用的异步 SQLAlchemy `Database`。它只接受默认 `sqlite_local`，或 `postgres_local_container + APP_ENV=local-container + postgresql+asyncpg@postgres:5432` 的内部容器组合；其他 backend/拓扑在连接前失败。探针查询带有超时，响应不会暴露数据库路径、SQL 错误或凭据。

### Prometheus 指标

默认地址为 `/metrics`，默认在当前本机教学环境开启。设置 `metrics_enabled=false` 后，中间件停止采集且端点返回 404。

HTTP 指标：

- `qa_http_requests_total{method,route,status_code}`：完成的请求数。
- `qa_http_request_duration_seconds{method,route}`：请求延迟直方图。
- `qa_http_requests_in_flight`：当前处理中请求数，不使用标签。

业务指标：

- `qa_automation_tasks{state}`：任务队列各生命周期状态数量。
- `qa_devices{state}`：设备各可用状态数量。
- `qa_provider_requests_total{provider,operation,outcome}`：CI Provider 操作结果。
- `qa_provider_request_duration_seconds{provider,operation}`：CI Provider 操作耗时。

`/metrics` 自身不计入 HTTP 指标，避免抓取行为污染请求量。每个 FastAPI 应用实例使用独立 `CollectorRegistry`，所以应用工厂测试、多次创建应用时不会发生全局重复注册。

#### 低基数约束

指标只接受固定枚举值。以下内容不得成为 Prometheus 标签：

- 用户、任务、设备、项目、流水线或缺陷 ID；
- Jenkins/GitLab/蓝盾地址、Job 名、分支名；
- URL 原始路径、查询参数、异常消息；
- 文件名、用户名、IP 地址、Request ID。

HTTP 的 `route` 使用 `/items/{item_id}` 这样的路由模板，而不是用户请求的真实路径。未匹配请求统一记为 `_unmatched`。这能避免攻击者通过不同 URL 制造无限时序。

### 结构化日志与 Request ID

请求中间件会：

1. 接受满足安全字符与长度限制的 `X-Request-ID`，否则生成随机 ID；
2. 将 ID 放到 `request.state.request_id` 和上下文变量；
3. 在响应中返回 `X-Request-ID`；
4. 请求结束后输出一行 JSON。

请求日志只包含事件名、Request ID、HTTP 方法、路由模板、状态码和耗时。它不会记录请求体、Cookie、Authorization、查询字符串或具体资源路径。`JsonLogFormatter` 可供其他业务 logger 复用。

## 应用接线

先在后端依赖中固定版本：

```text
prometheus-client==0.26.0
```

然后在应用工厂创建 `Database` 后安装中间件，并直接挂载可观测性路由。不要给它加 `/api/v1` 前缀，这样容器和 Prometheus 可以使用常见的探针路径。

```python
from app.observability import install_observability, observability_router

database = Database(current_settings.database_url)
application.state.container = build_container(database, current_settings)

install_observability(
    application,
    database=database,
    settings=current_settings,
)
application.include_router(observability_router)
```

共享 `Settings` 已包含以下字段，并在 `from_environment()` 中使用项目现有的严格布尔解析函数读取环境变量：

```python
metrics_enabled: bool = True
request_logging_enabled: bool = True
```

对应环境变量是 `METRICS_ENABLED` 和 `REQUEST_LOGGING_ENABLED`，两项默认开启。关闭指标后 `/metrics` 返回 404；关闭请求日志不影响 Request ID 和指标采集。

业务服务可以通过 `application.state.observability.metrics.business` 更新指标。例如，在完成一次数据库聚合后整体刷新快照，缺失状态会自动归零：

```python
business = application.state.observability.metrics.business
business.set_task_snapshot({"queued": 8, "running": 2, "dead_letter": 1})
business.set_device_snapshot({"idle": 4, "leased": 2, "unhealthy": 0})
```

Provider 调用应在 Service/Adapter 边界使用单调时钟计时，并且无论成功、失败、拒绝还是超时都只提交固定枚举标签：

```python
business.observe_provider_request(
    provider="jenkins",
    operation="trigger",
    outcome="succeeded",
    duration_seconds=elapsed,
)
```

不要在 Router 中按记录逐个更新任务/设备 Gauge。推荐由后台刷新器一次查询并提交聚合快照；单进程教学版也可以在成功写入任务或租约后刷新。

## Prometheus 与告警建议

在同一宿主机直接运行 Prometheus 时，最小抓取配置可以指向后端环回地址：

```yaml
scrape_configs:
  - job_name: qa-platform-learning
    static_configs:
      - targets: ["127.0.0.1:23100"]
```

仓库 `infra/compose.phase2.yaml` 已让 Prometheus 通过内部网络抓取 `backend:23100`，并包含示例告警规则和使用空接收器的 Alertmanager。内部网络没有公网出口，Alertmanager 也不会向邮件或机器人发送通知。

推荐从以下信号开始学习告警：

```promql
# 5 分钟 5xx 比例超过 5%，并在告警规则中再增加最小请求量条件
sum(rate(qa_http_requests_total{status_code=~"5.."}[5m]))
/
clamp_min(sum(rate(qa_http_requests_total[5m])), 1)
> 0.05

# P95 延迟超过 1 秒
histogram_quantile(
  0.95,
  sum by (le) (rate(qa_http_request_duration_seconds_bucket[5m]))
) > 1

# 出现死信任务
qa_automation_tasks{state="dead_letter"} > 0

# 出现不健康设备
qa_devices{state="unhealthy"} > 0

# Provider 5 分钟失败或超时增量
sum(increase(qa_provider_requests_total{outcome=~"failed|timeout"}[5m])) > 0
```

告警必须同时设置 `for` 持续时间、流量下限和恢复通知，否则教学环境中的一次故意失败就会造成告警噪音。队列堆积更适合观察一段时间内的增长趋势，而不是只对瞬时绝对值报警。

## 生产边界

- 默认数据库仍是本机 SQLite；可选 PostgreSQL 仅是自建 Compose 内部容器适配。两种模式都仍按单进程学习设计，不代表多副本生产部署已经成立。
- 当前机器没有 Docker，PostgreSQL readiness 尚未对真实容器运行；现有自动化验证覆盖双 backend 分支和 PostgreSQL 离线方言，不应写成已完成实机联调。
- 默认 Prometheus Registry 是单进程模式。若未来使用多个 Worker，需要按 `prometheus_client` 官方多进程模式重新设计，或者先保持一个 Worker；不能直接把多个进程的 `/metrics` 当成同一实例。
- `/metrics` 不包含业务明细，但仍应只暴露给监控网络。生产环境应由反向代理、网络策略或独立监听端口限制访问，不要依赖“路径难猜”。
- liveness 不应检查数据库或外部 Provider，否则外部故障会触发无意义的重启风暴。
- readiness 只检查本实例的必要依赖。Jenkins、GitLab 和蓝盾连通性应作为独立业务指标或定时检查，不能放进每次 readiness。
- JSON 日志应写到标准输出，由容器平台采集；日志平台需要配置保留期、访问权限和脱敏策略。
- 本模块只提供信号。仓库的 Prometheus/Alertmanager 是本机演示配置；真实通知、Grafana 仪表盘、高可用、容量评估、长期保留和应急流程仍属于后续生产化工作。

## 验证

定向测试不会使用全局 Prometheus Registry，也不会发起网络请求：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_observability.py -q
```

测试覆盖路由模板聚合、Request ID、结构化日志、指标开关、SQLite/PostgreSQL readiness 分支、未知 backend 拒绝、业务标签枚举和多应用 Registry 隔离。真实 PostgreSQL 容器的启动、迁移、断连恢复和探针超时仍需在具备 Docker 的环境补做。
