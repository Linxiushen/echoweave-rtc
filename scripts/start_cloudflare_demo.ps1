[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,

    [switch]$UseOpenAICompatible,

    [ValidateRange(10, 120)]
    [int]$StartupTimeoutSeconds = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:BundledCloudflaredSha256 = "8635da433b6df8194746e88ed9d2589566c20e38bfc2a80e431a348b7c765841"
$script:BootstrapOrigin = "https://bootstrap.invalid"

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path)
}

function Get-TextSha256 {
    param([Parameter(Mandatory = $true)][string]$Value)

    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-DemoMutex {
    param([Parameter(Mandatory = $true)][string]$StatusPath)

    $suffix = (Get-TextSha256 (Get-NormalizedPath $StatusPath)).Substring(0, 32)
    return [Threading.Mutex]::new($false, "Global\EchoWeaveCloudflareDemo-$suffix")
}

function Enter-DemoMutex {
    param([Parameter(Mandatory = $true)][Threading.Mutex]$Mutex)

    try {
        return $Mutex.WaitOne(0)
    }
    catch [Threading.AbandonedMutexException] {
        return $true
    }
}

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $temporaryPath = Join-Path $directory (".{0}.{1}.tmp" -f ([IO.Path]::GetFileName($Path)), [guid]::NewGuid().ToString("N"))
    $backupPath = "$temporaryPath.backup"
    $encoding = New-Object Text.UTF8Encoding($false)
    try {
        [IO.File]::WriteAllText(
            $temporaryPath,
            ($Value | ConvertTo-Json -Depth 8),
            $encoding
        )
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporaryPath, $Path, $backupPath)
            Remove-Item -LiteralPath $backupPath -Force
        }
        else {
            [IO.File]::Move($temporaryPath, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
}

function Read-DemoState {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        throw "The Cloudflare demo status file is invalid; refusing to overwrite it."
    }
}

function Assert-DemoState {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$StatusDirectory,
        [Parameter(Mandatory = $true)][string]$ExpectedGatewayPath
    )

    if ([int]$State.schema_version -ne 2) {
        throw "The Cloudflare demo status schema is unsupported."
    }
    $runId = [string]$State.run_id
    if ($runId -notmatch "^[a-f0-9]{32}$") {
        throw "The Cloudflare demo status file contains an invalid run_id."
    }
    $expectedRunDirectory = Get-NormalizedPath (Join-Path $StatusDirectory $runId)
    if (-not [string]::Equals(
        (Get-NormalizedPath ([string]$State.run_directory)),
        $expectedRunDirectory,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The Cloudflare demo status file contains an invalid run directory."
    }
    if ($null -eq $State.processes) {
        throw "The Cloudflare demo status file has no process identities."
    }
    foreach ($role in @("gateway", "tunnel")) {
        $record = $State.processes.$role
        if ($null -eq $record) {
            continue
        }
        if ([string]$record.role -ne $role -or [string]$record.run_id -ne $runId) {
            throw "The Cloudflare demo status file contains an invalid $role identity."
        }
        if ($role -eq "gateway" -and -not [string]::Equals(
            (Get-NormalizedPath ([string]$record.executable_path)),
            (Get-NormalizedPath $ExpectedGatewayPath),
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "The Cloudflare demo status file contains an unexpected gateway executable."
        }
        if ($role -eq "tunnel") {
            $tunnelName = [IO.Path]::GetFileName([string]$record.executable_path)
            if ($tunnelName -notin @("cloudflared.exe", "cloudflared-windows-amd64.exe")) {
                throw "The Cloudflare demo status file contains an unexpected tunnel executable."
            }
        }
    }
}

function Get-ProcessIdentity {
    param(
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][ValidateSet("gateway", "tunnel")][string]$Role,
        [Parameter(Mandatory = $true)][string]$RunId
    )

    $Process.Refresh()
    if ($Process.HasExited) {
        throw "$Role exited before its process identity was recorded."
    }
    return [ordered]@{
        pid = [int]$Process.Id
        role = $Role
        run_id = $RunId
        executable_path = Get-NormalizedPath $Process.MainModule.FileName
        start_time_utc = $Process.StartTime.ToUniversalTime().ToString("o", [Globalization.CultureInfo]::InvariantCulture)
    }
}

function Get-RecordedProcess {
    param(
        $Record,
        [Parameter(Mandatory = $true)][string]$RunId
    )

    if ($null -eq $Record) {
        return [pscustomobject]@{ Status = "not_recorded"; Process = $null }
    }
    if ([string]$Record.run_id -ne $RunId) {
        throw "A recorded process has the wrong run_id."
    }
    $processId = 0
    if (-not [int]::TryParse([string]$Record.pid, [ref]$processId) -or $processId -le 0) {
        throw "A recorded process has an invalid PID."
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [pscustomobject]@{ Status = "missing"; Process = $null }
    }
    try {
        $actualPath = Get-NormalizedPath $process.MainModule.FileName
        $actualStart = $process.StartTime.ToUniversalTime()
        $expectedStart = [DateTime]::ParseExact(
            [string]$Record.start_time_utc,
            "o",
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
    }
    catch {
        return [pscustomobject]@{ Status = "mismatch"; Process = $null }
    }
    if (-not [string]::Equals(
        $actualPath,
        (Get-NormalizedPath ([string]$Record.executable_path)),
        [StringComparison]::OrdinalIgnoreCase
    ) -or $actualStart.Ticks -ne $expectedStart.Ticks) {
        return [pscustomobject]@{ Status = "mismatch"; Process = $null }
    }
    return [pscustomobject]@{ Status = "running"; Process = $process }
}

function Archive-StaleState {
    param(
        [Parameter(Mandatory = $true)][string]$StatusPath,
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$ExpectedGatewayPath
    )

    $statusDirectory = Split-Path -Parent $StatusPath
    Assert-DemoState $State $statusDirectory $ExpectedGatewayPath
    $running = @()
    foreach ($role in @("gateway", "tunnel")) {
        $result = Get-RecordedProcess $State.processes.$role ([string]$State.run_id)
        if ($result.Status -eq "running") {
            $running += $role
        }
        elseif ($result.Status -eq "mismatch") {
            throw "The existing $role process identity does not match its status record; refusing to archive or overwrite it."
        }
    }
    if ($running.Count -gt 0) {
        throw "A Cloudflare demo is already running ($($running -join ', ')). Stop it before starting another."
    }
    $archiveName = "current.stale-{0}-{1}.json" -f [string]$State.run_id, (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
    [IO.File]::Move($StatusPath, (Join-Path $statusDirectory $archiveName))
}

function Resolve-ApprovedCloudflared {
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

    $bundledPath = Join-Path $RepoRoot "runtime\tools\cloudflared-windows-amd64.exe"
    if (Test-Path -LiteralPath $bundledPath -PathType Leaf) {
        $actualHash = (Get-FileHash -LiteralPath $bundledPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if (-not [string]::Equals(
            $actualHash,
            $script:BundledCloudflaredSha256,
            [StringComparison]::Ordinal
        )) {
            throw "The bundled cloudflared binary failed its pinned SHA-256 check."
        }
        return (Resolve-Path -LiteralPath $bundledPath).Path
    }

    $commands = @(Get-Command cloudflared -CommandType Application -All -ErrorAction SilentlyContinue)
    foreach ($command in $commands) {
        $candidate = (Resolve-Path -LiteralPath $command.Source).Path
        $signature = Get-AuthenticodeSignature -LiteralPath $candidate
        $subject = if ($null -ne $signature.SignerCertificate) {
            [string]$signature.SignerCertificate.Subject
        }
        else {
            ""
        }
        if ($signature.Status -eq "Valid" -and $subject -match '(?i)(CN|O)="?Cloudflare,\s*Inc\."?(,|$)') {
            return $candidate
        }
    }
    throw "No approved cloudflared binary was found. Install the officially signed client or use the pinned bundled binary."
}

function Get-BaseChildEnvironment {
    param([switch]$IncludeProxy)

    $result = @{}
    foreach ($name in @(
        "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA",
        "PROGRAMDATA", "ProgramFiles", "ProgramFiles(x86)",
        "CommonProgramFiles", "CommonProgramFiles(x86)",
        "SSL_CERT_FILE", "SSL_CERT_DIR"
    )) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $result[$name] = $value
        }
    }
    if ($IncludeProxy) {
        foreach ($name in @(
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "all_proxy"
        )) {
            $value = [Environment]::GetEnvironmentVariable($name, "Process")
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $result[$name] = $value
            }
        }
    }
    return $result
}

function Copy-EnvironmentMap {
    param([Parameter(Mandatory = $true)][hashtable]$Source)

    $copy = @{}
    foreach ($name in $Source.Keys) {
        $copy[[string]$name] = [string]$Source[$name]
    }
    return $copy
}

function Assert-CloudflaredEnvironment {
    param([Parameter(Mandatory = $true)][hashtable]$Environment)

    foreach ($name in $Environment.Keys) {
        if ([string]$name -match "(?i)(API[_-]?KEY|TOKEN|SECRET|PASSWORD|ECHOWEAVE|DEEPSEEK|OPENAI|QWEN|VOXCPM|SOULX|MODEL_WORKER|HF_TOKEN)") {
            throw "The cloudflared child environment contains a forbidden secret variable: $name"
        }
    }
}

function Invoke-WithIsolatedEnvironment {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    $snapshot = @{}
    foreach ($entry in @(Get-ChildItem Env:)) {
        $snapshot[$entry.Name] = [string]$entry.Value
    }
    try {
        foreach ($entry in @(Get-ChildItem Env:)) {
            [Environment]::SetEnvironmentVariable($entry.Name, $null, "Process")
        }
        foreach ($name in $Environment.Keys) {
            [Environment]::SetEnvironmentVariable(
                [string]$name,
                [string]$Environment[$name],
                "Process"
            )
        }
        return & $Action
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env:)) {
            [Environment]::SetEnvironmentVariable($entry.Name, $null, "Process")
        }
        foreach ($name in $snapshot.Keys) {
            [Environment]::SetEnvironmentVariable(
                [string]$name,
                [string]$snapshot[$name],
                "Process"
            )
        }
    }
}

function Start-IsolatedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][hashtable]$Environment,
        [string]$RedirectStandardOutput,
        [string]$RedirectStandardError
    )

    $parameters = @{
        FilePath = $FilePath
        ArgumentList = $ArgumentList
        WorkingDirectory = $WorkingDirectory
        WindowStyle = "Hidden"
        PassThru = $true
    }
    if (-not [string]::IsNullOrWhiteSpace($RedirectStandardOutput)) {
        $parameters.RedirectStandardOutput = $RedirectStandardOutput
    }
    if (-not [string]::IsNullOrWhiteSpace($RedirectStandardError)) {
        $parameters.RedirectStandardError = $RedirectStandardError
    }
    return Invoke-WithIsolatedEnvironment $Environment {
        Start-Process @parameters
    }
}

function New-DemoAccessToken {
    $randomBytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($randomBytes)
        return [Convert]::ToBase64String($randomBytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    }
    finally {
        [Array]::Clear($randomBytes, 0, $randomBytes.Length)
        $random.Dispose()
    }
}

function New-GatewayEnvironment {
    param(
        [Parameter(Mandatory = $true)][hashtable]$BaseEnvironment,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$AccessToken,
        [Parameter(Mandatory = $true)][string]$AllowedOrigin,
        [Parameter(Mandatory = $true)][bool]$UseHostedLLM,
        [string]$ProviderApiKey,
        [string]$ProviderBaseUrl,
        [string]$ProviderModel,
        [string]$ProviderMaxTokens
    )

    $environment = Copy-EnvironmentMap $BaseEnvironment
    $environment["PYTHONUTF8"] = "1"
    $environment["PYTHONUNBUFFERED"] = "1"
    $environment["ECHOWEAVE_HOST"] = "127.0.0.1"
    $environment["ECHOWEAVE_PORT"] = [string]$Port
    $environment["ECHOWEAVE_ACCESS_TOKEN"] = $AccessToken
    $environment["ECHOWEAVE_ALLOWED_ORIGINS"] = $AllowedOrigin
    $environment["ECHOWEAVE_TRUSTED_PROXY_IPS"] = "127.0.0.1"
    $environment["ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT"] = "false"
    $environment["ECHOWEAVE_ALLOWED_PERSONAS"] = "demo"
    $environment["ECHOWEAVE_ASR_BACKEND"] = "demo"
    $environment["ECHOWEAVE_TTS_BACKEND"] = "browser"
    $environment["ECHOWEAVE_AVATAR_BACKEND"] = "client_lipsync"
    if ($UseHostedLLM) {
        $environment["ECHOWEAVE_LLM_BACKEND"] = "openai_compatible"
        $environment["OPENAI_COMPAT_API_KEY"] = $ProviderApiKey
        $environment["OPENAI_COMPAT_BASE_URL"] = $ProviderBaseUrl
        $environment["OPENAI_COMPAT_MODEL"] = $ProviderModel
        $environment["OPENAI_COMPAT_MAX_TOKENS"] = $ProviderMaxTokens
    }
    else {
        $environment["ECHOWEAVE_LLM_BACKEND"] = "demo"
    }
    return $environment
}

function Stop-OwnedProcess {
    param([Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }
    $Process.Refresh()
    if (-not $Process.HasExited) {
        $Process.Kill()
        if (-not $Process.WaitForExit(5000)) {
            throw "A demo child process did not exit after termination."
        }
    }
}

function Wait-LocalGateway {
    param(
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Gateway,
        [Parameter(Mandatory = $true)][string]$HealthUrl,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$ErrorLog
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Gateway.HasExited) {
            throw "EchoWeave exited during startup. See $ErrorLog"
        }
        try {
            $response = Invoke-WebRequest -Uri $HealthUrl -Method Get -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "The local gateway health check did not become ready in time."
}

function Wait-QuickTunnelUrl {
    param(
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Tunnel,
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Tunnel.HasExited) {
            throw "cloudflared exited before creating a quick tunnel."
        }
        if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
            $match = [regex]::Match(
                (Get-Content -LiteralPath $LogPath -Raw),
                "https://[a-z0-9-]+\.trycloudflare\.com"
            )
            if ($match.Success) {
                return $match.Value
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out while waiting for the Cloudflare quick-tunnel URL."
}

function Test-PublicHealth {
    param(
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Gateway,
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Tunnel,
        [Parameter(Mandatory = $true)][string]$PublicUrl
    )

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if ($Gateway.HasExited -or $Tunnel.HasExited) {
            return "failed"
        }
        try {
            $response = Invoke-WebRequest -Uri "$PublicUrl/api/health/live" -Method Get -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                return "verified"
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return "pending"
}

function Remove-OwnedStatus {
    param(
        [Parameter(Mandatory = $true)][string]$StatusPath,
        [Parameter(Mandatory = $true)][string]$RunId
    )

    if (-not (Test-Path -LiteralPath $StatusPath -PathType Leaf)) {
        return
    }
    try {
        $state = Read-DemoState $StatusPath
        if ([string]$state.run_id -eq $RunId) {
            Remove-Item -LiteralPath $StatusPath -Force
        }
    }
    catch {
        # Preserve an unexpected state file for manual inspection.
    }
}

function Invoke-StartCloudflareDemo {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $gatewayExecutable = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $gatewayExecutable -PathType Leaf)) {
        throw "EchoWeave's .venv Python was not found. Install the project before starting the demo."
    }
    $gatewayExecutable = (Resolve-Path -LiteralPath $gatewayExecutable).Path
    $cloudflaredExecutable = Resolve-ApprovedCloudflared $repoRoot

    $statusDirectory = Join-Path $repoRoot "runtime\cloudflare-demo"
    $statusPath = Join-Path $statusDirectory "current.json"
    New-Item -ItemType Directory -Path $statusDirectory -Force | Out-Null
    $mutex = Get-DemoMutex $statusPath
    $mutexHeld = $false
    $gateway = $null
    $tunnel = $null
    $runId = [guid]::NewGuid().ToString("N")
    $accessToken = $null

    try {
        $mutexHeld = Enter-DemoMutex $mutex
        if (-not $mutexHeld) {
            throw "Another Cloudflare demo start/stop operation is in progress."
        }
        if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
            Archive-StaleState $statusPath (Read-DemoState $statusPath) $gatewayExecutable
        }

        $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        if ($listeners | Where-Object { $_.Port -eq $Port }) {
            throw "TCP port $Port is already in use."
        }

        $providerApiKey = ""
        $providerBaseUrl = ""
        $providerModel = ""
        $providerMaxTokens = "800"
        if ($UseOpenAICompatible) {
            $providerApiKey = [Environment]::GetEnvironmentVariable("OPENAI_COMPAT_API_KEY", "Process")
            $providerBaseUrl = [Environment]::GetEnvironmentVariable("OPENAI_COMPAT_BASE_URL", "Process")
            $providerModel = [Environment]::GetEnvironmentVariable("OPENAI_COMPAT_MODEL", "Process")
            $configuredMaxTokens = [Environment]::GetEnvironmentVariable("OPENAI_COMPAT_MAX_TOKENS", "Process")
            if (-not [string]::IsNullOrWhiteSpace($configuredMaxTokens)) {
                $providerMaxTokens = $configuredMaxTokens
            }
            foreach ($setting in ([ordered]@{
                OPENAI_COMPAT_API_KEY = $providerApiKey
                OPENAI_COMPAT_BASE_URL = $providerBaseUrl
                OPENAI_COMPAT_MODEL = $providerModel
            }).GetEnumerator()) {
                if ([string]::IsNullOrWhiteSpace([string]$setting.Value)) {
                    throw "$($setting.Name) is required with -UseOpenAICompatible."
                }
            }
        }

        $accessToken = New-DemoAccessToken
        $runDirectory = Join-Path $statusDirectory $runId
        New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
        $tunnelLog = Join-Path $runDirectory "cloudflared.log"
        $tunnelOutLog = Join-Path $runDirectory "cloudflared.out.log"
        $tunnelErrorLog = Join-Path $runDirectory "cloudflared.err.log"
        $bootstrapOutLog = Join-Path $runDirectory "gateway.bootstrap.out.log"
        $bootstrapErrorLog = Join-Path $runDirectory "gateway.bootstrap.err.log"
        $gatewayOutLog = Join-Path $runDirectory "gateway.out.log"
        $gatewayErrorLog = Join-Path $runDirectory "gateway.err.log"
        $origin = "http://127.0.0.1:$Port"
        $gatewayBaseEnvironment = Get-BaseChildEnvironment -IncludeProxy
        $cloudflaredEnvironment = Get-BaseChildEnvironment
        Assert-CloudflaredEnvironment $cloudflaredEnvironment

        $state = [ordered]@{
            schema_version = 2
            run_id = $runId
            phase = "initializing"
            started_at = (Get-Date).ToUniversalTime().ToString("o")
            public_url = $null
            run_directory = $runDirectory
            llm_backend = if ($UseOpenAICompatible) { "openai_compatible" } else { "demo" }
            public_health = "pending"
            processes = [ordered]@{
                gateway = $null
                tunnel = $null
            }
        }
        Write-AtomicJson $state $statusPath

        $bootstrapEnvironment = New-GatewayEnvironment `
            $gatewayBaseEnvironment $Port $accessToken $script:BootstrapOrigin `
            ([bool]$UseOpenAICompatible) $providerApiKey $providerBaseUrl `
            $providerModel $providerMaxTokens
        $gateway = Start-IsolatedProcess `
            $gatewayExecutable `
            @("-m", "echoweave.cli", "serve", "--host", "127.0.0.1", "--port", [string]$Port) `
            $repoRoot $bootstrapEnvironment $bootstrapOutLog $bootstrapErrorLog
        $state.processes.gateway = Get-ProcessIdentity $gateway "gateway" $runId
        $state.phase = "bootstrap_gateway"
        Write-AtomicJson $state $statusPath
        Wait-LocalGateway $gateway "$origin/api/health/live" $StartupTimeoutSeconds $bootstrapErrorLog

        $tunnelArguments = @(
            "tunnel", "--no-autoupdate", "--protocol", "http2",
            "--url", $origin, "--logfile", $tunnelLog, "--loglevel", "info"
        )
        $tunnel = Start-IsolatedProcess `
            $cloudflaredExecutable $tunnelArguments $repoRoot `
            $cloudflaredEnvironment $tunnelOutLog $tunnelErrorLog
        $state.processes.tunnel = Get-ProcessIdentity $tunnel "tunnel" $runId
        $state.phase = "tunnel_starting"
        Write-AtomicJson $state $statusPath
        $publicUrl = Wait-QuickTunnelUrl $tunnel $tunnelLog $StartupTimeoutSeconds
        $state.public_url = $publicUrl

        # The bootstrap gateway already requires this run's token and rejects every
        # public WebSocket Origin. Restart only after Cloudflare supplies the exact host.
        Stop-OwnedProcess $gateway
        $gateway = $null
        $state.processes.gateway = $null
        $state.phase = "gateway_restarting"
        Write-AtomicJson $state $statusPath

        $gatewayEnvironment = New-GatewayEnvironment `
            $gatewayBaseEnvironment $Port $accessToken $publicUrl `
            ([bool]$UseOpenAICompatible) $providerApiKey $providerBaseUrl `
            $providerModel $providerMaxTokens
        $gateway = Start-IsolatedProcess `
            $gatewayExecutable `
            @("-m", "echoweave.cli", "serve", "--host", "127.0.0.1", "--port", [string]$Port) `
            $repoRoot $gatewayEnvironment $gatewayOutLog $gatewayErrorLog
        $state.processes.gateway = Get-ProcessIdentity $gateway "gateway" $runId
        $state.phase = "gateway_starting"
        Write-AtomicJson $state $statusPath
        Wait-LocalGateway $gateway "$origin/api/health/live" $StartupTimeoutSeconds $gatewayErrorLog
        if ($tunnel.HasExited) {
            throw "cloudflared exited while the final gateway was starting. See $tunnelLog"
        }

        $state.public_health = Test-PublicHealth $gateway $tunnel $publicUrl
        $state.phase = "ready"
        Write-AtomicJson $state $statusPath

        Write-Output "PUBLIC_URL=$publicUrl"
        Write-Output "ACCESS_URL=$publicUrl/#token=$accessToken"
        Write-Output "PUBLIC_HEALTH=$($state.public_health)"
        Write-Output "STATUS_PATH=$statusPath"

        $gateway = $null
        $tunnel = $null
    }
    catch {
        $failure = $_
        $cleanupSucceeded = $true
        foreach ($process in @($tunnel, $gateway)) {
            try {
                Stop-OwnedProcess $process
            }
            catch {
                $cleanupSucceeded = $false
            }
        }
        if ($cleanupSucceeded) {
            Remove-OwnedStatus $statusPath $runId
        }
        throw $failure
    }
    finally {
        $accessToken = $null
        if ($mutexHeld) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    Invoke-StartCloudflareDemo
}
