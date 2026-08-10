[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

function Read-DemoState {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        throw "The Cloudflare demo status file is invalid; no process was stopped."
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

function Get-VerifiedRecordedProcess {
    param(
        $Record,
        [Parameter(Mandatory = $true)][string]$RunId
    )

    if ($null -eq $Record) {
        return $null
    }
    if ([string]$Record.run_id -ne $RunId) {
        throw "A recorded process has the wrong run_id; no process was stopped."
    }
    $processId = 0
    if (-not [int]::TryParse([string]$Record.pid, [ref]$processId) -or $processId -le 0) {
        throw "A recorded process has an invalid PID; no process was stopped."
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
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
        throw "A recorded process identity could not be verified; no process was stopped."
    }
    if (-not [string]::Equals(
        $actualPath,
        (Get-NormalizedPath ([string]$Record.executable_path)),
        [StringComparison]::OrdinalIgnoreCase
    ) -or $actualStart.Ticks -ne $expectedStart.Ticks) {
        throw "A recorded PID now belongs to a different process; no process was stopped."
    }
    return $process
}

function Stop-VerifiedProcess {
    param([Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }
    $Process.Refresh()
    if ($Process.HasExited) {
        return
    }
    # Kill through the already-open Process handle after identity verification;
    # never look the PID up again, which would reintroduce a PID-reuse race.
    $Process.Kill()
    if (-not $Process.WaitForExit(5000)) {
        throw "A verified demo process did not exit after termination."
    }
}

function Invoke-StopCloudflareDemo {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $statusDirectory = Join-Path $repoRoot "runtime\cloudflare-demo"
    $statusPath = Join-Path $statusDirectory "current.json"
    $gatewayExecutable = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
        throw "No running Cloudflare demo status file was found."
    }
    $mutex = Get-DemoMutex $statusPath
    $mutexHeld = $false
    try {
        $mutexHeld = Enter-DemoMutex $mutex
        if (-not $mutexHeld) {
            throw "Another Cloudflare demo start/stop operation is in progress."
        }
        $state = Read-DemoState $statusPath
        Assert-DemoState $state $statusDirectory $gatewayExecutable

        # Validate every recorded identity before terminating either process.
        $tunnel = Get-VerifiedRecordedProcess $state.processes.tunnel ([string]$state.run_id)
        $gateway = Get-VerifiedRecordedProcess $state.processes.gateway ([string]$state.run_id)

        # Remove public ingress first, then terminate the loopback-only gateway.
        Stop-VerifiedProcess $tunnel
        Stop-VerifiedProcess $gateway
        Remove-Item -LiteralPath $statusPath -Force
        Write-Output "Cloudflare demo stopped. Logs remain under runtime/cloudflare-demo/$($state.run_id)."
    }
    finally {
        if ($mutexHeld) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    Invoke-StopCloudflareDemo
}
