"""Unit tests for plain-SQL DataIO Session persistence operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal, Self, cast

import pytest
from psycopg import Connection, sql

from mxm.dataio.models import CacheMode, Session
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.sessions import (
    SessionConflictError,
    SessionPersistenceError,
    fetch_latest_session_id,
    fetch_session_by_id,
    insert_session,
    mark_session_ended,
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
        """Enter the fake cursor context."""

        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        _ = exc_type, exc_value, traceback
        """Exit the fake cursor context."""

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
        """Return the next scripted cursor."""

        self.cursor_calls += 1

        if not self._cursors:
            raise AssertionError(
                "Unexpected cursor request: no scripted cursor remains"
            )

        return self._cursors.pop(0)

    def commit(self) -> None:
        """Record an unexpected commit request."""

        self.commit_calls += 1

    def rollback(self) -> None:
        """Record an unexpected rollback request."""

        self.rollback_calls += 1


def _as_connection(
    connection: FakeConnection,
) -> Connection[PostgresRow]:
    """Cast a fake connection to the production connection type."""

    return cast(
        Connection[PostgresRow],
        connection,
    )


# ---------------------------------------------------------------------------
# Timestamp fixtures and boundary helpers
# ---------------------------------------------------------------------------


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_STARTED_AT = ts_ns_from_str("2026-09-03T09:00:00.000000000Z")
_ENDED_AT = ts_ns_from_str("2026-09-03T09:30:00.000000000Z")


def _db_datetime(
    value: TSNSScalar,
) -> datetime:
    """Encode a microsecond-aligned MXM timestamp as a fake Psycopg value."""

    nanoseconds = ts_ns_to_int(value)

    assert nanoseconds % 1_000 == 0

    return _UNIX_EPOCH + timedelta(
        microseconds=nanoseconds // 1_000,
    )


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------


def _session(
    *,
    session_id: str = "session-1",
    source: str = "example-source",
    cache_mode: CacheMode = CacheMode.DEFAULT,
    ttl_seconds: float | None = 300.0,
    as_of_bucket: str | None = None,
    cache_tag: str | None = None,
    started_at: TSNSScalar = _STARTED_AT,
    ended_at: TSNSScalar | None = None,
) -> Session:
    """Construct one representative DataIO Session."""

    return Session(
        id=session_id,
        source=source,
        cache_mode=cache_mode,
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        started_at=started_at,
        ended_at=ended_at,
    )


def _session_row(
    session: Session,
) -> PostgresRow:
    """Encode one Session as a fake Psycopg PostgreSQL result row."""

    return (
        session.id,
        session.source,
        session.cache_mode.value,
        session.ttl_seconds,
        session.as_of_bucket,
        session.cache_tag,
        _db_datetime(session.started_at),
        (_db_datetime(session.ended_at) if session.ended_at is not None else None),
    )


# ---------------------------------------------------------------------------
# PostgreSQL row and execution helpers
# ---------------------------------------------------------------------------


def _row(
    *values: object,
) -> PostgresRow:
    """Construct an arbitrary PostgreSQL result row."""

    return tuple(values)


def _row_with_value(
    row: PostgresRow,
    index: int,
    value: object,
) -> PostgresRow:
    """Return a PostgreSQL row with one value replaced."""

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
    """Render and normalise one composed SQL query."""

    return " ".join(query.as_string().split())


def _single_execution(
    connection: FakeConnection,
) -> Execution:
    """Return the sole recorded SQL operation."""

    assert len(connection.executions) == 1

    return connection.executions[0]


# ---------------------------------------------------------------------------
# fetch_session_by_id
# ---------------------------------------------------------------------------


def test_fetch_session_by_id_returns_none_when_absent() -> None:
    """A missing Session ID returns no persisted Session."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    session = fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id="session-1",
    )

    assert session is None
    assert connection.cursor_calls == 1


def test_fetch_session_by_id_reconstructs_complete_session() -> None:
    """A persisted row reconstructs lifecycle and cache context."""

    expected = _session(
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=600.0,
        as_of_bucket="2026-09-03",
        cache_tag="vendor-v2",
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(expected),
                ]
            ),
        ]
    )

    session = fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=expected.id,
    )

    assert session == expected


def test_fetch_session_by_id_reconstructs_absent_optional_context() -> None:
    """Nullable cache-context fields reconstruct as None."""

    expected = _session(
        ttl_seconds=None,
        as_of_bucket=None,
        cache_tag=None,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(expected),
                ]
            ),
        ]
    )

    session = fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=expected.id,
    )

    assert session == expected


def test_fetch_session_by_id_converts_database_timestamps_to_mxm_timestamp() -> None:
    """Psycopg datetime values reconstruct as canonical MXM timestamps."""

    expected = _session(
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(expected),
                ]
            ),
        ]
    )

    session = fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=expected.id,
    )

    assert session is not None
    assert session.started_at == _STARTED_AT
    assert session.ended_at == _ENDED_AT


def test_fetch_session_by_id_normalises_aware_database_timestamp_to_utc() -> None:
    """Equivalent aware database timestamps reconstruct to one UTC instant."""

    expected = _session()

    row = _session_row(expected)

    utc_plus_one = timezone(
        timedelta(hours=1),
    )

    row = _row_with_value(
        row,
        6,
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

    session = fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=expected.id,
    )

    assert session is not None
    assert session.started_at == _STARTED_AT


def test_fetch_session_by_id_uses_configured_schema_and_identity() -> None:
    """Session lookup uses the caller-provided schema and requested ID."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_session_by_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id="session-1",
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."sessions"' in query_text
    assert '"public"."sessions"' not in query_text
    assert "WHERE id = %s" in query_text
    assert execution.parameters == ("session-1",)


def test_fetch_session_by_id_rejects_invalid_identifier_before_sql() -> None:
    """Invalid requested Session identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=r"session_id must be non-empty text",
    ):
        fetch_session_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="",
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_session_by_id_rejects_multiple_database_rows() -> None:
    """One Session ID cannot reconstruct from multiple persisted rows."""

    session = _session()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(session),
                    _session_row(session),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"multiple rows.*session-1",
    ):
        fetch_session_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id=session.id,
        )


_SESSION_ROW = _session_row(_session())


@pytest.mark.parametrize(
    (
        "row",
        "error_match",
    ),
    [
        (
            _row(*_SESSION_ROW[:-1]),
            r"unexpected row shape",
        ),
        (
            _row(*_SESSION_ROW, "extra"),
            r"unexpected row shape",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                0,
                123,
            ),
            r"id must be non-empty text",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                0,
                "",
            ),
            r"id must be non-empty text",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                1,
                123,
            ),
            r"source must be non-empty text",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                1,
                "",
            ),
            r"source must be non-empty text",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                2,
                "unknown-cache-mode",
            ),
            r"cache_mode is not recognised",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                3,
                -1.0,
            ),
            r"ttl_seconds must be a finite non-negative number",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                3,
                float("inf"),
            ),
            r"ttl_seconds must be a finite non-negative number",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                3,
                True,
            ),
            r"ttl_seconds must be a finite non-negative number",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                4,
                123,
            ),
            r"as_of_bucket must be text or NULL",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                5,
                123,
            ),
            r"cache_tag must be text or NULL",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                6,
                "2026-09-03T09:00:00Z",
            ),
            r"started_at must be a datetime",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                6,
                datetime(
                    2026,
                    9,
                    3,
                    9,
                    0,
                ),
            ),
            r"started_at must be timezone-aware",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                7,
                "2026-09-03T09:30:00Z",
            ),
            r"ended_at must be a datetime",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                7,
                datetime(
                    2026,
                    9,
                    3,
                    9,
                    30,
                ),
            ),
            r"ended_at must be timezone-aware",
        ),
        (
            _row_with_value(
                _SESSION_ROW,
                7,
                datetime(
                    2026,
                    9,
                    3,
                    8,
                    59,
                    tzinfo=UTC,
                ),
            ),
            r"ended_at must not be before started_at",
        ),
    ],
)
def test_fetch_session_by_id_rejects_invalid_rows(
    row: PostgresRow,
    error_match: str,
) -> None:
    """Malformed persisted Session rows cannot enter the model."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[row]),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=error_match,
    ):
        fetch_session_by_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="session-1",
        )


# ---------------------------------------------------------------------------
# fetch_latest_session_id
# ---------------------------------------------------------------------------


def test_fetch_latest_session_id_returns_none_when_source_has_no_sessions() -> None:
    """A source with no persisted Sessions has no latest Session ID."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    session_id = fetch_latest_session_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        source="example-source",
    )

    assert session_id is None


def test_fetch_latest_session_id_returns_persisted_identity() -> None:
    """The latest-Session query returns its persisted Session ID."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    ("session-latest",),
                ]
            ),
        ]
    )

    session_id = fetch_latest_session_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        source="example-source",
    )

    assert session_id == "session-latest"


def test_fetch_latest_session_id_uses_source_and_deterministic_ordering() -> None:
    """Latest-Session lookup is source-specific with deterministic ties."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    fetch_latest_session_id(
        _as_connection(connection),
        schema="dataio_test_abc",
        source="example-source",
    )

    execution = _single_execution(connection)
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."sessions"' in query_text
    assert "WHERE source = %s" in query_text
    assert "ORDER BY started_at DESC, id DESC" in query_text
    assert "LIMIT 1" in query_text
    assert execution.parameters == ("example-source",)


def test_fetch_latest_session_id_rejects_invalid_source_before_sql() -> None:
    """Invalid source identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=r"source must be non-empty text",
    ):
        fetch_latest_session_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            source="",
        )

    assert connection.cursor_calls == 0


@pytest.mark.parametrize(
    "rows",
    [
        [
            ("session-a",),
            ("session-b",),
        ],
        [
            ("session-a", "extra"),
        ],
        [
            (123,),
        ],
        [
            ("",),
        ],
    ],
)
def test_fetch_latest_session_id_rejects_invalid_result(
    rows: list[PostgresRow],
) -> None:
    """Malformed latest-Session query results are rejected."""

    connection = FakeConnection(
        [
            FakeCursor(rows=rows),
        ]
    )

    with pytest.raises(SessionPersistenceError):
        fetch_latest_session_id(
            _as_connection(connection),
            schema="dataio_test_abc",
            source="example-source",
        )


# ---------------------------------------------------------------------------
# insert_session
# ---------------------------------------------------------------------------


def test_insert_session_encodes_complete_session_state() -> None:
    """One Session is encoded with lifecycle and cache context."""

    session = _session(
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=600.0,
        as_of_bucket="2026-09-03",
        cache_tag="vendor-v2",
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(session),
                ]
            ),
        ]
    )

    insert_session(
        _as_connection(connection),
        schema="dataio_test_abc",
        session=session,
    )

    execution = connection.executions[0]
    query_text = _query_text(execution.query)

    assert '"dataio_test_abc"."sessions"' in query_text
    assert "ON CONFLICT (id) DO NOTHING" in query_text

    assert execution.parameters == (
        session.id,
        session.source,
        session.cache_mode.value,
        session.ttl_seconds,
        session.as_of_bucket,
        session.cache_tag,
        _db_datetime(session.started_at),
        (_db_datetime(session.ended_at) if session.ended_at is not None else None),
    )


def test_insert_session_accepts_matching_persisted_state() -> None:
    """An identical existing Session is an idempotent replay."""

    session = _session()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(session),
                ]
            ),
        ]
    )

    insert_session(
        _as_connection(connection),
        schema="dataio_test_abc",
        session=session,
    )

    assert len(connection.executions) == 2
    assert "INSERT INTO" in _query_text(connection.executions[0].query)
    assert "SELECT" in _query_text(connection.executions[1].query)


def test_insert_session_rejects_conflicting_cache_context() -> None:
    """One Session ID cannot identify different cache context."""

    requested = _session(
        cache_mode=CacheMode.DEFAULT,
    )

    persisted = replace(
        requested,
        cache_mode=CacheMode.BYPASS,
    )

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(persisted),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionConflictError,
        match=r"Persisted Session conflicts.*session-1",
    ):
        insert_session(
            _as_connection(connection),
            schema="dataio_test_abc",
            session=requested,
        )


def test_insert_session_rejects_missing_state_after_insert() -> None:
    """A requested Session must exist after insertion."""

    session = _session()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(rows=[]),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"not present after insertion.*session-1",
    ):
        insert_session(
            _as_connection(connection),
            schema="dataio_test_abc",
            session=session,
        )


def test_insert_session_rejects_non_microsecond_aligned_timestamp() -> None:
    """Persistence never silently truncates canonical nanosecond timestamps."""

    invalid_timestamp = ts_ns_from_int(
        ts_ns_to_int(_STARTED_AT) + 1,
    )

    session = _session(
        started_at=invalid_timestamp,
    )

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=r"cannot be represented exactly.*microsecond precision",
    ):
        insert_session(
            _as_connection(connection),
            schema="dataio_test_abc",
            session=session,
        )

    assert connection.cursor_calls == 0


@pytest.mark.parametrize(
    (
        "session",
        "error_match",
    ),
    [
        (
            _session(
                session_id="",
            ),
            r"id must be non-empty text",
        ),
        (
            _session(
                source="",
            ),
            r"source must be non-empty text",
        ),
        (
            _session(
                ttl_seconds=-1.0,
            ),
            r"ttl_seconds must be a finite non-negative number",
        ),
        (
            _session(
                ttl_seconds=float("inf"),
            ),
            r"ttl_seconds must be a finite non-negative number",
        ),
        (
            _session(
                ended_at=ts_ns_from_str("2026-09-03T08:59:00.000000000Z"),
            ),
            r"ended_at must not be before started_at",
        ),
    ],
)
def test_insert_session_rejects_invalid_state_before_sql(
    session: Session,
    error_match: str,
) -> None:
    """Invalid persistence invariants fail before database access."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=error_match,
    ):
        insert_session(
            _as_connection(connection),
            schema="dataio_test_abc",
            session=session,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


# ---------------------------------------------------------------------------
# mark_session_ended
# ---------------------------------------------------------------------------


def test_mark_session_ended_records_completion() -> None:
    """An open persisted Session can be completed once."""

    open_session = _session(
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl_seconds=600.0,
        as_of_bucket="2026-09-03",
        cache_tag="vendor-v2",
    )

    ended_session = replace(
        open_session,
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(open_session),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(ended_session),
                ]
            ),
        ]
    )

    mark_session_ended(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=open_session.id,
        ended_at=_ENDED_AT,
    )

    assert len(connection.executions) == 3

    update_execution = connection.executions[1]
    update_query = _query_text(update_execution.query)

    assert '"dataio_test_abc"."sessions"' in update_query
    assert "SET ended_at = %s" in update_query
    assert "WHERE id = %s" in update_query
    assert "ended_at IS NULL" in update_query

    assert update_execution.parameters == (
        _db_datetime(_ENDED_AT),
        open_session.id,
    )


def test_mark_session_ended_is_idempotent_for_same_timestamp() -> None:
    """Repeating the same completion timestamp performs no update."""

    ended_session = _session(
        ended_at=_ENDED_AT,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(ended_session),
                ]
            ),
        ]
    )

    mark_session_ended(
        _as_connection(connection),
        schema="dataio_test_abc",
        session_id=ended_session.id,
        ended_at=_ENDED_AT,
    )

    assert connection.cursor_calls == 1
    assert len(connection.executions) == 1

    query_text = _query_text(connection.executions[0].query)

    assert "SELECT" in query_text
    assert "UPDATE" not in query_text


def test_mark_session_ended_rejects_different_existing_completion() -> None:
    """A recorded Session completion cannot be rewritten."""

    existing_end = ts_ns_from_str("2026-09-03T09:20:00.000000000Z")

    ended_session = _session(
        ended_at=existing_end,
    )

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(ended_session),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionConflictError,
        match=r"already has a different completion time",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id=ended_session.id,
            ended_at=_ENDED_AT,
        )

    assert connection.cursor_calls == 1


def test_mark_session_ended_rejects_unknown_session() -> None:
    """A nonexistent Session cannot be completed."""

    connection = FakeConnection(
        [
            FakeCursor(rows=[]),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"not persisted.*session-1",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="session-1",
            ended_at=_ENDED_AT,
        )


def test_mark_session_ended_rejects_completion_before_start() -> None:
    """A Session cannot complete before its persisted start time."""

    session = _session()

    invalid_end = ts_ns_from_str("2026-09-03T08:59:00.000000000Z")

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(session),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"ended_at must not be before started_at",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id=session.id,
            ended_at=invalid_end,
        )

    assert connection.cursor_calls == 1


def test_mark_session_ended_rejects_noncanonical_timestamp_before_sql() -> None:
    """Completion requires a canonical MXM timestamp."""

    connection = FakeConnection()

    invalid_timestamp = cast(
        TSNSScalar,
        "2026-09-03T09:30:00Z",
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"ended_at must be a valid canonical MXM timestamp",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="session-1",
            ended_at=invalid_timestamp,
        )

    assert connection.cursor_calls == 0


def test_mark_session_ended_rejects_non_microsecond_aligned_timestamp() -> None:
    """Completion never silently loses nanosecond precision."""

    connection = FakeConnection()

    invalid_timestamp = ts_ns_from_int(
        ts_ns_to_int(_ENDED_AT) + 1,
    )

    with pytest.raises(
        SessionPersistenceError,
        match=r"cannot be represented exactly.*microsecond precision",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="session-1",
            ended_at=invalid_timestamp,
        )

    assert connection.cursor_calls == 0


def test_mark_session_ended_rejects_invalid_session_id_before_sql() -> None:
    """Completion requires a valid persisted Session identity."""

    connection = FakeConnection()

    with pytest.raises(
        SessionPersistenceError,
        match=r"session_id must be non-empty text",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id="",
            ended_at=_ENDED_AT,
        )

    assert connection.cursor_calls == 0


def test_mark_session_ended_rejects_unexpected_post_update_state() -> None:
    """Completion must be visible with the requested value after updating."""

    open_session = _session()
    still_open_session = _session()

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    _session_row(open_session),
                ]
            ),
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(still_open_session),
                ]
            ),
        ]
    )

    with pytest.raises(
        SessionConflictError,
        match=r"completion differs from requested value",
    ):
        mark_session_ended(
            _as_connection(connection),
            schema="dataio_test_abc",
            session_id=open_session.id,
            ended_at=_ENDED_AT,
        )


# ---------------------------------------------------------------------------
# Transaction ownership
# ---------------------------------------------------------------------------


def test_session_operations_do_not_control_transactions() -> None:
    """Session SQL helpers neither commit nor roll back transactions."""

    session = _session()

    connection = FakeConnection(
        [
            FakeCursor(),
            FakeCursor(
                rows=[
                    _session_row(session),
                ]
            ),
        ]
    )

    insert_session(
        _as_connection(connection),
        schema="dataio_test_abc",
        session=session,
    )

    assert connection.commit_calls == 0
    assert connection.rollback_calls == 0
