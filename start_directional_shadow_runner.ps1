$ErrorActionPreference = "Stop"

param(
    [int]$Hours = 8,
    [double]$PollSecs = 1.0,
    [string]$BaseLogDir = "logs\directional_shadow_sessions",
    [switch]$RestartOnFailure
)

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$sessionDir = Join-Path $BaseLogDir $timestamp
New-Item -ItemType Directory -Force -Path $sessionDir | Out-Null

$durationSeconds = [Math]::Max(60, $Hours * 3600)
$runnerArgs = @(
    "diagnostics_directional_shadow_runner_v1.py",
    "--seconds", "$durationSeconds",
    "--poll-secs", "$PollSecs",
    "--log-dir", "$sessionDir"
)

$stdoutPath = Join-Path $sessionDir "runner_stdout.log"
$stderrPath = Join-Path $sessionDir "runner_stderr.log"
$metaPath = Join-Path $sessionDir "session_meta.json"

$meta = @{
    started_at = (Get-Date).ToString("o")
    hours = $Hours
    poll_secs = $PollSecs
    session_dir = $sessionDir
    command = @("python") + $runnerArgs
    restart_on_failure = [bool]$RestartOnFailure
}
$meta | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $metaPath

Write-Host "[SHADOW_RUNNER] session_dir=$sessionDir"
Write-Host "[SHADOW_RUNNER] stdout=$stdoutPath"
Write-Host "[SHADOW_RUNNER] stderr=$stderrPath"

do {
    python @runnerArgs 1>> $stdoutPath 2>> $stderrPath
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        break
    }

    Write-Warning "[SHADOW_RUNNER] runner exited with code $exitCode"
    if (-not $RestartOnFailure) {
        exit $exitCode
    }

    Add-Content -Encoding UTF8 $stdoutPath "[RESTART] $(Get-Date -Format o) exit_code=$exitCode"
    Start-Sleep -Seconds 5
}
while ($true)

Write-Host "[SHADOW_RUNNER] finished with exit_code=$exitCode"
exit $exitCode
