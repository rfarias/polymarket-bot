import sys, os
sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv
load_dotenv()

print("=== PREFLIGHT ===")

# 1. Compile check
import py_compile
py_compile.compile("market/live_current_almost_resolved_real_v1.py", doraise=True)
print("[OK] compile: market/live_current_almost_resolved_real_v1.py")

# 2. Broker connectivity + balance
from broker.clob_broker_v2 import ClobBrokerV2
b = ClobBrokerV2()
bal = b.get_balance_allowance(asset_type="COLLATERAL")
usd = round(float(bal.get("balance", 0)) / 1e6, 4)
print(f"[OK] colateral disponivel: ${usd}")
if usd < 1.0:
    print("[WARN] colateral baixo — verificar antes de entrar")

# 3. Env flags
passive = os.environ.get("POLY_CURRENT_ALMOST_RESOLVED_PASSIVE_CAPTURE_ONLY", "true").lower()
enabled = os.environ.get("POLY_CURRENT_ALMOST_RESOLVED_REAL_ENABLED", "false").lower()
print(f"[OK] PASSIVE_CAPTURE_ONLY={passive}  REAL_ENABLED={enabled}")

# 4. State file check
from pathlib import Path
state = Path("state/current_almost_resolved_real_v1.json")
if state.exists():
    import json
    s = json.loads(state.read_text())
    mode = s.get("mode", "?")
    print(f"[INFO] state existente: mode={mode}")
    if mode not in ("idle", "awaiting_redeem"):
        print(f"[WARN] posicao aberta detectada no state: {s}")
else:
    print("[OK] sem state anterior")

print("=== PREFLIGHT OK ===")
