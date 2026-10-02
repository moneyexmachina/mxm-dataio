"""Unit tests for plain-SQL DataIO Request persistence operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal, Self, cast

import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from mxm.dataio.models import CacheMode, Request
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.requests import (
    RequestConflictError,
    RequestPersistenceError,
    fetch_request_by_id,
    fetch_requests_by_hash,
    insert_request,
)
from mxm.types import JSONLike, JSONObj
from mxm.types.timestamps import (
    TSNSScalar,
    ts_ns_from_int,
    ts_ns_from_str,
    ts_ns_to_int,
)

type ExecutableQuery = sql.SQL | sql.Composed


# ---------------------------------------------------------------------------
# Fake PostgreSQL boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Execution:
    """One SQL operation issued through a fake cursor."""

    operation: Literal["execute"]
    query: ExecutableQuery
    parameters: object | None


class FakeCursor:
    """Scripted cursor recording SQL operations and returning fixed rows."""

    def __init__(
        self,
        *,
        rows: list[PostgresRow] | None = None,
    ) -> None:
        self._rows = list(rows or [])
        self._executions: list[Execution] = []
        self.fetchall_calls = 0

    def bind_executions(
        self,
        executions: list[Execution],
    ) -> None:
        """Bind this cursor to its connection's execution log."""

        self._executions = executions

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        _ = exc_type, exc_value, traceback
        return None

    def execute(
        self,
        query: ExecutableQuery,
        parameters: object | None = None,
    ) -> None:
        """Record one execute operation."""

        self._executions.append(
            Execution(
                operation="execute",
                query=query,
                parameters=parameters,
            )
        )

    def fetchall(self) -> list[PostgresRow]:
        """Return the scripted result rows."""

        self.fetchall_calls += 1
        return list(self._rows)


class FakeConnection:
    """Connection returning scripted cursors in invocation order."""

    def __init__(
        self,
        cursors: list[FakeCursor] | None = None,
    ) -> None:
        self._cursors = list(cursors or [])
        self.executions: list[Execution] = []
        self.cursor_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

        for cursor in self._cursors:
            cursor.bind_executions(self.executions)

    def cursor(self) -> FakeCursor:
        self.cursor_calls += 1

        if not self._cursors:
            raise AssertionError(
                "Unexpected cursor request: no scripted cursor remains"
            )

        return self._cursors.pop(0)

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1


def _as_connection(
    connection: FakeConnection,
) -> Connection[PostgresRow]:
    return cast(
        Connection[PostgresRow],
        connection,
    )


# ---------------------------------------------------------------------------
# Timestamp fixtures and boundary helpers
# ---------------------------------------------------------------------------


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_CREATED_AT = ts_ns_from_str("2026-09-03T10:00:00.123456000Z")


def _db_datetime(
    value: TSNSScalar,
) -> datetime:
    """Encode one microsecond-aligned MXM timestamp as fake Psycopg output."""

    nanoseconds = ts_ns_to_int(value)

    assert nanoseconds % 1_000 == 0

    return _UNIX_EPOCH + timedelta(
        microseconds=nanoseconds // 1_000,
    )


# ---------------------------------------------------------------------------
# Request fixtures
# ---------------------------------------------------------------------------


def _request(
    *,
    request_id: str = "request-1",
    source: str = "source-a",
    kind: str = "prices",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    params: JSONObj | None = None,
    ttl_seconds: float | None = 60.0,
    as_of_bucket: str | None = "bucket-a",
    cache_tag: str | None = "vendor-v1",
    created_at: TSNSScalar = _CREATED_AT,
) -> Request:
    """Construct one representative immutable Request occurrence."""

    return Request(
        id=request_id,
        source=source,
        kind=kind,
        cache_mode=cache_mode,
        params=params,
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        created_at=created_at,
    )


def _request_row(
    request: Request,
) -> PostgresRow:
    """Encode one Request as a fake Psycopg PostgreSQL result row."""

    return (
        request.id,
        request.source,
        request.kind,
        request.params,
        request.cache_mode.value,
        request.ttl_seconds,
        request.as_of_bucket,
        request.cache_tag,
        request.hash,
        _db_datetime(request.created_at),
    )


# ---------------------------------------------------------------------------
# PostgreSQL row and execution helpers
# ---------------------------------------------------------------------------


def _row(
    *values: object,
) -> PostgresRow:
    return tuple(values)


def _row_with_value(
    row: PostgresRow,
    index: int,
    value: object,
) -> PostgresRow:
    values = list(row)

    if index < 0 or index >= len(values):
        raise AssertionError(
            "Test row replacement index is out of range: "
            f"index={index}, row_length={len(values)}"
        )

    values[index] = value

    return _row(*values)


def _query_text(
    query: ExecutableQuery,
) -> str:
    return " ".join(query.as_string().split())


def _single_execution(
    connection: FakeConnection,
) -> Execution:
    assert len(connection.executions) == 1
    return connection.executions[0]


def _execution_parameters(
    execution: Execution,
) -> tuple[object, ...]:
    parameters = execution.parameters

    if not isinstance(parameters, tuple):
        raise AssertionError(f"Expected tuple execution parameters, got {parameters!r}")

    return cast(
        tuple[object, ...],
        parameters,
    )


# ---------------------------------------------------------------------------
# Logical-question identity
# ---------------------------------------------------------------------------


def test_request_occurrences_can_share_question_identity() -> None:
    """Different occurrences may represent exactly the same logical question."""

    first = _request(
        request_id="request-1",
        source="source-a",
        params={
            "symbol": "ES",
        },
    )

    second = _request(
        request_id="request-2",
        source="source-b",
        params={
            "symbol": "ES",
        },
    )

    assert first.id != second.id
    assert first.source != second.source
    assert first.hash == second.hash


@pytest.mark.parametrize(
    "second",
    [
        _request(
            request_id="request-2",
            source="different-source",
        ),
        _request(
            request_id="request-2",
            cache_mode=CacheMode.ONLY_IF_CACHED,
            ttl_seconds=3_600.0,
            as_of_bucket="bucket-b",
            cache_tag="vendor-v2",
        ),
        _request(
            request_id="request-2",
            created_at=ts_ns_from_str("2026-09-03T11:00:00.000000000Z"),
        ),
    ],
)
def test_occurrence_and_resolution_context_does_not_change_question_hash(
    second: Request,
) -> None:
    """Occurrence and resolution context do not participate in question identity."""

    first = _request(
        request_id="request-1",
    )

    assert first.hash == second.hash


@pytest.mark.parametrize(
    "second",
    [
        _request(
            request_id="request-2",
            kind="settlements",
        ),
        _request(
            request_id="request-2",
            params={
                "symbol": "DIFFERENT",
            },
        ),
    ],
)
def test_question_fields_change_request_hash(
    second: Request,
) -> None:
    """Changing one logical question-bearing field changes identity."""

    first = _request(
        request_id="request-1",
    )

    assert first.hash != second.hash


# ---------------------------------------------------------------------------
# fetch_request_by_id
# ---------------------------------------------------------------------------


def test_fetch_request_by_id_returns_none_when_absent() -> None:
    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    request = fetch_request_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    assert request is None
    assert connection.cursor_calls == 1


def test_fetch_request_by_id_reconstructs_request() -> None:
    """A persisted row reconstructs the complete Request object."""

    expected = _request(
        params={
            "symbol": "TEST",
            "limit": 10,
            "filters": {
                "venue": "CME",
            },
        },
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(expected),
                ]
            ),
        ]
    )

    request = fetch_request_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id=expected.id,
    )

    assert request == expected


def test_fetch_request_by_id_converts_database_timestamp_to_mxm_timestamp() -> None:
    expected = _request()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(expected),
                ]
            ),
        ]
    )

    request = fetch_request_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id=expected.id,
    )

    assert request is not None
    assert request.created_at == expected.created_at


def test_fetch_request_by_id_normalises_aware_database_timestamp_to_utc() -> None:
    """Equivalent aware database timestamps reconstruct to one canonical instant."""

    expected = _request(created_at=ts_ns_from_str("2026-09-03T09:00:00.000000000Z"))

    row = _request_row(expected)

    utc_plus_one = timezone(
        timedelta(hours=1),
    )

    row = _row_with_value(
        row,
        9,
        datetime(
            2026,
            9,
            3,
            10,
            0,
            tzinfo=utc_plus_one,
        ),
    )

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    request = fetch_request_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id=expected.id,
    )

    assert request is not None
    assert request.created_at == expected.created_at


def test_fetch_request_by_id_uses_configured_schema_and_identity() -> None:
    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_request_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."requests"' in query_text
    assert '"public"."requests"' not in query_text
    assert "WHERE id = %s" in query_text
    assert execution.parameters == ("request-1",)


def test_fetch_request_by_id_rejects_invalid_identifier_before_sql() -> None:
    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"request_id must be non-empty text",
    ):
        fetch_request_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id="",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_request_by_id_rejects_multiple_database_rows() -> None:
    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(request),
                    _request_row(request),
                ]
            ),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"multiple rows.*request-1",
    ):
        fetch_request_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id=request.id,
        )


_REQUEST_ROW = _request_row(_request())


@pytest.mark.parametrize(
    (
        "row",
        "error_match",
    ),
    [
        (
            _row(*_REQUEST_ROW[:-1]),
            r"unexpected row shape",
        ),
        (
            _row(*_REQUEST_ROW, "extra"),
            r"unexpected row shape",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                0,
                123,
            ),
            r"id must be non-empty text",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                1,
                123,
            ),
            r"source must be non-empty text",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                2,
                "",
            ),
            r"kind must be non-empty text",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                3,
                ["not-an-object"],
            ),
            r"params must be a JSON object",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                4,
                "future-mode",
            ),
            r"cache_mode is not recognised",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                5,
                -1.0,
            ),
            r"ttl_seconds must be a finite non-negative number or NULL",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                6,
                123,
            ),
            r"as_of_bucket must be text or NULL",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                7,
                123,
            ),
            r"cache_tag must be text or NULL",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                8,
                "not-a-hash",
            ),
            r"64-character lowercase hexadecimal",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                9,
                "2026-09-03T10:00:00Z",
            ),
            r"created_at must be a datetime",
        ),
        (
            _row_with_value(
                _REQUEST_ROW,
                9,
                datetime(
                    2026,
                    9,
                    3,
                    10,
                    0,
                ),
            ),
            r"created_at must be timezone-aware",
        ),
    ],
)
def test_fetch_request_by_id_rejects_invalid_rows(
    row: PostgresRow,
    error_match: str,
) -> None:
    """Malformed persisted Request rows cannot enter the model."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=error_match,
    ):
        fetch_request_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id="request-1",
        )


def test_fetch_request_by_id_rejects_hash_inconsistent_with_question() -> None:
    """Persisted hash must agree with kind + params."""

    row = _row_with_value(
        _REQUEST_ROW,
        8,
        "0" * 64,
    )

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"hash is inconsistent with logical-question identity",
    ):
        fetch_request_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_id="request-1",
        )


# ---------------------------------------------------------------------------
# fetch_requests_by_hash
# ---------------------------------------------------------------------------


def test_fetch_requests_by_hash_returns_empty_mapping() -> None:
    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    requests = fetch_requests_by_hash(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_hash=request.hash,
    )

    assert requests == {}


def test_fetch_requests_by_hash_preserves_occurrences_across_contexts() -> None:
    first = _request(
        request_id="request-1",
        source="source-a",
        params={
            "symbol": "ES",
        },
        created_at=ts_ns_from_str("2026-09-03T10:00:00.000000000Z"),
    )

    second = _request(
        request_id="request-2",
        source="source-b",
        params={
            "symbol": "ES",
        },
        created_at=ts_ns_from_str("2026-09-03T11:00:00.000000000Z"),
    )

    assert first.hash == second.hash
    assert first.id != second.id
    assert first.source != second.source

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(first),
                    _request_row(second),
                ]
            ),
        ]
    )

    requests = fetch_requests_by_hash(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_hash=first.hash,
    )

    assert requests == {
        first.id: first,
        second.id: second,
    }


def test_fetch_requests_by_hash_uses_hash_filter_and_deterministic_ordering() -> None:
    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_requests_by_hash(
        _as_connection(connection),
        schema="dataio_test_abc",
        request_hash=request.hash,
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."requests"' in query_text
    assert "WHERE hash = %s" in query_text
    assert "ORDER BY created_at, id" in query_text
    assert execution.parameters == (request.hash,)


def test_fetch_requests_by_hash_rejects_invalid_hash_before_sql() -> None:
    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"64-character lowercase hexadecimal",
    ):
        fetch_requests_by_hash(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_hash="not-a-hash",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_requests_by_hash_rejects_duplicate_occurrence_identity() -> None:
    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _request_row(request),
                    _request_row(request),
                ]
            ),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"duplicate Request occurrence ID.*request-1",
    ):
        fetch_requests_by_hash(
            _as_connection(connection),
            schema="dataio_test_abc",
            request_hash=request.hash,
        )


# ---------------------------------------------------------------------------
# insert_request: encoding
# ---------------------------------------------------------------------------


def test_insert_request_encodes_complete_request_state() -> None:
    request = _request(
        source="source-complete",
        cache_mode=CacheMode.ONLY_IF_CACHED,
        params={
            "symbol": "TEST",
            "limit": 10,
            "filters": {
                "venue": "CME",
            },
        },
        ttl_seconds=120.5,
        as_of_bucket="bucket-complete",
        cache_tag="vendor-v2",
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    execution = connection.executions[0]
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."requests"' in query_text
    assert '"public"."requests"' not in query_text
    assert "ON CONFLICT (id) DO NOTHING" in query_text

    parameters = _execution_parameters(execution)

    assert len(parameters) == 10

    assert parameters[0] == request.id
    assert parameters[1] == request.source
    assert parameters[2] == request.kind

    params_json = parameters[3]

    assert isinstance(
        params_json,
        Jsonb,
    )
    assert params_json.obj == request.params

    assert parameters[4] == request.cache_mode.value
    assert parameters[5] == request.ttl_seconds
    assert parameters[6] == request.as_of_bucket
    assert parameters[7] == request.cache_tag
    assert parameters[8] == request.hash
    assert parameters[9] == _db_datetime(request.created_at)


def test_insert_request_encodes_absent_params_as_null() -> None:
    request = _request(
        params=None,
        ttl_seconds=None,
        as_of_bucket=None,
        cache_tag=None,
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    parameters = _execution_parameters(connection.executions[0])

    assert parameters[3] is None
    assert parameters[5:8] == (None, None, None)


def test_insert_request_preserves_zero_ttl_and_empty_partition_text() -> None:
    """Former Session fields retain their existing permissive boundaries."""

    request = _request(
        ttl_seconds=0.0,
        as_of_bucket="",
        cache_tag="",
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    parameters = _execution_parameters(connection.executions[0])

    assert parameters[5:8] == (0.0, "", "")


def test_insert_request_uses_occurrence_identity_conflict_clause() -> None:
    """Persistence conflicts on Q identity, not logical-question hash."""

    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    query_text = _query_text(connection.executions[0].query)

    assert "ON CONFLICT (id) DO NOTHING" in query_text
    assert "ON CONFLICT (hash)" not in query_text


# ---------------------------------------------------------------------------
# insert_request: persisted-state verification
# ---------------------------------------------------------------------------


def test_insert_request_accepts_matching_persisted_state() -> None:
    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    assert len(connection.executions) == 2
    assert "INSERT INTO" in _query_text(connection.executions[0].query)
    assert "SELECT" in _query_text(connection.executions[1].query)


@pytest.mark.parametrize(
    "persisted",
    [
        _request(
            source="source-b",
        ),
        _request(
            cache_mode=CacheMode.BYPASS,
        ),
        _request(
            ttl_seconds=120.0,
        ),
        _request(
            as_of_bucket="bucket-b",
        ),
        _request(
            cache_tag="vendor-v2",
        ),
        _request(
            kind="different-kind",
        ),
    ],
)
def test_insert_request_rejects_conflicting_persisted_occurrence(
    persisted: Request,
) -> None:
    requested = _request()

    assert persisted.id == requested.id

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(persisted),
                ]
            ),
        ]
    )

    with pytest.raises(
        RequestConflictError,
        match=r"Persisted Request conflicts.*request-1",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=requested,
        )


def test_insert_request_rejects_missing_state_after_insert() -> None:
    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(rows=[]),
        ]
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"not present after insertion.*request-1",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request,
        )


# ---------------------------------------------------------------------------
# insert_request: caller-state validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "request_occurrence",
        "error_match",
    ),
    [
        (
            _request(
                request_id="",
            ),
            r"id must be non-empty text",
        ),
        (
            _request(
                source="",
            ),
            r"source must be non-empty text",
        ),
        (
            _request(
                source=cast(str, 123),
            ),
            r"source must be non-empty text",
        ),
        (
            _request(
                kind="",
            ),
            r"kind must be non-empty text",
        ),
        (
            _request(
                ttl_seconds=cast(float, True),
            ),
            r"ttl_seconds must be a finite non-negative number or NULL",
        ),
        (
            _request(
                ttl_seconds=-1.0,
            ),
            r"ttl_seconds must be a finite non-negative number or NULL",
        ),
        (
            _request(
                ttl_seconds=float("inf"),
            ),
            r"ttl_seconds must be a finite non-negative number or NULL",
        ),
        (
            _request(
                ttl_seconds=float("nan"),
            ),
            r"ttl_seconds must be a finite non-negative number or NULL",
        ),
        (
            _request(
                as_of_bucket=cast(str, 123),
            ),
            r"as_of_bucket must be text or NULL",
        ),
        (
            _request(
                cache_tag=cast(str, 123),
            ),
            r"cache_tag must be text or NULL",
        ),
    ],
)
def test_insert_request_rejects_invalid_state_before_sql(
    request_occurrence: Request,
    error_match: str,
) -> None:
    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=error_match,
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request_occurrence,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_insert_request_rejects_noncanonical_timestamp_before_sql() -> None:
    connection = FakeConnection()

    invalid_timestamp = cast(
        TSNSScalar,
        "2026-09-03T10:00:00Z",
    )

    request = _request(
        created_at=invalid_timestamp,
    )

    with pytest.raises(
        RequestPersistenceError,
        match=r"created_at must be a valid canonical MXM timestamp",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request,
        )

    assert connection.cursor_calls == 0


def test_insert_request_rejects_non_microsecond_aligned_timestamp() -> None:
    """Persistence never silently truncates canonical nanosecond timestamps."""

    invalid_timestamp = ts_ns_from_int(
        ts_ns_to_int(_CREATED_AT) + 1,
    )

    request = _request(
        created_at=invalid_timestamp,
    )

    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"cannot be represented exactly.*microsecond precision",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request,
        )

    assert connection.cursor_calls == 0


def test_insert_request_rejects_nested_identity_mutation_before_sql() -> None:
    """Mutation inside question-bearing JSON is detected before persistence."""

    request = _request(
        params={
            "symbol": "TEST",
        }
    )

    assert request.params is not None

    mutable_params = cast(
        dict[str, JSONLike],
        request.params,
    )

    mutable_params["symbol"] = "DIFFERENT"

    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"hash is inconsistent with logical-question identity",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_insert_request_rejects_nested_non_json_params_before_sql() -> None:
    request = _request(
        params={
            "bad": "initially-valid",
        }
    )

    assert request.params is not None

    mutable_params = cast(
        dict[str, object],
        request.params,
    )

    mutable_params["bad"] = object()

    connection = FakeConnection()

    with pytest.raises(
        RequestPersistenceError,
        match=r"params\.bad must contain JSON-compatible values",
    ):
        insert_request(
            _as_connection(connection),
            schema="dataio_test_abc",
            request=request,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


# ---------------------------------------------------------------------------
# Transaction ownership
# ---------------------------------------------------------------------------


def test_request_operations_do_not_control_transactions() -> None:
    request = _request()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _request_row(request),
                ]
            ),
        ]
    )

    insert_request(
        _as_connection(connection),
        schema="dataio_test_abc",
        request=request,
    )

    assert connection.commit_calls == 0
    assert connection.rollback_calls == 0
