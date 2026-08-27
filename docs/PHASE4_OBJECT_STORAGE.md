# 阶段四：对象存储与 Artifact

这一阶段不是为了学会某个厂商控制台，而是把“附件内容放在哪里”从业务逻辑中拆出去，练习可迁移的对象存储能力：异步 S3 HTTP、流式 I/O、摘要校验、失败补偿、生命周期和安全边界。

## 本阶段结论

- 默认仍是 `local_filesystem`，源码启动和普通 Compose 启动不会连接 S3。
- 可选模式只有 `s3_local_container`，并且只允许 `APP_ENV=local-container` 下的精确地址 `http://seaweedfs:8333`。
- Bucket 固定为 `qa-artifacts`。数据库保存附件归属、对象键、大小、MIME 和摘要；SeaweedFS 只保存对象内容。
- Compose 的 `object-storage` profile 使用单节点 SeaweedFS，只挂载本项目命名卷。后端只连接 S3-only 网关；SeaweedFS 核心位于第二层内部网络，不直接与后端共享网络，也不发布任何端口。
- 启动参数显式设置 `-master.telemetry=false`，因此进程不会尝试连接上游遥测服务；WebDAV、Admin UI、Iceberg、Lance 和嵌入式 IAM API 也被禁用。SeaweedFS `mini` 仍会在最内层启动运行所需的 Master、Filer、Volume 和 Admin/Worker 监听，S3-only 网关用于把这些端口与后端隔开。
- 任何公司 S3、AWS 账号、云端 tiering、外部 IAM 或宿主机默认凭据链都不在本阶段范围内。

## 为什么选择 SeaweedFS

截至 2026-08-27，SeaweedFS 仍在维护，采用 Apache-2.0 许可证，并由上游文档直接提供 `weed mini` 的单节点 S3 学习方式。上游说明 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` 和 `S3_BUCKET` 可创建静态凭据与初始 Bucket；缺少凭据时会进入匿名 `Allow All`，所以本项目在启动脚本中额外做非空校验并失败关闭。

一手资料：

- [SeaweedFS 官方仓库与 `weed mini` 说明](https://github.com/seaweedfs/seaweedfs)
- [SeaweedFS S3 API 兼容矩阵](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)
- [SeaweedFS 4.44 发布](https://github.com/seaweedfs/seaweedfs/releases/tag/4.44)

SeaweedFS 适合学习核心 Bucket/Object、multipart 和常见 S3 请求，但它不是 AWS S3 的完整复制品。Bucket notification、网站托管以及部分云厂商扩展不能因为“协议兼容”就假定存在；每项需要用契约测试确认。

### 为什么不继续默认使用 MinIO CE

[MinIO CE 官方仓库](https://github.com/minio/minio)已于 2026-04-25 归档并明确不再维护；[官方 Docker Hub](https://hub.docker.com/r/minio/minio/tags)的 `latest` 停在 2025-09-07，早于修复高危 CVE-2025-62506 的[最终源码发布](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z)。该漏洞的[官方安全公告](https://github.com/minio/minio/security/advisories/GHSA-jjjj-jwhf-8rgr)说明没有 workaround。

因此 Compose 不包含 MinIO 镜像。以后若做历史兼容实验，也只能在额外隔离环境中从最终安全源码标签自行构建，不能使用旧 `latest`、历史二进制或来源不明的第三方重打包镜像。

## 镜像固定与可复现性

Compose 使用：

```text
chrislusf/seaweedfs:4.44@sha256:e67e8c385484120b78bff47ba5f4debbca47fbd27ed1a39f016f47e8baea615b
```

这里的 digest 是 Docker Hub 为 `4.44` 展示的多平台 **index digest**，不是从短摘要猜出的值。可核验来源：

- [Docker Hub 4.44 镜像详情](https://hub.docker.com/layers/chrislusf/seaweedfs/4.44/images/sha256-ffd20a68489a7d818ea8c5a7d78de6121fc53038976c63c7d15c56a3e6143262)
- [4.44 对应上游提交 `3563738699f29fd1c9efde6fcbf4ba253439cac8`](https://github.com/seaweedfs/seaweedfs/commit/3563738699f29fd1c9efde6fcbf4ba253439cac8)

精确 tag 便于人读，index digest 防止 tag 后续被重新指向其他内容。升级时必须重新核对上游发布、完整 digest、许可证、安全公告和本项目契约测试，不能自动追随 `latest`。源码 commit 可以支持审计，但上游构建过程仍包含随时间变化的软件仓库操作，所以“同一源码 commit”本身不代表字节级可复现；自行构建时还要固定 builder、基础镜像和依赖来源。

S3-only 网关同样固定为 NGINX 官方 GHCR 包的 `1.30.4-alpine3.24` 与完整 index digest `sha256:93722936b82ec8a1178d48448e619226680d2de3706a1640800e186cd5fa7fd3`，来源见[上游包版本页](https://github.com/nginx/docker-nginx-unprivileged/pkgs/container/nginx-unprivileged/versions?filters%5Bversion_type%5D=tagged)。它不使用浮动 `stable` 或 `latest`。

## 两种运行模式

| 模式 | 适用场景 | 网络行为 | 数据位置 |
| --- | --- | --- | --- |
| `local_filesystem` | 默认开发、单元测试、最小学习闭环 | 不创建 S3 客户端 | `.data/uploads` 或 Compose 的 `qa-data` |
| `s3_local_container` | 显式对象存储练习 | 只访问内部 S3-only 网关 `http://seaweedfs:8333` | `seaweedfs-data` 命名卷 |

Storage Port 保持同一组业务语义：保存、单次异步读取、删除，以及删除前的隔离/失败恢复。业务层只使用受控对象键，不接受用户提供完整路径、URL、Bucket 或任意 S3 参数。

## 异步 HTTP 学习点

Python 3.10 的 S3 适配器使用异步客户端和生命周期管理，而不是在事件循环里直接调用同步 `boto3`：

1. 第一次 S3 操作时惰性创建客户端上下文，应用退出时有界关闭连接池；默认文件模式不会导入或构造 S3 SDK。
2. 上传先复用本机适配器完成大小、MIME、图片重编码和 SHA-256 校验，再从受上限约束的暂存文件按 5 MiB 顺序分块执行 multipart；失败或取消会在有界清理中 abort，并尝试删除不确定完成的对象。当前仍是单请求内顺序分块，后续容量练习再评估并行 part、断点续传和专用上传状态表。
3. 下载使用单次异步流，必须消费或关闭响应 Body，连接才会返回连接池。
4. 连接、读取和每次 SDK 操作设置有界超时；重试次数有限，不能把永久故障变成无限等待。
5. S3 endpoint、region、path-style、凭据和 Bucket 全部显式设置，不读取 AWS 配置目录或云实例元数据。
6. `404/NoSuchKey`、认证失败、超时和服务不可用应转换成稳定的 Storage 错误，不把 SDK 异常正文或签名信息返回浏览器。

当前 Python 3.10 依赖固定为 `aiobotocore==3.9.0` 与其兼容范围内的 `botocore==1.43.56`。`aiobotocore` 是社区维护的 asyncio 适配层，底层 `botocore` 由 AWS 维护；两者必须同步升级并锁定传递依赖。它适合本项目学习真正的异步 HTTP，但不能被描述成 AWS 官方异步 Python SDK。

## 数据库与对象存储不是同一个事务

以下操作不能通过一个 SQL 事务原子完成：

```text
写对象 ──成功──> 写附件元数据
  │                  │
  └─失败              └─失败后可能留下孤儿对象
```

当前学习重点是显式承认这条边界，并保证错误可观察、对象键可重复计算、删除幂等。生产演进需要：

- 附件 `pending/ready/failed` 状态；
- 事务 outbox 与后台 finalize；
- 定时扫描和回收孤儿对象；
- 以 SHA-256、大小和对象元数据做完整性校验；
- 删除墓碑、保留期与失败重试；
- 数据库和对象卷分别备份，并做成对恢复演练。

迁移 `20260827_0007` 会为旧附件回填 `local_filesystem` 路由。反向降级会删除这两个路由字段，因此一旦存在 `s3_local_container` 附件，迁移会在任何 DDL 执行前拒绝降级；PostgreSQL 离线 SQL 也包含同样的执行时保护。必须先完成经过校验的对象回迁或保留路由备份，不能用降级命令静默把 S3 对象误标为本地文件。

## 安全边界

- `OBJECT_STORAGE_ACCESS_KEY` 和 `OBJECT_STORAGE_SECRET_KEY` 只放入被 Git 忽略的根目录 `.env`，不得复用数据库、Broker 或个人云账号密码。
- SeaweedFS 核心和后端容器从环境变量获得同一组本机实验凭据；S3-only 网关不接收凭据，只保留签名所需的原始 `Host` 请求头。`docker compose config`、`docker inspect`、故障采集和某些进程工具可能显示明文；不要提交、截图或分享这些输出。
- Compose 明确设置 `AWS_EC2_METADATA_DISABLED=true`，不挂载 `~/.aws`，也不配置 role、web identity、云端 tier 或外部 IAM。
- 默认网络、对象存储客户端网络和对象存储核心网络都是 `internal: true`。后端只加入客户端网络，SeaweedFS 核心只加入核心网络，S3-only 网关桥接两者且配置中只有固定上游 `seaweedfs-core:8333`。`expose` 不被当作防火墙；真正的边界来自双层网络、单端口代理、禁用非必要功能和不配置任何 `ports`。不要为了调试临时增加 `0.0.0.0:8333` 或管理端口映射。
- MIME、文件名和图片内容始终不可信；下载使用安全响应头，上传继续执行大小、像素、格式和摘要校验。
- 初期不把预签名 URL 返回浏览器，避免内部主机名不可解析以及签名 query 泄露；由后端按权限流式传输。
- 阶段五再接入 Vault/Secret Manager、短期凭据和轮换；当前 `.env` 方案只是本机教学折中。

## 当前验证边界

本阶段已经提供配置和容器边界的静态测试，但当前开发机没有可用 Docker 运行时，**没有实际启动 SeaweedFS 4.44 或 S3-only Nginx 网关**，也没有验证镜像拉取、健康检查、SigV4 透传、卷权限或断线恢复。固定版本的源码入口会用 `CHOWN/SETUID/SETGID` 修正命名卷后降到专用 `seaweed` 用户；Compose 删除其余 capabilities 并启用 `no-new-privileges`。这是源码级核对，不等于容器行为已经实跑验收。拥有 Docker 的学习机器必须按 [DEPLOYMENT_PHASE4.md](../infra/DEPLOYMENT_PHASE4.md) 补做真实启动、进程身份和故障练习。

当前仍是单节点、单机命名卷：没有复制、高可用、容量治理、病毒扫描、对象生命周期、备份恢复、跨存储数据搬迁或多实例并发保证，不能当作生产对象存储方案。
