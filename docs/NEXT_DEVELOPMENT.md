# 下次开发必读交接

> 这份文档只用于继续写代码时的交接。按现有教程学习、启动和操作平台时，
> 不要求先阅读本页。

## 开发恢复规则

下次开始开发前，先阅读本页，再查看 `git status`、最近提交和
[阶段六 A 说明](PHASE6_CI_LAB.md)。不要重新连接或探测任何公司 Jenkins、
GitLab、蓝盾、数据库、IdP、Vault、S3 或其他线上系统。

不可改变的边界：

- 默认保持 `local_lab + sqlite_local + disabled_local broker + local_filesystem + local_accounts + env_local`。
- `ci_lab_local` 只允许仓库自有 `learning_ci`，固定宿主机
  `127.0.0.1:23020` 或容器 `172.30.60.2:8080/32`。
- Jenkins/GitLab/BK-CI 真实联调继续关闭；以后也只连接我们自己安装的测试实例。
- 只提交本仓库自行编写的内容；`.env`、Token、数据库、日志、构建产物和依赖目录不得提交。

## 本次已经完成

- 阶段六 A 独立 Learning CI Lab：独立 FastAPI 进程、独立 SQLite、固定不可变
  Definition、Bearer 机器身份、16 KiB 请求上限、幂等 trigger/get/cancel、确定性
  状态推进和重启恢复。
- `learning_ci` Provider 与 `ci_lab_local` 固定目标模式；通用 Host/CIDR/Port、页面
  和数据库都不能覆盖目标。
- HTTP 客户端禁环境代理、重定向和压缩响应，按原始字节限制响应大小。
- Compose `ci-lab` profile、独立 internal 网络、固定 IP、非 root/read-only 镜像、
  本机环回发布和临时机器 Secret 启动脚本。
- 无 Docker 源码脚本使用专用 `.data/ci-lab-source` 数据库，强制关闭其他网络实验
  模式；Token 位于固定本地磁盘的当前用户 `LocalApplicationData` 随机目录，关闭
  ACL 继承并在退出时精确清理。
- 旧流水线快照兼容：新触发只接受字符串变量，但历史 Run 仍可读取数字、布尔和
  对象变量。

## 尚未完成，按此顺序继续

### 1. 阶段六 A 实机补验

- 在个人隔离 Docker 环境构建 `ci-lab` profile，验证固定 IP、容器间真实 HTTP、
  health、停止/重启、Token 轮换和命名卷恢复。
- 不要把“YAML/单测通过”写成“容器已运行通过”。
- 复核 `scripts/start-ci-lab-source.ps1` 的 Windows ACL、端口占用拒绝和 Ctrl+C 清理。

### 2. 阶段六 B

1. 审批记录和不可绕过的质量门禁状态机。
2. 测试报告/Artifact 元数据接入现有 Storage Port，补摘要、pending、补偿和审计。
3. 独立签名 Webhook：时间窗、常量时间 HMAC、事件唯一键、重放保护、乱序处理与
   轮询对账。不要复用当前浏览器 Session/CSRF callback。
4. 把 QA→CI 的外部触发从长数据库事务改成 trigger intent/outbox；CI Lab 的
   `Idempotency-Key` 只能去重，不能消除“远端成功、本地提交失败”的未知结果。

### 3. 阶段六 C

1. 独立 Alembic migration Job，启动顺序为 migration → Web/Worker/Scheduler。
2. 拆出独立 Scheduler，用 PostgreSQL claim/CAS 保证并发 tick，不依赖进程锁。
3. 完成 transactional outbox、RabbitMQ 重复消息、Worker 崩溃/租约过期/Broker
   中断/数据库恢复演练。
4. 在完成真实并发与故障验证前，Web 保持单实例，不宣称高可用。

## 下次开发的最小检查

```powershell
git status --short
.\backend\.venv\Scripts\python.exe -m pytest backend\tests -q
.\backend\.venv\Scripts\python.exe -m compileall -q backend\app backend\tests
.\backend\.venv\Scripts\python.exe -m pip check
cd frontend
pnpm run type-check
pnpm run build
```

随后检查 `git diff --check`、Compose YAML、PowerShell AST 和敏感信息，再形成一个
边界清晰的小提交。
