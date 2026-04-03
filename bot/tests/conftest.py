import pytest


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
