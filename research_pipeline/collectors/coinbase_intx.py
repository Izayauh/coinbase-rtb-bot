"""Minute-cadence public Coinbase INTX derivatives context collector."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..context import AccessGap, CoinbaseIntxDerivativesAdapter


class CoinbaseIntxPoller:
    """Poll BTC-PERP open interest and official funding without credentials."""

    def __init__(
        self,
        store,
        *,
        product_id: str = "BTC-PERP",
        poll_seconds: float = 60.0,
        adapter: Any | None = None,
    ):
        self.store = store
        self.poll_seconds = poll_seconds
        self.adapter = adapter or CoinbaseIntxDerivativesAdapter(product_id)

    async def run(self, *, max_seconds: float) -> dict:
        started = time.monotonic()
        totals = {
            "polls": 0,
            "inserted": 0,
            "duplicates": 0,
            "access_gaps": 0,
            "last_error": None,
        }
        while True:
            now_us = int(time.time() * 1_000_000)
            try:
                records = await asyncio.to_thread(
                    self.adapter.fetch,
                    now_us - 48 * 60 * 60 * 1_000_000,
                    now_us + 5 * 60 * 1_000_000,
                )
                for record in records:
                    if self.store.insert_context(record.to_row()):
                        totals["inserted"] += 1
                    else:
                        totals["duplicates"] += 1
            except AccessGap as exc:
                totals["access_gaps"] += 1
                totals["last_error"] = str(exc)
            totals["polls"] += 1

            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.poll_seconds, remaining))
        return totals
