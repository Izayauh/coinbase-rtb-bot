import sys
import types
import pytest

# ---------------------------------------------------------------------------
# Stub the coinbase SDK so tests can import bot.adapters (PaperAdapter)
# without needing the real coinbase-advanced-py package installed.
# ---------------------------------------------------------------------------
def _make_coinbase_stub():
    coinbase_pkg = types.ModuleType("coinbase")
    rest_mod = types.ModuleType("coinbase.rest")
    jwt_mod = types.ModuleType("coinbase.jwt_generator")

    class _RESTClient:
        def __init__(self, *a, **kw):
            pass

    rest_mod.RESTClient = _RESTClient
    jwt_mod.build_ws_jwt = lambda *a, **kw: "stub_jwt"

    coinbase_pkg.rest = rest_mod
    coinbase_pkg.jwt_generator = jwt_mod
    sys.modules.setdefault("coinbase", coinbase_pkg)
    sys.modules.setdefault("coinbase.rest", rest_mod)
    sys.modules.setdefault("coinbase.jwt_generator", jwt_mod)


_make_coinbase_stub()


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """
    Provides a fresh, isolated Database instance per test.

    Patches bot.db.db in-place so all Journal/ExecutionService calls in the
    test automatically use the temp file.  monkeypatch restores the original
    db_path after each test; pytest's tmp_path handles file cleanup.
    """
    from bot.db import db
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "db_path", db_file)
    db._init_db()
    yield db
