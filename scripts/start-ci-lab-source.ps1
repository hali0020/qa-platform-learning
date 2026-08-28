$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot "backend"
$PythonPath = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$DataRoot = Join-Path $ProjectRoot ".data\ci-lab-source"
$QaDatabasePath = Join-Path $DataRoot "qa.db"
$CiDatabasePath = Join-Path $DataRoot "ci-lab.db"
$UploadRoot = Join-Path $DataRoot "uploads"
$LabStdoutPath = Join-Path $DataRoot "ci-lab.stdout.log"
$LabStderrPath = Join-Path $DataRoot "ci-lab.stderr.log"
$WorkerStdoutPath = Join-Path $DataRoot "ci-lab-webhook-worker.stdout.log"
$WorkerStderrPath = Join-Path $DataRoot "ci-lab-webhook-worker.stderr.log"
$QaDatabaseUrl = "sqlite+aiosqlite:///$($QaDatabasePath.Replace('\', '/'))"

$LocalApplicationData = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::LocalApplicationData
)
if ([String]::IsNullOrWhiteSpace($LocalApplicationData)) {
    throw "无法解析当前用户的 LocalApplicationData，拒绝创建 CI Lab Secret。"
}
$SecretParentRoot = [IO.Path]::GetFullPath($LocalApplicationData)
$SecretParentUri = [Uri]::new($SecretParentRoot)
$SecretDriveRoot = [IO.Path]::GetPathRoot($SecretParentRoot)
if (
    $SecretParentUri.IsUnc -or
    $SecretParentRoot.StartsWith('\\') -or
    $SecretParentRoot.StartsWith('//') -or
    [String]::IsNullOrWhiteSpace($SecretDriveRoot) -or
    [IO.DriveInfo]::new($SecretDriveRoot).DriveType -ne [IO.DriveType]::Fixed
) {
    throw "CI Lab Secret 只允许当前用户固定本地磁盘上的 LocalApplicationData。"
}
$SecretRoot = [IO.Path]::GetFullPath(
    (Join-Path $SecretParentRoot ("qa-ci-lab-{0}" -f [Guid]::NewGuid()))
)
$SecretParent = [IO.Directory]::GetParent($SecretRoot)
if (
    $null -eq $SecretParent -or
    -not [String]::Equals(
        $SecretParent.FullName.TrimEnd('\', '/'),
        $SecretParentRoot.TrimEnd('\', '/'),
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "CI Lab Secret 目录必须是 LocalApplicationData 的随机直接子目录。"
}
$TokenPath = Join-Path $SecretRoot "machine.token"
$WebhookSecretPath = Join-Path $SecretRoot "webhook.secret"

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "未找到 Python 3.10 后端虚拟环境，请先初始化 backend\.venv。"
}

function Assert-LoopbackPortAvailable {
    param([Parameter(Mandatory = $true)][int]$Port)

    $Socket = [Net.Sockets.Socket]::new(
        [Net.Sockets.AddressFamily]::InterNetwork,
        [Net.Sockets.SocketType]::Stream,
        [Net.Sockets.ProtocolType]::Tcp
    )
    try {
        $Socket.ExclusiveAddressUse = $true
        $Socket.Bind(
            [Net.IPEndPoint]::new([Net.IPAddress]::Loopback, $Port)
        )
    }
    catch [Net.Sockets.SocketException] {
        throw "本机环回端口 $Port 已被占用；拒绝连接或复用未知进程。"
    }
    finally {
        $Socket.Dispose()
    }
}

function Test-ProcessOwnsListener {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$ProcessId
    )

    $EscapedEndpoint = [Regex]::Escape("127.0.0.1:$Port")
    $Pattern = "^\s*TCP\s+$EscapedEndpoint\s+\S+\s+\S+\s+(\d+)\s*$"
    $Lines = & netstat.exe -ano -p tcp
    foreach ($MatchInfo in ($Lines | Select-String -Pattern $Pattern)) {
        $OwnerProcessId = [int]$MatchInfo.Matches[0].Groups[1].Value
        try {
            $Owner = Get-Process -Id $OwnerProcessId -ErrorAction Stop
            # A Windows venv launcher waits on the real interpreter, so the
            # socket owner is normally its direct child. Validate the retained
            # launcher handle and that exact parent relationship.
            if (
                $Owner.Id -eq $ProcessId -or
                ($null -ne $Owner.Parent -and $Owner.Parent.Id -eq $ProcessId)
            ) {
                return $true
            }
        }
        catch {
            continue
        }
    }
    return $false
}

function Protect-SecretDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $CurrentSid = $Identity.User
        if ($null -eq $CurrentSid) {
            throw "无法解析当前 Windows 用户 SID。"
        }
        $Acl = [Security.AccessControl.DirectorySecurity]::new()
        $Acl.SetOwner($CurrentSid)
        $Acl.SetAccessRuleProtection($true, $false)
        $Inheritance = (
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        )
        $Rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $CurrentSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $Inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $Acl.AddAccessRule($Rule)
        Set-Acl -LiteralPath $Path -AclObject $Acl

        $Verified = Get-Acl -LiteralPath $Path
        if (-not $Verified.AreAccessRulesProtected) {
            throw "CI Lab Secret 目录仍在继承 ACL。"
        }
        foreach ($Access in $Verified.Access) {
            $Sid = $Access.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            )
            if (
                $Access.AccessControlType -eq
                    [Security.AccessControl.AccessControlType]::Allow -and
                $Sid.Value -ne $CurrentSid.Value
            ) {
                throw "CI Lab Secret 目录存在非当前用户的允许规则。"
            }
        }
    }
    finally {
        $Identity.Dispose()
    }
}

function Protect-SecretFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $CurrentSid = $Identity.User
        if ($null -eq $CurrentSid) {
            throw "无法解析当前 Windows 用户 SID。"
        }
        $Acl = [Security.AccessControl.FileSecurity]::new()
        $Acl.SetOwner($CurrentSid)
        $Acl.SetAccessRuleProtection($true, $false)
        $Rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $CurrentSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $Acl.AddAccessRule($Rule)
        Set-Acl -LiteralPath $Path -AclObject $Acl

        $Verified = Get-Acl -LiteralPath $Path
        if (-not $Verified.AreAccessRulesProtected) {
            throw "CI Lab Secret 文件仍在继承 ACL。"
        }
        foreach ($Access in $Verified.Access) {
            $Sid = $Access.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            )
            if (
                $Access.AccessControlType -eq
                    [Security.AccessControl.AccessControlType]::Allow -and
                $Sid.Value -ne $CurrentSid.Value
            ) {
                throw "CI Lab Secret 文件存在非当前用户的允许规则。"
            }
        }
    }
    finally {
        $Identity.Dispose()
    }
}

function Write-OwnerOnlySecretFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $Stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $Bytes = [Text.Encoding]::ASCII.GetBytes($Value)
        try {
            $Stream.Write($Bytes, 0, $Bytes.Length)
        }
        finally {
            [Array]::Clear($Bytes, 0, $Bytes.Length)
        }
    }
    finally {
        $Stream.Dispose()
    }
    Protect-SecretFile -Path $Path
}

$ManagedVariables = @(
    "APP_ENV",
    "DEBUG",
    "HOST",
    "PORT",
    "LOCAL_ONLY",
    "CORS_ORIGINS",
    "DATABASE_RUNTIME_MODE",
    "DATABASE_URL",
    "DATABASE_SCHEMA_MODE",
    "LOCAL_DATA_ROOT",
    "BROKER_RUNTIME_MODE",
    "BROKER_URL",
    "OBJECT_STORAGE_RUNTIME_MODE",
    "OBJECT_STORAGE_ENDPOINT_URL",
    "OBJECT_STORAGE_BUCKET",
    "OBJECT_STORAGE_ACCESS_KEY",
    "OBJECT_STORAGE_SECRET_KEY",
    "AUTH_ENABLED",
    "AUTH_RUNTIME_MODE",
    "OIDC_ISSUER",
    "OIDC_BROWSER_AUTHORIZATION_ENDPOINT",
    "OIDC_TOKEN_ENDPOINT",
    "OIDC_JWKS_ENDPOINT",
    "OIDC_CLIENT_ID",
    "OIDC_REDIRECT_URI",
    "OIDC_POST_LOGIN_REDIRECT_URI",
    "SECRET_STORE_RUNTIME_MODE",
    "VAULT_ENDPOINT_URL",
    "VAULT_KV_MOUNT",
    "VAULT_APP_TOKEN_FILE",
    "VAULT_APP_TOKEN",
    "UPLOAD_ROOT",
    "PROVIDER_RUNTIME_MODE",
    "PROVIDER_SELF_HOSTED_OWNERSHIP_ACKNOWLEDGED",
    "PROVIDER_ALLOWED_HOSTS",
    "PROVIDER_ALLOWED_PORTS",
    "PROVIDER_ALLOWED_NETWORKS",
    "PROVIDER_ALLOW_LOOPBACK_HTTP",
    "PROVIDER_SECRET_ENV_ALLOWLIST",
    "QA_PROVIDER_SECRET_CI_LAB",
    "QA_PROVIDER_SECRET_CI_LAB_WEBHOOK",
    "CI_LAB_DATABASE_PATH",
    "CI_LAB_MACHINE_TOKEN_FILE",
    "CI_LAB_WEBHOOK_SECRET_FILE",
    "CI_LAB_WEBHOOK_TARGET_MODE",
    "CI_LAB_WEBHOOK_TARGET_URL",
    "CI_LAB_WEBHOOK_WORKER_ID"
)
$PreviousValues = @{}
foreach ($Name in $ManagedVariables) {
    $PreviousValues[$Name] = [Environment]::GetEnvironmentVariable(
        $Name,
        [EnvironmentVariableTarget]::Process
    )
}

$MachineToken = $null
$WebhookSecret = $null
$LabProcess = $null
$WebhookWorkerProcess = $null
try {
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
    Assert-LoopbackPortAvailable -Port 23020
    Assert-LoopbackPortAvailable -Port 23100

    if (Test-Path -LiteralPath $SecretRoot) {
        throw "随机 CI Lab 临时 Secret 目录发生冲突。"
    }
    [IO.Directory]::CreateDirectory($SecretRoot) | Out-Null
    Protect-SecretDirectory -Path $SecretRoot

    $RandomBytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    try {
        $MachineToken = [Convert]::ToBase64String($RandomBytes).`
            Replace('+', '-').Replace('/', '_').TrimEnd('=')
    }
    finally {
        [Array]::Clear($RandomBytes, 0, $RandomBytes.Length)
    }

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

    Write-OwnerOnlySecretFile -Path $TokenPath -Value $MachineToken
    Write-OwnerOnlySecretFile -Path $WebhookSecretPath -Value $WebhookSecret

    $Values = @{
        APP_ENV = "local"
        DEBUG = "false"
        HOST = "127.0.0.1"
        PORT = "23100"
        LOCAL_ONLY = "true"
        CORS_ORIGINS = "http://127.0.0.1:5173,http://localhost:5173"
        DATABASE_RUNTIME_MODE = "sqlite_local"
        DATABASE_URL = $QaDatabaseUrl
        DATABASE_SCHEMA_MODE = "verify"
        LOCAL_DATA_ROOT = $DataRoot
        BROKER_RUNTIME_MODE = "disabled_local"
        BROKER_URL = ""
        OBJECT_STORAGE_RUNTIME_MODE = "local_filesystem"
        OBJECT_STORAGE_ENDPOINT_URL = ""
        OBJECT_STORAGE_BUCKET = ""
        OBJECT_STORAGE_ACCESS_KEY = ""
        OBJECT_STORAGE_SECRET_KEY = ""
        AUTH_ENABLED = "true"
        AUTH_RUNTIME_MODE = "local_accounts"
        OIDC_ISSUER = ""
        OIDC_BROWSER_AUTHORIZATION_ENDPOINT = ""
        OIDC_TOKEN_ENDPOINT = ""
        OIDC_JWKS_ENDPOINT = ""
        OIDC_CLIENT_ID = ""
        OIDC_REDIRECT_URI = ""
        OIDC_POST_LOGIN_REDIRECT_URI = ""
        SECRET_STORE_RUNTIME_MODE = "env_local"
        VAULT_ENDPOINT_URL = ""
        VAULT_KV_MOUNT = ""
        VAULT_APP_TOKEN_FILE = ""
        VAULT_APP_TOKEN = ""
        UPLOAD_ROOT = $UploadRoot
        PROVIDER_RUNTIME_MODE = "ci_lab_local"
        PROVIDER_SELF_HOSTED_OWNERSHIP_ACKNOWLEDGED = "false"
        PROVIDER_ALLOWED_HOSTS = ""
        PROVIDER_ALLOWED_PORTS = "443"
        PROVIDER_ALLOWED_NETWORKS = ""
        PROVIDER_ALLOW_LOOPBACK_HTTP = "false"
        PROVIDER_SECRET_ENV_ALLOWLIST = (
            "QA_PROVIDER_SECRET_CI_LAB,QA_PROVIDER_SECRET_CI_LAB_WEBHOOK"
        )
        QA_PROVIDER_SECRET_CI_LAB = $MachineToken
        QA_PROVIDER_SECRET_CI_LAB_WEBHOOK = $WebhookSecret
        CI_LAB_DATABASE_PATH = $CiDatabasePath
        CI_LAB_MACHINE_TOKEN_FILE = $TokenPath
        CI_LAB_WEBHOOK_SECRET_FILE = $WebhookSecretPath
        CI_LAB_WEBHOOK_TARGET_MODE = "host_loopback"
        CI_LAB_WEBHOOK_TARGET_URL = ""
        CI_LAB_WEBHOOK_WORKER_ID = "ci-lab-source-webhook-worker"
    }
    foreach ($Entry in $Values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable(
            $Entry.Key,
            $Entry.Value,
            [EnvironmentVariableTarget]::Process
        )
    }

    # The API process needs only its machine-token file. Remove every raw
    # credential and the unrelated webhook file from its inherited block.
    $LabExcludedValues = @{}
    foreach ($Name in @(
        "QA_PROVIDER_SECRET_CI_LAB",
        "QA_PROVIDER_SECRET_CI_LAB_WEBHOOK",
        "CI_LAB_WEBHOOK_SECRET_FILE"
    )) {
        $LabExcludedValues[$Name] = [Environment]::GetEnvironmentVariable(
            $Name,
            [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            $Name,
            $null,
            [EnvironmentVariableTarget]::Process
        )
    }
    try {
        $LabProcess = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList @(
                "-m", "uvicorn", "app.ci_lab.main:app",
                "--host", "127.0.0.1", "--port", "23020", "--no-access-log"
            ) `
            -WorkingDirectory $BackendRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $LabStdoutPath `
            -RedirectStandardError $LabStderrPath `
            -PassThru
    }
    finally {
        foreach ($Name in $LabExcludedValues.Keys) {
            [Environment]::SetEnvironmentVariable(
                $Name,
                $LabExcludedValues[$Name],
                [EnvironmentVariableTarget]::Process
            )
        }
        $LabExcludedValues.Clear()
        $LabExcludedValues = $null
    }

    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 150; $Attempt++) {
        $LabProcess.Refresh()
        if ($LabProcess.HasExited) {
            throw "CI Lab 源码进程提前退出，请查看 .data\ci-lab-source\ci-lab.stderr.log。"
        }
        if (-not (Test-ProcessOwnsListener -Port 23020 -ProcessId $LabProcess.Id)) {
            Start-Sleep -Milliseconds 200
            continue
        }
        try {
            $Health = Invoke-RestMethod `
                -Uri "http://127.0.0.1:23020/health/live" `
                -TimeoutSec 1
            if ($Health.service -eq "ci-lab" -and $Health.status -eq "ok") {
                $Ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if (-not $Ready) {
        throw "CI Lab 未在受控进程上就绪，请查看 .data\ci-lab-source\ci-lab.stderr.log。"
    }

    # Start the sender with file-based signing access only. Temporarily clear
    # every machine-token or plaintext webhook value so Start-Process cannot
    # copy those credentials into the worker's environment block.
    $WorkerExcludedValues = @{}
    foreach ($Name in @(
        "QA_PROVIDER_SECRET_CI_LAB",
        "QA_PROVIDER_SECRET_CI_LAB_WEBHOOK",
        "CI_LAB_MACHINE_TOKEN_FILE"
    )) {
        $WorkerExcludedValues[$Name] = [Environment]::GetEnvironmentVariable(
            $Name,
            [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            $Name,
            $null,
            [EnvironmentVariableTarget]::Process
        )
    }
    try {
        $WebhookWorkerProcess = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList @("-m", "app.ci_lab.webhook_worker_main") `
            -WorkingDirectory $BackendRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $WorkerStdoutPath `
            -RedirectStandardError $WorkerStderrPath `
            -PassThru
    }
    finally {
        foreach ($Name in $WorkerExcludedValues.Keys) {
            [Environment]::SetEnvironmentVariable(
                $Name,
                $WorkerExcludedValues[$Name],
                [EnvironmentVariableTarget]::Process
            )
        }
        $WorkerExcludedValues.Clear()
        $WorkerExcludedValues = $null
    }
    Start-Sleep -Milliseconds 300
    $WebhookWorkerProcess.Refresh()
    if ($WebhookWorkerProcess.HasExited) {
        throw "Webhook Worker 提前退出，请查看 .data\ci-lab-source\ci-lab-webhook-worker.stderr.log。"
    }

    # Both child processes now own their minimal inherited environment blocks.
    # The foreground QA process needs only the two allowlisted provider values,
    # never either CI Lab secret-file path or worker routing setting.
    foreach ($Name in @(
        "CI_LAB_DATABASE_PATH",
        "CI_LAB_MACHINE_TOKEN_FILE",
        "CI_LAB_WEBHOOK_SECRET_FILE",
        "CI_LAB_WEBHOOK_TARGET_MODE",
        "CI_LAB_WEBHOOK_TARGET_URL",
        "CI_LAB_WEBHOOK_WORKER_ID"
    )) {
        [Environment]::SetEnvironmentVariable(
            $Name,
            $null,
            [EnvironmentVariableTarget]::Process
        )
    }

    Push-Location $BackendRoot
    try {
        & $PythonPath -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw "本机专用数据库迁移失败，QA 后端未启动。"
        }
        Write-Host "Learning CI Lab：http://127.0.0.1:23020/health/live"
        Write-Host "QA API：http://127.0.0.1:23100（Ctrl+C 会同时停止 CI Lab 与 Webhook Worker）"
        & $PythonPath -m uvicorn app.main:app `
            --host 127.0.0.1 --port 23100 --no-access-log
        if ($LASTEXITCODE -ne 0) {
            throw "QA 后端异常退出。"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    try {
        if ($null -ne $WebhookWorkerProcess) {
            try {
                $WebhookWorkerProcess.Refresh()
                if (-not $WebhookWorkerProcess.HasExited) {
                    # Kill only through the exact retained Process handle.
                    $WebhookWorkerProcess.Kill($true)
                }
                $WebhookWorkerProcess.WaitForExit(5000) | Out-Null
            }
            catch [InvalidOperationException] {
                # The exact retained process exited between Refresh and Kill.
            }
            catch [System.ComponentModel.Win32Exception] {
                Write-Warning "Webhook Worker 子进程清理失败；请仅核对本脚本启动的隐藏进程。"
            }
            finally {
                $WebhookWorkerProcess.Dispose()
            }
        }
        if ($null -ne $LabProcess) {
            try {
                $LabProcess.Refresh()
                if (-not $LabProcess.HasExited) {
                    # Kill through the retained Process handle, never a raw PID.
                    $LabProcess.Kill($true)
                }
                $LabProcess.WaitForExit(5000) | Out-Null
            }
            catch [InvalidOperationException] {
                # The exact retained process exited between Refresh and Kill.
            }
            catch [System.ComponentModel.Win32Exception] {
                Write-Warning "CI Lab 子进程清理失败；请仅核对本脚本启动的隐藏进程。"
            }
            finally {
                $LabProcess.Dispose()
            }
        }
    }
    finally {
        try {
            try {
                if (Test-Path -LiteralPath $WebhookSecretPath -PathType Leaf) {
                    Remove-Item -LiteralPath $WebhookSecretPath -Force
                }
            }
            finally {
                if (Test-Path -LiteralPath $TokenPath -PathType Leaf) {
                    Remove-Item -LiteralPath $TokenPath -Force
                }
            }
        }
        finally {
            try {
                if (Test-Path -LiteralPath $SecretRoot -PathType Container) {
                    # Deliberately omit -Recurse. Unknown files make cleanup
                    # fail safely instead of broadening the deletion target.
                    Remove-Item -LiteralPath $SecretRoot -Force
                }
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
        }
    }
}
