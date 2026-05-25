"""
Analisa em detalhe os snapshots do trade catastrófico das 19:03.
Mostra signed_distance_from_open_bps, spot_delta e market_delta
para ver se havia sinal de reversão antes do colapso.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

log = Path("logs/current_almost_resolved_real_20260517_185650/current_almost_resolved_real.jsonl")
events = []
with open(log, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except Exception:
                pass

TARGET_SLUG = "btc-updown-5m-1779055200"

print("Snapshots detalhados do trade catastrofico (DOWN@0.98 => STOP@0.001)")
print(f"{'Hora':10}  {'secs':5}  {'down_buy':9}  {'dist_bps':9}  {'d5s_bps':8}  {'d15s_bps':8}  {'mrk_d5s':7}  {'mode':20}")
print("-" * 100)

for e in events:
    if e.get("type") != "snapshot":
        continue
    slug = e.get("trade", {}).get("event_slug") or e.get("current_slug", "")
    if slug != TARGET_SLUG:
        continue

    tr = e.get("trade", {})
    mode = tr.get("mode", "")
    sig = e.get("signal", {})
    ctx = e.get("current_scalp_context", {})
    ref = e.get("reference", {})

    ts = datetime.fromtimestamp(e["ts"], tz=timezone.utc).astimezone()
    secs = e.get("current_secs", "?")
    down_buy = sig.get("down_buy", "?")
    dist_bps = sig.get("signed_distance_from_open_bps", ctx.get("signed_distance_from_open_bps", "?"))
    d5s = ctx.get("spot_delta_5s_bps", "?")
    d15s = ctx.get("spot_delta_15s_bps", "?")
    mrk_d5s = ctx.get("market_delta_5s", "?")
    ref_price = ref.get("reference_price", "?")

    flag = ""
    if mode not in ("idle",):
        # Marcar onde o sinal de reversão poderia ter sido detectado
        try:
            if float(str(d5s)) > 0 and float(str(dist_bps)) > -5:
                flag = "  *** SINAL SPOT"
        except Exception:
            pass

    print(f"  {ts.strftime('%H:%M:%S')}  {str(secs):5}  {str(down_buy):9}  "
          f"{str(dist_bps):9}  {str(d5s):8}  {str(d15s):8}  {str(mrk_d5s):7}  "
          f"{mode:20}  {str(ref_price):10}{flag}")

print()
print("Legenda:")
print("  dist_bps   = signed_distance_from_open_bps: BTC spot vs abertura da janela (neg=DOWN ganhando)")
print("  d5s_bps    = variacao BTC spot nos ultimos 5s (bps)")
print("  d15s_bps   = variacao BTC spot nos ultimos 15s (bps)")
print("  mrk_d5s    = variacao do mercado Polymarket nos ultimos 5s")
print("  *** SINAL  = dist_bps > -5 E d5s > 0 (BTC subindo, margem perigosamente pequena)")
