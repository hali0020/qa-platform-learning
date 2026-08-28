$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot "backend"
$PythonPath = Join-Path $BackendRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "未找到后端虚拟环境，请先按照 docs/DEVELOPMENT.md 初始化环境。"
}

$env:APP_ENV = "local"
$env:LOCAL_ONLY = "true"
$env:HOST = "127.0.0.1"
$env:PORT = "23100"
$env:DATABASE_SCHEMA_MODE = "verify"
$env:PROVIDER_RUNTIME_MODE = "local_lab"
$env:PROVIDER_SELF_HOSTED_OWNERSHIP_ACKNOWLEDGED = "false"
$env:PROVIDER_ALLOWED_HOSTS = ""
$env:PROVIDER_ALLOWED_NETWORKS = ""
$env:PROVIDER_SECRET_ENV_ALLOWLIST = ""

Push-Location $BackendRoot
try {
    & $PythonPath -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "本地数据库迁移失败，后端未启动。"
    }
    # The project logger records route templates without query strings. The
    # default Uvicorn access log would include OIDC callback codes and state.
    & $PythonPath -m uvicorn app.main:app --reload --host 127.0.0.1 --port 23100 --no-access-log
}
finally {
    Pop-Location
}
