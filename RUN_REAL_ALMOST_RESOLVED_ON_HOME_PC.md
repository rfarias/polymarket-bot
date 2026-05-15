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
aggressive entry order type: limit FAK by default, so it either fills immediately or does not rest
0.99 case: allowed when market is almost fully resolved and distance/buffer filters pass
target: disabled while holding winner to resolution
exit before resolution: stop, structural_stop, profit_protect
post resolution: awaiting_redeem state blocks new entries
```

The real runner logs `entry_order_style`, `entry_order_type`, and the immediate matched size returned by the broker. This is important because earlier paper results could overstate fills from passive orders. For conservative paper comparisons, rerun the paper with:

```powershell
python diagnostics_current_almost_resolved_paper_v1.py --seconds 21600 --poll-secs 1 --order-qty 6 --hybrid-passive-to-aggressive --hybrid-aggressive-after-secs 2 --hybrid-aggressive-max-price 0.99 --passive-fill-touch-polls 2 --hold-winner-to-resolution --resolution-settle-secs 1 --enable-gray-zone --log-file logs\current_almost_resolved_full_setup_paper_v1\full_setup_live.jsonl
```

Use the paper summary's `execution_funnel` before switching to real. The important comparison is not only profit, but conversion from `signal_allowed` to `trade_opened_from_fill`, plus how many candidates died at `aggressive_limit_skip`, `order_cancel`, or `watch_waiting_tick_or_post_only`.

Also compare `stats.by_entry_order_style`. Keep aggressive replacement only if `aggressive_limit` adds meaningful positive PnL after conservative fill assumptions; otherwise run the real path without chasing.

Before enabling any real split execution, test the paper-only split command from `README_ALMOST_RESOLVED_DYNAMIC_DISTANCE.md`. The split is meant only for clear winning `passive_extreme_liquidity_capture` cases that are becoming extreme resolved, not for generic entries.

## 5.1 Future Go Runner

The long-term objective is to make the bot execute as close as possible to the proven manual workflow: capture the same almost-resolved opportunities, avoid unnecessary risk, and prevent one bad exit from erasing many small gains.

Do not rewrite the research stack first. Keep Python for:

```text
historical replay
paper trading
setup research
diagnostics
parameter comparisons
log analysis
```

If real supervised logs confirm that execution latency/fill conversion is the main bottleneck, build a small Go runner only for the real-time execution layer:

```text
market data reader
current almost-resolved signal evaluator
passive/aggressive order executor
cancel/repost loop
stop/structural/profit-protect exits
hard risk guards
JSONL logs compatible with the Python analyzers
```

The Go version must start in dry-run/paper mode and be compared side by side with the Python paper/runner before any real orders. Success criteria are not only PnL; the important metrics are:

```text
signal_allowed -> order_posted conversion
order_posted -> matched fill conversion
fill price versus intended paper entry
exit slippage during thin books
number of missed manual-like opportunities
number of prevented bad exits
```

Only consider Go for real execution after the Python runner has exposed the exact execution gaps we need to solve.

## 5.2 Consistency And Risk Proportionality

The main production goal is not to avoid every loss. Losses and entries that move against us are normal. The real requirement is that one bad trade cannot give back a full day or week of accumulated gains.

Operational rule:

```text
acceptable loss = normal cost of doing business
unacceptable loss = one event that erases many good hands
```

Every real entry must be evaluated against proportional loss, not only against entry quality. A setup should be allowed only when the realistic bad exit is proportional to the average gain of one or a few hands.

Required safeguards before scaling size:

```text
fixed small size during validation
max loss per trade known before entry
max daily loss
max consecutive bad exits
pause after abnormal slippage
liquidity check for exit, not only entry
block entries where likely gain is 1-2 ticks but realistic bad exit is many ticks
log theoretical stop and pessimistic stop separately
```

The bot should optimize for:

```text
more good fills
bounded loss per failed idea
fast recognition of a bad book
no trade when the exit cannot be controlled
```

More entries are useful only if the worst realistic exit stays controlled. A missed trade is acceptable; an uncontrolled exit is not.

The paper logs `signal.planned_exit_risk` for allowed signals. Check it before real execution:

```text
best_bid
observed_bid_depth
qty_at_or_above_stop
vwap_exit_for_qty
theoretical_stop_loss_ticks
pessimistic_exit_loss_ticks
enough_depth_for_qty
exit_depth_covers_stop
```

If the theoretical stop is small but the pessimistic exit loss is large, treat the setup as unsafe even if the entry signal is good.

## 5.3 Future Partially Autonomous Agent

A partially autonomous agent can be added later, but it should not be the first real-money executor. The safer architecture is:

```text
deterministic runner = posts/cancels/exits orders under hard rules
agent layer = observes, summarizes, ranks opportunities, suggests parameter changes
human or hard policy = approves changes before real-money execution
```

The agent may analyze:

```text
current book behavior
price-to-beat distance
recent spot movement
micro trend and reversal risk
chart-like features
missed manual-style opportunities
slippage and failed exits
```

The agent must not bypass hard guards:

```text
max size
max loss per trade
max daily loss
minimum exit liquidity
allowed market types
allowed time windows
no entry during degraded data
no averaging down without explicit rule
```

Chart analysis can help as an additional signal, especially for trend continuation, reversal risk, and whether the current move is stable or exhausted. It should be treated as a filter or confidence score first, not as permission to override execution risk.

Near-term roadmap:

```text
1. finish reliable deterministic paper and real supervised runner
2. measure fill conversion, slippage, and bad-exit frequency
3. add chart/microstructure features to the logs
4. train/evaluate an agent only as an advisor on historical and live paper data
5. allow the agent to recommend, not execute
6. consider limited autonomous decisions only after hard risk guards prove stable
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

## 8. Manual Guardian

For manual trading, use the manual guardian instead of the autonomous entry runner. You place the entry manually; the guardian adopts the current BTC 5m position/order and manages exits.

Start it before entering manually:

```powershell
.\scripts\watch_manual_adopt_current_almost_resolved.ps1 -ArmReal -RunSeconds 1800 -PollSeconds 0.5 -MinAdoptQty 1
```

What it does:

```text
adopts one manual BUY order on the current BTC 5m market
also adopts an already-filled UP/DOWN token balance on the current BTC 5m market
posts exit orders for stop, structural_stop, or profit_protect
does not take target by default
holds winner to resolution by default
marks awaiting_redeem after resolution and waits for manual claim/redeem
```

State and logs:

```text
logs\current_almost_resolved_manual_adopt_state.json
logs\manual_adopt_current_almost_resolved_*
```

Do not run the autonomous almost-resolved real runner and the manual guardian on the same market/account at the same time.
