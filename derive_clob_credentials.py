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


def main():
    load_dotenv()
    status = load_broker_env()

    print("[TEST] Starting CLOB credential derivation...")
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

    creds = temp_client.create_or_derive_api_creds()

    print("\n[RESULT] Derived CLOB credentials successfully")
    print("\nPaste these into your .env:\n")
    print(f"POLY_API_KEY={creds['apiKey']}")
    print(f"POLY_API_SECRET={creds['secret']}")
    print(f"POLY_PASSPHRASE={creds['passphrase']}")


if __name__ == "__main__":
    main()
