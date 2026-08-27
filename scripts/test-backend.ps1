$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot "backend\.venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "未找到后端虚拟环境，请先按照 docs/DEVELOPMENT.md 初始化环境。"
}

Push-Location $ProjectRoot
try {
    & $PythonPath -m pytest
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
