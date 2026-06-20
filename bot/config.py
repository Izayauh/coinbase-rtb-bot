"""
Config loader for config.yaml.

Loads at import time. Call validate() during startup to fail fast on bad config.
"""
import os
import sys
import logging
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def _load() -> dict:
    path = os.path.abspath(_CONFIG_PATH)
    with open(path, "r") as f:
        return yaml.safe_load(f)


_raw: dict = _load()


# ---------------------------------------------------------------------------
# Typed accessors
# ---------------------------------------------------------------------------

def runtime_mode() -> str:
    """'paper' or 'live'. Defaults to 'paper' if key is absent."""
    return _raw.get("runtime", {}).get("mode", "paper")


def trading_enabled() -> bool:
    return bool(_raw.get("runtime", {}).get("trading_enabled", True))


def portfolio_value() -> float:
    return float(_raw.get("runtime", {}).get("portfolio_value", 10000.0))


def paper_db_path() -> str:
    return str(_raw.get("runtime", {}).get("paper_db_path", "paper_journal.db"))


def live_db_path() -> str:
    return str(_raw.get("runtime", {}).get("live_db_path", "live_journal.db"))


def symbols() -> list:
    return list(_raw.get("symbols", []))


def symbol() -> str:
    """The single configured symbol. Assumes validate() has already been called."""
    return symbols()[0]


def reconcile_interval_sec() -> int:
    return int(_raw.get("execution", {}).get("reconcile_interval_sec", 5))


def ws_stale_timeout_sec() -> int:
    return int(_raw.get("execution", {}).get("ws_stale_timeout_sec", 15))


def max_pending_order_age_sec() -> int:
    return int(_raw.get("execution", {}).get("max_pending_order_age_sec", 60))


def max_daily_loss() -> float:
    return float(_raw.get("risk", {}).get("max_daily_loss", 0.015))


# ---------------------------------------------------------------------------
# Safety accessors
# ---------------------------------------------------------------------------

def kill_switch_file() -> str:
    return str(_raw.get("safety", {}).get("kill_switch_file", "KILL_SWITCH"))


def product_allowlist() -> list:
    return list(_raw.get("safety", {}).get("product_allowlist", []))


def max_order_size_usd() -> float:
    return float(_raw.get("safety", {}).get("max_order_size_usd", 500.0))


def max_position_size_usd() -> float:
    return float(_raw.get("safety", {}).get("max_position_size_usd", 1000.0))


def live_trading_confirmed() -> bool:
    return bool(_raw.get("safety", {}).get("live_trading_confirmed", False))


def live_test_order_notional_usd() -> float:
    """Fixed USD notional override for first live order. 0.0 = use normal risk sizing."""
    return float(_raw.get("live", {}).get("test_order_notional_usd", 0.0))


def strategy_id() -> str:
    return str(_raw.get("strategy", {}).get("id", "unversioned_strategy"))


def strategy_version() -> str:
    return str(_raw.get("strategy", {}).get("version", "0"))


def strategy_authorization_file() -> str:
    return str(
        _raw.get("strategy", {}).get(
            "authorization_file", "strategy_authorization.json"
        )
    )


def require_strategy_authorization() -> bool:
    return bool(
        _raw.get("safety", {}).get("require_strategy_authorization", False)
    )


def acceptance_receipt_file() -> str:
    return str(
        _raw.get("safety", {}).get(
            "acceptance_receipt_file", "tiny_live_acceptance.json"
        )
    )


def acceptance_receipt_max_age_seconds() -> int:
    return int(
        _raw.get("safety", {}).get(
            "acceptance_receipt_max_age_seconds", 300
        )
    )


def research_advisory_project() -> str:
    return str(
        (_raw.get("research") or {}).get(
            "advisory_project", "bitwise-trader"
        )
    )


def research_advisory_bucket() -> str:
    return str((_raw.get("research") or {}).get("advisory_bucket", ""))


def research_advisory_object() -> str:
    return str((_raw.get("research") or {}).get("advisory_object", ""))


def research_advisory_max_age_seconds() -> int:
    return int(
        (_raw.get("research") or {}).get("advisory_max_age_seconds", 180)
    )


# ---------------------------------------------------------------------------
# Fail-fast startup validation
# ---------------------------------------------------------------------------

def validate() -> None:
    """
    Call once at process startup before any components are initialised.
    Exits the process with a clear error message on any invalid condition.
    """
    errors = []

    # Runtime mode
    mode = _raw.get("runtime", {}).get("mode")  # None means key absent → default paper
    if mode is not None:
        if mode not in ("paper", "live"):
            errors.append(
                f"Invalid runtime mode: '{mode}'. Must be 'paper' or 'live'."
            )

    # Single symbol
    syms = symbols()
    if len(syms) != 1:
        errors.append(
            f"Exactly one symbol required. Got {len(syms)}: {syms}"
        )

    # portfolio_value
    pv = _raw.get("runtime", {}).get("portfolio_value", 10000.0)
    if float(pv) <= 0:
        errors.append(f"portfolio_value must be > 0. Got {pv}")

    # reconcile_interval_sec
    ris = _raw.get("execution", {}).get("reconcile_interval_sec", 5)
    if int(ris) <= 0:
        errors.append(f"reconcile_interval_sec must be > 0. Got {ris}")

    # max_pending_order_age_sec
    mpa = _raw.get("execution", {}).get("max_pending_order_age_sec", 60)
    if int(mpa) <= 0:
        errors.append(f"max_pending_order_age_sec must be > 0. Got {mpa}")

    # Product allowlist — configured symbol must be in the allowlist
    allowlist = product_allowlist()
    if allowlist and len(syms) == 1 and syms[0] not in allowlist:
        errors.append(
            f"Symbol '{syms[0]}' is not in the product allowlist: {allowlist}"
        )

    # Size caps must be positive
    if float(_raw.get("safety", {}).get("max_order_size_usd", 500.0)) <= 0:
        errors.append("safety.max_order_size_usd must be > 0")
    if float(_raw.get("safety", {}).get("max_position_size_usd", 1000.0)) <= 0:
        errors.append("safety.max_position_size_usd must be > 0")

    if require_strategy_authorization():
        if not strategy_id().strip() or strategy_id() == "unversioned_strategy":
            errors.append("strategy.id must be set when authorization is required")
        if not strategy_version().strip() or strategy_version() == "0":
            errors.append("strategy.version must be set when authorization is required")
        if not research_advisory_bucket():
            errors.append(
                "research.advisory_bucket must be set when authorization is required"
            )
        if not research_advisory_object():
            errors.append(
                "research.advisory_object must be set when authorization is required"
            )

    # Live gate: if mode is live, both config flag AND env var must be set.
    # Checked here as defence-in-depth even though live mode exits above.
    if mode == "live":
        if not live_trading_confirmed():
            errors.append(
                "Live mode requires safety.live_trading_confirmed=true in config."
            )
        if not os.environ.get("LIVE_TRADING_CONFIRMED", "").lower() == "true":
            errors.append(
                "Live mode requires environment variable LIVE_TRADING_CONFIRMED=true."
            )
        # live.test_order_notional_usd must be positive when set
        notional = float(_raw.get("live", {}).get("test_order_notional_usd", 0.0))
        if notional < 0:
            errors.append("live.test_order_notional_usd must be >= 0.")
        max_order = float(_raw.get("safety", {}).get("max_order_size_usd", 500.0))
        if 0 < notional > max_order:
            errors.append(
                f"live.test_order_notional_usd ({notional}) must not exceed "
                f"safety.max_order_size_usd ({max_order})."
            )

    if errors:
        for e in errors:
            logger.error("Config validation error: %s", e)
        sys.exit(1)
