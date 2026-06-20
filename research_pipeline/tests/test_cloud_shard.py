from pathlib import Path
import sys

from research_pipeline.cli.run_cloud_shard import (
    _clean_closed_shard,
    _run,
    _upload_report,
    mirror_query_tables,
)


class FakeBlob:
    def __init__(self, name, objects):
        self.name = name
        self.objects = objects
        self.metadata = {}
        self.content_type = None
        self.size = None

    def upload_from_filename(self, filename, **_kwargs):
        body = Path(filename).read_bytes()
        self.objects[self.name] = {
            "body": body,
            "metadata": dict(self.metadata),
        }
        self.size = len(body)

    def reload(self):
        obj = self.objects[self.name]
        self.size = len(obj["body"])
        self.metadata = dict(obj["metadata"])


class FakeBucket:
    def __init__(self, objects):
        self.objects = objects

    def blob(self, name):
        return FakeBlob(name, self.objects)


class FakeClient:
    def __init__(self):
        self.objects = {}

    def bucket(self, _name):
        return FakeBucket(self.objects)


def test_clean_closed_shard_removes_only_closed_artifacts(tmp_path):
    db = tmp_path / "db" / "shard.db"
    db.parent.mkdir()
    db.write_bytes(b"db")
    for suffix in ("-wal", "-shm", ".collector.lock"):
        Path(str(db) + suffix).write_bytes(b"x")
    archive = tmp_path / "archive" / "shard"
    archive.mkdir(parents=True)
    (archive / "manifest.json").write_text("{}")
    report = tmp_path / "reports" / "shard.json"
    report.parent.mkdir()
    report.write_text("{}")

    _clean_closed_shard(db, archive)

    assert not db.exists()
    assert not archive.exists()
    assert report.exists()


def test_query_mirror_uses_stable_table_first_paths(tmp_path):
    archive = tmp_path / "archive"
    feature_dir = (
        archive
        / "features"
        / "archive_date=2026-06-20"
        / "archive_hour=00"
    )
    feature_dir.mkdir(parents=True)
    (feature_dir / "data.parquet").write_bytes(b"PAR1-feature")
    (archive / "raw_events").mkdir()
    (archive / "raw_events" / "data.parquet").write_bytes(b"PAR1-raw")
    client = FakeClient()

    result = mirror_query_tables(
        archive,
        bucket="research",
        base_prefix="coinbase/BTC-USD/shards/query",
        shard_id="20260620T000000Z",
        project="bitwise-trader",
        client=client,
    )

    assert result["objects_verified"] == 1
    assert result["tables"] == ["features"]
    assert (
        "coinbase/BTC-USD/shards/query/features/"
        "20260620T000000Z/archive_date=2026-06-20/"
        "archive_hour=00/data.parquet"
    ) in client.objects


def test_report_upload_verifies_size_and_hash(tmp_path):
    report = tmp_path / "report.json"
    report.write_text('{"status":"ok"}\n')
    client = FakeClient()

    result = _upload_report(
        report,
        bucket="research",
        object_name="reports/test.json",
        project="bitwise-trader",
        client=client,
    )

    assert result["bytes"] == report.stat().st_size
    assert client.objects["reports/test.json"]["metadata"]["sha256"] == (
        result["sha256"]
    )


def test_run_can_accept_documented_non_passing_evidence_exit_code():
    result = _run(
        [
            sys.executable,
            "-c",
            "import sys; print('{}'); sys.exit(2)",
        ],
        allowed_returncodes=(0, 2),
    )
    assert result["returncode"] == 2
