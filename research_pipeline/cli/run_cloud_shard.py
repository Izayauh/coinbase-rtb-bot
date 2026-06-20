"""Run one closed research shard from collection through verified GCS upload."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

from google.cloud import storage


class ShardRunError(RuntimeError):
    """Raised when any stage of a cloud research shard fails."""


QUERY_TABLES = (
    "features",
    "order_math",
    "labels",
    "context_events",
    "trades",
    "quotes",
    "variant_registry",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: Sequence[str],
    *,
    allowed_returncodes: Sequence[int] = (0,),
) -> dict:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
    )
    record = {
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode not in set(allowed_returncodes):
        raise ShardRunError(json.dumps(record, indent=2))
    return record


def _upload_report(
    report_path: Path,
    *,
    bucket: str,
    object_name: str,
    project: str | None,
    client=None,
) -> dict:
    client = client or storage.Client(project=project)
    blob = client.bucket(bucket).blob(object_name)
    digest = _sha256(report_path)
    blob.metadata = {"sha256": digest}
    blob.upload_from_filename(
        str(report_path),
        content_type="application/json",
    )
    blob.reload()
    if int(blob.size or -1) != report_path.stat().st_size:
        raise ShardRunError(f"report size mismatch for {object_name}")
    if (blob.metadata or {}).get("sha256") != digest:
        raise ShardRunError(f"report SHA-256 mismatch for {object_name}")
    return {
        "key": object_name,
        "bytes": report_path.stat().st_size,
        "sha256": digest,
    }


def mirror_query_tables(
    archive_dir: Path,
    *,
    bucket: str,
    base_prefix: str,
    shard_id: str,
    project: str | None,
    client=None,
) -> dict:
    """Mirror compact tables to stable table-first prefixes for BigQuery."""
    client = client or storage.Client(project=project)
    bucket_ref = client.bucket(bucket)
    objects = []
    for table in QUERY_TABLES:
        table_dir = archive_dir / table
        if not table_dir.exists():
            continue
        for path in sorted(table_dir.rglob("*.parquet")):
            relative = path.relative_to(table_dir).as_posix()
            object_name = (
                f"{base_prefix.strip('/')}/{table}/{shard_id}/{relative}"
            )
            digest = _sha256(path)
            blob = bucket_ref.blob(object_name)
            blob.metadata = {"sha256": digest, "shard_id": shard_id}
            blob.content_type = "application/vnd.apache.parquet"
            blob.upload_from_filename(str(path))
            blob.reload()
            if int(blob.size or -1) != path.stat().st_size:
                raise ShardRunError(
                    f"query mirror size mismatch for {object_name}"
                )
            if (blob.metadata or {}).get("sha256") != digest:
                raise ShardRunError(
                    f"query mirror SHA-256 mismatch for {object_name}"
                )
            objects.append(
                {
                    "table": table,
                    "key": object_name,
                    "bytes": path.stat().st_size,
                    "sha256": digest,
                }
            )
    return {
        "objects_verified": len(objects),
        "bytes_uploaded": sum(item["bytes"] for item in objects),
        "tables": sorted({item["table"] for item in objects}),
    }


def _clean_closed_shard(db_path: Path, archive_dir: Path) -> None:
    for suffix in ("", "-wal", "-shm", ".collector.lock"):
        target = Path(str(db_path) + suffix)
        if target.exists():
            target.unlink()
    shutil.rmtree(archive_dir, ignore_errors=True)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="collect, derive, evaluate, archive, and upload one shard"
    )
    parser.add_argument("--seconds", type=int, default=10_800)
    parser.add_argument("--max-frames", type=int, default=20_000_000)
    parser.add_argument("--work-dir", default="research_pipeline_data/cloud")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--project", default=None)
    parser.add_argument("--dataset", default="crypto_research")
    parser.add_argument("--location", default="us-east1")
    parser.add_argument("--prefix", default="coinbase/BTC-USD/shards")
    parser.add_argument("--config", default=None)
    parser.add_argument("--keep-local", action="store_true")
    args = parser.parse_args(argv)
    effective_project = args.project or storage.Client().project
    if not effective_project:
        parser.error("--project is required when ADC has no default project")

    started = datetime.now(timezone.utc)
    shard_id = started.strftime("%Y%m%dT%H%M%SZ")
    work_dir = Path(args.work_dir).resolve()
    db_path = work_dir / "db" / f"{shard_id}.db"
    archive_dir = work_dir / "archive" / shard_id
    report_path = work_dir / "reports" / f"{shard_id}.json"
    evidence_path = (
        work_dir / "reports" / f"{shard_id}.strategy_evidence.json"
    )
    history_path = (
        work_dir / "reports" / f"{shard_id}.candidate_history.json"
    )
    advisory_path = (
        work_dir / "reports" / f"{shard_id}.candidate_advisory.json"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    common = ["--db", str(db_path)]
    if args.config:
        common += ["--config", args.config]

    report = {
        "shard_id": shard_id,
        "started_at_utc": started.isoformat(),
        "status": "RUNNING",
        "db_path": str(db_path),
        "archive_dir": str(archive_dir),
        "stages": {},
    }
    try:
        report["stages"]["candidate_history"] = _run([
            python,
            "-m",
            "research_pipeline.cli.export_candidate_history",
            "--project",
            effective_project,
            "--dataset",
            args.dataset,
            "--location",
            args.location,
            "--output",
            str(history_path),
        ])
        report["stages"]["context"] = _run([
            python,
            "-m",
            "research_pipeline.cli.collect_context",
            *common,
            "--lookback-days",
            "120",
        ])
        report["stages"]["collect"] = _run([
            python,
            "-m",
            "research_pipeline.cli.collect",
            *common,
            "--seconds",
            str(args.seconds),
            "--max-frames",
            str(args.max_frames),
            "--candidate-history",
            str(history_path),
            "--advisory-output",
            str(advisory_path),
            "--advisory-bucket",
            args.bucket,
            "--advisory-object",
            (
                "coinbase/BTC-USD/advisory/"
                "btc_derivatives_stress_exhaustion/1.0.0/latest.json"
            ),
            "--project",
            effective_project,
        ])
        report["stages"]["derive"] = _run([
            python,
            "-m",
            "research_pipeline.cli.derive",
            *common,
            "--step-seconds",
            "60",
            "--max-points",
            "20000",
        ])
        report["stages"]["evaluate"] = _run([
            python,
            "-m",
            "research_pipeline.cli.evaluate",
            *common,
            "--horizon",
            "5m",
        ])
        report["stages"]["verification"] = _run([
            python,
            "-m",
            "research_pipeline.cli.verify_candidate_runtime",
        ])
        archive_command = [
            python,
            "-m",
            "research_pipeline.cli.archive",
            *common,
            "--output",
            str(archive_dir),
        ]
        report["stages"]["archive"] = _run(archive_command)
        remote_prefix = (
            f"{args.prefix.strip('/')}/{started:%Y/%m/%d}/{shard_id}"
        )
        upload_command = [
            python,
            "-m",
            "research_pipeline.cli.upload_gcs_archive",
            "--archive",
            str(archive_dir),
            "--bucket",
            args.bucket,
            "--prefix",
            remote_prefix,
        ]
        upload_command += ["--project", effective_project]
        report["stages"]["upload"] = _run(upload_command)
        report["stages"]["query_mirror"] = mirror_query_tables(
            archive_dir,
            bucket=args.bucket,
            base_prefix=f"{args.prefix.strip('/')}/query",
            shard_id=shard_id,
            project=effective_project,
        )
        _write_json(report_path, report)
        candidate_command = [
            python,
            "-m",
            "research_pipeline.cli.evaluate_derivatives_stress",
            "--project",
            effective_project,
            "--dataset",
            args.dataset,
            "--location",
            args.location,
            "--bucket",
            args.bucket,
            "--reports-prefix",
            args.prefix,
            "--current-verification-passed",
            "--current-shard-report",
            str(report_path),
            "--output",
            str(evidence_path),
        ]
        if args.config:
            candidate_command += ["--config", args.config]
        report["stages"]["strategy_evidence"] = _run(
            candidate_command,
            allowed_returncodes=(0, 2),
        )
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence_object = (
            f"{remote_prefix}/strategy_evidence/"
            "btc_derivatives_stress_exhaustion_v1.json"
        )
        latest_evidence_object = (
            f"{args.prefix.strip('/')}/strategy_evidence/"
            "btc_derivatives_stress_exhaustion/1.0.0/latest.json"
        )
        _upload_report(
            evidence_path,
            bucket=args.bucket,
            object_name=evidence_object,
            project=effective_project,
        )
        _upload_report(
            evidence_path,
            bucket=args.bucket,
            object_name=latest_evidence_object,
            project=effective_project,
        )
        report["stages"]["strategy_evidence_upload"] = {
            "per_shard_object": evidence_object,
            "latest_object": latest_evidence_object,
            "evidence_status": evidence.get("evidence_status"),
            "live_authority_granted": evidence.get(
                "live_authority_granted"
            ),
        }
        report["status"] = "UPLOADED_AND_VERIFIED"
        report["remote_prefix"] = remote_prefix
    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = str(exc)
    finally:
        report["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(report_path, report)

    if report["status"] != "UPLOADED_AND_VERIFIED":
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    report_object = (
        f"{report['remote_prefix']}/reports/shard_result.json"
    )
    _upload_report(
        report_path,
        bucket=args.bucket,
        object_name=report_object,
        project=effective_project,
    )
    if not args.keep_local:
        _clean_closed_shard(db_path, archive_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
