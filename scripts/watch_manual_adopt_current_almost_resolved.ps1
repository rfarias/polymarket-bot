param(
    [int]$RunSeconds = 1800,
    [double]$PollSeconds = 0.5,
    [double]$MinAdoptQty = 1.0,
    [switch]$ArmReal
)

$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$python = "python"

Set-Location $repo

if ($ArmReal) {
    $env:POLY_GUARDED_ENABLED = "true"
    $env:POLY_GUARDED_SHADOW_ONLY = "false"
    $env:POLY_GUARDED_REAL_POSTS_ENABLED = "true"
}

$env:POLY_MANUAL_ADOPT_CURRENT_ALMOST_RESOLVED_ENABLED = "true"
$env:POLY_MANUAL_ADOPT_EXISTING_BALANCE = "true"
$env:POLY_MANUAL_ADOPT_HOLD_WINNER_TO_RESOLUTION = "true"
$env:POLY_MANUAL_ADOPT_MIN_QTY = [string]$MinAdoptQty
$env:POLY_MANUAL_ADOPT_POLL_SECS = [string]$PollSeconds
$env:POLY_MANUAL_ADOPT_RUN_SECONDS = [string]$RunSeconds

& $python "run_manual_adopt_current_almost_resolved_v1.py" "--seconds" ([string]$RunSeconds)
