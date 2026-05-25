import json, datetime, time, glob, os, sys

def fmt(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime('%H:%M:%S')
    except:
        return str(ts)

def latest_log():
    dirs = sorted(glob.glob(r'logs\current_almost_resolved_real_2026*'))
    if not dirs:
        return None
    return os.path.join(dirs[-1], 'current_almost_resolved_real.jsonl')

print("Monitorando fills... Ctrl+C para parar")
print()

seen_lines = 0
last_session = None
last_mode = None
last_allow_print = 0

while True:
    log = latest_log()
    if not log or not os.path.exists(log):
        time.sleep(1)
        continue

    session = log.split(os.sep)[-2]
    if session != last_session:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Sessao ativa: {session}")
        last_session = session
        seen_lines = 0

    with open(log) as f:
        lines = f.readlines()

    for line in lines[seen_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
            ev = j.get('event') or j.get('type')
            ts = fmt(j.get('ts', ''))
            tr = j.get('trade', {})
            sig = j.get('signal', {})

            if ev == 'snapshot':
                mode = tr.get('mode', 'idle')
                allow = sig.get('allow', False)
                secs = sig.get('secs_to_end', '?')
                variant = sig.get('setup_variant', '')
                side = sig.get('side', '')
                now = time.time()
                if allow and now - last_allow_print > 5:
                    price = sig.get('entry_price', '')
                    print(f"[{ts}] SINAL | {variant} {side} @ {price} | {secs}s restantes")
                    last_allow_print = now
                elif mode != 'idle' and mode != last_mode:
                    print(f"[{ts}] modo -> {mode}")
                last_mode = mode

            elif ev in ('enter', 'passive_limit_placed'):
                side = tr.get('side') or sig.get('side', '')
                price = tr.get('entry_price') or sig.get('entry_price', '')
                variant = tr.get('setup_variant') or sig.get('setup_variant', '')
                print(f"[{ts}] ENTRADA | {variant} {side} @ {price}")

            elif ev in ('fill', 'trade_opened_from_fill'):
                print()
                print(f"{'='*60}")
                print(f"[{ts}] FILL | {tr.get('side')} @ {tr.get('entry_price')} qty={tr.get('entry_qty_filled')}")
                print(f"  variant={tr.get('setup_variant')}  stop={tr.get('stop_price')}  target={tr.get('target_price')}")
                print(f"  hold_to_resolution={tr.get('hold_to_resolution')}")
                print(f"{'='*60}")

            elif ev == 'awaiting_redeem':
                reason = tr.get('last_reason', '')
                print(f"[{ts}] AWAITING_REDEEM | {tr.get('side')} reason={reason}")

            elif ev == 'awaiting_redeem_balance_lag':
                print(f"[{ts}] BALANCE_LAG (fix ativo) | entry_filled={j.get('entry_qty_filled')} secs_since={j.get('secs_since_resolution'):.1f}s")

            elif ev in ('flat', 'redeem_flat'):
                print(f"[{ts}] {ev.upper()} | {tr.get('side')} reason={tr.get('last_reason','')} exit_qty={tr.get('exit_qty_filled',0)}")

            elif ev == 'exit_posted':
                print(f"[{ts}] EXIT_POSTED | {tr.get('side')} reason={tr.get('last_reason','')} stop={tr.get('stop_price')}")

            elif ev == 'entry_cancel':
                print(f"[{ts}] CANCEL | reason={j.get('reason','')}")

            elif ev == 'entry_blocked':
                pass  # silenciar bloqueios rotineiros

            elif ev == 'exception':
                print(f"[{ts}] EXCECAO | {str(j.get('error',''))[:100]}")

        except Exception as e:
            pass

    seen_lines = len(lines)
    time.sleep(0.5)
