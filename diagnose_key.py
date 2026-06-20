import os
from cryptography.hazmat.primitives.serialization import load_pem_private_key

secret = os.getenv("COINBASE_API_SECRET", "")
key_bytes = secret.encode()

print("Has real newlines:", chr(10) in secret)
print("PEM header:", secret.strip().split(chr(10))[0] if secret else "(empty)")
print("Total lines:", secret.count(chr(10)))
print()

try:
    key = load_pem_private_key(key_bytes, password=None)
    print("Key loaded OK")
    print("Key type:", type(key).__name__)
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
    if isinstance(key, EllipticCurvePrivateKey):
        print("Curve:", key.curve.name)
    else:
        print("WARNING: Not an EC key — Coinbase Advanced Trade requires EC P-256")
except Exception as e:
    print("Key load FAILED:", e)
