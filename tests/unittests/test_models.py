import hashlib
from dataclasses import FrozenInstanceError

import pytest

from mxm.dataio.models import (
    AdapterResult,
    CacheMode,
    Request,
    RequestMethod,
    Response,
    ResponseStatus,
    Session,
    SessionMode,
)
from mxm.types import JSONObj


def _request(
    *,
    source: str = "example-source",
    session_id: str = "s1",
    kind: str = "fetch",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    params: JSONObj | None = None,
) -> Request:
    """Construct one representative immutable request occurrence."""

    return Request(
        session_id=session_id,
        source=source,
        kind=kind,
        cache_mode=cache_mode,
        params=params,
    )


def test_request_hash_determinism() -> None:
    """Equivalent logical requests have the same deterministic hash."""

    params = {
        "symbol": "AAPL",
        "limit": 10,
    }

    r1 = _request(
        session_id="s1",
        params=params,
    )
    r2 = _request(
        session_id="s2",
        params=params,
    )

    assert r1.id != r2.id
    assert r1.hash == r2.hash


def test_request_hash_changes_with_logical_request() -> None:
    """Changing logical request content changes its hash."""

    r1 = _request(
        params={
            "x": 1,
        }
    )
    r2 = _request(
        params={
            "x": 2,
        }
    )

    assert r1.hash != r2.hash


def test_request_hash_includes_source() -> None:
    """The external source participates in logical request identity."""

    r1 = _request(
        source="source-a",
        params={
            "x": 1,
        },
    )
    r2 = _request(
        source="source-b",
        params={
            "x": 1,
        },
    )

    assert r1.hash != r2.hash


def test_request_hash_excludes_cache_policy() -> None:
    """Cache policy changes execution semantics but not logical identity."""

    r1 = _request(
        cache_mode=CacheMode.DEFAULT,
        params={
            "x": 1,
        },
    )
    r2 = _request(
        cache_mode=CacheMode.BYPASS,
        params={
            "x": 1,
        },
    )

    assert r1.hash == r2.hash


def test_request_is_immutable() -> None:
    """A request occurrence cannot be rewritten after construction."""

    request = _request()

    with pytest.raises(FrozenInstanceError):
        request.source = "different-source"  # type: ignore[misc]


def test_response_from_bytes_derives_payload_identity() -> None:
    """Exact payload bytes determine checksum and size."""

    data = b"hello world"

    response = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=data,
    )

    assert response.verify_payload(data)
    assert not response.verify_payload(b"tampered")
    assert response.size_bytes == len(data)
    assert response.payload_checksum == hashlib.sha256(data).hexdigest()


def test_distinct_responses_may_share_payload_identity() -> None:
    """Distinct observations may contain byte-for-byte identical payloads."""

    data = b"identical external payload"

    first = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=data,
    )

    second = Response.from_bytes(
        request_id="request-2",
        status=ResponseStatus.OK,
        data=data,
    )

    assert first.id != second.id
    assert first.request_id != second.request_id

    assert first.payload_checksum == second.payload_checksum
    assert first.size_bytes == second.size_bytes


def test_response_from_adapter_result_preserves_generic_metadata() -> None:
    """Generic adapter-boundary metadata becomes observation metadata."""

    result = AdapterResult(
        data=b'{"value": 42}',
        media_type="application/json",
        encoding="utf-8",
        elapsed_ms=125,
        adapter_meta={
            "vendor_request_id": "abc-123",
            "transport": {
                "status": 200,
            },
        },
    )

    response = Response.from_adapter_result(
        request_id="request-1",
        status=ResponseStatus.OK,
        result=result,
    )

    assert response.payload_checksum == hashlib.sha256(result.data).hexdigest()
    assert response.size_bytes == len(result.data)

    assert response.media_type == "application/json"
    assert response.encoding == "utf-8"
    assert response.elapsed_ms == 125

    assert response.adapter_meta == {
        "vendor_request_id": "abc-123",
        "transport": {
            "status": 200,
        },
    }


def test_response_is_immutable() -> None:
    """An external observation cannot be rewritten after construction."""

    response = Response.from_bytes(
        request_id="request-1",
        status=ResponseStatus.OK,
        data=b"payload",
    )

    with pytest.raises(FrozenInstanceError):
        response.status = ResponseStatus.ERROR  # type: ignore[misc]


def test_adapter_result_meta_dict_contains_generic_metadata() -> None:
    """The legacy metadata helper exposes only generic acquisition metadata."""

    result = AdapterResult(
        data=b"payload",
        media_type="text/plain",
        encoding="utf-8",
        elapsed_ms=17,
        adapter_meta={
            "device_id": "sensor-1",
        },
    )

    assert result.meta_dict() == {
        "media_type": "text/plain",
        "encoding": "utf-8",
        "elapsed_ms": 17,
        "adapter_meta": {
            "device_id": "sensor-1",
        },
    }


def test_session_end_sets_timestamp() -> None:
    session = Session(
        source="test",
        mode=SessionMode.SYNC,
    )

    session.end()

    assert session.ended_at is not None
    assert session.ended_at >= session.started_at


def test_enum_roundtrip() -> None:
    assert SessionMode("async") == SessionMode.ASYNC
    assert RequestMethod("GET") == RequestMethod.GET
    assert CacheMode("default") == CacheMode.DEFAULT
    assert ResponseStatus("ok") == ResponseStatus.OK
