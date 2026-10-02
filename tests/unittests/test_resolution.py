"""Unit tests for the DataIO resolution state machine."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Never, cast

import pytest
from psycopg import Connection

import mxm.dataio.resolution as resolution_workflow
from mxm.dataio.models import (
    AdapterResult,
    CacheMode,
    Request,
    Resolution,
    ResolutionKind,
    Response,
    ResponseStatus,
)
from mxm.dataio.payloads import (
    PayloadNotFoundError,
    PayloadStore,
    compute_payload_checksum,
)
from mxm.dataio.resolution import CacheMissError, resolve
from mxm.dataio.sql.postgres import PostgresDatabase, PostgresRow
from mxm.dataio.sql.reuse import ReuseLookupError
from mxm.types.timestamps import TSNSScalar, ts_ns_from_str

_REQUEST_AT = ts_ns_from_str("2026-10-02T10:00:00.000001000Z")
_FETCHED_AT = ts_ns_from_str("2026-10-02T10:00:01.000002000Z")
_RESPONSE_AT = ts_ns_from_str("2026-10-02T10:00:01.000003000Z")
_RESOLVED_AT = ts_ns_from_str("2026-10-02T10:00:01.000004000Z")
_PAYLOAD = b"exact external payload"


class TransactionRecord:
    """One fake transaction and its pending operations."""

    def __init__(self) -> None:
        self.operations: list[str] = []
        self.committed = False
        self.rolled_back = False


class FakeDatabase:
    """Record transaction boundaries and committed operations."""

    schema = "dataio_test_abc"

    def __init__(self) -> None:
        self.transactions: list[TransactionRecord] = []
        self.durable_operations: list[str] = []

    @contextmanager
    def transaction(
        self,
    ) -> Generator[Connection[PostgresRow]]:
        transaction = TransactionRecord()
        self.transactions.append(transaction)

        try:
            yield cast(
                Connection[PostgresRow],
                transaction,
            )
        except BaseException:
            transaction.rolled_back = True
            raise
        else:
            transaction.committed = True
            self.durable_operations.extend(transaction.operations)


class RecordingPayloadStore:
    """Record payload writes made by acquisition."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(
        self,
        checksum: str,
        data: bytes,
    ) -> None:
        self.puts.append(
            (
                checksum,
                data,
            )
        )

    def get(
        self,
        checksum: str,
    ) -> bytes:
        raise PayloadNotFoundError(checksum)


class RecordingFetcher:
    """Return one result while recording adapter invocations."""

    def __init__(
        self,
        *,
        source: str = "source-a",
        status: ResponseStatus = ResponseStatus.OK,
    ) -> None:
        self.source = source
        self.status = status
        self.requests: list[Request] = []

    def fetch(
        self,
        request: Request,
    ) -> AdapterResult:
        self.requests.append(request)
        return AdapterResult(
            status=self.status,
            data=_PAYLOAD,
            media_type="application/octet-stream",
            adapter_meta={
                "vendor_request_id": "vendor-1",
            },
        )


class RaisingFetcher:
    """Raise one configured adapter error."""

    source = "source-a"

    def __init__(
        self,
        error: Exception,
    ) -> None:
        self.error = error
        self.requests: list[Request] = []

    def fetch(
        self,
        request: Request,
    ) -> Never:
        self.requests.append(request)
        raise self.error


class TimestampSource:
    """Return deterministic timestamps in call order."""

    def __init__(
        self,
        *values: TSNSScalar,
    ) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self) -> TSNSScalar:
        self.calls += 1
        return next(self._values)


def _as_database(
    database: FakeDatabase,
) -> PostgresDatabase:
    return cast(
        PostgresDatabase,
        database,
    )


def _as_payload_store(
    payload_store: RecordingPayloadStore,
) -> PayloadStore:
    return cast(
        PayloadStore,
        payload_store,
    )


def _transaction(
    connection: Connection[PostgresRow],
) -> TransactionRecord:
    return cast(
        TransactionRecord,
        connection,
    )


def _install_persistence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    request_error: Exception | None = None,
    resolution_error: Exception | None = None,
) -> None:
    """Replace SQL writes with transaction-aware recording functions."""

    def record_request(
        connection: Connection[PostgresRow],
        *,
        schema: str,
        request: Request,
    ) -> None:
        assert schema == FakeDatabase.schema
        _transaction(connection).operations.append(
            f"request:{request.id}",
        )

        if request_error is not None:
            raise request_error

    def record_response(
        connection: Connection[PostgresRow],
        *,
        schema: str,
        response: Response,
    ) -> None:
        assert schema == FakeDatabase.schema
        _transaction(connection).operations.append(
            f"response:{response.id}",
        )

    def record_resolution(
        connection: Connection[PostgresRow],
        *,
        schema: str,
        resolution: Resolution,
    ) -> None:
        assert schema == FakeDatabase.schema
        _transaction(connection).operations.append(
            f"resolution:{resolution.request_id}",
        )

        if resolution_error is not None:
            raise resolution_error

    monkeypatch.setattr(
        resolution_workflow,
        "insert_request",
        record_request,
    )
    monkeypatch.setattr(
        resolution_workflow,
        "insert_response",
        record_response,
    )
    monkeypatch.setattr(
        resolution_workflow,
        "insert_resolution",
        record_resolution,
    )


def _install_reuse(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: tuple[Response, bytes] | None,
    requests: list[Request],
    error: Exception | None = None,
) -> None:
    """Replace reuse recovery with one configured result."""

    def find_reusable(
        *,
        database: PostgresDatabase,
        payload_store: PayloadStore,
        request: Request,
    ) -> tuple[Response, bytes] | None:
        _ = database, payload_store
        requests.append(request)

        if error is not None:
            raise error

        return result

    monkeypatch.setattr(
        resolution_workflow,
        "find_reusable_response",
        find_reusable,
    )


def _historical_response() -> Response:
    """Construct one existing reusable Response."""

    return Response(
        id="response-historical",
        request_id="request-historical",
        status=ResponseStatus.OK,
        payload_checksum=compute_payload_checksum(
            _PAYLOAD,
        ),
        size_bytes=len(
            _PAYLOAD,
        ),
        fetched_at=_FETCHED_AT,
        created_at=_RESPONSE_AT,
    )


def _resolve(
    *,
    database: FakeDatabase,
    payload_store: RecordingPayloadStore,
    timestamp_source: Callable[[], TSNSScalar],
    adapter: RecordingFetcher | RaisingFetcher,
    cache_mode: CacheMode = CacheMode.DEFAULT,
) -> resolution_workflow.ResolveResult:
    """Resolve one representative logical request."""

    return resolve(
        database=_as_database(
            database,
        ),
        payload_store=_as_payload_store(
            payload_store,
        ),
        timestamp_source=timestamp_source,
        source="source-a",
        kind="example.fetch",
        params={
            "symbol": "ES",
        },
        adapter=adapter,
        cache_mode=cache_mode,
        ttl_seconds=60.0,
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
    )


def test_source_mismatch_fails_before_request_or_external_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source identity is validated before the resolution attempt begins."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher(
        source="different-source",
    )
    timestamp_source = TimestampSource(
        _REQUEST_AT,
    )
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
    )

    with pytest.raises(
        ValueError,
        match=r"Adapter source does not match requested source",
    ):
        _resolve(
            database=database,
            payload_store=payload_store,
            timestamp_source=timestamp_source,
            adapter=adapter,
        )

    assert timestamp_source.calls == 0
    assert database.transactions == []
    assert reuse_requests == []
    assert adapter.requests == []
    assert payload_store.puts == []


def test_request_persistence_failure_stops_resolution_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution does not continue unless the Request becomes durable."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher()
    timestamp_source = TimestampSource(
        _REQUEST_AT,
    )
    error = RuntimeError("request insert failed")
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
        request_error=error,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
    )

    with pytest.raises(RuntimeError) as raised:
        _resolve(
            database=database,
            payload_store=payload_store,
            timestamp_source=timestamp_source,
            adapter=adapter,
        )

    assert raised.value is error
    assert len(database.transactions) == 1
    assert database.transactions[0].rolled_back
    assert database.durable_operations == []
    assert reuse_requests == []
    assert adapter.requests == []
    assert payload_store.puts == []


def test_reuse_infrastructure_failure_does_not_become_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse infrastructure errors propagate after Request persistence."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher()
    timestamp_source = TimestampSource(
        _REQUEST_AT,
    )
    error = ReuseLookupError("reuse lookup unavailable")
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
        error=error,
    )

    with pytest.raises(ReuseLookupError) as raised:
        _resolve(
            database=database,
            payload_store=payload_store,
            timestamp_source=timestamp_source,
            adapter=adapter,
        )

    assert raised.value is error
    assert len(reuse_requests) == 1
    request = reuse_requests[0]
    assert len(database.transactions) == 1
    assert database.transactions[0].committed
    assert database.durable_operations == [
        f"request:{request.id}",
    ]
    assert adapter.requests == []
    assert payload_store.puts == []


def test_reuse_hit_persists_reused_resolution_without_adapter_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reusable historical Response satisfies the new Request unchanged."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher()
    timestamp_source = TimestampSource(
        _REQUEST_AT,
        _RESOLVED_AT,
    )
    historical_response = _historical_response()
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
    )
    _install_reuse(
        monkeypatch,
        result=(
            historical_response,
            _PAYLOAD,
        ),
        requests=reuse_requests,
    )

    result = _resolve(
        database=database,
        payload_store=payload_store,
        timestamp_source=timestamp_source,
        adapter=adapter,
    )

    assert reuse_requests == [result.request]
    assert result.request.created_at == _REQUEST_AT
    assert result.response is historical_response
    assert result.data is _PAYLOAD
    assert result.resolution == Resolution(
        request_id=result.request.id,
        response_id=historical_response.id,
        kind=ResolutionKind.REUSED,
        resolved_at=_RESOLVED_AT,
    )
    assert adapter.requests == []
    assert payload_store.puts == []
    assert len(database.transactions) == 2
    assert database.transactions[0].operations == [
        f"request:{result.request.id}",
    ]
    assert database.transactions[1].operations == [
        f"resolution:{result.request.id}",
    ]
    assert all(transaction.committed for transaction in database.transactions)


def test_default_miss_acquires_and_atomically_persists_ok_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFAULT acquires on a miss and commits Response with Resolution."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher()
    timestamp_source = TimestampSource(
        _REQUEST_AT,
        _FETCHED_AT,
        _RESPONSE_AT,
        _RESOLVED_AT,
    )
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
    )

    result = _resolve(
        database=database,
        payload_store=payload_store,
        timestamp_source=timestamp_source,
        adapter=adapter,
    )

    assert reuse_requests == [result.request]
    assert adapter.requests == [result.request]
    assert result.request.created_at == _REQUEST_AT
    assert result.response.request_id == result.request.id
    assert result.response.status is ResponseStatus.OK
    assert result.response.fetched_at == _FETCHED_AT
    assert result.response.created_at == _RESPONSE_AT
    assert result.resolution == Resolution(
        request_id=result.request.id,
        response_id=result.response.id,
        kind=ResolutionKind.ACQUIRED,
        resolved_at=_RESOLVED_AT,
    )
    assert result.data is _PAYLOAD
    assert payload_store.puts == [
        (
            result.response.payload_checksum,
            _PAYLOAD,
        )
    ]
    assert len(database.transactions) == 2
    assert database.transactions[1].operations == [
        f"response:{result.response.id}",
        f"resolution:{result.request.id}",
    ]
    assert database.transactions[1].committed


def test_bypass_acquires_after_reuse_capability_returns_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BYPASS acquisition follows the reuse capability's policy-aware miss."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher()
    timestamp_source = TimestampSource(
        _REQUEST_AT,
        _FETCHED_AT,
        _RESPONSE_AT,
        _RESOLVED_AT,
    )
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
    )

    result = _resolve(
        database=database,
        payload_store=payload_store,
        timestamp_source=timestamp_source,
        adapter=adapter,
        cache_mode=CacheMode.BYPASS,
    )

    assert reuse_requests == [result.request]
    assert adapter.requests == [result.request]
    assert result.resolution is not None
    assert result.resolution.kind is ResolutionKind.ACQUIRED


def test_cache_only_miss_leaves_persisted_unresolved_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ONLY_IF_CACHED miss records the attempt without contacting the source."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher()
    timestamp_source = TimestampSource(
        _REQUEST_AT,
    )
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
    )

    with pytest.raises(
        CacheMissError,
        match=r"No reusable Response.*request_id=.*request_hash=",
    ):
        _resolve(
            database=database,
            payload_store=payload_store,
            timestamp_source=timestamp_source,
            adapter=adapter,
            cache_mode=CacheMode.ONLY_IF_CACHED,
        )

    assert len(reuse_requests) == 1
    request = reuse_requests[0]
    assert database.durable_operations == [
        f"request:{request.id}",
    ]
    assert adapter.requests == []
    assert payload_store.puts == []
    assert timestamp_source.calls == 1


def test_adapter_exception_leaves_persisted_unresolved_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter exceptions propagate after the Request transaction commits."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    error = RuntimeError("vendor unavailable")
    adapter = RaisingFetcher(error)
    timestamp_source = TimestampSource(
        _REQUEST_AT,
    )
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
    )

    with pytest.raises(RuntimeError) as raised:
        _resolve(
            database=database,
            payload_store=payload_store,
            timestamp_source=timestamp_source,
            adapter=adapter,
        )

    assert raised.value is error
    assert adapter.requests == reuse_requests
    assert database.durable_operations == [
        f"request:{adapter.requests[0].id}",
    ]
    assert payload_store.puts == []
    assert len(database.transactions) == 1


def test_error_response_is_persisted_without_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ERROR observation is returned and persisted but does not resolve."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher(
        status=ResponseStatus.ERROR,
    )
    timestamp_source = TimestampSource(
        _REQUEST_AT,
        _FETCHED_AT,
        _RESPONSE_AT,
    )
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
    )

    result = _resolve(
        database=database,
        payload_store=payload_store,
        timestamp_source=timestamp_source,
        adapter=adapter,
    )

    assert result.response.status is ResponseStatus.ERROR
    assert result.response.fetched_at == _FETCHED_AT
    assert result.response.created_at == _RESPONSE_AT
    assert result.resolution is None
    assert result.data is _PAYLOAD
    assert payload_store.puts == [
        (
            result.response.payload_checksum,
            _PAYLOAD,
        )
    ]
    assert database.transactions[1].operations == [
        f"response:{result.response.id}",
    ]
    assert timestamp_source.calls == 3


def test_acquired_resolution_failure_rolls_back_response_and_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acquired Response and Resolution share one rollback boundary."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher()
    error = RuntimeError("resolution insert failed")
    timestamp_source = TimestampSource(
        _REQUEST_AT,
        _FETCHED_AT,
        _RESPONSE_AT,
        _RESOLVED_AT,
    )
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
        resolution_error=error,
    )
    _install_reuse(
        monkeypatch,
        result=None,
        requests=reuse_requests,
    )

    with pytest.raises(RuntimeError) as raised:
        _resolve(
            database=database,
            payload_store=payload_store,
            timestamp_source=timestamp_source,
            adapter=adapter,
        )

    assert raised.value is error
    assert len(database.transactions) == 2
    assert database.transactions[1].operations[0].startswith("response:")
    assert database.transactions[1].operations[1].startswith("resolution:")
    assert database.transactions[1].rolled_back
    assert database.durable_operations == [
        f"request:{adapter.requests[0].id}",
    ]
    assert payload_store.puts != []


def test_reused_resolution_failure_propagates_without_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed REUSED Resolution write leaves only the new Request durable."""

    database = FakeDatabase()
    payload_store = RecordingPayloadStore()
    adapter = RecordingFetcher()
    error = RuntimeError("resolution insert failed")
    timestamp_source = TimestampSource(
        _REQUEST_AT,
        _RESOLVED_AT,
    )
    historical_response = _historical_response()
    reuse_requests: list[Request] = []
    _install_persistence(
        monkeypatch,
        resolution_error=error,
    )
    _install_reuse(
        monkeypatch,
        result=(
            historical_response,
            _PAYLOAD,
        ),
        requests=reuse_requests,
    )

    with pytest.raises(RuntimeError) as raised:
        _resolve(
            database=database,
            payload_store=payload_store,
            timestamp_source=timestamp_source,
            adapter=adapter,
        )

    assert raised.value is error
    assert database.transactions[1].rolled_back
    assert database.durable_operations == [
        f"request:{reuse_requests[0].id}",
    ]
    assert adapter.requests == []
    assert payload_store.puts == []
