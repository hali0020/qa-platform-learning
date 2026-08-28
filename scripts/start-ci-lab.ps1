$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot "infra\compose.phase2.yaml"
$EnvironmentFile = Join-Path $ProjectRoot ".env"
$CiLabMain = Join-Path $ProjectRoot "backend\app\ci_lab\main.py"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 Docker。请在个人隔离环境安装 Docker Desktop 后再启动 CI Lab。"
}
if (-not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
    throw "未找到仓库根目录 .env。请先复制 .env.example；不要提交填写后的文件。"
}
if (-not (Test-Path -LiteralPath $CiLabMain -PathType Leaf)) {
    throw "CI Lab 服务源码尚未完成，拒绝构建不完整镜像。"
}

$ManagedVariables = @(
    "COMPOSE_PROVIDER_RUNTIME_MODE",
    "COMPOSE_PROVIDER_SECRET_ENV_ALLOWLIST",
    "QA_PROVIDER_SECRET_CI_LAB",
    "QA_PROVIDER_SECRET_CI_LAB_WEBHOOK"
)
$PreviousValues = @{}
foreach ($Name in $ManagedVariables) {
    $PreviousValues[$Name] = [Environment]::GetEnvironmentVariable(
        $Name,
        [EnvironmentVariableTarget]::Process
    )
}

$MachineToken = $PreviousValues["QA_PROVIDER_SECRET_CI_LAB"]
$WebhookSecret = $PreviousValues["QA_PROVIDER_SECRET_CI_LAB_WEBHOOK"]
if ([string]::IsNullOrWhiteSpace($MachineToken)) {
    $RandomBytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    try {
        $MachineToken = [Convert]::ToBase64String($RandomBytes).`
            Replace('+', '-').Replace('/', '_').TrimEnd('=')
    }
    finally {
        [Array]::Clear($RandomBytes, 0, $RandomBytes.Length)
    }
}
if ($MachineToken -notmatch '^[A-Za-z0-9_-]{32,256}$') {
    throw "CI Lab Token 必须是 32-256 位字母、数字、下划线或连字符。"
}
if ([string]::IsNullOrWhiteSpace($WebhookSecret)) {
    $WebhookRandomBytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    try {
        $WebhookSecret = [Convert]::ToBase64String($WebhookRandomBytes).`
            Replace('+', '-').Replace('/', '_').TrimEnd('=')
    }
    finally {
        [Array]::Clear(
            $WebhookRandomBytes,
            0,
            $WebhookRandomBytes.Length
        )
    }
}
if ($WebhookSecret -notmatch '^[A-Za-z0-9_-]{32,256}$') {
    throw "CI Lab Webhook Secret 必须是 32-256 位安全 ASCII 字符。"
}

try {
    [Environment]::SetEnvironmentVariable(
        "COMPOSE_PROVIDER_RUNTIME_MODE",
        "ci_lab_local",
        [EnvironmentVariableTarget]::Process
    )
    [Environment]::SetEnvironmentVariable(
        "COMPOSE_PROVIDER_SECRET_ENV_ALLOWLIST",
        "QA_PROVIDER_SECRET_CI_LAB,QA_PROVIDER_SECRET_CI_LAB_WEBHOOK",
        [EnvironmentVariableTarget]::Process
    )
    [Environment]::SetEnvironmentVariable(
        "QA_PROVIDER_SECRET_CI_LAB",
        $MachineToken,
        [EnvironmentVariableTarget]::Process
    )
    [Environment]::SetEnvironmentVariable(
        "QA_PROVIDER_SECRET_CI_LAB_WEBHOOK",
        $WebhookSecret,
        [EnvironmentVariableTarget]::Process
    )

    Push-Location $ProjectRoot
    try {
        & docker compose `
            --env-file $EnvironmentFile `
            -f $ComposeFile `
            --profile ci-lab `
            config --quiet
        if ($LASTEXITCODE -ne 0) {
            throw "CI Lab Compose 静态配置校验失败。"
        }

        & docker compose `
            --env-file $EnvironmentFile `
            -f $ComposeFile `
            --profile ci-lab `
            up --build --detach --force-recreate `
            ci-lab backend ci-lab-webhook-worker frontend
        if ($LASTEXITCODE -ne 0) {
            throw "CI Lab 本机容器启动失败。"
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "CI Lab 已请求启动：http://127.0.0.1:23020/health/live"
    Write-Host "QA 平台入口：http://127.0.0.1:23010/"
    Write-Host "Webhook 投递 Worker 已启动，仅连接仓库自建的内部网络。"
    Write-Host "Token 与 Webhook Secret 未写入脚本或输出；请勿运行会展开环境值的 compose config。"
}
finally {
    $MachineToken = $null
    $WebhookSecret = $null
    foreach ($Name in $ManagedVariables) {
        [Environment]::SetEnvironmentVariable(
            $Name,
            $PreviousValues[$Name],
            [EnvironmentVariableTarget]::Process
        )
    }
}
