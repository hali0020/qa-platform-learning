# 第六阶段：本机 Learning CI Lab

## 0. 这一阶段解决什么

Learning CI 是本仓库自己实现的异步 HTTP 教学服务，用来练习“QA 平台如何触发
一个 CI 运行、按幂等键去重、轮询状态和取消运行”。它不是 Jenkins、GitLab CI
或蓝盾 BK-CI，也不会搜索、导入或连接公司已有系统。

默认 `docker compose up` 不会启动它，backend 仍是无网络的 `local_lab`。只有
`ci-lab` profile 与 `COMPOSE_PROVIDER_RUNTIME_MODE=ci_lab_local` 同时显式启用时，
Learning CI 客户端才可以访问一个代码中固定的目标：

```text
浏览器 ── 127.0.0.1:23010 ── QA frontend/backend
                                      │
                                      │ 仅 172.30.60.2/32:8080
                                      ▼
                         Learning CI + /data/ci-lab.db
                                      │
                         127.0.0.1:23020（教学直看）
```

CI Lab 只加入 `172.30.60.0/28` 独立内部网络，固定地址为
`172.30.60.2`；backend 只额外加入这一条专网。CI Lab 不加入 default、对象存储、
身份或 Secret 网络。宿主机端口只绑定环回地址，不能被局域网访问。
frontend Nginx 不提供 `/ci-lab`、`/__ci_lab` 或其他 Lab 代理路径；`23010` 只暴露
QA 平台，教学直看必须显式访问独立的 `127.0.0.1:23020`。

这是单机故障与协议实验，不是高可用部署。CI Lab、入口、数据库和 Docker 宿主机
仍可能成为单点。

## 1. 准备本机配置

在仓库根目录复制示例文件：

```powershell
Copy-Item .env.example .env
```

保持以下默认值，不要把模式永久改成通用 `self_hosted_lab`：

```dotenv
COMPOSE_PROVIDER_RUNTIME_MODE=local_lab
COMPOSE_PROVIDER_SECRET_ENV_ALLOWLIST=
QA_PROVIDER_SECRET_CI_LAB=
QA_PROVIDER_SECRET_CI_LAB_WEBHOOK=
```

推荐由启动脚本为每次本机实验生成两个相互独立的 256 bit 随机 Secret：出站
Bearer Token 与入站 Webhook HMAC key。它们只在脚本的进程
环境中短暂存在：CI Lab 得到只读
`/run/secrets/ci_lab_machine_token`，backend 因当前 Secret Store 启动边界限制，
通过严格白名单的 `QA_PROVIDER_SECRET_CI_LAB` 环境变量得到同一个值；Webhook key
使用 `QA_PROVIDER_SECRET_CI_LAB_WEBHOOK`，不得与 Bearer Token 复用。

因此 backend 的容器 metadata 仍可能包含这个教学 Token。不要运行会展开环境值的
`docker compose config`，也不要提交 `.env`、粘贴 `docker inspect`、截图或复用该
Token。后续可把 backend 一侧也改成运行时 Secret 文件/Vault 引导。

如果 `172.30.60.0/28` 与个人 Docker/VPN 地址池冲突，Docker 会拒绝创建网络。
不要把它临时改成公司网段；应先停止实验并在隔离的个人 Docker 环境处理地址池。

## 2. 启动

从仓库根目录运行：

```powershell
.\scripts\start-ci-lab.ps1
```

脚本会完成以下动作：

1. 确认 `.env`、Docker 和 CI Lab 源码存在；
2. 在内存中生成不输出、相互独立的随机机器 Token 与 Webhook Secret；
3. 仅对本次 Compose 调用设置 `ci_lab_local` 和精确 Secret 名；
4. 使用 `config --quiet` 做静态配置检查；
5. 构建并重建 `ci-lab`、`backend` 和 `frontend`，使本轮机器 Token 在两个消费端
   保持一致，并把独立 Webhook Secret 只交给 QA 接收端；命名卷数据不会因重建容器
   而删除；
6. 恢复调用者原来的进程环境变量。

首次构建会从公开 Python/Docker 软件源下载项目声明的依赖和基础镜像。运行后的
Compose 网络是 `internal: true`，不会借此连接任何公司 CI、数据库或身份系统。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:23020/health/live
Invoke-RestMethod http://127.0.0.1:23010/health/ready
```

`/health/live` 是 CI Lab 唯一无需 Bearer Token 的接口。不要为排查临时关闭其他
接口的机器鉴权，也不要开启会记录 Authorization、请求体或查询参数的 access log。

当前维护机器没有 Docker，因此仓库只完成了 YAML、Dockerfile、PowerShell 与
Python 契约的静态验证，**没有声称镜像已经构建或容器已经启动**。

## 3. 在 QA 平台创建固定连接

打开 `http://127.0.0.1:23010/`，进入“集成与 CI”页面，创建连接：

| 字段 | 值 |
| --- | --- |
| 名称 | `Learning CI Lab` |
| Provider 类型 | `learning_ci` |
| CI Lab 固定定义 | `local-quality-gate` |
| Base URL | 留空；地址由服务端固定 |
| Config | 空对象 |
| Secret 引用 | `QA_PROVIDER_SECRET_CI_LAB` |
| Webhook Secret 引用 | 需要练习接收时填写 `QA_PROVIDER_SECRET_CI_LAB_WEBHOOK` |
| Enabled | 开启 |

连接表只保存 Secret 的变量名，不保存 Token。`base_url` 也必须为空；试图改成任意
URL、host、端口或 CIDR 都应被后端拒绝。

触发运行时填写普通分支/变量，并使用唯一 `correlation_id`。前端会生成安全的随机
值；HTTP 客户端把它转换为 CI Lab 的 `Idempotency-Key`。相同键与相同请求应返回
同一运行，不同请求复用同一键应被拒绝。

Lab 与平台之间只有定义读取和三种运行操作需要机器鉴权：

```text
GET  /api/v1/definitions
POST /api/v1/definitions/{definition}/runs
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/cancel
POST /api/v1/runs/{run_id}/gate-decisions
```

没有“任意 URL 请求”“执行 Shell”“动态 import”“公司项目发现”或凭据回显接口。
Lab 也不启动后台 Executor：运行依据持久化的 `created_at` 与源码中固定的定义时间线，
在查询、幂等重放或取消时确定性物化。`local-quality-gate` 在固定测试步骤后进入
`waiting_approval`，由显式机器 gate-decision 接口批准后成功或拒绝后失败；
`local-failure-demo` 确定失败。状态与审批持久化，因此重启后可以恢复，同一运行不会
因时钟回拨退回旧状态，也没有绕过门禁的隐藏教学控制端点。

## 4. 建议练习

按顺序验证：

1. 不带 `--profile ci-lab` 启动，确认 CI Lab 不存在且 runtime mode 为
   `local_lab`。
2. 使用脚本启动，确认只有 `127.0.0.1:23020` 暴露 Lab，容器目标固定为
   `172.30.60.2:8080`。
3. 创建 `learning_ci` 连接，触发一次带唯一 correlation ID 的运行；先观察 QA 只写
   Run/Intent。这个脚本的 SQLite 教学拓扑使用页面/API 手工 dispatch；只有另行启用
   PostgreSQL `provider-dispatcher` profile 时才由独立进程自动 claim 并执行 HTTP。
4. 重复相同触发，观察幂等 replay；修改参数后复用相同 ID，观察冲突。
5. 查询运行，再取消一个尚未结束的运行，确认终态不会被反向覆盖。
6. 分别触发 `local-quality-gate` 和 `local-failure-demo`；前者应停在
   `waiting_approval`，验证触发人不能自批、第二人可幂等批准/拒绝，Webhook 不能绕过。
7. 上传 JSON/JUnit XML 测试报告或 Artifact，观察 pending、摘要、补偿和审计；用
   测试客户端练习独立签名 Webhook 的 duplicate/stale/gap/终态回退。
8. 重启 CI Lab，确认独立 `ci-lab-data` 卷中的运行历史仍能读取。
9. 停止 CI Lab 后触发或查询，观察有界超时和脱敏错误；恢复服务后重试。
10. 把 runtime mode 改回 `local_lab`，确认即使容器仍在运行，backend 也拒绝网络
   Provider 操作。

数据库持久化、HTTP 幂等和运行状态是三个不同层次：数据库保存运行不代表消息
“恰好一次”，租约或重试也不能自动保证外部副作用恰好一次。真正接入自建 CI
产品时仍需稳定 correlation ID、服务端唯一约束、回调去重和补偿流程。

## 5. 停止与数据边界

停止进程但保留独立数据卷：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile ci-lab stop ci-lab backend frontend
```

普通 `stop` 或 `down` 不会删除 `ci-lab-data`。本说明不提供常规
`down --volumes` 命令，因为它会同时不可恢复地删除其他本机实验卷。确需清空 CI
Lab 时，应先核对完整 Compose project 和卷名、导出仍需保留的教学运行，再把删除
作为单独的显式数据销毁操作。

## 6. 还没有完成什么

- 没有真实 Jenkins、GitLab、蓝盾或公司环境联调；
- CI Lab 没有主动 webhook delivery Worker；当前只验证 QA 端独立签名接收；
- 没有真正的源码拉取、构建 Executor 或部署；QA 平台已有 Run Artifact，但不是
  完整制品仓库/构建产物发布系统；
- 没有 TLS、镜像签名、SBOM、漏洞扫描或不可篡改审计；
- 没有多实例 CI Lab、数据库复制、备份恢复验收或跨宿主机高可用；
- 没有在当前机器执行 Docker 构建、真实 PostgreSQL/RabbitMQ、网络隔离、多实例、
  重启和故障注入验证；Web 保持单实例，不宣称 HA。

这些限制不妨碍学习可迁移的核心：固定出站边界、异步 HTTP、Bearer 机器身份、
幂等触发、状态归一化、取消、持久化和故障恢复。
