"""
Live-mode readiness checker.

check_readiness() → (ready: bool, blockers: list[str], live_balances: dict)

Reports YES only when every gate confirms the bot could place a live buy
order right now. Does not start the bot or place any orders.

parse_coinbase_balances() is exported for use in main.py's startup banner.
"""
import json
import os
import sqlite3

import bot.config as config


def parse_coinbase_balances(response) -> dict:
    """
    Parse a Coinbase get_accounts() response → {currency: available_float}.

    Handles SDK response objects, plain dicts, and iterables defensively.
    Returns {} if the response is unparseable.
    """
    accounts = []
    if hasattr(response, "accounts") and response.accounts is not None:
        try:
            accounts = list(response.accounts)
        except Exception:
            pass
    elif isinstance(response, dict):
        accounts = response.get("accounts", [])
    elif hasattr(response, "__iter__"):
        try:
            accounts = list(response)
        except Exception:
            pass

    balances: dict = {}
    for acct in accounts:
        ab = None
        if hasattr(acct, "available_balance"):
            ab = acct.available_balance
        elif isinstance(acct, dict):
            ab = acct.get("available_balance", {})

        if ab is None:
            continue

        if hasattr(ab, "currency") and hasattr(ab, "value"):
            currency, value = str(ab.currency), str(ab.value)
        elif isinstance(ab, dict):
            currency = str(ab.get("currency", ""))
            value = str(ab.get("value", "0"))
        else:
            continue

        if currency:
            try:
                balances[currency] = float(value)
            except (TypeError, ValueError):
                balances[currency] = 0.0

    return balances


def _check_stale_safeguard_db(db_path: str, ks_file: str) -> str | None:
    """
    Inspect the live journal DB for persisted safeguard state that would block
    trading on startup WITHOUT being auto-cleared by Safeguards.__init__.

    Design: Safeguards.__init__ auto-clears a stale kill_switch entry when the
    file is gone, so a kill_switch-only entry with the file absent is not an
    independent blocker. Any other tripped guard is non-recoverable and must
    be reported.

    Returns a blocker string if found, else None.
    """
    if not os.path.isfile(db_path):
        return None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='runtime_state'"
        )
        if not cur.fetchone():
            conn.close()
            return None
        cur.execute("SELECT value FROM runtime_state WHERE key='safeguards'")
        row = cur.fetchone()
        conn.close()
    except Exception as exc:
        return f"Could not read live DB '{db_path}': {exc}"

    if not row:
        return None

    try:
        state = json.loads(row["value"])
    except Exception:
        return None

    if state.get("trading_enabled", True):
        return None

    tripped = set(state.get("tripped", []))
    # kill_switch with the file gone would be auto-cleared at startup — not a blocker
    file_gone = not os.path.exists(ks_file)
    effective_tripped = (tripped - {"kill_switch"}) if file_gone else tripped

    if effective_tripped:
        return (
            f"Live DB has persisted safeguard state with trading_enabled=false. "
            f"Non-recoverable tripped guards: {sorted(effective_tripped)}. "
            f"These require explicit operator investigation and manual DB reset."
        )
    return None


def _response_dict(response) -> dict:
    if hasattr(response, "to_dict"):
        try:
            return response.to_dict()
        except Exception:
            return {}
    if isinstance(response, dict):
        return response
    return getattr(response, "__dict__", {}) or {}


def _response_field(response, name: str, default=None):
    if isinstance(response, dict):
        return response.get(name, default)
    if hasattr(response, name):
        return getattr(response, name)
    data = _response_dict(response)
    return data.get(name, default)


def _preview_configured_live_buy(client) -> str | None:
    """
    Preview the exact first-live BUY shape without placing an order.

    The bot submits limit IOC orders. For fixed-notional first-live mode we can
    derive a representative base size from the live product price, then ask
    Coinbase whether that order is acceptable. This catches insufficient funds,
    product restrictions, and account-scope issues before startup says READY.
    """
    notional = config.live_test_order_notional_usd()
    if notional <= 0:
        return None

    syms = config.symbols()
    if not syms:
        return None
    symbol = syms[0]

    try:
        product = client.get_product(symbol)
        price = float(_response_field(product, "price", 0.0) or 0.0)
        if price <= 0:
            response = client.preview_market_order_buy(
                product_id=symbol,
                quote_size=f"{notional:.2f}",
            )
        else:
            from bot.risk import RiskManager  # noqa: PLC0415

            base_size = round(notional / price, 8)
            if base_size <= 0:
                return (
                    f"Configured live buy preview failed: ${notional:.2f} "
                    f"is too small for {symbol} at price {price:.2f}"
                )
            response = client.preview_limit_order_ioc_buy(
                product_id=symbol,
                base_size=f"{base_size:.8f}",
                limit_price=f"{RiskManager.get_ioc_limit(price):.2f}",
            )
    except Exception as exc:
        return f"Configured live buy preview failed for {symbol}: {exc}"

    data = _response_dict(response)
    errors = data.get("errors") or data.get("error_response")
    failure = (
        data.get("preview_failure_reason")
        or data.get("failure_reason")
        or data.get("reject_reason")
    )
    if errors or failure:
        return (
            f"Configured live buy preview failed for {symbol}: "
            f"{errors or failure}"
        )
    return None


def check_runtime_readiness() -> tuple:
    """Return readiness for keeping the live runtime online, not for BUYs.

    Entry halts, failed strategy authorization, and persisted risk guards must
    not prevent the process from restarting: market-data capture, monitoring,
    reconciliation, and risk-reducing exits still need a live runtime.
    """
    blockers: list[str] = []
    live_balances: dict = {}

    if config.runtime_mode() != "live":
        blockers.append("config.yaml runtime.mode must be 'live'")
    if not config.live_trading_confirmed():
        blockers.append("config.yaml safety.live_trading_confirmed must be true")
    if os.environ.get("LIVE_TRADING_CONFIRMED", "").lower() != "true":
        blockers.append("Env LIVE_TRADING_CONFIRMED must be true for the child process")

    api_key = os.environ.get("COINBASE_API_KEY", "")
    api_secret = os.environ.get("COINBASE_API_SECRET", "")
    if not api_key:
        blockers.append("Env var COINBASE_API_KEY is not set or empty")
    if not api_secret:
        blockers.append("Env var COINBASE_API_SECRET is not set or empty")

    syms = config.symbols()
    allowlist = config.product_allowlist()
    if len(syms) != 1:
        blockers.append(f"Exactly one runtime symbol is required; got {syms}")
    elif allowlist and syms[0] not in allowlist:
        blockers.append(f"Symbol '{syms[0]}' is not in product_allowlist")

    if api_key and api_secret:
        try:
            from coinbase.rest import RESTClient

            client = RESTClient(api_key=api_key, api_secret=api_secret)
            live_balances = parse_coinbase_balances(client.get_accounts())
            if not live_balances:
                blockers.append("Coinbase REST returned no accounts")
        except Exception as exc:
            blockers.append(f"Coinbase runtime authentication failed: {exc}")

    return not blockers, blockers, live_balances


def check_readiness() -> tuple:
    """
    Return (ready: bool, blockers: list[str], live_balances: dict).

    ready=True only when every gate is green.
    live_balances is populated when Coinbase REST auth succeeds; {} otherwise.

    Checks performed:
      1.  runtime.mode == live
      2.  safety.live_trading_confirmed == true
      3.  Env LIVE_TRADING_CONFIRMED == true
      4.  COINBASE_API_KEY present
      5.  COINBASE_API_SECRET present
      6.  Kill switch file absent
      7.  No stale non-recoverable persisted safeguard state in live DB
      8.  Configured symbol is in product allowlist
      9.  Coinbase REST auth works and returns accounts
      10. Live order adapter is CoinbaseAdapter (implied by mode+creds, not checked separately)
    """
    blockers: list = []
    live_balances: dict = {}

    # 1. runtime.mode == live
    mode = config.runtime_mode()
    if mode != "live":
        blockers.append(
            f"config.yaml: runtime.mode='{mode}' — must be 'live'"
        )

    # 2. safety.live_trading_confirmed == true
    if not config.live_trading_confirmed():
        blockers.append(
            "config.yaml: safety.live_trading_confirmed is false or absent"
        )

    # 3. Env var LIVE_TRADING_CONFIRMED == true
    if os.environ.get("LIVE_TRADING_CONFIRMED", "").lower() != "true":
        blockers.append(
            "Env var LIVE_TRADING_CONFIRMED is not set to 'true' "
            "(set for this session only — do NOT persist permanently)"
        )

    # 4. COINBASE_API_KEY present
    api_key = os.environ.get("COINBASE_API_KEY", "")
    if not api_key:
        blockers.append("Env var COINBASE_API_KEY is not set or empty")

    # 5. COINBASE_API_SECRET present
    api_secret = os.environ.get("COINBASE_API_SECRET", "")
    if not api_secret:
        blockers.append("Env var COINBASE_API_SECRET is not set or empty")

    # 6. Kill switch file absent
    ks_file = config.kill_switch_file()
    if os.path.exists(ks_file):
        blockers.append(
            f"Kill switch file '{ks_file}' exists — delete it to re-arm live trading"
        )

    # 7. Stale persisted safeguard state
    db_path = config.live_db_path()
    stale = _check_stale_safeguard_db(db_path, ks_file)
    if stale:
        blockers.append(stale)

    # 8. Configured symbol is in product allowlist
    syms = config.symbols()
    allowlist = config.product_allowlist()
    if syms and allowlist and syms[0] not in allowlist:
        blockers.append(
            f"Symbol '{syms[0]}' is not in product_allowlist {allowlist}"
        )

    # 9. Strategy evidence and explicit live authorization
    if config.require_strategy_authorization():
        from bot.strategy_authorization import validate_configured_authorization

        authorized, reason, _ = validate_configured_authorization()
        if not authorized:
            blockers.append(f"Strategy authorization blocked: {reason}")

    # 10. Coinbase REST auth + account fetch (only attempted when creds present)
    if api_key and api_secret:
        try:
            from coinbase.rest import RESTClient  # noqa: PLC0415
            client = RESTClient(api_key=api_key, api_secret=api_secret)
            resp = client.get_accounts()
            live_balances = parse_coinbase_balances(resp)
            if not live_balances:
                blockers.append(
                    "Coinbase REST call returned no accounts — "
                    "verify credentials map to the correct portfolio scope"
                )
            preview_blocker = _preview_configured_live_buy(client)
            if preview_blocker:
                blockers.append(preview_blocker)
        except Exception as exc:
            blockers.append(f"Coinbase REST auth/account fetch failed: {exc}")

    # 11. Adapter selection: CoinbaseAdapter when mode==live and _enabled=True.
    #     Covered structurally by checks 1+4+5. No independent check added.

    ready = len(blockers) == 0
    return ready, blockers, live_balances
