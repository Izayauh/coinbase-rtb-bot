from pathlib import Path

from research_pipeline.archive import GCSUploadError, upload_archive_to_gcs


class FakeBlob:
    def __init__(self, name, objects):
        self.name = name
        self.objects = objects
        self.metadata = {}
        self.content_type = None
        self.size = None

    def upload_from_filename(self, filename):
        body = Path(filename).read_bytes()
        self.objects[self.name] = {
            "body": body,
            "metadata": dict(self.metadata),
            "content_type": self.content_type,
        }
        self.size = len(body)

    def upload_from_string(self, body, content_type=None):
        body = bytes(body)
        self.objects[self.name] = {
            "body": body,
            "metadata": dict(self.metadata),
            "content_type": content_type,
        }
        self.size = len(body)

    def reload(self):
        obj = self.objects[self.name]
        self.size = len(obj["body"])
        self.metadata = dict(obj["metadata"])

    def download_as_bytes(self):
        return self.objects[self.name]["body"]


class FakeBucket:
    def __init__(self, objects):
        self.objects = objects

    def blob(self, name):
        return FakeBlob(name, self.objects)


class FakeGCS:
    def __init__(self):
        self.objects = {}

    def bucket(self, name):
        return FakeBucket(self.objects)


def test_gcs_upload_verifies_objects(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "manifest.json").write_text(
        '{"manifest_version": 1}\n',
        encoding="utf-8",
    )
    table = archive / "quotes"
    table.mkdir()
    (table / "data.parquet").write_bytes(b"PAR1-test")

    client = FakeGCS()
    result = upload_archive_to_gcs(
        archive,
        bucket="research",
        prefix="coinbase/btc/2026-06-19",
        client=client,
    )

    assert result["objects_verified"] == 2
    assert result["manifest_key"].endswith("/manifest.json")
    assert (
        "coinbase/btc/2026-06-19/quotes/data.parquet"
        in client.objects
    )


def test_gcs_upload_requires_manifest(tmp_path):
    try:
        upload_archive_to_gcs(
            tmp_path,
            bucket="research",
            prefix="missing",
            client=FakeGCS(),
        )
    except GCSUploadError as exc:
        assert "manifest" in str(exc)
    else:
        raise AssertionError("expected GCSUploadError")
