#!/usr/bin/env python
"""
verify_coinbase.py — Coinbase credential + account verification.

Proves that COINBASE_API_KEY and COINBASE_API_SECRET map to a real
Coinbase Advanced Trade account by making a live REST call and printing
the fetched account/balance data.

Exit codes:
  0  PASS — credentials are valid and account data was returned
  1  FAIL — missing credentials, auth error, or empty response

Usage:
    python verify_coinbase.py
"""
import os
import sys

# Allow running from project root without package install
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.readiness import parse_coinbase_balances


def main() -> int:
    api_key = os.environ.get("COINBASE_API_KEY", "")
    api_secret = os.environ.get("COINBASE_API_SECRET", "")

    print()
    print("Coinbase Credential Verification")
    print("=" * 50)
    print(f"  COINBASE_API_KEY    : {'SET (' + api_key[:24] + '...)' if api_key else 'NOT SET'}")
    print(f"  COINBASE_API_SECRET : {'SET (present, not shown)' if api_secret else 'NOT SET'}")
    print()

    if not api_key:
        print("FAIL: COINBASE_API_KEY is not set.")
        print("  Set it as a User environment variable:")
        print("  [System.Environment]::SetEnvironmentVariable('COINBASE_API_KEY', '<key-id>', 'User')")
        return 1

    if not api_secret:
        print("FAIL: COINBASE_API_SECRET is not set.")
        print("  Set it as a User environment variable (preserve PEM newlines):")
        print("  $pem = Get-Content path\\to\\key.pem -Raw")
        print("  [System.Environment]::SetEnvironmentVariable('COINBASE_API_SECRET', $pem, 'User')")
        return 1

    try:
        from coinbase.rest import RESTClient
        print("Connecting to Coinbase REST API ...")
        client = RESTClient(api_key=api_key, api_secret=api_secret)
        resp = client.get_accounts()
    except Exception as exc:
        print(f"FAIL: REST call failed — {exc}")
        print()
        print("Common causes:")
        print("  - Invalid key ID or secret PEM")
        print("  - PEM has literal \\n instead of real newlines (run diagnose_key.py)")
        print("  - Key does not have 'view' permissions on the target portfolio")
        return 1

    balances = parse_coinbase_balances(resp)

    if not balances:
        print("FAIL: Auth succeeded but no accounts were returned.")
        print("  Verify the API key is scoped to the correct portfolio.")
        return 1

    print(f"OK: Connected to Coinbase. {len(balances)} currency account(s) found.\n")
    print("Available balances:")
    any_nonzero = False
    for currency, value in sorted(balances.items()):
        if value > 0:
            print(f"  {currency:<8}: {value:>20,.8f}")
            any_nonzero = True
    if not any_nonzero:
        print("  (all account balances are zero)")
        for currency, value in sorted(balances.items()):
            print(f"  {currency:<8}: {value:>20,.8f}")

    print()
    print("VERIFICATION: PASS — credentials are valid and account data fetched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
