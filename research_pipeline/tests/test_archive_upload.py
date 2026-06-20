import io
from pathlib import Path

from research_pipeline.archive import UploadError, upload_archive


class FakeS3:
    def __init__(self):
        self.objects = {}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = {
            "body": Path(filename).read_bytes(),
            "metadata": (ExtraArgs or {}).get("Metadata", {}),
        }

    def head_object(self, Bucket, Key):
        obj = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(obj["body"]),
            "Metadata": obj["metadata"],
        }

    def put_object(self, Bucket, Key, Body, ContentType, Metadata):
        self.objects[(Bucket, Key)] = {
            "body": bytes(Body),
            "metadata": Metadata,
        }

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]["body"])}


def test_upload_archive_verifies_objects(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "manifest.json").write_text('{"manifest_version": 1}\n')
    table = archive / "quotes"
    table.mkdir()
    (table / "data.parquet").write_bytes(b"PAR1-test")

    client = FakeS3()
    result = upload_archive(
        archive,
        bucket="research",
        prefix="coinbase/btc/2026-06-19",
        client=client,
    )

    assert result["objects_verified"] == 2
    assert result["manifest_key"].endswith("/manifest.json")
    assert (
        "research",
        "coinbase/btc/2026-06-19/quotes/data.parquet",
    ) in client.objects


def test_upload_requires_manifest(tmp_path):
    try:
        upload_archive(
            tmp_path,
            bucket="research",
            prefix="missing",
            client=FakeS3(),
        )
    except UploadError as exc:
        assert "manifest" in str(exc)
    else:
        raise AssertionError("expected UploadError")
