import os
from coinbase import jwt_generator

api_key = os.getenv("COINBASE_API_KEY", "")
api_secret = os.getenv("COINBASE_API_SECRET", "")

print("API key prefix:", api_key[:50] if api_key else "(empty)")
print("Secret loaded: yes" if api_secret else "(empty)")
print()

try:
    token = jwt_generator.build_ws_jwt(api_key, api_secret)
    print("JWT build OK")
    print("Token prefix:", token[:60])
except Exception as e:
    print("JWT build FAILED:", e)

print()

try:
    token2 = jwt_generator.build_rest_jwt("/api/v3/brokerage/accounts", api_key, api_secret)
    print("REST JWT build OK")
    print("Token prefix:", token2[:60])
except Exception as e:
    print("REST JWT build FAILED:", e)
