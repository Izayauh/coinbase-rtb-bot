"""Immutable ingestion + normalized storage for research_pipeline."""
from .store import ResearchStore, canonical_json, payload_sha256, AppendOnlyError

__all__ = ["ResearchStore", "canonical_json", "payload_sha256", "AppendOnlyError"]
