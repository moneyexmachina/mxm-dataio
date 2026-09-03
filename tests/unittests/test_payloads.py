"""Unit tests for the backend-independent payload storage contract."""

from __future__ import annotations

import hashlib

import pytest

from mxm.dataio.payloads import (
    PayloadIntegrityError,
    PayloadNotFoundError,
    PayloadStoreError,
    compute_payload_checksum,
    require_payload_matches_checksum,
    validate_payload_checksum,
)

# ---------------------------------------------------------------------------
# Payload identity
# ---------------------------------------------------------------------------


def test_compute_payload_checksum_returns_sha256_identity() -> None:
    """Exact payload bytes determine their canonical SHA-256 identity."""

    data = b"hello world"

    checksum = compute_payload_checksum(
        data,
    )

    assert (
        checksum
        == hashlib.sha256(
            data,
        ).hexdigest()
    )


def test_compute_payload_checksum_is_deterministic() -> None:
    """The same exact bytes always produce the same payload identity."""

    data = b"identical payload"

    first = compute_payload_checksum(
        data,
    )

    second = compute_payload_checksum(
        data,
    )

    assert first == second


def test_different_payload_bytes_have_different_checksums() -> None:
    """Different exact payload bytes produce different content identities."""

    first = compute_payload_checksum(
        b"payload-a",
    )

    second = compute_payload_checksum(
        b"payload-b",
    )

    assert first != second


# ---------------------------------------------------------------------------
# Checksum validation
# ---------------------------------------------------------------------------


def test_validate_payload_checksum_accepts_canonical_sha256() -> None:
    """A lowercase hexadecimal SHA-256 identity is accepted."""

    checksum = compute_payload_checksum(
        b"payload",
    )

    validate_payload_checksum(
        checksum,
    )


@pytest.mark.parametrize(
    "checksum",
    [
        "",
        "abc",
        "0" * 63,
        "0" * 65,
        "g" * 64,
        "A" * 64,
        "0123456789abcdef" * 3 + "0123456789abcdeG",
        "0" * 63 + "\n",
    ],
)
def test_validate_payload_checksum_rejects_noncanonical_identity(
    checksum: str,
) -> None:
    """Payload identities must be canonical lowercase hexadecimal SHA-256."""

    with pytest.raises(
        PayloadIntegrityError,
        match=r"64-character lowercase hexadecimal SHA-256",
    ):
        validate_payload_checksum(
            checksum,
        )


# ---------------------------------------------------------------------------
# Payload integrity
# ---------------------------------------------------------------------------


def test_require_payload_matches_checksum_accepts_matching_bytes() -> None:
    """Exact bytes matching their declared identity pass integrity validation."""

    data = b"external response bytes"

    checksum = compute_payload_checksum(
        data,
    )

    require_payload_matches_checksum(
        checksum,
        data,
    )


def test_require_payload_matches_checksum_rejects_different_bytes() -> None:
    """Bytes cannot be stored under another payload's content identity."""

    expected_data = b"expected payload"

    checksum = compute_payload_checksum(
        expected_data,
    )

    with pytest.raises(
        PayloadIntegrityError,
        match=r"Payload bytes do not match declared checksum",
    ):
        require_payload_matches_checksum(
            checksum,
            b"different payload",
        )


def test_require_payload_matches_checksum_rejects_invalid_checksum() -> None:
    """Checksum syntax is validated before payload identity comparison."""

    with pytest.raises(
        PayloadIntegrityError,
        match=r"64-character lowercase hexadecimal SHA-256",
    ):
        require_payload_matches_checksum(
            "not-a-sha256",
            b"payload",
        )


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


def test_payload_errors_share_storage_error_base() -> None:
    """Callers may handle all payload-storage failures through one base error."""

    assert issubclass(
        PayloadIntegrityError,
        PayloadStoreError,
    )

    assert issubclass(
        PayloadNotFoundError,
        PayloadStoreError,
    )
