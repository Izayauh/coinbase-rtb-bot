"""
Configuration loader for research_pipeline.

Loads docs-frozen conservative defaults from config/default.yaml. An optional
override path (or dict) may be supplied for tests/runs. Stdlib + PyYAML only;
imports nothing from bot/.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml

_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "config", "default.yaml")


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the default config, optionally merged with a YAML override file."""
    with open(_DEFAULT_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            override = yaml.safe_load(fh) or {}
        cfg = _deep_merge(cfg, override)
    return cfg


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass(frozen=True)
class CostModel:
    """cost_model_v1 — see IMPLEMENTATION_CONTRACT.md §10.

    Friction is expressed per side in basis points and applied on top of the
    *executable* quote (so the bid/ask spread is captured by the quotes, not by
    a constant). `half_spread_bps_fallback` is only used when no real quote
    exists and the labeler must fall back to mid +/- half spread.
    """

    version: str = "cost_model_v1"
    taker_fee_bps: float = 60.0
    slippage_bps: float = 2.0
    adverse_selection_bps: float = 2.0
    latency_us: int = 500_000
    half_spread_bps_fallback: float = 1.0

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "CostModel":
        cm = cfg.get("cost_model", {})
        return cls(
            version=cm.get("version", "cost_model_v1"),
            taker_fee_bps=float(cm.get("taker_fee_bps", 60.0)),
            slippage_bps=float(cm.get("slippage_bps", 2.0)),
            adverse_selection_bps=float(cm.get("adverse_selection_bps", 2.0)),
            latency_us=int(cm.get("latency_us", 500_000)),
            half_spread_bps_fallback=float(cm.get("half_spread_bps_fallback", 1.0)),
        )

    def entry_fee_frac(self, sensitivity: float = 1.0) -> float:
        """Entry-side fee+slippage as a return fraction (added to the ask)."""
        return (self.taker_fee_bps + self.slippage_bps) * sensitivity / 10_000.0

    def exit_fee_frac(self, sensitivity: float = 1.0) -> float:
        """Exit-side fee+slippage as a return fraction (subtracted from the bid)."""
        return (self.taker_fee_bps + self.slippage_bps) * sensitivity / 10_000.0

    def adverse_frac(self, sensitivity: float = 1.0) -> float:
        return self.adverse_selection_bps * sensitivity / 10_000.0
