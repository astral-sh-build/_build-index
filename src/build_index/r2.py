"""Shared Cloudflare R2 S3 client configuration."""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlparse

from boto3.session import Session
from botocore.config import Config


class S3Client(Protocol):
    def delete_objects(self, **kwargs: object) -> dict[str, Any]: ...

    def head_object(self, **kwargs: object) -> dict[str, Any]: ...

    def list_objects_v2(self, **kwargs: object) -> dict[str, Any]: ...

    def put_object(self, **kwargs: object) -> dict[str, Any]: ...


def create_s3_client(endpoint: str, *, max_pool_connections: int) -> S3Client:
    """Create one pooled S3 client configured for Cloudflare R2."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("R2 endpoint must be an HTTPS URL")
    return Session().client(
        "s3",
        endpoint_url=endpoint.rstrip("/"),
        region_name="auto",
        config=Config(
            max_pool_connections=max_pool_connections,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"mode": "standard", "total_max_attempts": 8},
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            tcp_keepalive=True,
        ),
    )
