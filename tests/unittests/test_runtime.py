"""Unit tests for the thin DataIO runtime façade."""

from __future__ import annotations

from collections.abc import Callable
from typing import Never

import pytest

import mxm.dataio.runtime as runtime
from mxm.dataio import CacheMissError, DataIO, ResolveResult, compose_dataio
from mxm.dataio.models import (
    AdapterResult,
    CacheMode,
    Request,
    Resolution,
    ResolutionKind,
    Response,
    ResponseStatus,
)
from mxm.dataio.payloads import PayloadNotFoundError, PayloadStore
from mxm.dataio.sql.postgres import PostgresDatabase
from mxm.types import JSONObj
from mxm.types.timestamps import TSNSScalar, ts_ns_from_str


class UnusedPayloadStore:
    """Payload dependency that fails if the facade performs storage work."""

    def put(
        self,
        checksum: str,
        data: bytes,
    ) -> Never:
        raise AssertionError(f"Unexpected payload write: {checksum!r}, {data!r}")

    def get(
        self,
        checksum: str,
    ) -> Never:
        raise PayloadNotFoundError(checksum)


class UnusedFetcher:
    """Adapter that fails if the facade performs acquisition."""

    source = "source-a"

    def fetch(
        self,
        request: Request,
    ) -> AdapterResult:
        raise AssertionError(f"Unexpected adapter call: {request!r}")


def test_dataio_resolve_delegates_all_arguments_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The facade only binds dependencies and delegates resolution."""

    database = PostgresDatabase(
        "postgresql://mxm@localhost/mxm_dev",
    )
    payload_store = UnusedPayloadStore()
    adapter = UnusedFetcher()
    params: JSONObj = {
        "symbol": "ES",
    }

    def timestamp_source() -> TSNSScalar:
        return ts_ns_from_str("2026-10-02T10:00:00.000001000Z")

    request = Request(
        id="request-1",
        source="source-a",
        kind="example.fetch",
        params=params,
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=60.0,
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
        created_at=ts_ns_from_str("2026-10-02T10:00:00.000001000Z"),
    )
    response = Response(
        id="response-1",
        request_id=request.id,
        status=ResponseStatus.OK,
        payload_checksum="0" * 64,
        size_bytes=0,
        fetched_at=ts_ns_from_str("2026-10-02T09:59:00.000002000Z"),
        created_at=ts_ns_from_str("2026-10-02T09:59:00.000003000Z"),
    )
    resolved = Resolution(
        request_id=request.id,
        response_id=response.id,
        kind=ResolutionKind.REUSED,
        resolved_at=ts_ns_from_str("2026-10-02T10:00:00.000004000Z"),
    )
    expected = ResolveResult(
        request=request,
        response=response,
        resolution=resolved,
        data=b"",
    )
    calls = 0

    def delegate(
        *,
        database: PostgresDatabase,
        payload_store: PayloadStore,
        timestamp_source: Callable[[], TSNSScalar],
        source: str,
        kind: str,
        params: JSONObj | None,
        adapter: runtime.Fetcher,
        cache_mode: CacheMode,
        ttl_seconds: float | None,
        as_of_bucket: str | None,
        cache_tag: str | None,
    ) -> ResolveResult:
        nonlocal calls
        calls += 1

        assert database is expected_database
        assert payload_store is expected_payload_store
        assert timestamp_source is expected_timestamp_source
        assert source == "source-a"
        assert kind == "example.fetch"
        assert params is expected_params
        assert adapter is expected_adapter
        assert cache_mode is CacheMode.ONLY_IF_CACHED
        assert ttl_seconds == 60.0
        assert as_of_bucket == "bucket-a"
        assert cache_tag == "vendor-v1"

        return expected

    expected_database = database
    expected_payload_store = payload_store
    expected_timestamp_source = timestamp_source
    expected_params = params
    expected_adapter = adapter

    monkeypatch.setattr(
        runtime.resolution,
        "resolve",
        delegate,
    )

    dataio = compose_dataio(
        database=database,
        payload_store=payload_store,
        timestamp_source=timestamp_source,
    )

    actual = dataio.resolve(
        source="source-a",
        kind="example.fetch",
        params=params,
        adapter=adapter,
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=60.0,
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
    )

    assert isinstance(dataio, DataIO)
    assert actual is expected
    assert calls == 1
    assert issubclass(CacheMissError, RuntimeError)
