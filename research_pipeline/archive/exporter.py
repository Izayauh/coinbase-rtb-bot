"""Export a closed SQLite research store to verified Parquet partitions.

The exporter is provider-neutral. Its output can be uploaded to any
S3-compatible object store. Nothing is deleted from the source database.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None


class ArchiveError(RuntimeError):
    """Raised when an archive cannot be produced or verified."""


@dataclass(frozen=True)
class TableSpec:
    time_column: Optional[str]


TABLE_SPECS: Dict[str, TableSpec] = {
    "sources": TableSpec(None),
    "ingestion_runs": TableSpec("started_us"),
    "raw_events": TableSpec("recv_time_us"),
    "trades": TableSpec("event_time_us"),
    "l2_updates": TableSpec("recv_time_us"),
    "quotes": TableSpec("recv_time_us"),
    "gaps": TableSpec("detected_us"),
    "labels": TableSpec("decision_time_us"),
    "features": TableSpec("event_time_us"),
    "order_math": TableSpec("event_time_us"),
    "variant_registry": TableSpec("registered_us"),
    "context_events": TableSpec("availability_time_us"),
}


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_names(con) -> set[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_catalog='src' AND table_schema='main'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _copy_table(con, table: str, spec: TableSpec, table_dir: Path) -> None:
    if spec.time_column is None:
        target_file = table_dir / "data.parquet"
        con.execute(
            f"COPY (SELECT * FROM src.main.{table}) "
            f"TO {_sql_string(target_file.as_posix())} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        return

    time_col = spec.time_column
    query = (
        f"SELECT *, "
        f"strftime(to_timestamp({time_col} / 1000000.0), '%Y-%m-%d') "
        f"AS archive_date, "
        f"strftime(to_timestamp({time_col} / 1000000.0), '%H') "
        f"AS archive_hour "
        f"FROM src.main.{table}"
    )
    con.execute(
        f"COPY ({query}) TO {_sql_string(table_dir.as_posix())} "
        "(FORMAT PARQUET, COMPRESSION ZSTD, "
        "PARTITION_BY (archive_date, archive_hour), OVERWRITE_OR_IGNORE TRUE)"
    )


def _file_record(con, root: Path, path: Path) -> dict:
    rows = int(
        con.execute(
            f"SELECT COUNT(*) FROM read_parquet({_sql_string(path.as_posix())})"
        ).fetchone()[0]
    )
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "rows": rows,
        "sha256": _sha256(path),
    }


def export_sqlite_to_parquet(
    db_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    tables: Optional[Iterable[str]] = None,
    overwrite: bool = False,
) -> dict:
    """Export research tables and return a verified archive manifest."""
    if duckdb is None:
        raise ArchiveError(
            "duckdb is required for Parquet export; install project requirements"
        )

    source = Path(db_path).resolve()
    destination = Path(output_dir).resolve()
    if not source.is_file():
        raise ArchiveError(f"research database does not exist: {source}")
    if destination.exists():
        if not overwrite:
            raise ArchiveError(
                f"archive destination already exists: {destination}; "
                "pass overwrite=True to replace it"
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    selected = list(tables) if tables is not None else list(TABLE_SPECS)
    unknown = sorted(set(selected) - set(TABLE_SPECS))
    if unknown:
        raise ArchiveError(f"unsupported archive tables: {', '.join(unknown)}")

    con = duckdb.connect()
    manifest = {
        "manifest_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(source),
            "bytes": source.stat().st_size,
        },
        "format": "parquet",
        "compression": "zstd",
        "partitioning": "UTC hour",
        "tables": {},
    }
    try:
        con.execute("INSTALL sqlite")
        con.execute("LOAD sqlite")
        con.execute(
            f"ATTACH {_sql_string(source.as_posix())} AS src "
            "(TYPE SQLITE, READ_ONLY)"
        )
        available = _table_names(con)
        missing = sorted(set(selected) - available)
        if missing:
            raise ArchiveError(
                f"source database is missing tables: {', '.join(missing)}"
            )

        for table in selected:
            source_rows = int(
                con.execute(f"SELECT COUNT(*) FROM src.main.{table}").fetchone()[0]
            )
            table_dir = destination / table
            table_dir.mkdir(parents=True)
            _copy_table(con, table, TABLE_SPECS[table], table_dir)
            files = sorted(table_dir.rglob("*.parquet"))
            records = [_file_record(con, destination, path) for path in files]
            exported_rows = sum(record["rows"] for record in records)
            if exported_rows != source_rows:
                raise ArchiveError(
                    f"{table}: source has {source_rows} rows but archive has "
                    f"{exported_rows}"
                )
            manifest["tables"][table] = {
                "source_rows": source_rows,
                "exported_rows": exported_rows,
                "bytes": sum(record["bytes"] for record in records),
                "verified": True,
                "files": records,
            }
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        con.close()

    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest["manifest"] = {
        "path": manifest_path.name,
        "bytes": manifest_path.stat().st_size,
        "sha256": _sha256(manifest_path),
    }
    return manifest
