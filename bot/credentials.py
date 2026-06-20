"""Credential loading helpers for operator verification commands."""
from __future__ import annotations

import os


def pem_secret_looks_valid(value: str | None) -> bool:
    secret = value or ""
    return (
        len(secret) >= 100
        and "-----BEGIN" in secret
        and "-----END" in secret
        and "\n" in secret
    )


def refresh_coinbase_credentials_from_user_environment() -> bool:
    """Refresh stale process credentials from Windows User environment.

    A terminal can inherit an old/truncated PEM even after the User variable is
    repaired.  Verification commands call this helper so a restart is not a
    hidden prerequisite.  Returns True when either variable was refreshed.
    """
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            user_key, _ = winreg.QueryValueEx(key, "COINBASE_API_KEY")
            user_secret, _ = winreg.QueryValueEx(key, "COINBASE_API_SECRET")
    except (FileNotFoundError, OSError):
        return False

    changed = False
    if user_key and os.environ.get("COINBASE_API_KEY") != user_key:
        os.environ["COINBASE_API_KEY"] = str(user_key)
        changed = True
    if pem_secret_looks_valid(str(user_secret)) and not pem_secret_looks_valid(
        os.environ.get("COINBASE_API_SECRET")
    ):
        os.environ["COINBASE_API_SECRET"] = str(user_secret)
        changed = True
    return changed
