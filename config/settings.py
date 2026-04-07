import os
from dotenv import load_dotenv

load_dotenv()

GAMMA_API = "https://gamma-api.polymarket.com"

API_KEY = os.getenv("POLY_API_KEY")
API_SECRET = os.getenv("POLY_API_SECRET")
PASSPHRASE = os.getenv("POLY_PASSPHRASE")

print("[CONFIG] Loaded settings")
