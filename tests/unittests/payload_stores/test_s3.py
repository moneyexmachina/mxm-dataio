"""Unit tests for the S3-backed immutable PayloadStore."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from mxm.dataio.payload_stores.s3 import S3PayloadStore
from mxm.dataio.payloads import (
    PayloadIntegrityError,
    PayloadNotFoundError,
    PayloadStoreError,
    compute_payload_checksum,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


# ---------------------------------------------------------------------------
# Fake S3 boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PutCall:
    """One put_object call issued to the fake S3 client."""

    bucket: str
    key: str
    body: bytes
    content_length: int


@dataclass(frozen=True)
class GetCall:
    """One get_object call issued to the fake S3 client."""

    bucket: str
    key: str


class FakeBody:
    """Minimal streaming response body used by S3PayloadStore."""

    def __init__(
        self,
        data: bytes,
    ) -> None:
        self._data = data
        self.read_calls = 0

    def read(self) -> bytes:
        """Return the scripted object bytes."""

        self.read_calls += 1
        return self._data


class FakeS3Client:
    """Scripted S3 client recording calls without any network access."""

    def __init__(
        self,
        *,
        get_data: bytes = b"",
        put_error: Exception | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.body = FakeBody(
            get_data,
        )

        self.put_error = put_error
        self.get_error = get_error

        self.put_calls: list[PutCall] = []
        self.get_calls: list[GetCall] = []

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentLength: int,
    ) -> dict[str, object]:
        """Record one put_object operation."""

        self.put_calls.append(
            PutCall(
                bucket=Bucket,
                key=Key,
                body=Body,
                content_length=ContentLength,
            )
        )

        if self.put_error is not None:
            raise self.put_error

        return {}

    def get_object(
        self,
        *,
        Bucket: str,
        Key: str,
    ) -> dict[str, object]:
        """Record one get_object operation and return a scripted body."""

        self.get_calls.append(
            GetCall(
                bucket=Bucket,
                key=Key,
            )
        )

        if self.get_error is not None:
            raise self.get_error

        return {
            "Body": self.body,
        }


class ScriptedBotoCoreError(BotoCoreError):
    """Concrete botocore failure for exception-translation tests."""

    fmt = "scripted boto failure"


def _as_s3_client(
    client: FakeS3Client,
) -> S3Client:
    """Cast the fake client to the injected production boundary."""

    return cast(
        "S3Client",
        client,
    )


def _store(
    client: FakeS3Client,
    *,
    bucket: str = "payload-test",
    prefix: str = "payloads/sha256",
) -> S3PayloadStore:
    """Construct one S3PayloadStore around a fake client."""

    return S3PayloadStore(
        _as_s3_client(
            client,
        ),
        bucket=bucket,
        prefix=prefix,
    )


def _client_error(
    code: str,
    *,
    operation: str = "GetObject",
) -> ClientError:
    """Construct one scripted botocore ClientError."""

    return ClientError(
        {
            "Error": {
                "Code": code,
                "Message": "scripted S3 failure",
            }
        },
        operation,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_store_rejects_empty_bucket() -> None:
    """An S3 payload store requires an explicit bucket."""

    client = FakeS3Client()

    with pytest.raises(
        ValueError,
        match=r"bucket must be non-empty",
    ):
        _store(
            client,
            bucket="",
        )


# ---------------------------------------------------------------------------
# put
# ---------------------------------------------------------------------------


def test_put_writes_payload_under_deterministic_content_key() -> None:
    """Payload identity determines the S3 object key."""

    data = b"external payload"
    checksum = compute_payload_checksum(
        data,
    )

    client = FakeS3Client()
    store = _store(
        client,
    )

    store.put(
        checksum,
        data,
    )

    assert client.put_calls == [
        PutCall(
            bucket="payload-test",
            key=f"payloads/sha256/{checksum}",
            body=data,
            content_length=len(data),
        )
    ]


def test_put_normalises_prefix() -> None:
    """Leading and trailing separators are not part of the configured prefix."""

    data = b"payload"
    checksum = compute_payload_checksum(
        data,
    )

    client = FakeS3Client()
    store = _store(
        client,
        prefix="/archive/payloads/",
    )

    store.put(
        checksum,
        data,
    )

    assert client.put_calls[0].key == f"archive/payloads/{checksum}"


def test_put_with_empty_prefix_uses_checksum_as_key() -> None:
    """A deliberately empty prefix stores directly by content identity."""

    data = b"payload"
    checksum = compute_payload_checksum(
        data,
    )

    client = FakeS3Client()
    store = _store(
        client,
        prefix="",
    )

    store.put(
        checksum,
        data,
    )

    assert client.put_calls[0].key == checksum


def test_put_rejects_checksum_mismatch_before_s3_access() -> None:
    """Bytes cannot be written under another payload's identity."""

    checksum = compute_payload_checksum(
        b"expected",
    )

    client = FakeS3Client()
    store = _store(
        client,
    )

    with pytest.raises(
        PayloadIntegrityError,
        match=r"Payload bytes do not match declared checksum",
    ):
        store.put(
            checksum,
            b"different",
        )

    assert client.put_calls == []


def test_repeated_puts_issue_same_deterministic_operation() -> None:
    """Repeated writes of one payload identity are semantically idempotent."""

    data = b"same immutable payload"
    checksum = compute_payload_checksum(
        data,
    )

    client = FakeS3Client()
    store = _store(
        client,
    )

    store.put(
        checksum,
        data,
    )

    store.put(
        checksum,
        data,
    )

    assert client.put_calls == [
        PutCall(
            bucket="payload-test",
            key=f"payloads/sha256/{checksum}",
            body=data,
            content_length=len(data),
        ),
        PutCall(
            bucket="payload-test",
            key=f"payloads/sha256/{checksum}",
            body=data,
            content_length=len(data),
        ),
    ]


@pytest.mark.parametrize(
    "error",
    [
        _client_error(
            "AccessDenied",
            operation="PutObject",
        ),
        ScriptedBotoCoreError(),
    ],
)
def test_put_translates_backend_failures(
    error: Exception,
) -> None:
    """S3 SDK failures do not leak through the PayloadStore boundary."""

    data = b"payload"
    checksum = compute_payload_checksum(
        data,
    )

    client = FakeS3Client(
        put_error=error,
    )

    store = _store(
        client,
    )

    with pytest.raises(
        PayloadStoreError,
        match=r"Failed to persist payload in S3",
    ) as caught:
        store.put(
            checksum,
            data,
        )

    assert caught.value.__cause__ is error


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_reads_payload_from_deterministic_content_key() -> None:
    """Payload retrieval addresses the object solely by content identity."""

    data = b"stored external payload"
    checksum = compute_payload_checksum(
        data,
    )

    client = FakeS3Client(
        get_data=data,
    )

    store = _store(
        client,
    )

    result = store.get(
        checksum,
    )

    assert result == data

    assert client.get_calls == [
        GetCall(
            bucket="payload-test",
            key=f"payloads/sha256/{checksum}",
        )
    ]

    assert client.body.read_calls == 1


def test_get_rejects_invalid_checksum_before_s3_access() -> None:
    """Invalid payload identity fails before contacting the backend."""

    client = FakeS3Client()
    store = _store(
        client,
    )

    with pytest.raises(
        PayloadIntegrityError,
        match=r"64-character lowercase hexadecimal SHA-256",
    ):
        store.get(
            "not-a-sha256",
        )

    assert client.get_calls == []
    assert client.body.read_calls == 0


def test_get_detects_corrupt_or_miskeyed_payload() -> None:
    """Retrieved bytes must match the requested content identity."""

    checksum = compute_payload_checksum(
        b"expected bytes",
    )

    client = FakeS3Client(
        get_data=b"wrong bytes",
    )

    store = _store(
        client,
    )

    with pytest.raises(
        PayloadIntegrityError,
        match=r"Payload bytes do not match declared checksum",
    ):
        store.get(
            checksum,
        )

    assert client.body.read_calls == 1


@pytest.mark.parametrize(
    "code",
    [
        "NoSuchKey",
        "NotFound",
        "404",
    ],
)
def test_get_translates_missing_object(
    code: str,
) -> None:
    """Ordinary S3 missing-object responses become PayloadNotFoundError."""

    checksum = compute_payload_checksum(
        b"absent payload",
    )

    error = _client_error(
        code,
    )

    client = FakeS3Client(
        get_error=error,
    )

    store = _store(
        client,
    )

    with pytest.raises(
        PayloadNotFoundError,
        match=r"Payload does not exist in S3",
    ) as caught:
        store.get(
            checksum,
        )

    assert caught.value.__cause__ is error


def test_get_translates_non_missing_client_error() -> None:
    """Non-404 S3 client failures remain generic payload-store failures."""

    checksum = compute_payload_checksum(
        b"payload",
    )

    error = _client_error(
        "AccessDenied",
    )

    client = FakeS3Client(
        get_error=error,
    )

    store = _store(
        client,
    )

    with pytest.raises(
        PayloadStoreError,
        match=r"Failed to retrieve payload from S3",
    ) as caught:
        store.get(
            checksum,
        )

    assert type(caught.value) is PayloadStoreError
    assert caught.value.__cause__ is error


def test_get_translates_botocore_failure() -> None:
    """Transport/SDK failures become PayloadStoreError."""

    checksum = compute_payload_checksum(
        b"payload",
    )

    error = ScriptedBotoCoreError()

    client = FakeS3Client(
        get_error=error,
    )

    store = _store(
        client,
    )

    with pytest.raises(
        PayloadStoreError,
        match=r"Failed to retrieve payload from S3",
    ) as caught:
        store.get(
            checksum,
        )

    assert caught.value.__cause__ is error
