import json

import duckdb

from research_pipeline.archive import ArchiveError, export_sqlite_to_parquet


def test_verified_parquet_archive(store, run_id, tmp_path):
    raw_id, inserted = store.append_raw(
        run_id,
        "coinbase_ws",
        "ticker",
        {"price": "100.0"},
        1_700_000_000_000_000,
        1_700_000_000_000_000,
    )
    assert inserted
    store.insert_quote(
        "BTC-USD",
        99.0,
        2.0,
        101.0,
        1.0,
        1_700_000_000_000_000,
        1_700_000_000_000_000,
        raw_id,
    )
    store.close()

    output = tmp_path / "archive"
    manifest = export_sqlite_to_parquet(
        tmp_path / "research.db",
        output,
        tables=["raw_events", "quotes"],
    )

    assert manifest["tables"]["raw_events"]["verified"] is True
    assert manifest["tables"]["raw_events"]["exported_rows"] == 1
    assert manifest["tables"]["quotes"]["exported_rows"] == 1
    assert (output / "manifest.json").is_file()

    on_disk = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["compression"] == "zstd"

    quote_files = list((output / "quotes").rglob("*.parquet"))
    con = duckdb.connect()
    try:
        assert con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{quote_files[0].as_posix()}')"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_archive_refuses_existing_destination(store, tmp_path):
    store.close()
    destination = tmp_path / "archive"
    destination.mkdir()
    try:
        export_sqlite_to_parquet(
            tmp_path / "research.db",
            destination,
            tables=["sources"],
        )
    except ArchiveError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected ArchiveError")
