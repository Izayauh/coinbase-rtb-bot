"""Verified upload of an archive directory to S3-compatible storage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None


class UploadError(RuntimeError):
    """Raised when an object upload or remote verification fails."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _key(prefix: str, relative: str) -> str:
    clean_prefix = prefix.strip("/")
    return f"{clean_prefix}/{relative}" if clean_prefix else relative


def upload_archive(
    archive_dir: str | Path,
    *,
    bucket: str,
    prefix: str,
    endpoint_url: Optional[str] = None,
    region_name: Optional[str] = None,
    client=None,
) -> dict:
    """Upload Parquet files first, then publish and read back the manifest.

    Each object receives a `sha256` metadata field. A successful return means
    every remote object matched the local size and digest metadata, and the
    manifest bytes were read back exactly.
    """
    root = Path(archive_dir).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise UploadError(f"archive manifest does not exist: {manifest_path}")
    try:
        json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UploadError(f"invalid archive manifest: {exc}") from exc

    if client is None:
        if boto3 is None:
            raise UploadError(
                "boto3 is required for object-storage upload; "
                "install project requirements"
            )
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
        )

    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    )
    records = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        object_key = _key(prefix, relative)
        digest = _sha256(path)
        client.upload_file(
            str(path),
            bucket,
            object_key,
            ExtraArgs={
                "Metadata": {"sha256": digest},
                "ContentType": "application/vnd.apache.parquet",
            },
        )
        head = client.head_object(Bucket=bucket, Key=object_key)
        remote_digest = (head.get("Metadata") or {}).get("sha256")
        if int(head.get("ContentLength", -1)) != path.stat().st_size:
            raise UploadError(f"remote size mismatch for {object_key}")
        if remote_digest != digest:
            raise UploadError(f"remote SHA-256 metadata mismatch for {object_key}")
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
    client.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=manifest_bytes,
        ContentType="application/json",
        Metadata={"sha256": manifest_digest},
    )
    remote = client.get_object(Bucket=bucket, Key=manifest_key)
    remote_bytes = remote["Body"].read()
    if remote_bytes != manifest_bytes:
        raise UploadError("remote manifest readback does not match local manifest")

    return {
        "bucket": bucket,
        "prefix": prefix.strip("/"),
        "objects_verified": len(records) + 1,
        "bytes_uploaded": sum(record["bytes"] for record in records)
        + len(manifest_bytes),
        "manifest_key": manifest_key,
        "manifest_sha256": manifest_digest,
    }
