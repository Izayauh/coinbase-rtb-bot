import pytest

from research_pipeline.cli.collect import _collector_lock


def test_collector_lock_rejects_second_writer(tmp_path):
    db = str(tmp_path / "research.db")
    with _collector_lock(db):
        with pytest.raises(RuntimeError):
            with _collector_lock(db):
                pass
