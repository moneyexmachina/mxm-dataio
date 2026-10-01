"""Unit tests for generic external-response acquisition."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Never

import pytest

from mxm.dataio.acquisition import acquire_response
from mxm.dataio.adapters import Fetcher
from mxm.dataio.models import AdapterResult, CacheMode, Request, ResponseStatus
from mxm.dataio.payloads import PayloadNotFoundError, PayloadStoreError
from mxm.types.timestamps import TSNSScalar, ts_ns_from_int


class RecordingPayloadStore:
    """Record payload writes and optionally fail them."""

    def __init__(
        self,
        *,
        put_error: PayloadStoreError | None = None,
    ) -> None:
        self.put_error = put_error
        self.puts: list[tuple[str, bytes]] = []

    def put(
        self,
        checksum: str,
        data: bytes,
    ) -> None:
        """Record or fail one payload write."""

        self.puts.append(
            (
                checksum,
                data,
            )
        )

        if self.put_error is not None:
            raise self.put_error

    def get(
        self,
        checksum: str,
    ) -> bytes:
        """Payload reads are outside acquisition."""

        raise PayloadNotFoundError(checksum)


class RecordingFetcher:
    """Return one configured result while recording the Request."""

    def __init__(
        self,
        *,
        source: str,
        result: AdapterResult,
    ) -> None:
        self.source = source
        self.result = result
        self.requests: list[Request] = []

    def fetch(
        self,
        request: Request,
    ) -> AdapterResult:
        """Record the exact Request and return the configured result."""

        self.requests.append(request)
        return self.result


class RaisingFetcher:
    """Raise one configured exception while recording the Request."""

    def __init__(
        self,
        *,
        source: str,
        error: Exception,
    ) -> None:
        self.source = source
        self.error = error
        self.requests: list[Request] = []

    def fetch(
        self,
        request: Request,
    ) -> Never:
        """Record the exact Request and raise the configured exception."""

        self.requests.append(request)
        raise self.error


def _ts(value: int) -> TSNSScalar:
    """Construct a deterministic canonical timestamp."""

    return ts_ns_from_int(value)


def _request(
    *,
    source: str = "source-a",
) -> Request:
    """Construct one representative Request occurrence."""

    return Request(
        source=source,
        kind="example.fetch",
        cache_mode=CacheMode.DEFAULT,
        params={
            "symbol": "ES",
        },
        created_at=_ts(1),
    )


def _timestamp_source(
    *timestamps: TSNSScalar,
) -> Callable[[], TSNSScalar]:
    """Return deterministic timestamps in call order."""

    values = iter(timestamps)
    return lambda: next(values)


def _ok_result(
    *,
    data: bytes = b"external payload",
) -> AdapterResult:
    """Construct one representative successful adapter result."""

    return AdapterResult(
        status=ResponseStatus.OK,
        data=data,
        media_type="application/octet-stream",
        encoding="binary",
        elapsed_ms=17,
        adapter_meta={
            "vendor_request_id": "vendor-1",
        },
    )


def test_acquire_response_stores_and_returns_exact_adapter_result() -> None:
    """A successful acquisition preserves its exact bytes and metadata."""

    request = _request()
    result = _ok_result()
    adapter: Fetcher = RecordingFetcher(
        source=request.source,
        result=result,
    )
    payload_store = RecordingPayloadStore()
    fetched_at = _ts(10)
    created_at = _ts(11)

    response, data = acquire_response(
        payload_store=payload_store,
        timestamp_source=_timestamp_source(
            fetched_at,
            created_at,
        ),
        request=request,
        adapter=adapter,
    )

    assert isinstance(adapter, RecordingFetcher)
    assert adapter.requests == [request]
    assert response.request_id == request.id
    assert response.status is ResponseStatus.OK
    assert response.payload_checksum == hashlib.sha256(result.data).hexdigest()
    assert response.size_bytes == len(result.data)
    assert response.fetched_at == fetched_at
    assert response.created_at == created_at
    assert response.media_type == result.media_type
    assert response.encoding == result.encoding
    assert response.elapsed_ms == result.elapsed_ms
    assert response.adapter_meta == result.adapter_meta
    assert payload_store.puts == [
        (
            response.payload_checksum,
            result.data,
        )
    ]
    assert data is result.data


def test_acquire_response_rejects_source_mismatch_before_external_io() -> None:
    """A mismatched adapter cannot fetch or write a payload."""

    request = _request()
    adapter = RecordingFetcher(
        source="different-source",
        result=_ok_result(),
    )
    payload_store = RecordingPayloadStore()

    with pytest.raises(
        ValueError,
        match=r"Adapter source does not match Request source",
    ):
        acquire_response(
            payload_store=payload_store,
            timestamp_source=_timestamp_source(
                _ts(10),
                _ts(11),
            ),
            request=request,
            adapter=adapter,
        )

    assert adapter.requests == []
    assert payload_store.puts == []


def test_acquire_response_propagates_adapter_exception_without_payload_write() -> None:
    """An adapter failure propagates without synthesizing a Response."""

    request = _request()
    error = RuntimeError("vendor unavailable")
    adapter = RaisingFetcher(
        source=request.source,
        error=error,
    )
    payload_store = RecordingPayloadStore()

    with pytest.raises(RuntimeError) as raised:
        acquire_response(
            payload_store=payload_store,
            timestamp_source=_timestamp_source(
                _ts(10),
                _ts(11),
            ),
            request=request,
            adapter=adapter,
        )

    assert raised.value is error
    assert adapter.requests == [request]
    assert payload_store.puts == []


def test_acquire_response_propagates_payload_store_failure() -> None:
    """A payload write failure propagates after Response construction."""

    request = _request()
    result = _ok_result()
    adapter = RecordingFetcher(
        source=request.source,
        result=result,
    )
    error = PayloadStoreError("storage unavailable")
    payload_store = RecordingPayloadStore(
        put_error=error,
    )

    with pytest.raises(PayloadStoreError) as raised:
        acquire_response(
            payload_store=payload_store,
            timestamp_source=_timestamp_source(
                _ts(10),
                _ts(11),
            ),
            request=request,
            adapter=adapter,
        )

    assert raised.value is error
    assert payload_store.puts == [
        (
            hashlib.sha256(result.data).hexdigest(),
            result.data,
        )
    ]


def test_acquire_response_persists_and_returns_error_result() -> None:
    """An adapter ERROR is a real external Response with durable bytes."""

    request = _request()
    result = AdapterResult(
        status=ResponseStatus.ERROR,
        data=b'{"error":"rate limited"}',
        media_type="application/json",
        adapter_meta={
            "transport_status": 429,
        },
    )
    adapter = RecordingFetcher(
        source=request.source,
        result=result,
    )
    payload_store = RecordingPayloadStore()

    response, data = acquire_response(
        payload_store=payload_store,
        timestamp_source=_timestamp_source(
            _ts(10),
            _ts(11),
        ),
        request=request,
        adapter=adapter,
    )

    assert response.status is ResponseStatus.ERROR
    assert payload_store.puts == [
        (
            response.payload_checksum,
            result.data,
        )
    ]
    assert data is result.data
