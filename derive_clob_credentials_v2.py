from pprint import pprint
from dotenv import load_dotenv
import os

from market.broker_env import load_broker_env

try:
    from py_clob_client.client import ClobClient
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "py-clob-client is not installed. Run: pip install py-clob-client"
    ) from exc


def _extract_creds(creds):
    if isinstance(creds, dict):
        return {
            "api_key": creds.get("apiKey") or creds.get("api_key"),
            "secret": creds.get("secret") or creds.get("apiSecret") or creds.get("api_secret"),
            "passphrase": creds.get("passphrase") or creds.get("apiPassphrase") or creds.get("api_passphrase"),
        }

    return {
        "api_key": getattr(creds, "api_key", None) or getattr(creds, "apiKey", None),
        "secret": getattr(creds, "secret", None) or getattr(creds, "api_secret", None) or getattr(creds, "apiSecret", None),
        "passphrase": getattr(creds, "passphrase", None) or getattr(creds, "api_passphrase", None) or getattr(creds, "apiPassphrase", None),
    }


def main():
    load_dotenv()
    status = load_broker_env()

    print("[TEST] Starting CLOB credential derivation v2...")
    pprint(status.as_dict())

    private_key = os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("POLY_PRIVATE_KEY (or PRIVATE_KEY) is required")

    host = os.getenv("POLY_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLY_CHAIN_ID", "137"))

    temp_client = ClobClient(
        host=host,
        chain_id=chain_id,
        key=private_key,
    )

    raw_creds = temp_client.create_or_derive_api_creds()
    creds = _extract_creds(raw_creds)

    if not creds["api_key"] or not creds["secret"] or not creds["passphrase"]:
        print("[DEBUG] Unexpected credentials payload type:", type(raw_creds))
        print("[DEBUG] Raw credentials repr:", raw_creds)
        raise RuntimeError("Unable to extract apiKey/secret/passphrase from returned credentials object")

    print("\n[RESULT] Derived CLOB credentials successfully")
    print("\nPaste these into your .env:\n")
    print(f"POLY_API_KEY={creds['api_key']}")
    print(f"POLY_API_SECRET={creds['secret']}")
    print(f"POLY_PASSPHRASE={creds['passphrase']}")


if __name__ == "__main__":
    main()
