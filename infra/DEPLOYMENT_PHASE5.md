# 第五阶段本机身份与 Secret 部署说明

## 0. 前置边界

以下命令只面向仓库自己的 `identity-secrets` profile。Keycloak、Vault、数据库、
Broker 和对象存储都没有公司地址。不要把公司 Token、生产密码或现有 Vault
unseal key 放进这个实验。

先从仓库根目录复制本机配置：

```powershell
Copy-Item .env.example .env
```

在被 Git 忽略的 `.env` 中为以下两项生成不同的随机值：

```powershell
[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

```text
KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD=<至少 16 字符的随机值>
VAULT_APP_TOKEN=
```

`VAULT_APP_TOKEN` 此时必须留空，等 Vault 初始化并创建最小权限 token 后再填。
不要把 `.env`、初始化 JSON、真实 Secret JSON 或命令输出提交、截图或粘贴到
Issue。

## 1. 静态查看与构建

```powershell
docker compose -f infra/compose.phase2.yaml config --profiles
docker compose -f infra/compose.phase2.yaml --profile identity-secrets config --services
docker compose -f infra/compose.phase2.yaml --profile identity-secrets build keycloak-core keycloak vault
```

应看到 `identity-secrets`，但不带 `--profile` 的默认启动不会创建 Keycloak 或
Vault。构建仍需要从公开 registry 下载固定 digest 镜像；容器运行后所有网络都
是 `internal: true`，没有互联网出口。

## 2. 先保留本机账号，再创建并显式绑定 Keycloak 用户

此时不要切换 `COMPOSE_AUTH_RUNTIME_MODE`。先以默认 `local_accounts` 启动平台，
通过登录页的一次性初始化创建本机管理员，并创建所有要授权的本机用户：

```powershell
docker compose -f infra/compose.phase2.yaml up -d backend frontend
```

然后单独启动本机 Keycloak：

```powershell
docker compose -f infra/compose.phase2.yaml --profile identity-secrets up -d keycloak-core keycloak
docker compose -f infra/compose.phase2.yaml ps keycloak-core keycloak
```

首次启动会导入 `qa-learning-realm.json`。Keycloak 对已存在 realm 会跳过 startup
import；修改 JSON 不等于自动迁移已有 realm。需要演练重新导入时，应先导出
需要保留的数据、停止 Keycloak，再使用官方 import 流程。不要把“删命名卷”当
成普通升级方式。

平台不会 JIT 创建 OIDC 用户，也不会按 username/email 自动匹配。下面创建一个
Keycloak 用户并保留 `create -i` 返回的稳定用户 ID；该 ID 就是以后 ID token 的
`sub`。Keycloak username 只是登录名，不是平台授权依据。密码放进短暂进程环境，
不会出现在命令参数中；执行后立即清除：

```powershell
$env:KCADM_PASSWORD = Read-Host "Keycloak bootstrap admin password" -MaskInput
docker compose -f infra/compose.phase2.yaml exec -e KCADM_PASSWORD keycloak-core /bin/bash -ec '/opt/keycloak/bin/kcadm.sh config credentials --config /tmp/kcadm.config --server http://127.0.0.1:8080/identity --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KCADM_PASSWORD"'
$keycloakUsername = Read-Host "New Keycloak login username"
if ($keycloakUsername -notmatch '^[A-Za-z][A-Za-z0-9_.-]{2,49}$') { throw "Invalid local lesson username" }
$keycloakSubject = (docker compose -f infra/compose.phase2.yaml exec -T keycloak-core /opt/keycloak/bin/kcadm.sh create users --config /tmp/kcadm.config -r qa-learning -i -s "username=$keycloakUsername" -s enabled=true).Trim()
if ($keycloakSubject -notmatch '^[0-9a-fA-F-]{36}$') { throw "Keycloak did not return one user id" }
$env:KCADM_NEW_PASSWORD = Read-Host "Temporary student password" -MaskInput
docker compose -f infra/compose.phase2.yaml exec -e KCADM_NEW_PASSWORD keycloak-core /bin/bash -ec '/opt/keycloak/bin/kcadm.sh set-password --config /tmp/kcadm.config -r qa-learning --userid "$1" --new-password "$KCADM_NEW_PASSWORD" --temporary' -- "$keycloakSubject"
docker compose -f infra/compose.phase2.yaml exec keycloak-core rm -f /tmp/kcadm.config
Remove-Item Env:KCADM_PASSWORD,Env:KCADM_NEW_PASSWORD
```

在仍为 `local_accounts` 的平台上，以拥有 `users.manage` 的管理员登录。下面的
`WebRequestSession` 同时保存 HttpOnly Session Cookie 和 CSRF Cookie；绑定 POST
还必须带响应中的 CSRF token。选择的本地用户必须已存在且为启用状态：

```powershell
$platformAdminUsername = Read-Host "Local platform administrator username"
$platformAdminPassword = Read-Host "Local platform administrator password" -MaskInput
$localUsername = Read-Host "Existing local platform username to bind"
$qaWebSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
$originHeader = @{ Origin = "http://127.0.0.1:23010" }
$loginBody = @{ username = $platformAdminUsername; password = $platformAdminPassword } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:23010/api/v1/auth/login -WebSession $qaWebSession -Headers $originHeader -ContentType "application/json" -Body $loginBody
$users = Invoke-RestMethod -Method Get -Uri http://127.0.0.1:23010/api/v1/users -WebSession $qaWebSession
$targetUsers = @($users.data | Where-Object { $_.username -eq $localUsername })
if ($targetUsers.Count -ne 1) { throw "Expected exactly one existing local user" }
$bindingHeaders = @{ Origin = "http://127.0.0.1:23010"; "X-CSRF-Token" = $login.data.csrf_token }
$bindingBody = @{ subject = $keycloakSubject } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:23010/api/v1/users/$($targetUsers[0].id)/oidc-binding" -WebSession $qaWebSession -Headers $bindingHeaders -ContentType "application/json" -Body $bindingBody
$platformAdminPassword = $null
$qaWebSession = $null
```

预期返回 `data.bound=true`。重复 subject、已绑定的本地用户、禁用用户或没有
`users.manage` 权限都会被拒绝。每个需要通过 OIDC 登录的本地用户都要在切换前
完成一次显式绑定；尤其不要忘记绑定仍需管理平台的管理员账号。临时 Keycloak
密码会要求首次更新，realm 的 default action 还会要求扫描并验证 TOTP。

## 3. 初始化并 unseal Vault

只启动 core。它在初始化前会显示 unhealthy，这是 sealed server 的预期状态：

```powershell
docker compose -f infra/compose.phase2.yaml --profile identity-secrets up -d vault-core
New-Item -ItemType Directory -Force .data/secrets | Out-Null
docker compose -f infra/compose.phase2.yaml exec -T vault-core vault operator init -key-shares=1 -key-threshold=1 -format=json |
  Set-Content -NoNewline .data/secrets/vault-init.json
icacls .data\secrets\vault-init.json /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

`1/1` 只适合单人本机教学，避免把三份 share 假装分离却存在同一台机器。生产
必须设计真正的多人 quorum 或受信任 auto-unseal。初始化文件同时包含 unseal
key 和 initial root token；它已被 `.data/` 忽略，但仍应视为最高敏感文件。

每次 Vault 重启后执行下面的交互命令，在隐藏提示中粘贴 unseal key。不要把 key
作为命令行参数：

```powershell
docker compose -f infra/compose.phase2.yaml exec vault-core vault operator unseal
docker compose -f infra/compose.phase2.yaml ps vault-core
```

## 4. 创建 KV、写 Secret 与最小权限 token

把 initial root token 放进当前 PowerShell 进程，命令本身不包含值：

```powershell
$env:VAULT_TOKEN = Read-Host "Initial Vault root token" -MaskInput
docker compose -f infra/compose.phase2.yaml exec -T -e VAULT_TOKEN vault-core vault secrets enable -path=qa-platform -version=2 kv
docker compose -f infra/compose.phase2.yaml exec -T -e VAULT_TOKEN vault-core vault policy write qa-platform-read /vault/policies/qa-platform-read.hcl
```

复制 Provider 模板到忽略目录，替换所有占位符。不要编辑被 Git 跟踪的 example
文件：

```powershell
Copy-Item infra/vault/examples/providers.example.json .data/secrets/providers.json
notepad .data/secrets/providers.json
docker compose -f infra/compose.phase2.yaml cp .data/secrets/providers.json vault-core:/tmp/providers.json
docker compose -f infra/compose.phase2.yaml exec -T -e VAULT_TOKEN vault-core vault kv put -mount=qa-platform providers '@/tmp/providers.json'
docker compose -f infra/compose.phase2.yaml exec vault-core rm -f /tmp/providers.json
```

只有 `QA_PROVIDER_SECRET_*` 是本轮已接线的消费范围；没有 Provider allowlist 时
可以不创建 `providers` 文档。`runtime.example.json` 是后续 DB/Broker/S3 启动引导
接口的教学预留，把它写入 Vault 不会改变这些组件当前仍从 Settings/`.env` 取值
的事实。不要把 root token 写入 `.env`。创建只有两个 read path、24 小时有效且
不可续租的应用 token：

```powershell
$appToken = docker compose -f infra/compose.phase2.yaml exec -T -e VAULT_TOKEN vault-core vault token create -policy=qa-platform-read -no-default-policy -orphan -ttl=24h -renewable=false -field=token
$appToken = $appToken.Trim()
$envFile = Get-Content .env
$envFile -replace '^VAULT_APP_TOKEN=.*$', "VAULT_APP_TOKEN=$appToken" | Set-Content .env
$appToken = $null
Remove-Item Env:VAULT_TOKEN
```

Compose 将该值作为 `/run/secrets/vault_app_token` 文件只挂给 backend。Vault root
token 和 unseal key 没有对应 Compose secret，也不会进入 backend。

## 5. 显式切换并启动完整实验

在 `.env` 里取消阶段五注释，值必须与示例完全一致：

```text
COMPOSE_AUTH_RUNTIME_MODE=keycloak_local_container
COMPOSE_OIDC_ISSUER=http://127.0.0.1:23010/identity/realms/qa-learning
COMPOSE_OIDC_BROWSER_AUTHORIZATION_ENDPOINT=http://127.0.0.1:23010/identity/realms/qa-learning/protocol/openid-connect/auth
COMPOSE_OIDC_TOKEN_ENDPOINT=http://keycloak:8080/identity/realms/qa-learning/protocol/openid-connect/token
COMPOSE_OIDC_JWKS_ENDPOINT=http://keycloak:8080/identity/realms/qa-learning/protocol/openid-connect/certs
COMPOSE_OIDC_CLIENT_ID=qa-platform-web
COMPOSE_OIDC_REDIRECT_URI=http://127.0.0.1:23010/api/v1/auth/oidc/callback
COMPOSE_OIDC_POST_LOGIN_REDIRECT_URI=http://127.0.0.1:23010/
COMPOSE_SECRET_STORE_RUNTIME_MODE=vault_local_container
COMPOSE_VAULT_ENDPOINT_URL=http://vault:8200
COMPOSE_VAULT_KV_MOUNT=qa-platform
COMPOSE_VAULT_APP_TOKEN_FILE=/run/secrets/vault_app_token
```

```powershell
docker compose -f infra/compose.phase2.yaml --profile identity-secrets up -d
```

如果还启用 PostgreSQL、Worker 或对象存储，把对应 profile 一起列出；不要为了
方便把任何内部端口改成 `0.0.0.0` 发布。

## 6. 验收与故障练习

```powershell
$oidc = Invoke-RestMethod http://127.0.0.1:23010/identity/realms/qa-learning/.well-known/openid-configuration
$oidc.issuer
Invoke-WebRequest http://127.0.0.1:23010/identity/admin/ -SkipHttpErrorCheck
docker compose -f infra/compose.phase2.yaml ps
```

预期 issuer 精确等于 public issuer，`/identity/admin/` 为 404，宿主机不存在
8200、8080 或 9000 的 Vault/Keycloak 监听端口。然后完成一次：已显式绑定的
稳定 subject → PKCE → 修改临时密码 → 绑定 TOTP → 回调 → 再登录要求 OTP。
授权与 callback URL 分别含 `state`/`nonce` 和一次性 `code`/`state`；不要在验收
记录、截图或 Issue 中保存完整地址。前端 NGINX 对这两个路径关闭 access log。

建议依次练习：

1. seal Vault，确认已接线的 Provider 操作失败关闭且不会退回空密码或环境默认值；
2. 使用过期/无 policy token，确认 403 不泄漏 token；
3. 把 issuer、audience、nonce 或 PKCE verifier 改错，确认 OIDC fail closed；
4. 重启 Vault，观察 sealed 是正常状态并执行人工 unseal；
5. 修改 realm JSON 后重启，观察已存在 realm 不会被偷偷覆盖；
6. 请求 `/identity/realms/master/`、Vault `sys/` 或 PUT，确认 gateway 拒绝。

DB/Broker/S3 尚未消费 `runtime` 文档，因此 seal Vault 不应被误写成“整个应用
启动必然失败”。把同步启动构造改造成受审计、可超时且失败关闭的异步启动引导，
仍是后续阶段工作。

当前开发机没有 Docker，以上 pull/build/start/import/init/unseal 和浏览器流程均未
实际运行。仓库只完成了静态配置与边界验证；首次容器实跑必须记录版本、digest、
健康检查、volume 权限、重启和恢复结果。
