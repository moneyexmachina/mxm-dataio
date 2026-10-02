"""S3-compatible immutable payload storage for mxm-dataio.

This module implements the backend-independent ``PayloadStore`` contract using
an injected S3 client.

The implementation is provider-neutral. Runtime configuration such as endpoint,
region, credentials, signing behaviour, and connection settings belongs to the
application composition root that constructs the S3 client.

Payloads are stored under deterministic object keys derived solely from their
SHA-256 content identity:

    payloads/sha256/<checksum>

Neither S3 object keys nor any other backend-specific storage identity escape
the PayloadStore boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, cast

from botocore.exceptions import BotoCoreError, ClientError

from mxm.dataio.payloads import (
    PayloadNotFoundError,
    PayloadStoreError,
    require_payload_matches_checksum,
    validate_payload_checksum,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class _ReadableBody(Protocol):
    """Minimal S3 response-body interface used by PayloadStore."""

    def read(self) -> bytes: ...


class S3PayloadStore:
    """Immutable content-addressed payload storage backed by S3."""

    def __init__(
        self,
        client: S3Client,
        *,
        bucket: str,
        prefix: str = "payloads/sha256",
    ) -> None:
        """Construct an S3 payload store from an injected client."""

        if not bucket:
            raise ValueError("S3 payload bucket must be non-empty")

        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def put(
        self,
        checksum: str,
        data: bytes,
    ) -> None:
        """Persist exact payload bytes under their SHA-256 identity.

        The checksum is validated against the bytes before any backend
        operation occurs.

        Repeated writes of the same content identity are safe: because the S3
        key is derived from the checksum and the supplied bytes must hash to
        that checksum, every valid write for one key represents the same
        payload content.
        """

        require_payload_matches_checksum(
            checksum,
            data,
        )

        key = self._object_key(
            checksum,
        )

        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentLength=len(data),
            )
        except (ClientError, BotoCoreError) as err:
            raise PayloadStoreError(
                "Failed to persist payload in S3: "
                f"bucket={self._bucket!r}, "
                f"checksum={checksum!r}"
            ) from err

    def get(
        self,
        checksum: str,
    ) -> bytes:
        """Retrieve exact payload bytes and verify their SHA-256 identity."""

        validate_payload_checksum(
            checksum,
        )

        key = self._object_key(
            checksum,
        )

        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
            )

            body = cast(
                _ReadableBody,
                response["Body"],
            )
            data = body.read()

        except ClientError as err:
            if _is_not_found_error(
                err,
            ):
                raise PayloadNotFoundError(
                    "Payload does not exist in S3: "
                    f"bucket={self._bucket!r}, "
                    f"checksum={checksum!r}"
                ) from err

            raise PayloadStoreError(
                "Failed to retrieve payload from S3: "
                f"bucket={self._bucket!r}, "
                f"checksum={checksum!r}"
            ) from err

        except BotoCoreError as err:
            raise PayloadStoreError(
                "Failed to retrieve payload from S3: "
                f"bucket={self._bucket!r}, "
                f"checksum={checksum!r}"
            ) from err

        require_payload_matches_checksum(
            checksum,
            data,
        )

        return data

    def _object_key(
        self,
        checksum: str,
    ) -> str:
        """Return the private S3 object key for one payload identity."""

        if not self._prefix:
            return checksum

        return f"{self._prefix}/{checksum}"


def _is_not_found_error(
    error: ClientError,
) -> bool:
    """Return whether an S3 client error represents an absent object."""
    response = cast(
        Mapping[str, object],
        error.response,
    )
    error_detail = cast(
        Mapping[str, object],
        response["Error"],
    )

    code = cast(
        str,
        error_detail["Code"],
    )
    return code in {
        "NoSuchKey",
        "NotFound",
        "404",
    }


__all__ = [
    "S3PayloadStore",
]
