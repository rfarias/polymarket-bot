# Run Real Almost-Resolved On Home PC

Use this checklist after pulling the repo on the machine that has the wallet configured.

## 1. Update And Inspect

```powershell
git pull
git log -1 --oneline
git status --short
```

Expected commit: `283758a` or newer.

Stop if `git status --short` shows unexpected modified code files. Logs, `.env`, and local state files are the only expected local differences.

## 2. Compile Check

```powershell
python -m py_compile diagnostics_current_almost_resolved_paper_v1.py market\current_almost_resolved_signal_v1.py market\live_current_almost_resolved_real_v1.py run_live_current_almost_resolved_real_v1.py
```

Stop if this command prints any Python error.

## 3. Preflight

```powershell
$env:POLY_GUARDED_ENABLED="true"
$env:POLY_GUARDED_SHADOW_ONLY="false"
$env:POLY_GUARDED_REAL_POSTS_ENABLED="true"
$env:POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED="true"
$env:POLY_CURRENT_ALMOST_RESOLVED_QTY="6"

python run_live_current_almost_resolved_real_v1.py --preflight-only
```

Only continue if preflight confirms:

```text
broker env ready
broker health ok
no unexpected open orders
state absent, idle, or safely restorable
```

Do not start the runner if preflight shows:

```text
awaiting_redeem
unexpected open orders
missing POLY_* credentials
guard/shadow mode still blocking real posts
```

If `awaiting_redeem` appears, claim/redeem on Polymarket first, confirm USDC returned to portfolio, then reconcile/clear state deliberately.

## 4. Start Supervised Runner

Short first run:

```powershell
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 6 -RunSeconds 300 -PollSeconds 0.5
```

Longer supervised run:

```powershell
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 6 -RunSeconds 1800 -PollSeconds 0.5
```

The watcher runs one cycle by default and then stops. Use continuous mode only while actively monitoring:

```powershell
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 6 -RunSeconds 300 -PollSeconds 0.5 -Continuous
```

## 4.1 Position Size Plan

Use `6` shares only while the setup is still in supervised real testing.

Size roadmap:

```text
6 shares   -> test size
50 shares  -> first production target, because maker rebates start to matter from this size
100 shares -> later target, where each tick is approximately 1 USD
```

Do not move to `50` or `100` until the real logs confirm:

```text
entries are posting at the intended prices
unfilled passive entries are cancelled before aggressive replacement
stops and structural exits are closing correctly
resolution creates awaiting_redeem instead of opening a new trade
manual claim/redeem returns USDC to portfolio as expected
```

When ready for the next size, only change `-Qty`:

```powershell
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 50 -RunSeconds 1800 -PollSeconds 0.5
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 100 -RunSeconds 1800 -PollSeconds 0.5
```

## 5. What It Does

```text
setup: current almost-resolved
entry: hybrid limit
qty: controlled by -Qty, test default is 6 shares
first entry attempt: passive limit 1 tick below executable entry
second attempt: cancel passive, then aggressive/marketable limit at current ask, max 0.99
0.99 case: allowed when market is almost fully resolved and distance/buffer filters pass
target: disabled while holding winner to resolution
exit before resolution: stop, structural_stop, profit_protect
post resolution: awaiting_redeem state blocks new entries
```

## 6. While Running

Watch the terminal and latest log under:

```text
logs\current_almost_resolved_real_*
logs\current_almost_resolved_real_state.json
```

If a position reaches resolution, expect:

```text
mode = awaiting_redeem
```

At that point the bot should not open a new entry until claim/redeem is handled.

## 7. Stop

If no position is open, press `Ctrl+C`.

If a position is open, avoid killing blindly. Check the current state/log first and let the runner either exit by stop/protection or reach `awaiting_redeem`.
