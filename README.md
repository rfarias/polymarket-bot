# polymarket-bot

Base project for a BTC Polymarket bot.

Documentação de handoff:

- [README_ARCHITECTURE.md](/C:/Users/Letícia/Documents/polymarket-bot/README_ARCHITECTURE.md)
- [README_NEXT1_SCALP.md](/C:/Users/Letícia/Documents/polymarket-bot/README_NEXT1_SCALP.md)
- [README_NEXT1_SCALP_REAL_VALIDATION.md](/C:/Users/Letícia/Documents/polymarket-bot/README_NEXT1_SCALP_REAL_VALIDATION.md)
- [README_NEXT1_FILL_CYCLE.md](/C:/Users/Letícia/Documents/polymarket-bot/README_NEXT1_FILL_CYCLE.md)
- [README_CURRENT_SETUPS.md](/C:/Users/Letícia/Documents/polymarket-bot/README_CURRENT_SETUPS.md)
- [README_SCALP_REVERSAL.md](/C:/Users/Letícia/Documents/polymarket-bot/README_SCALP_REVERSAL.md)
- [README_ROADMAP_MULTI_REAL.md](/C:/Users/Letícia/Documents/polymarket-bot/README_ROADMAP_MULTI_REAL.md)
- [README_CONVENTIONS.md](/C:/Users/Letícia/Documents/polymarket-bot/README_CONVENTIONS.md)

## Quickstart (Git Bash / local)

```bash
pip install -r requirements.txt
cp .env.example .env
python run_guarded_bot.py --preflight-only
python run_guarded_bot.py --seconds 900
```

Veja `README_TESTS.md` para a trilha completa de testes offline e live guardado.

Runner de scalp reversal:

```bash
python run_scalp_reversal_bot.py --preflight-only
python run_scalp_reversal_bot.py --seconds 300
```

Runner real de `next1 scalp`:

```bash
python run_live_next1_scalp_real_v1.py --preflight-only
python run_live_next1_scalp_real_v1.py --seconds 1200
```

Runner real de `current scalp`:

```bash
python run_live_current_scalp_real_v1.py --preflight-only
python run_live_current_scalp_real_v1.py --seconds 1800
```
