from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = ROOT / "scripts" / "start_cloudflare_demo.ps1"
STOP_SCRIPT = ROOT / "scripts" / "stop_cloudflare_demo.ps1"
PINNED_CLOUDFLARED_SHA256 = (
    "8635da433b6df8194746e88ed9d2589566c20e38bfc2a80e431a348b7c765841"
)


def _script(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _main_body(source: str, function_name: str) -> str:
    marker = f"function {function_name}"
    return source[source.index(marker) :]


def test_launcher_accepts_only_pinned_or_officially_signed_cloudflared():
    source = _script(START_SCRIPT)

    assert PINNED_CLOUDFLARED_SHA256 in source
    assert "Get-FileHash -LiteralPath $bundledPath -Algorithm SHA256" in source
    assert "Get-AuthenticodeSignature -LiteralPath $candidate" in source
    assert '$signature.Status -eq "Valid"' in source
    assert "Cloudflare," in source
    assert "Get-Command cloudflared -CommandType Application" in source


def test_launcher_uses_fresh_token_and_isolated_child_environments():
    source = _script(START_SCRIPT)
    main = _main_body(source, "Invoke-StartCloudflareDemo")

    assert "$accessToken = New-DemoAccessToken" in main
    assert "IsNullOrWhiteSpace($env:ECHOWEAVE_ACCESS_TOKEN)" not in source
    assert "$env:ECHOWEAVE_ACCESS_TOKEN =" not in source
    assert 'Write-Output "ACCESS_URL=$publicUrl/#token=$accessToken"' in main
    assert "Invoke-WithIsolatedEnvironment" in source
    assert "Assert-CloudflaredEnvironment $cloudflaredEnvironment" in main
    assert "$gatewayBaseEnvironment = Get-BaseChildEnvironment -IncludeProxy" in main
    assert "$cloudflaredEnvironment = Get-BaseChildEnvironment" in main
    assert "API[_-]?KEY|TOKEN|SECRET|PASSWORD" in source
    assert '$environment["OPENAI_COMPAT_API_KEY"] = $ProviderApiKey' in source


def test_launcher_records_single_instance_atomic_identity_bound_state():
    source = _script(START_SCRIPT)
    main = _main_body(source, "Invoke-StartCloudflareDemo")

    assert "Get-DemoMutex" in source
    assert '"Global\\EchoWeaveCloudflareDemo-$suffix"' in source
    assert "Another Cloudflare demo start/stop operation is in progress" in main
    assert "Archive-StaleState" in main
    assert "refusing to archive or overwrite it" in source
    assert "Write-AtomicJson" in source
    assert "[IO.File]::Replace(" in source
    assert "[IO.File]::Move(" in source
    assert "schema_version = 2" in main
    assert "run_id = $RunId" in source
    assert "executable_path =" in source
    assert "start_time_utc =" in source


def test_authenticated_bootstrap_gateway_precedes_public_tunnel():
    source = _script(START_SCRIPT)
    main = _main_body(source, "Invoke-StartCloudflareDemo")
    bootstrap = main.index("$bootstrapEnvironment = New-GatewayEnvironment")
    first_gateway = main.index("$gateway = Start-IsolatedProcess", bootstrap)
    tunnel = main.index("$tunnel = Start-IsolatedProcess", first_gateway)
    final_environment = main.index(
        "$gatewayEnvironment = New-GatewayEnvironment", tunnel
    )

    assert '$script:BootstrapOrigin = "https://bootstrap.invalid"' in source
    assert bootstrap < first_gateway < tunnel < final_environment
    assert '$environment["ECHOWEAVE_ACCESS_TOKEN"] = $AccessToken' in source
    assert '$environment["ECHOWEAVE_ALLOWED_ORIGINS"] = $AllowedOrigin' in source
    assert '$environment["ECHOWEAVE_TRUSTED_PROXY_IPS"] = "127.0.0.1"' in source
    assert (
        '$environment["ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT"] = "false"' in source
    )
    assert "$cloudflaredEnvironment $tunnelOutLog $tunnelErrorLog" in main


def test_stop_validates_process_identity_and_removes_ingress_first():
    source = _script(STOP_SCRIPT)
    main = _main_body(source, "Invoke-StopCloudflareDemo")

    tunnel_validation = main.index("$tunnel = Get-VerifiedRecordedProcess")
    gateway_validation = main.index("$gateway = Get-VerifiedRecordedProcess")
    tunnel_stop = main.index("Stop-VerifiedProcess $tunnel")
    gateway_stop = main.index("Stop-VerifiedProcess $gateway")
    assert tunnel_validation < gateway_validation < tunnel_stop < gateway_stop
    assert "MainModule.FileName" in source
    assert "StartTime.ToUniversalTime()" in source
    assert "[string]$Record.run_id -ne $RunId" in source
    assert "$Process.Kill()" in source
    assert "Stop-Process -Id" not in source


def test_status_payload_does_not_persist_credentials():
    source = _script(START_SCRIPT)
    main = _main_body(source, "Invoke-StartCloudflareDemo")
    state_start = main.index("$state = [ordered]@{")
    state_end = main.index("Write-AtomicJson $state $statusPath", state_start)
    state_payload = main[state_start:state_end]

    assert "accessToken" not in state_payload
    assert "providerApiKey" not in state_payload
    assert "OPENAI_COMPAT_API_KEY" not in state_payload


POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None


def _run_powershell(command: str) -> subprocess.CompletedProcess[str]:
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell is required for this runtime probe")
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_isolated_process_does_not_inherit_provider_key(tmp_path: Path):
    output_path = tmp_path / "child-environment.txt"
    command = f"""
    . '{START_SCRIPT}'
    $env:OPENAI_COMPAT_API_KEY = 'offline-test-sentinel'
    $env:HTTPS_PROXY = 'https://user:password@example.invalid:443'
    $childEnvironment = Get-BaseChildEnvironment
    $process = Start-IsolatedProcess `
        $env:COMSPEC @('/d', '/c', 'set') '{ROOT}' `
        $childEnvironment '{output_path}' $null
    $process.WaitForExit()
    $childText = [IO.File]::ReadAllText('{output_path}')
    if ($childText.Contains('OPENAI_COMPAT_API_KEY')) {{ exit 11 }}
    if ($env:OPENAI_COMPAT_API_KEY -ne 'offline-test-sentinel') {{ exit 12 }}
    if ($childText.Contains('HTTPS_PROXY')) {{ exit 13 }}
    if ($env:HTTPS_PROXY -ne 'https://user:password@example.invalid:443') {{ exit 14 }}
    """

    result = _run_powershell(command)

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_stale_state_with_identity_mismatch_is_not_archived(tmp_path: Path):
    status_path = tmp_path / "current.json"
    run_id = "0123456789abcdef0123456789abcdef"
    run_directory = tmp_path / run_id
    command = f"""
    . '{START_SCRIPT}'
    $current = Get-Process -Id $PID
    $state = [ordered]@{{
        schema_version = 2
        run_id = '{run_id}'
        run_directory = '{run_directory}'
        processes = [ordered]@{{
            gateway = [ordered]@{{
                pid = $current.Id
                role = 'gateway'
                run_id = '{run_id}'
                executable_path = $current.MainModule.FileName
                start_time_utc = $current.StartTime.ToUniversalTime().AddSeconds(-1).ToString('o')
            }}
            tunnel = $null
        }}
    }}
    Write-AtomicJson $state '{status_path}'
    try {{
        Archive-StaleState '{status_path}' (Read-DemoState '{status_path}') `
            $current.MainModule.FileName
        exit 41
    }}
    catch {{
        if (-not (Test-Path -LiteralPath '{status_path}')) {{ exit 42 }}
        exit 0
    }}
    """

    result = _run_powershell(command)

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_process_identity_rejects_changed_start_time_without_stopping():
    command = f"""
    . '{STOP_SCRIPT}'
    $current = Get-Process -Id $PID
    $runId = '0123456789abcdef0123456789abcdef'
    $record = [pscustomobject]@{{
        pid = $current.Id
        role = 'gateway'
        run_id = $runId
        executable_path = $current.MainModule.FileName
        start_time_utc = $current.StartTime.ToUniversalTime().AddSeconds(-1).ToString('o')
    }}
    try {{
        Get-VerifiedRecordedProcess $record $runId | Out-Null
        exit 21
    }}
    catch {{
        if ($current.HasExited) {{ exit 22 }}
        exit 0
    }}
    """

    result = _run_powershell(command)

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_atomic_json_replacement_leaves_no_temporary_file(tmp_path: Path):
    status_path = tmp_path / "current.json"
    command = f"""
    . '{START_SCRIPT}'
    Write-AtomicJson @{{ value = 1 }} '{status_path}'
    Write-AtomicJson @{{ value = 2 }} '{status_path}'
    $state = Get-Content -LiteralPath '{status_path}' -Raw | ConvertFrom-Json
    if ($state.value -ne 2) {{ exit 31 }}
    if (Get-ChildItem -LiteralPath '{tmp_path}' -Filter '*.tmp') {{ exit 32 }}
    """

    result = _run_powershell(command)

    assert result.returncode == 0, result.stderr
