# 阶段五：本机 OIDC、MFA 与 Secret Manager

## 本阶段解决什么

阶段五把“账号密码写在应用里”和“敏感值散落在环境变量里”拆成两个可学习的
边界：Keycloak 负责认证，Vault 当前负责 Provider 运行时 Secret 的教学接入。两者都由我们自己在本机
创建，默认关闭，不连接公司 IdP、公司 Vault、LDAP、SMTP 或任何云端 Secret
Manager。

这不是把现有本机账号删掉。默认仍是 `local_accounts + env_local`，只有显式启用
`identity-secrets` profile 并填写固定配置后，才切换到自建容器。

```text
Browser
  │ http://127.0.0.1:23010/identity/...
  ▼
Frontend NGINX ──► keycloak gateway ──► keycloak-core
                         │                   │
                    identity network   identity-core network

Backend ──► vault gateway ──► vault-core + encrypted file volume
               │                   │
          secrets network     secrets-core network
```

Keycloak 和 Vault 核心服务都没有 `ports`，两个管理面也不在应用网络。前端只
转发 `/identity/`；Keycloak gateway 只允许 `qa-learning` realm 和登录静态资源。
Vault gateway 更窄，只接受以下两个精确 GET：

- `/v1/qa-platform/data/runtime`
- `/v1/qa-platform/data/providers`

## OIDC 为什么有两个地址

OIDC token 中的 issuer 必须是浏览器和应用共同认可的稳定身份。这里固定为：

```text
http://127.0.0.1:23010/identity/realms/qa-learning
```

浏览器通过这个 loopback URL 完成授权和 TOTP 页面。后端不绕回宿主机，而是
通过固定内部地址请求 token 和 JWKS：

```text
http://keycloak:8080/identity/realms/qa-learning/protocol/openid-connect/token
http://keycloak:8080/identity/realms/qa-learning/protocol/openid-connect/certs
```

“公开 issuer”和“内部传输地址”不是两个身份源。它们最终进入同一个只允许
固定路径的 Keycloak gateway；这样既满足浏览器可达性，也不开放 Keycloak
容器端口。任意外部 issuer、动态 discovery URL 和公司 SSO 都没有逃生开关。

## 可审计 realm

[`infra/keycloak/qa-learning-realm.json`](../infra/keycloak/qa-learning-realm.json)
是完整、可评审的教学配置，不包含用户或密码。关键决策包括：

| 配置 | 语义 |
| --- | --- |
| `qa-platform-web` | public client，不存在 client secret |
| Authorization Code + S256 PKCE | 禁止 implicit、password/direct grant 和 service account |
| 精确 redirect URI / web origin | 只接受本机 `127.0.0.1:23010` |
| audience mapper | ID token 的 `aud` 明确包含 `qa-platform-web` |
| `CONFIGURE_TOTP` default action | 每个新用户首次登录必须绑定 TOTP |
| TOTP SHA-256 / 6 位 / 30 秒 / 不可复用 | 后续登录由默认 browser flow 校验 OTP |
| brute-force protection | 连续失败进入递增等待 |
| 禁止自注册、邮箱登录、找回密码 | 不产生 SMTP 或外部身份依赖 |
| 不配置 Identity Provider | 不会联合到公司或公共 IdP |

OIDC 登录不会信任 Keycloak role，也不会自动创建或按 username/email 匹配平台
账号。切换模式前，拥有 `users.manage` 的本地管理员必须在 `local_accounts` 模式
调用受 Session + CSRF 保护的绑定 API，把 Keycloak 的稳定 `sub` 显式绑定到一个
已启用的本地用户。一个本地用户和一个 subject 都只能绑定一次；登录只用固定
issuer 与已验证的 `sub` 查找绑定，权限继续来自平台本机 RBAC。未知 subject、
重复绑定和 audience/nonce/PKCE 校验失败都应拒绝。Keycloak username 以后改名也
不会改变这条授权关系。

TOTP 是第二因素，不是“扫描二维码后永远放行”。`CONFIGURE_TOTP` 只负责首次
登记；默认 browser flow 在用户已有 OTP credential 后会在后续登录执行 OTP
表单。时间漂移、恢复码、遗失设备和强制重绑仍应作为故障练习。

当前“退出”只撤销 QA 平台 Session，不会结束 Keycloak 浏览器 SSO
Session。因此再次点击 OIDC 登录可能无需重输密码，但 Keycloak 仍会按
自己的 browser flow 和 OTP 策略判断。RP-Initiated Logout、单点退出和全局
Session 管理属于后续学习项，不应把当前按钮解释为“所有身份会话已退出”。

## Vault 为什么不用 dev root token

本阶段使用持久单节点 file storage，而不是 `vault server -dev`：

- 数据加密后写入命名卷；
- 第一次必须显式 `operator init`；
- 每次重启后保持 sealed，必须显式 unseal；
- root token 只做初始化，不挂载给 backend；
- backend 只得到 `qa-platform-read` policy 的短期、不可续租教学 token；
- token 通过 `/run/secrets/vault_app_token` 读取，不进入容器环境元数据；
- mlock 保持启用，容器只增加 `IPC_LOCK`；
- UI、Prometheus retention、匿名 metrics 和产品 usage reporting 关闭；
- `internal: true` 网络使容器运行期没有互联网出口。

[`infra/vault/policies/qa-platform-read.hcl`](../infra/vault/policies/qa-platform-read.hcl)
只授予两个 KV-v2 data path 的 `read`。它没有 `list`、metadata、create、update、
delete、`sys/` 或通配符权限。gateway 再做一次 HTTP 路径和方法约束，形成
“网络可达性 + Vault ACL”两道独立防线。

`providers` 文档只允许应用配置中已经精确列入 allowlist 的
`QA_PROVIDER_SECRET_*` 字段，Provider 在执行操作时异步读取它；这是本轮真正
接通的业务消费路径。模板只含占位符，真实 JSON 副本必须放在被 Git 忽略的
`.data/secrets/`。

`runtime` 文档定义数据库、Broker、对象存储和可选 OIDC client secret 字段；
当前 Keycloak client 是 public client，所以 `OIDC_CLIENT_SECRET` 为空。但该文档
目前只是未来启动引导的接口与故障练习预留：数据库、Broker 与 S3 仍在同步构造
阶段从现有 Settings/`.env` 读取。把值写入 Vault 不会切换这些组件，也不能宣称
它们已由 Vault 托管。

## 异步应用要学到什么

Provider Secret 读取和 JWKS/token 交换都是异步 HTTP，但不能因此变成无限并发：

- 固定 endpoint/path，DNS 或重定向不能把请求带出本机实验拓扑；
- 连接池、并发、timeout 和 retry 都有上限；
- 401/403、sealed、超时、内容超限和 schema 错误要 fail closed；
- JWKS 可以短时缓存，app token 和业务 Secret 不能写日志或返回前端；
- 取消请求时要释放 response body 和连接；
- 已接线的 Provider 取 Secret 失败时让该操作失败关闭，不能回退为空值；未来接通
  `runtime` 启动引导时，也必须在构造 DB/Broker/S3 之前完成验证。

OIDC 授权 URL 含 `state`/`nonce`，callback URL 含一次性 `code`/`state`。前端 NGINX
对 `/identity/` 和精确 callback location 关闭 access log，后端容器也关闭服务器
原始访问日志，只保留不含 query 的 route-template 结构化应用日志。排障时不要把
完整授权/callback URL、浏览器地址栏或网络抓包粘贴到 Issue；应只记录 Request ID、
状态码和已脱敏的错误类别。

## 镜像固定与来源

核验日期为 2026-08-27：

| 组件 | 固定引用 |
| --- | --- |
| Keycloak | `quay.io/keycloak/keycloak:26.7.2@sha256:831330513f55695572286e521f94fcd3c7e285250ed5b848090265a33192f669` |
| Vault | `hashicorp/vault:2.0.4@sha256:5be49781ecf78bfe775c5309c6a4d9f4e9e040b6c885c99eb2b12fb69855e1a2` |
| 两个协议网关 | `ghcr.io/nginx/nginx-unprivileged:1.30.4-alpine3.24@sha256:93722936b82ec8a1178d48448e619226680d2de3706a1640800e186cd5fa7fd3` |

Keycloak 的版本来自[官方 26.7.2 发布公告](https://www.keycloak.org/2026/08/keycloak-2672-released)，
完整 OCI index digest 通过[官方 Quay Registry manifest](https://quay.io/v2/keycloak/keycloak/manifests/26.7.2)
响应头核验。Vault 的版本来自[官方发布目录](https://releases.hashicorp.com/vault/2.0.4/)，
完整 multi-platform digest 来自[HashiCorp Verified Publisher tag API](https://hub.docker.com/v2/repositories/hashicorp/vault/tags/2.0.4)。
协议网关复用 NGINX 官方 unprivileged 镜像，digest 来源见其
[上游 GHCR 包版本页](https://github.com/nginx/docker-nginx-unprivileged/pkgs/container/nginx-unprivileged/versions?filters%5Bversion_type%5D=tagged)。

实现依据还包括 Keycloak 官方的[容器运行](https://www.keycloak.org/server/containers)、
[hostname v2](https://www.keycloak.org/server/hostname)、
[反向代理暴露路径](https://www.keycloak.org/server/reverseproxy)、
[realm 导入](https://www.keycloak.org/server/importExport)和
[Server Administration Guide](https://www.keycloak.org/docs/latest/server_admin/)；以及 Vault 官方的
[server 命令](https://developer.hashicorp.com/vault/docs/commands/server)、
[filesystem storage](https://developer.hashicorp.com/vault/docs/configuration/storage/filesystem)、
[init](https://developer.hashicorp.com/vault/docs/commands/operator/init)、
[unseal](https://developer.hashicorp.com/vault/docs/commands/operator/unseal)、
[policy](https://developer.hashicorp.com/vault/docs/concepts/policies)和
[KV-v2 put](https://developer.hashicorp.com/vault/docs/commands/kv/put)文档。

## 仍然不是生产身份平台

当前 Keycloak 使用单节点 `dev-file` 数据库，Vault 使用不支持 HA 的单节点 file
storage，内部 HTTP 依赖 Docker internal network 隔离。没有 TLS、自动 unseal、
HSM/KMS、OIDC 管理员分权、恢复码运营、邮件验证、集中审计导出、备份演练、
多副本或灾备。生产环境应使用 PostgreSQL、TLS/mTLS、Vault integrated storage
或受管 Secret Manager、自动轮换、最小权限 workload identity 和经验证的恢复
流程。

本机目前没有 Docker，因此本阶段只完成配置、JSON/HCL/YAML 和安全边界的静态
验证；镜像尚未实际 pull/build，realm import、浏览器 PKCE+TOTP、Vault init/
unseal、命名卷权限和重启恢复都必须在有 Docker 的机器上按部署说明实跑。
