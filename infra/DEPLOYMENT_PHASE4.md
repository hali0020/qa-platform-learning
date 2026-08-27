# 第四阶段本机对象存储拓扑

本说明只用于完全自建的本机 Docker Compose 学习环境。后端通过 `qa-platform-learning-object-storage-internal` 连接 S3-only 网关 `http://seaweedfs:8333`；真正的 SeaweedFS 位于 `qa-platform-learning-object-storage-core-internal`，只与网关共享该网络。Bucket 固定为 `qa-artifacts`，Compose 不向宿主机发布任何对象存储端口。

固定启动参数会关闭上游遥测（`-master.telemetry=false`），也禁用本项目不需要的 WebDAV、Admin UI、Iceberg、Lance 和嵌入式 IAM API。Master、Filer、Volume 和 `mini` 仍会启动的 Admin/Worker 监听只存在于核心网络；后端不加入该网络，只能通过固定转发 `seaweedfs-core:8333` 的网关访问 S3。

默认启动不会创建 SeaweedFS，也不会创建 S3 客户端。只有同时满足以下三项才进入对象存储练习：

1. 启动命令包含 `--profile object-storage`；
2. 被 Git 忽略的 `.env` 显式选择 `s3_local_container`、内部 endpoint 和固定 Bucket；
3. `.env` 提供两段不同的本机专用凭据。

## 镜像来源与固定方式

服务使用上游维护的多平台镜像：

```text
chrislusf/seaweedfs:4.44@sha256:e67e8c385484120b78bff47ba5f4debbca47fbd27ed1a39f016f47e8baea615b
```

[Docker Hub 的 4.44 镜像详情](https://hub.docker.com/layers/chrislusf/seaweedfs/4.44/images/sha256-ffd20a68489a7d818ea8c5a7d78de6121fc53038976c63c7d15c56a3e6143262)明确显示这个完整 index digest；对应上游源码提交为 [`3563738699f29fd1c9efde6fcbf4ba253439cac8`](https://github.com/seaweedfs/seaweedfs/commit/3563738699f29fd1c9efde6fcbf4ba253439cac8)。tag 和 digest 必须同时保留，不能改成 `latest`，也不能使用未核验的第三方重打包镜像。

S3-only 网关基于 NGINX 官方 unprivileged 镜像，同样固定为 `1.30.4-alpine3.24@sha256:93722936b82ec8a1178d48448e619226680d2de3706a1640800e186cd5fa7fd3`。完整 index digest 来自[上游 GHCR 包版本页](https://github.com/nginx/docker-nginx-unprivileged/pkgs/container/nginx-unprivileged/versions?filters%5Bversion_type%5D=tagged)，不能改成浮动 `stable` 或 `latest`。

在允许访问公共 Docker Hub 的个人学习机器上，可以额外核对：

```powershell
docker buildx imagetools inspect chrislusf/seaweedfs:4.44
```

期望顶层 manifest list digest 与 Compose 完全一致。这个命令和首次拉取镜像会访问公共 Docker Hub；它不应通过公司镜像仓库、公司代理或公司凭据执行。如果学习环境不允许访问公共 registry，应由个人可信缓存提供同一 digest，并在导入前核对 SHA-256。

## 准备被忽略的本机配置

在仓库根目录执行：

```powershell
Copy-Item .env.example .env
```

使用密码管理器生成两段仅供本项目使用的随机值，不复用 PostgreSQL、RabbitMQ、个人 AWS 或任何公司账号凭据。修改 `.env`：

```dotenv
OBJECT_STORAGE_ACCESS_KEY=<本机实验Access Key>
OBJECT_STORAGE_SECRET_KEY=<本机实验Secret Key>
COMPOSE_OBJECT_STORAGE_RUNTIME_MODE=s3_local_container
COMPOSE_OBJECT_STORAGE_ENDPOINT_URL=http://seaweedfs:8333
COMPOSE_OBJECT_STORAGE_BUCKET=qa-artifacts
```

宿主机源码模式继续保留无 S3 配置的默认值：

```dotenv
OBJECT_STORAGE_RUNTIME_MODE=local_filesystem
OBJECT_STORAGE_ENDPOINT_URL=
OBJECT_STORAGE_BUCKET=
```

`qa-artifacts` 不是任意可选 Bucket：Compose 只在显式 S3 模式下把 `COMPOSE_OBJECT_STORAGE_BUCKET` 注入后端，后端仍会拒绝其他值。把 Bucket 留在默认宿主变量中反而会形成 dormant S3 配置，所以 `local_filesystem` 模式必须保持为空。

Compose 会把同一组凭据作为后端 S3 客户端配置，并映射为 SeaweedFS `weed mini` 使用的 `AWS_ACCESS_KEY_ID` 与 `AWS_SECRET_ACCESS_KEY`。根据[上游 Quick Start](https://github.com/seaweedfs/seaweedfs)，缺少这两个变量时 S3 会以匿名 `Allow All` 启动；本项目的容器入口会在任意一个值为空时直接退出，绝不回退为匿名服务。

这些凭据仍是容器环境变量，会出现在展开后的 Compose 配置、`docker inspect` 和某些诊断采集中。不要运行或分享会打印完整配置的命令，不要提交 `.env`，日志也不得输出 endpoint 完整认证信息。阶段五再改为 Vault/Secret Manager 和可轮换的短期凭据。

## 启动

在仓库根目录执行：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile object-storage up --build
```

这会同时启动默认 Web 拓扑和 SeaweedFS。后端等待可选 SeaweedFS TCP 健康检查通过，但 TCP 可达不等于 S3 鉴权、Bucket 和读写语义全部正确；真实练习还应通过应用的对象存储契约测试验证 Put/Get/Head/Delete。

可以使用不打印容器环境的命令观察状态：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile object-storage ps
docker compose --env-file .env -f infra/compose.phase2.yaml --profile object-storage logs --tail 100 seaweedfs-core seaweedfs
```

不要为排障增加 `8333:8333`、`23646:23646` 或其他端口映射。浏览器和宿主机工具不需要直接访问 S3，后端通过内部 DNS 名 `seaweedfs` 调用。

## 容器与数据边界

- 持久内容只写入本项目命名卷 `seaweedfs-data:/data`，没有宿主机 bind mount、AWS 配置目录或公司共享盘。
- SeaweedFS 核心不加入应用默认网络或后端所在的对象存储客户端网络。S3-only 网关是两个内部网络之间唯一的共同成员，只固定转发到 `seaweedfs-core:8333`；遥测及不需要的附加功能在进程参数层面禁用，不能只依赖网络阻断。
- 容器根文件系统只读，临时文件只允许进入受限 tmpfs。
- 固定到 4.44 的上游 `/entrypoint.sh` 会先修正命名卷所有权，再通过 `su-exec` 降到 `seaweed` 用户；Compose 只保留启动所需的 `CHOWN/SETUID/SETGID`，删除其余 capabilities，并启用只读根文件系统与 `no-new-privileges`。当前只核对了固定版本源码，仍需实跑确认进程 UID/GID 和卷权限。
- `AWS_EC2_METADATA_DISABLED=true` 阻止后端 S3 SDK 尝试云实例元数据；配置不包含 role、web identity、外部 IAM、云端 tiering 或远程备份目标。
- 内部网络设置为 `internal: true`。镜像下载发生在启动前，运行中的容器没有默认公网出口。
- `qa-artifacts` 是唯一教学 Bucket；业务 API 不接受调用者传入其他 Bucket、远程 endpoint 或完整对象 URL。

## 停止与重置

普通停止保留对象卷：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile object-storage down
```

`down --volumes` 会不可恢复地删除 SQLite/PostgreSQL、RabbitMQ、SeaweedFS 和监控数据，因此不作为普通命令提供。确需重置时，应先确认项目名与 `seaweedfs-data` 卷、导出仍需保留的本机实验对象，并把删除视为显式的数据销毁操作。

## 建议故障练习

1. 不填写任一凭据，确认 SeaweedFS 失败关闭而不是匿名启动。
2. 保持 profile 关闭，确认平台继续使用 `local_filesystem` 且没有 S3 网络调用。
3. 只启动 `seaweedfs` 服务而不切换后端，理解“服务存在”和“业务选择它”是两个独立开关；完整 Web 拓扑会拒绝把 S3 凭据作为 dormant 配置留在 `local_filesystem` 模式。
4. 分别停止 `seaweedfs-core` 和 S3-only 网关 `seaweedfs`，观察有界超时、错误脱敏和 API 行为，再恢复服务。
5. 重复上传、读取和删除同一对象，验证对象键、SHA-256、流式 Body 关闭与幂等语义。
6. 在保留关系数据库但清空对象卷的副本环境中练习缺失对象检测；反向练习孤儿对象扫描。

## 静态验证

不打印展开配置的 Compose 语法检查：

```powershell
docker compose --env-file .env -f infra/compose.phase2.yaml --profile object-storage config --quiet
```

仓库侧边界测试：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_container_boundary.py -q
```

当前维护机器没有可用 Docker 运行时，所以只完成 YAML、Nginx 配置、应用配置和 Python 边界的静态验证，**没有声称已实际拉取或启动 SeaweedFS 与 S3-only 网关**。在进入下一阶段前，还必须在拥有 Docker 的隔离个人环境验证镜像架构、健康检查、SigV4 Host 透传、卷权限、最小权限用户、对象读写、重启恢复与网络不可达故障。

## 上生产前仍缺少什么

- Vault/Secret Manager、凭据轮换和最小权限身份；
- TLS、入口鉴权、网络 egress policy 和审计；
- 病毒扫描、对象配额、生命周期、保留策略和安全删除；
- 数据库元数据与对象内容的 outbox、补偿和孤儿回收；
- 备份/恢复、容量和性能测试、跨版本升级回滚；
- 多节点复制、高可用和多实例并发验证；
- SBOM、签名验证、持续漏洞扫描和镜像准入策略。

本 Compose 是单节点本机教学拓扑，不是生产 SeaweedFS 部署模板。
