"""Verified upload of a research archive to Google Cloud Storage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

try:
    from google.cloud import storage
except ImportError:  # pragma: no cover
    storage = None


class GCSUploadError(RuntimeError):
    """Raised when a Google Cloud Storage upload or verification fails."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _key(prefix: str, relative: str) -> str:
    clean_prefix = prefix.strip("/")
    return f"{clean_prefix}/{relative}" if clean_prefix else relative


def upload_archive_to_gcs(
    archive_dir: str | Path,
    *,
    bucket: str,
    prefix: str,
    project: str | None = None,
    client=None,
) -> dict:
    """Upload Parquet objects first, then publish and verify the manifest.

    Every object receives a SHA-256 metadata value. A successful return means
    remote object sizes and metadata matched and the manifest readback was
    byte-identical to the local manifest.
    """
    root = Path(archive_dir).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise GCSUploadError(
            f"archive manifest does not exist: {manifest_path}"
        )
    try:
        json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GCSUploadError(f"invalid archive manifest: {exc}") from exc

    if client is None:
        if storage is None:
            raise GCSUploadError(
                "google-cloud-storage is required for GCS upload; "
                "install project requirements"
            )
        client = storage.Client(project=project)

    bucket_ref = client.bucket(bucket)
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    )
    records = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        object_key = _key(prefix, relative)
        digest = _sha256(path)
        blob = bucket_ref.blob(object_key)
        blob.metadata = {"sha256": digest}
        blob.content_type = "application/vnd.apache.parquet"
        blob.upload_from_filename(str(path))
        blob.reload()
        if int(blob.size or -1) != path.stat().st_size:
            raise GCSUploadError(f"remote size mismatch for {object_key}")
        if (blob.metadata or {}).get("sha256") != digest:
            raise GCSUploadError(
                f"remote SHA-256 metadata mismatch for {object_key}"
            )
        records.append(
            {
                "key": object_key,
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
        )

    manifest_key = _key(prefix, "manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_blob = bucket_ref.blob(manifest_key)
    manifest_blob.metadata = {"sha256": manifest_digest}
    manifest_blob.content_type = "application/json"
    manifest_blob.upload_from_string(
        manifest_bytes,
        content_type="application/json",
    )
    if manifest_blob.download_as_bytes() != manifest_bytes:
        raise GCSUploadError(
            "remote manifest readback does not match local manifest"
        )

    return {
        "bucket": bucket,
        "prefix": prefix.strip("/"),
        "objects_verified": len(records) + 1,
        "bytes_uploaded": sum(record["bytes"] for record in records)
        + len(manifest_bytes),
        "manifest_key": manifest_key,
        "manifest_sha256": manifest_digest,
    }
