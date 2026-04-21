param(
    [Parameter(Mandatory = $true)]
    [int]$Seconds,

    [Parameter(Mandatory = $true)]
    [string]$LogDir
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = "python"

Set-Location $repoRoot
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

& $pythonExe "diagnostics_all_setups_paper_v1.py" `
    "--seconds" "$Seconds" `
    "--poll-secs" "2" `
    "--log-dir" "$LogDir"
