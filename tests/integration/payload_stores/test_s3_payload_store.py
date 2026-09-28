"""S3 integration tests for the immutable DataIO PayloadStore.

These tests exercise S3PayloadStore against a real disposable S3-compatible
server provided by Testcontainers.

They prove the protocol assumptions that unit tests with a fake S3 client
cannot establish:

- exact payload bytes survive a real S3 PUT/GET round trip;
- repeated PUTs of one content identity remain safe;
- an absent object becomes PayloadNotFoundError;
- corrupt or mis-keyed backend bytes are detected on retrieval.

Detailed request construction, object-key translation, checksum validation,
and SDK error mapping are tested separately by the S3PayloadStore unit tests.

The disposable server is MinIO. MinIO is test infrastructure only; the
production PayloadStore implementation remains provider-neutral and is
intended to run first against Hetzner Object Storage.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mypy_boto3_s3.client import S3Client
from testcontainers.core.container import DockerContainer

from mxm.dataio.payload_stores.s3 import S3PayloadStore
from mxm.dataio.payloads import (
    PayloadIntegrityError,
    PayloadNotFoundError,
    compute_payload_checksum,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


pytestmark = pytest.mark.s3


# ---------------------------------------------------------------------------
# Disposable S3 server
# ---------------------------------------------------------------------------

_MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z"
_MINIO_PORT = 9000

_MINIO_ACCESS_KEY = "mxmtestaccess"
_MINIO_SECRET_KEY = "mxmtestsecret123"

_PAYLOAD_PREFIX = "payloads/sha256"


def _make_s3_client(
    *,
    endpoint_url: str,
) -> S3Client:
    """Construct the disposable-test S3 client."""

    return boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=_MINIO_ACCESS_KEY,
        aws_secret_access_key=_MINIO_SECRET_KEY,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=1,
            read_timeout=1,
            retries={
                "max_attempts": 1,
                "mode": "standard",
            },
            s3={
                "addressing_style": "path",
            },
        ),
    )


@dataclass(frozen=True)
class S3TestContext:
    """One isolated bucket and PayloadStore inside the disposable S3 server."""

    client: S3Client
    bucket: str
    store: S3PayloadStore


@pytest.fixture(scope="session")
def s3_endpoint() -> Generator[str]:
    """Provide one disposable S3-compatible server for the test session."""

    container = (
        DockerContainer(
            _MINIO_IMAGE,
        )
        .with_env(
            "MINIO_ROOT_USER",
            _MINIO_ACCESS_KEY,
        )
        .with_env(
            "MINIO_ROOT_PASSWORD",
            _MINIO_SECRET_KEY,
        )
        .with_command(
            [
                "server",
                "/data",
            ]
        )
        .with_exposed_ports(
            _MINIO_PORT,
        )
    )

    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(
            _MINIO_PORT,
        )

        yield f"http://{host}:{port}"


@pytest.fixture(scope="session")
def s3_client(
    s3_endpoint: str,
) -> S3Client:
    """Provide a typed boto S3 client connected to the disposable server."""

    client = _make_s3_client(
        endpoint_url=s3_endpoint,
    )

    _wait_for_s3(
        client,
    )

    return client


@pytest.fixture()
def s3_payload_context(
    s3_client: S3Client,
) -> S3TestContext:
    """Provide one fresh bucket and S3PayloadStore for each test."""

    bucket = f"mxm-dataio-{uuid4().hex}"

    s3_client.create_bucket(
        Bucket=bucket,
    )

    store = S3PayloadStore(
        s3_client,
        bucket=bucket,
        prefix=_PAYLOAD_PREFIX,
    )

    return S3TestContext(
        client=s3_client,
        bucket=bucket,
        store=store,
    )


def _wait_for_s3(
    client: S3Client,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Wait until the disposable S3 server accepts API requests."""

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            client.list_buckets()
            return

        except (BotoCoreError, ClientError) as err:
            last_error = err
            time.sleep(
                0.1,
            )

    raise RuntimeError("Disposable S3 server did not become ready") from last_error


# ---------------------------------------------------------------------------
# Real S3 protocol behaviour
# ---------------------------------------------------------------------------


def test_payload_round_trips_through_s3(
    s3_payload_context: S3TestContext,
) -> None:
    """Exact external payload bytes survive a real S3 PUT/GET round trip."""

    data = b'\x00external-response\xff\n{"value":42}'

    checksum = compute_payload_checksum(
        data,
    )

    s3_payload_context.store.put(
        checksum,
        data,
    )

    retrieved = s3_payload_context.store.get(
        checksum,
    )

    assert retrieved == data


def test_repeated_put_is_idempotent_through_s3(
    s3_payload_context: S3TestContext,
) -> None:
    """Repeated PUTs of one immutable content identity preserve exact bytes."""

    data = b"same immutable external payload"

    checksum = compute_payload_checksum(
        data,
    )

    s3_payload_context.store.put(
        checksum,
        data,
    )

    s3_payload_context.store.put(
        checksum,
        data,
    )

    retrieved = s3_payload_context.store.get(
        checksum,
    )

    assert retrieved == data


def test_missing_payload_raises_not_found(
    s3_payload_context: S3TestContext,
) -> None:
    """A genuinely absent S3 object becomes PayloadNotFoundError."""

    checksum = compute_payload_checksum(
        b"payload that was never stored",
    )

    with pytest.raises(
        PayloadNotFoundError,
    ):
        s3_payload_context.store.get(
            checksum,
        )


def test_corrupt_stored_payload_is_detected(
    s3_payload_context: S3TestContext,
) -> None:
    """Backend bytes stored under the wrong content identity are rejected."""

    expected_data = b"expected immutable payload"

    checksum = compute_payload_checksum(
        expected_data,
    )

    key = f"{_PAYLOAD_PREFIX}/{checksum}"

    # Deliberately bypass S3PayloadStore so that the backend contains a state
    # its validated put() operation could never create.
    s3_payload_context.client.put_object(
        Bucket=s3_payload_context.bucket,
        Key=key,
        Body=b"corrupt payload bytes",
    )

    with pytest.raises(
        PayloadIntegrityError,
        match=r"Payload bytes do not match declared checksum",
    ):
        s3_payload_context.store.get(
            checksum,
        )
