param(
    [int]$TailEvents = 20,
    [int]$RefreshSeconds = 5
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Short-Id($value) {
    $text = [string]$value
    if ([string]::IsNullOrWhiteSpace($text)) { return "-" }
    if ($text.Length -le 12) { return $text }
    return $text.Substring(0, 8) + "..." + $text.Substring($text.Length - 4)
}

function Fmt-Num($value) {
    if ($null -eq $value) { return "-" }
    try {
        $n = [double]$value
        return $n.ToString("0.######", [Globalization.CultureInfo]::InvariantCulture)
    } catch {
        return [string]$value
    }
}

function First-Text($values) {
    foreach ($v in $values) {
        if ($null -ne $v -and -not [string]::IsNullOrWhiteSpace([string]$v)) {
            return [string]$v
        }
    }
    return "-"
}

function Event-Line($row) {
    $ts = "?"
    try {
        $dt = [DateTimeOffset]::FromUnixTimeSeconds([int64][double]$row.ts).ToLocalTime().DateTime
        $ts = $dt.ToString("HH:mm:ss")
    } catch {}

    $trade = $row.trade
    $signal = $row.signal
    $side = First-Text @($trade.side, $signal.side)
    $qty = First-Text @($trade.entry_qty_filled, $trade.entry_qty_requested)
    $entry = First-Text @($trade.entry_price, $signal.entry_price)

    switch ([string]$row.type) {
        "startup" {
            $mode = First-Text @($row.restored_trade.mode)
            $orders = 0
            if ($row.startup_orders) { $orders = @($row.startup_orders).Count }
            return "$ts | startup | open_orders=$orders | restored=$mode"
        }
        "enter" {
            $style = First-Text @($row.entry_order_style)
            $posted = First-Text @($row.posted_entry_price, $entry)
            $order = Short-Id $trade.entry_order_id
            return "$ts | ordem $style postada | BUY $side qty=$(Fmt-Num $trade.entry_qty_requested) @$(Fmt-Num $posted) | id=$order"
        }
        "entry_cancel" {
            $order = Short-Id $trade.entry_order_id
            return "$ts | ordem limite cancelada/encerrada | id=$order | mode=$(First-Text @($trade.mode))"
        }
        "entry_replace_aggressive_limit" {
            $from = Short-Id $row.from_trade.entry_order_id
            $to = Short-Id $trade.entry_order_id
            return "$ts | limite cancelada e agressiva ativada | @$(Fmt-Num $row.aggressive_price) | from=$from to=$to"
        }
        "entry_fak_no_match" {
            return "$ts | agressiva FAK sem contraparte | @$(Fmt-Num $row.aggressive_price) | segue sem panic"
        }
        "entry_aggressive_skip" {
            return "$ts | agressiva ignorada | ask=$(Fmt-Num $row.aggressive_price) max=$(Fmt-Num $row.max_price)"
        }
        "fill" {
            $order = Short-Id $trade.entry_order_id
            return "$ts | fill entrada | BUY $side qty=$(Fmt-Num $trade.entry_qty_filled) @$(Fmt-Num $entry) | id=$order"
        }
        "exit_posted" {
            $order = Short-Id $trade.exit_order_id
            return "$ts | saida postada | SELL $side restante=$(Fmt-Num $trade.remaining_position_qty) | motivo=$(First-Text @($row.reason, $trade.last_reason)) | id=$order"
        }
        "exit_repost" {
            $order = Short-Id $trade.exit_order_id
            return "$ts | saida repostada | SELL $side restante=$(Fmt-Num $trade.remaining_position_qty) | id=$order"
        }
        "awaiting_redeem" {
            return "$ts | aguardando redeem | $side qty=$(Fmt-Num $trade.entry_qty_filled) | bid=$(Fmt-Num $row.active_bid) | $(First-Text @($trade.last_reason, $row.reason))"
        }
        "redeem_flat" {
            return "$ts | redeem coletado/plataforma zerou | qty=$(Fmt-Num $row.token_balance_qty)"
        }
        "flat" {
            return "$ts | posicao zerada | qty=$(Fmt-Num $row.token_balance_qty) | mode=$(First-Text @($trade.mode))"
        }
        "shutdown_flat" {
            return "$ts | shutdown limpo | posicao zerada"
        }
        "exception" {
            $err = [string]$row.error
            if ($err.Length -gt 140) { $err = $err.Substring(0, 140) + "..." }
            return "$ts | ERRO | $err"
        }
        "panic" {
            $reason = [string]$row.reason
            if ($reason.Length -gt 140) { $reason = $reason.Substring(0, 140) + "..." }
            return "$ts | PANIC | $reason"
        }
        default {
            return $null
        }
    }
}

while ($true) {
    $latest = Get-ChildItem logs -Directory |
        Where-Object { $_.Name -like "current_almost_resolved_real_*" } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    Clear-Host
    Write-Host ("CURRENT ALMOST RESOLVED REAL - RESUMO - " + (Get-Date))

    $py = Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match "run_live_current_almost_resolved_real_v1.py" -and $_.Name -eq "python.exe" }
    if ($py) {
        Write-Host ("RUNNER: ON pid=" + (($py | Select-Object -ExpandProperty ProcessId) -join ",")) -ForegroundColor Green
    } else {
        Write-Host "RUNNER: STOPPED" -ForegroundColor Red
    }

    if (-not $latest) {
        Write-Host "LOG: aguardando diretorio..."
        Start-Sleep -Seconds $RefreshSeconds
        continue
    }

    $file = Join-Path $latest.FullName "current_almost_resolved_real.jsonl"
    Write-Host ("LOG: " + $file)
    if (-not (Test-Path $file)) {
        Write-Host "LOG: aguardando arquivo..."
        Start-Sleep -Seconds $RefreshSeconds
        continue
    }

    $age = [int]((Get-Date) - (Get-Item $file).LastWriteTime).TotalSeconds
    Write-Host ("LOG_AGE_SECONDS: " + $age)
    Write-Host ""

    $lines = New-Object System.Collections.Generic.List[string]
    Get-Content $file -Tail 500 | ForEach-Object {
        try {
            $row = $_ | ConvertFrom-Json
            $line = Event-Line $row
            if ($line) { $lines.Add($line) }
        } catch {}
    }

    if ($lines.Count -eq 0) {
        Write-Host "Sem eventos operacionais ainda. Aguardando sinal..."
    } else {
        $lines | Select-Object -Last $TailEvents | ForEach-Object { Write-Host $_ }
    }

    Start-Sleep -Seconds $RefreshSeconds
}
