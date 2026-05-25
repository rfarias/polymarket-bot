"""
_el_check.py — Mostra estado atual do Early Leader a partir dos logs do AR runner.
Roda em qualquer momento enquanto o runner estiver ativo.

Uso: python _el_check.py
"""
import json, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logs_dir = Path("logs")

# Sessão mais recente do AR runner
sessions = sorted(
    [d for d in logs_dir.iterdir() if d.is_dir() and d.name.startswith("current_almost_resolved_real_")],
    key=lambda d: d.stat().st_mtime, reverse=True,
)
if not sessions:
    print("Nenhuma sessão AR runner encontrada em logs/")
    sys.exit(1)

session = sessions[0]
log_file = session / "current_almost_resolved_real.jsonl"
if not log_file.exists():
    print(f"Log não encontrado: {log_file}")
    sys.exit(1)

# Última linha tipo snapshot
last_snap = None
with log_file.open(encoding="utf-8", errors="replace") as f:
    for line in f:
        try:
            e = json.loads(line)
            if e.get("type") == "snapshot":
                last_snap = e
        except Exception:
            continue

if not last_snap:
    print("Nenhum snapshot encontrado ainda.")
    sys.exit(0)

el = last_snap.get("early_leader") or {}
secs = last_snap.get("current_secs")
slug = last_snap.get("current_slug") or ""
signal = last_snap.get("signal") or {}

el_side   = el.get("early_side")
f3_ok     = el.get("f3_ok")
inverted  = el.get("inverted", False)
inv_side  = el.get("inversion_side")
inv_bid   = el.get("inversion_bid", 0.0)
inv_secs  = el.get("inversion_secs")
inv_strong = el.get("inversion_strong", False)

ar_side   = signal.get("side")
ar_allow  = signal.get("allow", False)

print(f"\n{'='*55}")
print(f"  EL CHECK  |  slug: ...{slug[-18:]}  |  secs: {secs}")
print(f"{'='*55}")
print(f"  Early leader:  {el_side or 'não detectado'}")
print(f"  F3 (cont_ok):  {f3_ok}")
print(f"  Invertido:     {inverted}")
if inverted:
    print(f"  Novo líder:    {inv_side}  bid={inv_bid:.3f}  @ secs={inv_secs}")
    print(f"  Inversão forte (>=0.72): {inv_strong}")
print(f"\n  AR signal:  side={ar_side}  allow={ar_allow}")
print()

# Diagnóstico simplificado
if not el_side:
    print("  [?] EL ainda não detectado (mercado pode estar nos primeiros secs)")
elif inverted and inv_strong:
    if inv_side == ar_side:
        print(f"  [✓] INVERSÃO FORTE → {inv_side} é o novo líder (100% histórico)  SEGURO para {inv_side}")
    else:
        print(f"  [✗] INVERSÃO FORTE → {inv_side} assumiu (100% histórico)  PERIGO se entrar em {ar_side}")
elif inverted:
    print(f"  [~] Invertido (bid={inv_bid:.3f} < 0.72) — sinal moderado para {inv_side}")
elif el_side == ar_side and f3_ok:
    print(f"  [✓] EL={el_side} + F3=ok → 93.4% confiança para {el_side}  SEGURO")
elif el_side == ar_side and f3_ok is False:
    print(f"  [~] EL={el_side} mas F3 falhou — momentum enfraqueceu, ~78% apenas")
elif el_side and el_side != ar_side and ar_side:
    print(f"  [✗] EL={el_side} contradiz AR={ar_side} (EXC.OPOSTO) — EVITAR entrada em {ar_side}")
