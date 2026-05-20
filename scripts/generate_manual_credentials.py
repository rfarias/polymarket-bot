"""
Generate and save API credentials for the manual trading account.

Reads the EOA private key via getpass (no screen echo), derives
API key/secret/passphrase using Polymarket's CLOB SDK, and writes
them to .env.manual — the private key is never printed or logged.

Usage:
    python scripts/generate_manual_credentials.py
"""

import getpass
import os
import sys

# Ensure project root is on the path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ENV_FILE = os.path.join(ROOT, ".env.manual")
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
SIGNATURE_TYPE = 1
DEFAULT_FUNDER = "0x531bfECd0f4C62784B9c2FDc01fEA239182DE19f"


def load_sdk():
    try:
        from py_clob_client_v2.client import ClobClient
        return ClobClient
    except ImportError:
        pass
    try:
        from py_clob_client.client import ClobClient
        return ClobClient
    except ImportError:
        pass
    print("[ERROR] py_clob_client_v2 (or py_clob_client) not installed.")
    print("        Run: pip install py-clob-client")
    sys.exit(1)


def read_existing_funder() -> str:
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE).readlines():
            if line.startswith("POLY_FUNDER="):
                val = line.strip().split("=", 1)[1]
                if val:
                    return val
    return DEFAULT_FUNDER


def main():
    print("=" * 55)
    print(" Polymarket — Gerador de credenciais API (conta manual)")
    print("=" * 55)
    print()
    print("A chave privada NÃO será exibida nem gravada em nenhum log.")
    print()

    # Read private key without echoing
    private_key = getpass.getpass("Digite sua EOA private key (0x...): ").strip()
    if not private_key:
        print("[ERROR] Nenhuma chave fornecida. Abortando.")
        sys.exit(1)
    # Remove 0x prefix, strip all whitespace/invisible chars, restore prefix
    hex_part = private_key.removeprefix("0x").removeprefix("0X")
    hex_part = "".join(c for c in hex_part if c in "0123456789abcdefABCDEF")
    if len(hex_part) != 64:
        print(f"[ERROR] Chave inválida: esperado 64 caracteres hex, encontrado {len(hex_part)}.")
        print("        Verifique se copiou a chave completa sem espaços extras.")
        sys.exit(1)
    private_key = "0x" + hex_part

    existing_funder = read_existing_funder()
    funder_input = input(f"Endereço proxy wallet/funder [{existing_funder}]: ").strip()
    funder = funder_input if funder_input else existing_funder

    print()
    print("[*] Conectando ao CLOB e derivando credenciais API...")

    ClobClient = load_sdk()
    try:
        client = ClobClient(
            host=HOST,
            key=private_key,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
            funder=funder,
        )
        creds = client.create_or_derive_api_creds()
    except Exception as exc:
        print(f"[ERROR] Falha ao derivar credenciais: {exc}")
        sys.exit(1)
    finally:
        # Wipe key from local scope as soon as possible
        private_key = None  # noqa: F841

    api_key = creds.api_key
    api_secret = creds.api_secret
    api_passphrase = creds.api_passphrase

    # Write to .env.manual (overwrites existing)
    content = (
        "# ===== Conta manual (guardian stop automático) =====\n"
        "# Gerado por scripts/generate_manual_credentials.py\n"
        "# Este arquivo NÃO é commitado no git.\n"
        f"POLY_HOST={HOST}\n"
        f"POLY_CHAIN_ID={CHAIN_ID}\n"
        f"POLY_SIGNATURE_TYPE={SIGNATURE_TYPE}\n"
        "POLY_PRIVATE_KEY=\n"
        f"POLY_FUNDER={funder}\n"
        f"POLY_API_KEY={api_key}\n"
        f"POLY_API_SECRET={api_secret}\n"
        f"POLY_PASSPHRASE={api_passphrase}\n"
    )

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print()
    print("[OK] Credenciais gravadas em .env.manual")
    print(f"     POLY_API_KEY    = {api_key}")
    print(f"     POLY_FUNDER     = {funder}")
    print()
    print("NOTA: POLY_PRIVATE_KEY foi deixada em branco no arquivo.")
    print("      O guardian usa apenas API key/secret/passphrase para")
    print("      consultas. Para executar ordens (--execute-stop),")
    print("      preencha POLY_PRIVATE_KEY manualmente no .env.manual.")
    print()
    print("Pronto. Você pode fechar este terminal.")


if __name__ == "__main__":
    main()
