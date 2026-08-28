param(
    [ValidateRange(1, 16)]
    [int]$WorkerScale = 1
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot "backend"
$PythonPath = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$ComposeFile = Join-Path $ProjectRoot "infra\compose.phase2.yaml"
$EnvironmentFile = Join-Path $ProjectRoot ".env"

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "未找到后端虚拟环境，请先按照 docs/DEVELOPMENT.md 初始化环境。"
}

Push-Location $BackendRoot
try {
    & $PythonPath -m app.ops.worker_profile_preflight `
        --env-file $EnvironmentFile
    if ($LASTEXITCODE -ne 0) {
        throw "Worker profile 本机配置预检失败；未调用 Docker。"
    }
}
finally {
    Pop-Location
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 Docker。请在个人隔离环境安装 Docker Desktop 后再启动。"
}

Push-Location $ProjectRoot
try {
    & docker compose `
        --env-file $EnvironmentFile `
        -f $ComposeFile `
        --profile worker `
        config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "Worker profile Compose 静态配置校验失败。"
    }

    & docker compose `
        --env-file $EnvironmentFile `
        -f $ComposeFile `
        --profile worker `
        up --build --scale "worker=$WorkerScale"
    if ($LASTEXITCODE -ne 0) {
        throw "Worker profile 本机容器启动失败。"
    }
}
finally {
    Pop-Location
}
