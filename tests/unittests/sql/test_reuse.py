"""Unit tests for plain-SQL DataIO reusable-Response candidate lookup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Self, cast

import pytest
from psycopg import Connection, sql

from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.reuse import (
    ReuseLookupError,
    fetch_reuse_candidate_response_id,
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
# Fixtures and helpers
# ---------------------------------------------------------------------------


_REQUEST_HASH = "a" * 64

_MINIMUM_FETCHED_AT = ts_ns_from_str("2026-09-07T07:30:00.123456000Z")

_UNIX_EPOCH = datetime(
    1970,
    1,
    1,
    tzinfo=UTC,
)


def _db_datetime(
    value: TSNSScalar,
) -> datetime:
    """Encode one microsecond-aligned MXM timestamp as fake Psycopg input."""

    nanoseconds = ts_ns_to_int(
        value,
    )

    assert nanoseconds % 1_000 == 0

    return _UNIX_EPOCH + timedelta(
        microseconds=nanoseconds // 1_000,
    )


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


def _lookup(
    connection: FakeConnection,
    *,
    source: str = "source-a",
    request_hash: str = _REQUEST_HASH,
    as_of_bucket: str | None = "bucket-a",
    cache_tag: str | None = "vendor-v1",
    minimum_fetched_at: TSNSScalar | None = None,
) -> str | None:
    """Execute one representative reuse lookup against a fake connection."""

    return fetch_reuse_candidate_response_id(
        _as_connection(
            connection,
        ),
        schema="dataio_test_abc",
        source=source,
        request_hash=request_hash,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        minimum_fetched_at=minimum_fetched_at,
    )


# ---------------------------------------------------------------------------
# Candidate result reconstruction
# ---------------------------------------------------------------------------


def test_fetch_reuse_candidate_returns_none_when_absent() -> None:
    """No metadata-eligible Response produces no candidate."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    candidate = _lookup(
        connection,
    )

    assert candidate is None
    assert connection.cursor_calls == 1


def test_fetch_reuse_candidate_returns_response_identity() -> None:
    """The selected relational candidate is returned as a Response ID."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    ("response-1",),
                ]
            ),
        ]
    )

    candidate = _lookup(
        connection,
    )

    assert candidate == "response-1"


@pytest.mark.parametrize(
    "rows",
    [
        [
            (),
        ],
        [
            (
                "response-1",
                "extra",
            ),
        ],
        [
            (123,),
        ],
        [
            ("",),
        ],
        [
            ("response-1",),
            ("response-2",),
        ],
    ],
)
def test_fetch_reuse_candidate_rejects_unexpected_query_results(
    rows: list[PostgresRow],
) -> None:
    """Malformed candidate-query results cannot cross the SQL boundary."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=rows,
            ),
        ]
    )

    with pytest.raises(
        ReuseLookupError,
    ):
        _lookup(
            connection,
        )


# ---------------------------------------------------------------------------
# Reuse namespace
# ---------------------------------------------------------------------------


def test_reuse_query_matches_acquisition_namespace() -> None:
    """Candidate discovery uses the acquiring Request context."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    _lookup(
        connection,
        source="source-a",
        request_hash=_REQUEST_HASH,
        as_of_bucket="bucket-a",
        cache_tag="vendor-v1",
    )

    execution = _single_execution(
        connection,
    )

    query_text = _query_text(
        execution.query,
    )

    assert '"dataio_test_abc"."responses" AS response' in query_text
    assert '"dataio_test_abc"."requests" AS acquiring_request' in query_text

    assert "acquiring_request.id = response.request_id" in query_text

    assert "acquiring_request.source = %s" in query_text
    assert "acquiring_request.hash = %s" in query_text

    assert "acquiring_request.as_of_bucket IS NOT DISTINCT FROM %s" in query_text
    assert "acquiring_request.cache_tag IS NOT DISTINCT FROM %s" in query_text
    assert "acquiring_request.cache_mode" not in query_text
    assert "acquiring_request.ttl_seconds" not in query_text
    assert "sessions" not in query_text.lower()

    assert execution.parameters == (
        "source-a",
        _REQUEST_HASH,
        "bucket-a",
        "vendor-v1",
        "ok",
    )


def test_reuse_query_uses_null_safe_partition_equality() -> None:
    """NULL partition coordinates are matched as opaque equality values."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    _lookup(
        connection,
        as_of_bucket=None,
        cache_tag=None,
    )

    execution = _single_execution(
        connection,
    )

    query_text = _query_text(
        execution.query,
    )

    assert "acquiring_request.as_of_bucket IS NOT DISTINCT FROM %s" in query_text
    assert "acquiring_request.cache_tag IS NOT DISTINCT FROM %s" in query_text

    assert execution.parameters == (
        "source-a",
        _REQUEST_HASH,
        None,
        None,
        "ok",
    )


# ---------------------------------------------------------------------------
# Candidate eligibility
# ---------------------------------------------------------------------------


def test_reuse_query_considers_only_successful_responses() -> None:
    """Only ResponseStatus.OK observations are SQL reuse candidates."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    _lookup(
        connection,
    )

    execution = _single_execution(
        connection,
    )

    query_text = _query_text(
        execution.query,
    )

    assert "response.status = %s" in query_text
    assert execution.parameters is not None
    assert execution.parameters == (
        "source-a",
        _REQUEST_HASH,
        "bucket-a",
        "vendor-v1",
        "ok",
    )


def test_reuse_query_has_no_freshness_filter_when_cutoff_is_absent() -> None:
    """An absent runtime cutoff leaves candidate age unrestricted."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    _lookup(
        connection,
        minimum_fetched_at=None,
    )

    execution = _single_execution(
        connection,
    )

    query_text = _query_text(
        execution.query,
    )

    assert "response.fetched_at >= %s" not in query_text

    assert execution.parameters == (
        "source-a",
        _REQUEST_HASH,
        "bucket-a",
        "vendor-v1",
        "ok",
    )


def test_reuse_query_applies_inclusive_fetched_at_cutoff() -> None:
    """Runtime freshness is applied to the original Response fetched_at."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    _lookup(
        connection,
        minimum_fetched_at=_MINIMUM_FETCHED_AT,
    )

    execution = _single_execution(
        connection,
    )

    query_text = _query_text(
        execution.query,
    )

    assert "response.fetched_at >= %s" in query_text

    assert execution.parameters == (
        "source-a",
        _REQUEST_HASH,
        "bucket-a",
        "vendor-v1",
        "ok",
        _db_datetime(
            _MINIMUM_FETCHED_AT,
        ),
    )


def test_reuse_query_orders_newest_acquisition_deterministically() -> None:
    """Candidate ranking is based on immutable Response acquisition metadata."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    _lookup(
        connection,
    )

    execution = _single_execution(
        connection,
    )

    query_text = _query_text(
        execution.query,
    )

    assert (
        "ORDER BY "
        "response.fetched_at DESC, "
        "response.created_at DESC, "
        "response.id DESC" in query_text
    )

    assert "LIMIT 1" in query_text


# ---------------------------------------------------------------------------
# Architectural exclusions
# ---------------------------------------------------------------------------


def test_reuse_query_does_not_traverse_resolutions() -> None:
    """Historical reuse does not transport a Response into another namespace."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    _lookup(
        connection,
    )

    query_text = _query_text(
        _single_execution(
            connection,
        ).query,
    )

    assert "resolutions" not in query_text.lower()


def test_reuse_query_does_not_apply_runtime_cache_policy() -> None:
    """Cache mode and TTL interpretation remain above the SQL read model."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[],
            ),
        ]
    )

    _lookup(
        connection,
    )

    query_text = _query_text(
        _single_execution(
            connection,
        ).query,
    )

    assert "cache_mode" not in query_text
    assert "ttl_seconds" not in query_text


# ---------------------------------------------------------------------------
# Caller-state validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "source",
        "error_match",
    ),
    [
        (
            "",
            r"source must be non-empty text",
        ),
    ],
)
def test_fetch_reuse_candidate_rejects_invalid_source_before_sql(
    source: str,
    error_match: str,
) -> None:
    """Invalid source identity fails before database access."""

    connection = FakeConnection()

    with pytest.raises(
        ReuseLookupError,
        match=error_match,
    ):
        _lookup(
            connection,
            source=source,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


@pytest.mark.parametrize(
    "request_hash",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
    ],
)
def test_fetch_reuse_candidate_rejects_invalid_request_hash_before_sql(
    request_hash: str,
) -> None:
    """Logical-question hashes must use canonical SHA-256 representation."""

    connection = FakeConnection()

    with pytest.raises(
        ReuseLookupError,
        match=r"request_hash must be a 64-character lowercase hexadecimal",
    ):
        _lookup(
            connection,
            request_hash=request_hash,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


@pytest.mark.parametrize(
    (
        "as_of_bucket",
        "cache_tag",
        "error_match",
    ),
    [
        (
            cast(str | None, 123),
            None,
            r"as_of_bucket must be text or NULL",
        ),
        (
            None,
            cast(str | None, 123),
            r"cache_tag must be text or NULL",
        ),
    ],
)
def test_fetch_reuse_candidate_rejects_invalid_partition_values_before_sql(
    as_of_bucket: str | None,
    cache_tag: str | None,
    error_match: str,
) -> None:
    """Opaque partition coordinates must still have valid representation."""

    connection = FakeConnection()

    with pytest.raises(
        ReuseLookupError,
        match=error_match,
    ):
        _lookup(
            connection,
            as_of_bucket=as_of_bucket,
            cache_tag=cache_tag,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_reuse_candidate_rejects_noncanonical_cutoff_before_sql() -> None:
    """Freshness cutoff must use the canonical MXM timestamp representation."""

    invalid_timestamp = cast(
        TSNSScalar,
        "2026-09-07T07:30:00Z",
    )

    connection = FakeConnection()

    with pytest.raises(
        ReuseLookupError,
        match=r"minimum_fetched_at must be a valid canonical MXM timestamp",
    ):
        _lookup(
            connection,
            minimum_fetched_at=invalid_timestamp,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


def test_fetch_reuse_candidate_rejects_sub_microsecond_cutoff_before_sql() -> None:
    """Freshness lookup never silently truncates MXM timestamp precision."""

    invalid_timestamp = ts_ns_from_int(
        ts_ns_to_int(
            _MINIMUM_FETCHED_AT,
        )
        + 1
    )

    connection = FakeConnection()

    with pytest.raises(
        ReuseLookupError,
        match=(
            r"minimum_fetched_at cannot be represented exactly.*"
            r"microsecond precision"
        ),
    ):
        _lookup(
            connection,
            minimum_fetched_at=invalid_timestamp,
        )

    assert connection.cursor_calls == 0
    assert connection.executions == []


# ---------------------------------------------------------------------------
# Transaction ownership
# ---------------------------------------------------------------------------


def test_reuse_lookup_does_not_control_transactions() -> None:
    """Reuse SQL lookup neither commits nor rolls back transactions."""

    connection = FakeConnection(
        [
            FakeCursor(
                rows=[
                    ("response-1",),
                ]
            ),
        ]
    )

    candidate = _lookup(
        connection,
    )

    assert candidate == "response-1"

    assert connection.commit_calls == 0
    assert connection.rollback_calls == 0
