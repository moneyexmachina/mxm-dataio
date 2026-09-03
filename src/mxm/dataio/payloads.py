"""Payload storage boundary for mxm-dataio.

This module defines the backend-independent contract for immutable external
payload bytes.

Payloads are content-addressed by the lowercase hexadecimal SHA-256 digest of
their exact bytes. The payload store has no knowledge of Sessions, Requests,
Resolutions, Responses, cache policy, external sources, or transport metadata.

The canonical metadata relationship is maintained elsewhere:

    Response.payload_checksum
        ↓
    PayloadStore
        ↓
    exact immutable bytes

Storage implementations may use local memory, S3-compatible object storage, or
other backends, but storage-specific identifiers such as filesystem paths,
bucket keys, URLs, or client objects must not escape this boundary.

Contract
--------
``put(checksum, data)``:

- requires ``checksum == SHA256(data)``;
- stores the exact bytes under that content identity;
- is safe to repeat with the same checksum and bytes;
- must never replace that identity with different bytes.

``get(checksum)``:

- returns the exact bytes stored under that content identity;
- raises ``PayloadNotFoundError`` when the payload does not exist;
- verifies that returned bytes still hash to the requested checksum;
- raises ``PayloadIntegrityError`` when integrity cannot be established.

Backend-specific failures are translated to ``PayloadStoreError`` or one of its
subclasses rather than leaking filesystem, HTTP, SDK, or provider exceptions.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol, runtime_checkable

_SHA256_PATTERN = re.compile(
    r"^[0-9a-f]{64}$",
    flags=re.ASCII,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PayloadStoreError(RuntimeError):
    """Base error for payload-storage failures."""


class PayloadNotFoundError(PayloadStoreError):
    """Raised when a requested payload identity does not exist."""


class PayloadIntegrityError(PayloadStoreError):
    """Raised when payload identity or stored bytes fail integrity checks."""


# ---------------------------------------------------------------------------
# Payload identity and integrity
# ---------------------------------------------------------------------------


def compute_payload_checksum(
    data: bytes,
) -> str:
    """Return the canonical SHA-256 identity of exact payload bytes."""

    return hashlib.sha256(
        data,
    ).hexdigest()


def validate_payload_checksum(
    checksum: str,
) -> None:
    """Require a canonical lowercase hexadecimal SHA-256 payload identity."""

    if (
        _SHA256_PATTERN.fullmatch(
            checksum,
        )
        is None
    ):
        raise PayloadIntegrityError(
            "Payload checksum must be a 64-character lowercase hexadecimal "
            f"SHA-256 value, got {checksum!r}"
        )


def require_payload_matches_checksum(
    checksum: str,
    data: bytes,
) -> None:
    """Require exact payload bytes to match their declared content identity."""

    validate_payload_checksum(
        checksum,
    )

    actual_checksum = compute_payload_checksum(
        data,
    )

    if actual_checksum != checksum:
        raise PayloadIntegrityError(
            "Payload bytes do not match declared checksum: "
            f"expected={checksum!r}, "
            f"actual={actual_checksum!r}"
        )


# ---------------------------------------------------------------------------
# Storage protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PayloadStore(Protocol):
    """Immutable content-addressed storage for exact external payload bytes."""

    def put(
        self,
        checksum: str,
        data: bytes,
    ) -> None:
        """Persist exact payload bytes under their SHA-256 identity.

        Implementations must validate that ``data`` hashes to ``checksum``.

        Repeating the operation with the same checksum and bytes must be safe
        and idempotent.

        Implementations must not expose backend-specific storage identity to
        callers.
        """
        ...

    def get(
        self,
        checksum: str,
    ) -> bytes:
        """Return exact payload bytes for one SHA-256 identity.

        Implementations must validate the checksum syntax and verify that the
        retrieved bytes hash to the requested checksum.

        Raises:
            PayloadNotFoundError:
                If no payload exists for the requested checksum.
            PayloadIntegrityError:
                If the checksum is invalid or retrieved bytes do not match it.
            PayloadStoreError:
                If the backend cannot complete the operation.
        """
        ...


__all__ = [
    "PayloadIntegrityError",
    "PayloadNotFoundError",
    "PayloadStore",
    "PayloadStoreError",
    "compute_payload_checksum",
    "require_payload_matches_checksum",
    "validate_payload_checksum",
]
