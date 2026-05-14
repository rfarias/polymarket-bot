# BTC 5m Current: Probability Research and Live Runners

This document records the current state of the BTC 5m Polymarket research as of 2026-05-13. The goal is to continue the work from another machine with real-order credentials configured.

## Main Conclusion

The most promising direction found so far is not buying `0.98/0.99` almost-resolved outcomes. The stronger statistical candidate is:

```text
after the market becomes current,
identify the first evident side,
enter that side while odds are still near 0.45-0.55,
and manage risk mechanically.
```

In saved logs, the market started to one side and finished on the same side about `68%` of the time.

When entry price was recalculated from contextual bid/ask:

```text
odds 0.45-0.50:
67 cases
67.2% same-side finish
estimated ROI: +40.8%

odds 0.50-0.55:
66 cases
63.6% same-side finish
estimated ROI: +20.7%

odds 0.45-0.55 combined:
133 cases
65.4% same-side finish
average price: 0.5019
estimated ROI: +30.3%
```

This should be treated as a candidate edge from our logs, not as final proof.

## Almost Resolved Risk Finding

For `0.98/0.99` almost-resolved cases, the biggest recurring danger was not counter price. It was:

```text
the contract looked resolved,
but BTC was still very close to price_to_beat/open.
```

High-price failure patterns from saved logs:

```text
peak_side_price >= 0.98
abs(distance_from_open_bps) <= 2:
  80% of failures
  6% of survivors

abs(distance_from_open_bps) <= 4:
  90% of failures
  17% of survivors
```

Distance should be interpreted relative to volatility:

```text
crossed_price_to_beat median distance/range_60s: ~0.97
survived median distance/range_60s: ~2.20
```

So a fixed `70 USD` distance can still be fragile if the recent 60s range is also near `70 USD`.

## Reversal Against Almost Resolved

The best observed candidate for buying the cheap opposite side was:

```text
leader price: 0.95-0.96
counter price: 0.03-0.04
secs_to_end: about 45-75
adverse move against leader in last 5s >= 1 bps
distance still fragile versus price_to_beat
```

Observed from logs:

```text
0.95-0.96 + pre_5s_adverse >= 1 bps:
3 signals
2 wins
66.7% hit rate
```

The sample is very small. This setup is not practical manually because the useful signal appears in a very short 5s window. It is only suitable for a bot.

The broader `0.95-0.97` group:

```text
31 signals
5 reversals
16.1% hit rate
average counter entry: 0.0242
average successful exit: 0.596
paper ROI estimate: +225%
```

The 30s adverse move did not work well as an entry filter for this setup. The 5s adverse move looked more relevant.

## Distance and Reversal Probabilities

New analyzer:

```text
analyze_5m_market_statistics_v1.py
```

Run used:

```powershell
python analyze_5m_market_statistics_v1.py --paths logs/current_almost_resolved*.jsonl logs/rigid_resolved_tick*.jsonl --out-prefix logs/market_statistics_current_plus_rigid_v1
```

Outputs:

```text
logs/market_statistics_current_plus_rigid_v1.first_touch.csv
logs/market_statistics_current_plus_rigid_v1.start_finish.csv
logs/market_statistics_current_plus_rigid_v1.summary.json
```

Summary from `484` slugs and `20,898` snapshots:

```text
After first touching >= 50 USD from price_to_beat:
329 cases
43 final reversals
13.1% final reversal
17.0% crossed price_to_beat after touch
```

By USD distance:

```text
>= 20 USD  -> 24.5% final reversal
>= 30 USD  -> 18.4%
>= 40 USD  -> 14.6%
>= 50 USD  -> 13.1%
>= 60 USD  ->  9.5%
>= 70 USD  ->  6.1%
>= 80 USD  ->  4.7%
>=100 USD  ->  3.0%
```

By bps:

```text
>= 2 bps   -> 27.5% final reversal
>= 4 bps   -> 16.9%
>= 6 bps   -> 13.3%
>= 8 bps   ->  8.8%
>=10 bps   ->  5.4%
>=12 bps   ->  3.5%
```

By distance/recent 60s volatility:

```text
>= 0.75x range_60s -> 30.6% final reversal
>= 1.00x range_60s -> 26.1%
>= 1.50x range_60s -> 23.4%
>= 2.00x range_60s -> 19.9%
>= 2.50x range_60s -> 17.7%
>= 3.00x range_60s -> 14.9%
```

## Previous Candle and Next1 Predictors

New analyzer:

```text
analyze_direction_predictors_v1.py
```

Run used:

```powershell
python analyze_direction_predictors_v1.py --out-prefix logs/direction_predictors_current_plus_rigid_v1
```

Outputs:

```text
logs/direction_predictors_current_plus_rigid_v1.previous_candle.csv
logs/direction_predictors_current_plus_rigid_v1.next1.csv
logs/direction_predictors_current_plus_rigid_v1.summary.json
```

Findings:

```text
same-side current cases with previous candle available: 232
previous final side == current start side: 58.6%
previous final side != current start side: 41.4%
```

But this did not improve prediction:

```text
current started continuing previous final side:
64.5% finished same-side

current started reversing previous final side:
64.9% finished same-side
```

Using `all_setups` logs where `next1_slug` was available:

```text
518 joined cases
next1 odds predicted current start side: 51.9%
next1 odds predicted final side: 53.3%

next1_edge >= 0.10:
151 cases
predicted final side: 50.3%
```

Conclusion: previous candle and `next_1` odds are context only for now. They are not strong entry triggers in the current logs.

## Runners Added

### Current Almost-Resolved Guardian Runner

Files:

```text
market/live_current_almost_resolved_real_v1.py
run_live_current_almost_resolved_real_v1.py
scripts/watch_current_almost_resolved_real.ps1
```

Current operating line:

```text
setup: current almost-resolved
entry: hybrid limit
  1. post passive limit 1 tick below current executable entry
  2. if still valid and unfilled after 1.5s, cancel passive order
  3. only after cancel confirmation, post aggressive/marketable limit at current ask
target: disabled in guardian hold mode
exits: stop, structural_stop, profit_protect
winner handling: hold to resolution when the side remains favorable
post-resolution: awaiting_redeem state, no new entries until claim/redeem is handled
```

Default real quantity is `6` shares while the runner is still in supervised test mode:

```text
POLY_CURRENT_ALMOST_RESOLVED_QTY=6
```

The technical minimum remains `5`, but `6` is the operational test default because fills/residuals can end up around `4.99`, which can block a later limit close due to minimum order size.

Size roadmap:

```text
6 shares   -> supervised test size only
50 shares  -> first production size target, because this is where maker rebates start to matter
100 shares -> later target size, where 1 tick is approximately 1 USD
```

Do not move from `6` to `50` or `100` until the supervised real logs show that entries, stops, profit protection, resolution handling, and manual claim/redeem are behaving correctly.

Recommended real start command on the configured wallet machine:

```powershell
python run_live_current_almost_resolved_real_v1.py --preflight-only
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 6 -RunSeconds 300 -PollSeconds 0.5
```

The watcher now runs one cycle by default. This is the safer mode when you only want the bot active while you are monitoring it. To run a longer supervised session, increase `-RunSeconds`, for example:

```powershell
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 6 -RunSeconds 1800 -PollSeconds 0.5
```

After the test phase, the same runner can be started with larger size:

```powershell
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 50 -RunSeconds 1800 -PollSeconds 0.5
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 100 -RunSeconds 1800 -PollSeconds 0.5
```

Use continuous restart mode only while you are actively watching the machine:

```powershell
.\scripts\watch_current_almost_resolved_real.ps1 -ArmReal -Qty 6 -RunSeconds 300 -PollSeconds 0.5 -Continuous
```

Operational checklist after `git pull` on another machine:

```powershell
git status --short
git log -1 --oneline
python -m py_compile diagnostics_current_almost_resolved_paper_v1.py market\live_current_almost_resolved_real_v1.py run_live_current_almost_resolved_real_v1.py
python run_live_current_almost_resolved_real_v1.py --preflight-only
```

Only start real monitoring if:

```text
git status --short is clean, or only contains known local config/log files
latest commit is 06dbda8 or newer
py_compile passes
preflight passes broker/env/open-order checks
logs/current_almost_resolved_real_state.json is absent or mode is idle
there are no unexpected open orders on Polymarket
```

If preflight prints `awaiting_redeem`, do not start the runner. First claim/redeem the resolved position on Polymarket, confirm USDC returned to the portfolio, then clear/reconcile the state deliberately.

Required real flags:

```text
POLY_GUARDED_ENABLED=true
POLY_GUARDED_SHADOW_ONLY=false
POLY_GUARDED_REAL_POSTS_ENABLED=true
POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED=true
POLY_CURRENT_ALMOST_RESOLVED_HYBRID_ENTRY=true
POLY_CURRENT_ALMOST_RESOLVED_HOLD_WINNER_TO_RESOLUTION=true
POLY_CURRENT_ALMOST_RESOLVED_AUTO_REDEEM_ENABLED=false
```

The watcher sets these guard flags for the current PowerShell process only when `-ArmReal` is passed. Credentials still need to come from `.env` or the machine environment.

Important post-resolution behavior:

```text
If a position is held to resolution, the bot writes:
logs/current_almost_resolved_real_state.json
mode = awaiting_redeem

In this state the bot does not open new entries. The winning position must be claimed/redeemed manually on Polymarket, then the state can be cleared after confirming the USDC returned to the portfolio.
```

Stop instructions:

```text
If running in the foreground, press Ctrl+C.
If a position is already open, do not kill the terminal blindly; first check the current log/state and let the runner flatten or reach awaiting_redeem.
If using -Continuous, Ctrl+C stops future cycles only after interrupting the current process.
```

### Counter-Reversal Runner

Files:

```text
market/live_counter_reversal_real_v1.py
run_live_counter_reversal_real_v1.py
```

Paper mode:

```powershell
python run_live_counter_reversal_real_v1.py --seconds 21600 --log-dir logs\counter_reversal_live_paper_v1
```

Real mode:

```powershell
python run_live_counter_reversal_real_v1.py --execute --seconds 21600
```

Real mode requires:

```text
POLY_GUARDED_ENABLED=true
POLY_GUARDED_SHADOW_ONLY=false
POLY_GUARDED_REAL_POSTS_ENABLED=true
POLY_COUNTER_REVERSAL_REAL_ENABLED=true
POLY_PRIVATE_KEY
POLY_API_KEY
POLY_API_SECRET
POLY_PASSPHRASE
```

Default rule:

```text
leader 0.95-0.965
counter 0.01-0.04
45s-75s remaining
adverse 5s against leader >= 1 bps
fragile distance:
  abs(distance_from_open_bps) <= 7
  or distance/range_60s <= 2.5
```

Default exits:

```text
target: 0.18
runner target: 0.45
deadline flatten: 4s
cheap option stop: 0.01
leader recovered: flatten
```

### Dual Setup Coordination

`market/live_current_almost_resolved_real_v1.py` and `market/live_counter_reversal_real_v1.py` now check each other's state files:

```text
logs/current_almost_resolved_real_state.json
logs/counter_reversal_real_state.json
```

If one setup has an active state, the other logs `entry_blocked` and does not enter.

This is intended to allow both real runners to run at the same time without opening conflicting positions on the same current market.

## Guardian / Stop Work

Files:

```text
diagnostics_current_almost_resolved_guardian_v1.py
diagnostics_current_almost_resolved_guardian_matrix_v1.py
```

Purpose:

```text
monitor manual/current almost-resolved positions
detect fragile almost-resolved states
simulate or execute stop attempts
retry limit first, then market/FAK when needed
```

The guardian includes protective grace for cases where the side is still winning, time remains, distance is large enough, and adverse pressure is not extreme. It also blocks false confidence when `0.98/0.99` is close to `price_to_beat` relative to recent volatility.

## Important Caution

The current statistical edge comes from saved local logs, not a complete historical Polymarket dataset. Before increasing size:

```text
1. keep collecting live paper logs
2. rerun the analyzers daily
3. compare paper fills with realistic bid/ask execution
4. only enable real orders with small size first
```

The next development target should be a dedicated `initial_direction_continuation` runner:

```text
current market
wait until initial side is evident
entry odds 0.45-0.55
avoid high source divergence and wide spread
exit/stop mechanically
log every decision
```
