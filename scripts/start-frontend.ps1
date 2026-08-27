$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$FrontendRoot = Join-Path $ProjectRoot "frontend"

if (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot "package.json"))) {
    throw "前端骨架尚未完成。"
}

Push-Location $FrontendRoot
try {
    pnpm dev --host 127.0.0.1
}
finally {
    Pop-Location
}
