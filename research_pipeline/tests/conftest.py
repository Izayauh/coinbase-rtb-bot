"""Shared fixtures for research_pipeline tests."""
import os
import sys

import pytest

# Ensure the repo root is importable when pytest is invoked from elsewhere.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def store(tmp_path):
    from research_pipeline.storage import ResearchStore

    s = ResearchStore(str(tmp_path / "research.db"))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def run_id(store):
    store.register_source("coinbase_ws", "coinbase_ws",
                          "wss://advanced-trade-ws.coinbase.com", 1)
    return store.start_run("test", {"product_ids": ["BTC-USD"]})
