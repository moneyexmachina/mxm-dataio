"""Unit tests for plain-SQL DataIO Resolution persistence operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal, Self, cast

import pytest
from psycopg import Connection, sql

from mxm.dataio.models import Resolution, ResolutionKind
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.resolutions import (
    ResolutionConflictError,
    ResolutionPersistenceError,
    fetch_resolution_by_request_id,
    fetch_resolutions_by_response_id,
    insert_resolution,
)
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
            cursor.bind_executions(
                self.executions,
            )

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

_RESOLVED_AT = ts_ns_from_str("2026-09-03T10:30:00.123456000Z")


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
# Resolution fixtures
# ---------------------------------------------------------------------------


def _resolution(
    *,
    request_id: str = "request-1",
    response_id: str = "response-1",
    kind: ResolutionKind = ResolutionKind.ACQUIRED,
    resolved_at: TSNSScalar = _RESOLVED_AT,
) -> Resolution:
    """Construct one representative immutable Resolution."""

    return Resolution(
        request_id=request_id,
        response_id=response_id,
        kind=kind,
        resolved_at=resolved_at,
    )


def _resolution_row(
    resolution: Resolution,
) -> PostgresRow:
    """Encode one Resolution as a fake Psycopg PostgreSQL result row."""

    return (
        resolution.request_id,
        resolution.response_id,
        resolution.kind.value,
        _db_datetime(resolution.resolved_at),
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

    return _row(
        *values,
    )


def _query_text(
    query: ExecutableQuery,
) -> str:
    return " ".join(query.as_string().split())


def _single_execution(
    connection: FakeConnection,
) -> Execution:
    assert len(connection.executions) == 1
    return connection.executions[0]


# ---------------------------------------------------------------------------
# fetch_resolution_by_request_id
# ---------------------------------------------------------------------------


def test_fetch_resolution_by_request_id_returns_none_when_absent() -> None:
    """A Request with no final Resolution returns None."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    resolution = fetch_resolution_by_request_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    assert resolution is None
    assert connection.cursor_calls == 1


def test_fetch_resolution_by_request_id_reconstructs_resolution() -> None:
    """A persisted row reconstructs the complete Resolution."""

    expected = _resolution(
        request_id="request-2",
        response_id="response-1",
        kind=ResolutionKind.REUSED,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _resolution_row(
                        expected,
                    ),
                ]
            ),
        ]
    )

    resolution = fetch_resolution_by_request_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        request_id=expected.request_id,
    )

    assert resolution == expected


def test_fetch_resolution_by_request_id_converts_database_timestamp_to_mxm_timestamp() -> (
    None
):
    """Psycopg datetime reconstructs as the canonical MXM timestamp."""

    expected = _resolution()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _resolution_row(
                        expected,
                    ),
                ]
            ),
        ]
    )

    resolution = fetch_resolution_by_request_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        request_id=expected.request_id,
    )

    assert resolution is not None
    assert resolution.resolved_at == expected.resolved_at


def test_fetch_resolution_by_request_id_normalises_aware_database_timestamp_to_utc() -> (
    None
):
    """Equivalent aware database timestamps reconstruct to one instant."""

    expected = _resolution(
        resolved_at=ts_ns_from_str("2026-09-03T10:30:00.000000000Z"),
    )

    row = _resolution_row(
        expected,
    )

    utc_plus_one = timezone(
        timedelta(hours=1),
    )

    row = _row_with_value(
        row,
        3,
        datetime(
            2026,
            9,
            3,
            11,
            30,
            tzinfo=utc_plus_one,
        ),
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    row,
                ]
            ),
        ]
    )

    resolution = fetch_resolution_by_request_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        request_id=expected.request_id,
    )

    assert resolution is not None
    assert resolution.resolved_at == expected.resolved_at


def test_fetch_resolution_by_request_id_uses_schema_and_request_identity() -> None:
    """Resolution lookup uses the configured schema and Request ID."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    fetch_resolution_by_request_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        request_id="request-1",
    )

    execution = _single_execution(
        connection,
    )

    query_text = _query_text(
        execution.query,
    )

    assert '"dataio_test_abc"."resolutions"' in query_text
    assert '"public"."resolutions"' not in query_text
    assert "WHERE request_id = %s" in query_text
    assert execution.parameters == ("request-1",)


def test_fetch_resolution_by_request_id_rejects_invalid_identity_before_sql() -> None:
    """Invalid Request identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"request_id must be non-empty text",
    ):
        fetch_resolution_by_request_id(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            request_id="",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_resolution_by_request_id_rejects_multiple_rows() -> None:
    """One Request occurrence cannot have multiple persisted Resolutions."""

    resolution = _resolution()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _resolution_row(
                        resolution,
                    ),
                    _resolution_row(
                        resolution,
                    ),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"multiple rows.*request-1",
    ):
        fetch_resolution_by_request_id(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            request_id=resolution.request_id,
        )


_RESOLUTION_ROW = _resolution_row(
    _resolution(),
)


@pytest.mark.parametrize(
    (
        "row",
        "error_match",
    ),
    [
        (
            _row(
                *_RESOLUTION_ROW[:-1],
            ),
            r"unexpected row shape",
        ),
        (
            _row(
                *_RESOLUTION_ROW,
                "extra",
            ),
            r"unexpected row shape",
        ),
        (
            _row_with_value(
                _RESOLUTION_ROW,
                0,
                123,
            ),
            r"request_id must be non-empty text",
        ),
        (
            _row_with_value(
                _RESOLUTION_ROW,
                0,
                "",
            ),
            r"request_id must be non-empty text",
        ),
        (
            _row_with_value(
                _RESOLUTION_ROW,
                1,
                123,
            ),
            r"response_id must be non-empty text",
        ),
        (
            _row_with_value(
                _RESOLUTION_ROW,
                1,
                "",
            ),
            r"response_id must be non-empty text",
        ),
        (
            _row_with_value(
                _RESOLUTION_ROW,
                2,
                "invented",
            ),
            r"kind is not recognised",
        ),
        (
            _row_with_value(
                _RESOLUTION_ROW,
                3,
                "2026-09-03T10:30:00Z",
            ),
            r"resolved_at must be a datetime",
        ),
        (
            _row_with_value(
                _RESOLUTION_ROW,
                3,
                datetime(
                    2026,
                    9,
                    3,
                    10,
                    30,
                ),
            ),
            r"resolved_at must be timezone-aware",
        ),
    ],
)
def test_fetch_resolution_by_request_id_rejects_invalid_rows(
    row: PostgresRow,
    error_match: str,
) -> None:
    """Malformed persisted Resolution rows cannot enter the model."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    row,
                ]
            ),
        ]
    )

    with pytest.raises(
        ResolutionPersistenceError,
        match=error_match,
    ):
        fetch_resolution_by_request_id(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            request_id="request-1",
        )


# ---------------------------------------------------------------------------
# fetch_resolutions_by_response_id
# ---------------------------------------------------------------------------


def test_fetch_resolutions_by_response_id_returns_empty_mapping() -> None:
    """An unused Response observation has no Resolutions."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    resolutions = fetch_resolutions_by_response_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        response_id="response-1",
    )

    assert resolutions == {}


def test_fetch_resolutions_by_response_id_preserves_multiple_requests() -> None:
    """One Response may satisfy multiple distinct Request occurrences."""

    acquired = _resolution(
        request_id="request-1",
        response_id="response-1",
        kind=ResolutionKind.ACQUIRED,
        resolved_at=ts_ns_from_str("2026-09-03T10:00:00.000000000Z"),
    )

    reused = _resolution(
        request_id="request-2",
        response_id="response-1",
        kind=ResolutionKind.REUSED,
        resolved_at=ts_ns_from_str("2026-09-03T10:05:00.000000000Z"),
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _resolution_row(
                        acquired,
                    ),
                    _resolution_row(
                        reused,
                    ),
                ]
            ),
        ]
    )

    resolutions = fetch_resolutions_by_response_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        response_id="response-1",
    )

    assert resolutions == {
        acquired.request_id: acquired,
        reused.request_id: reused,
    }


def test_fetch_resolutions_by_response_id_uses_filter_and_ordering() -> None:
    """Response lookup uses Response identity and deterministic ordering."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    fetch_resolutions_by_response_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        response_id="response-1",
    )

    execution = _single_execution(
        connection,
    )

    query_text = _query_text(
        execution.query,
    )

    assert '"dataio_test_abc"."resolutions"' in query_text
    assert "WHERE response_id = %s" in query_text
    assert "ORDER BY resolved_at, request_id" in query_text
    assert execution.parameters == ("response-1",)


def test_fetch_resolutions_by_response_id_rejects_invalid_identity_before_sql() -> None:
    """Invalid Response identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"response_id must be non-empty text",
    ):
        fetch_resolutions_by_response_id(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            response_id="",
        )

    assert connection.cursor_calls == 0


def test_fetch_resolutions_by_response_id_rejects_duplicate_request_identity() -> None:
    """A result set cannot contain one Request Resolution twice."""

    resolution = _resolution()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _resolution_row(
                        resolution,
                    ),
                    _resolution_row(
                        resolution,
                    ),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"duplicate Request occurrence ID.*request-1",
    ):
        fetch_resolutions_by_response_id(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            response_id=resolution.response_id,
        )


# ---------------------------------------------------------------------------
# insert_resolution: acquired semantics
# ---------------------------------------------------------------------------


def test_insert_acquired_resolution_encodes_complete_state() -> None:
    """ACQUIRED references a Response acquired by the same Request."""

    resolution = _resolution(
        request_id="request-1",
        response_id="response-1",
        kind=ResolutionKind.ACQUIRED,
    )

    connection = FakeConnection(
        [
            # Response relationship validation.
            FakeCursor(
                rows=[
                    ("request-1",),
                ]
            ),
            # INSERT.
            FakeCursor(),
            # Fetch persisted Resolution.
            FakeCursor(
                rows=[
                    _resolution_row(
                        resolution,
                    ),
                ]
            ),
        ]
    )

    insert_resolution(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        resolution=resolution,
    )

    assert len(connection.executions) == 3

    response_lookup = connection.executions[0]
    response_lookup_text = _query_text(
        response_lookup.query,
    )

    assert '"dataio_test_abc"."responses"' in response_lookup_text
    assert "SELECT request_id" in response_lookup_text
    assert "WHERE id = %s" in response_lookup_text
    assert response_lookup.parameters == (resolution.response_id,)

    insert_execution = connection.executions[1]
    insert_query = _query_text(
        insert_execution.query,
    )

    assert '"dataio_test_abc"."resolutions"' in insert_query
    assert "ON CONFLICT (request_id) DO NOTHING" in insert_query

    assert insert_execution.parameters == (
        resolution.request_id,
        resolution.response_id,
        resolution.kind.value,
        _db_datetime(
            resolution.resolved_at,
        ),
    )


def test_insert_acquired_resolution_rejects_response_from_different_request() -> None:
    """ACQUIRED cannot claim another Request occurrence's Response."""

    resolution = _resolution(
        request_id="request-2",
        response_id="response-1",
        kind=ResolutionKind.ACQUIRED,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    ("request-1",),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"Acquired Resolution must reference a Response acquired by the same",
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=resolution,
        )

    assert connection.cursor_calls == 1
    assert len(connection.executions) == 1


# ---------------------------------------------------------------------------
# insert_resolution: reused semantics
# ---------------------------------------------------------------------------


def test_insert_reused_resolution_accepts_existing_external_observation() -> None:
    """REUSED may satisfy Q2 from a Response acquired by Q1."""

    resolution = _resolution(
        request_id="request-2",
        response_id="response-1",
        kind=ResolutionKind.REUSED,
    )

    connection = FakeConnection(
        [
            # response-1 was acquired by request-1.
            FakeCursor(
                rows=[
                    ("request-1",),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[
                    _resolution_row(
                        resolution,
                    ),
                ]
            ),
        ]
    )

    insert_resolution(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        resolution=resolution,
    )

    assert len(connection.executions) == 3

    insert_execution = connection.executions[1]

    assert insert_execution.parameters == (
        "request-2",
        "response-1",
        ResolutionKind.REUSED.value,
        _db_datetime(
            resolution.resolved_at,
        ),
    )


def test_insert_reused_resolution_rejects_response_from_same_request() -> None:
    """REUSED cannot describe a Response acquired by the current Request."""

    resolution = _resolution(
        request_id="request-1",
        response_id="response-1",
        kind=ResolutionKind.REUSED,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    ("request-1",),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"Reused Resolution must reference a Response acquired by a different",
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=resolution,
        )

    assert connection.cursor_calls == 1


# ---------------------------------------------------------------------------
# insert_resolution: referenced Response validation
# ---------------------------------------------------------------------------


def test_insert_resolution_rejects_missing_response() -> None:
    """A Resolution cannot reference a Response that is not persisted."""

    resolution = _resolution()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"Response that is not persisted.*response-1",
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=resolution,
        )

    assert connection.cursor_calls == 1


@pytest.mark.parametrize(
    "rows",
    [
        [
            ("request-1",),
            ("request-1",),
        ],
        [
            (
                "request-1",
                "extra",
            ),
        ],
        [
            (123,),
        ],
        [
            ("",),
        ],
    ],
)
def test_insert_resolution_rejects_invalid_response_lookup_result(
    rows: list[PostgresRow],
) -> None:
    """Malformed Response acquisition provenance is rejected."""

    resolution = _resolution()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=rows,
            ),
        ]
    )

    with pytest.raises(
        ResolutionPersistenceError,
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=resolution,
        )

    assert connection.cursor_calls == 1


# ---------------------------------------------------------------------------
# insert_resolution: persisted-state verification
# ---------------------------------------------------------------------------


def test_insert_resolution_accepts_matching_persisted_state() -> None:
    """An identical existing Resolution is an idempotent replay."""

    resolution = _resolution()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    (resolution.request_id,),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[
                    _resolution_row(
                        resolution,
                    ),
                ]
            ),
        ]
    )

    insert_resolution(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        resolution=resolution,
    )

    assert len(connection.executions) == 3

    assert "SELECT request_id" in _query_text(
        connection.executions[0].query,
    )

    assert "INSERT INTO" in _query_text(
        connection.executions[1].query,
    )

    assert "SELECT" in _query_text(
        connection.executions[2].query,
    )


def test_insert_resolution_rejects_conflicting_persisted_state() -> None:
    """One Request occurrence cannot have two different final Resolutions."""

    requested = _resolution(
        response_id="response-1",
    )

    persisted = replace(
        requested,
        response_id="response-2",
    )

    connection = FakeConnection(
        [
            # Requested response-1 is structurally valid for request-1.
            FakeCursor(
                rows=[
                    ("request-1",),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[
                    _resolution_row(
                        persisted,
                    ),
                ]
            ),
        ]
    )

    with pytest.raises(
        ResolutionConflictError,
        match=r"Persisted Resolution conflicts.*request-1",
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=requested,
        )


def test_insert_resolution_rejects_missing_state_after_insert() -> None:
    """The requested final Resolution must exist after insertion."""

    resolution = _resolution()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    (resolution.request_id,),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[],
            ),
        ]
    )

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"not present after insertion.*request-1",
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=resolution,
        )


# ---------------------------------------------------------------------------
# insert_resolution: caller-state validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "resolution",
        "error_match",
    ),
    [
        (
            _resolution(
                request_id="",
            ),
            r"request_id must be non-empty text",
        ),
        (
            _resolution(
                response_id="",
            ),
            r"response_id must be non-empty text",
        ),
    ],
)
def test_insert_resolution_rejects_invalid_state_before_sql(
    resolution: Resolution,
    error_match: str,
) -> None:
    """Invalid persistence invariants fail before database access."""

    connection = FakeConnection()

    with pytest.raises(
        ResolutionPersistenceError,
        match=error_match,
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=resolution,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_insert_resolution_rejects_noncanonical_timestamp_before_sql() -> None:
    """Resolution timestamps must use the canonical MXM representation."""

    invalid_timestamp = cast(
        TSNSScalar,
        "2026-09-03T10:30:00Z",
    )

    resolution = _resolution(
        resolved_at=invalid_timestamp,
    )

    connection = FakeConnection()

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"resolved_at must be a valid canonical MXM timestamp",
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=resolution,
        )

    assert connection.cursor_calls == 0


def test_insert_resolution_rejects_non_microsecond_aligned_timestamp() -> None:
    """Persistence never silently truncates Resolution nanoseconds."""

    invalid_timestamp = ts_ns_from_int(
        ts_ns_to_int(_RESOLVED_AT) + 1,
    )

    resolution = _resolution(
        resolved_at=invalid_timestamp,
    )

    connection = FakeConnection()

    with pytest.raises(
        ResolutionPersistenceError,
        match=r"resolved_at cannot be represented exactly.*microsecond precision",
    ):
        insert_resolution(
            _as_connection(
                connection,
            ),
            schema="dataio_test_abc",
            resolution=resolution,
        )

    assert connection.cursor_calls == 0


# ---------------------------------------------------------------------------
# Transaction ownership
# ---------------------------------------------------------------------------


def test_resolution_operations_do_not_control_transactions() -> None:
    """Resolution SQL helpers neither commit nor roll back transactions."""

    resolution = _resolution()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    (resolution.request_id,),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[
                    _resolution_row(
                        resolution,
                    ),
                ]
            ),
        ]
    )

    insert_resolution(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        resolution=resolution,
    )

    assert connection.commit_calls == 0
    assert connection.rollback_calls == 0
