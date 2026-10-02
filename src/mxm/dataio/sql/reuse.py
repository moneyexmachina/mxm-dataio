"""Relational lookup of reusable DataIO Response candidates.

This module owns the PostgreSQL query that selects the newest Response
observation eligible for consideration by the DataIO reuse runtime.

Reuse candidate selection is a cross-entity read model. It relates:

    Request -> Response

The acquiring Request supplies both the logical-question hash and its durable
source and opaque reuse-partition context.

A candidate must therefore have been acquired under the same:

- source;
- logical-question hash;
- as-of bucket;
- cache tag.

Only successful external observations are candidates.

An optional ``minimum_fetched_at`` applies the current runtime's freshness
cutoff to ``Response.fetched_at``. This module does not calculate that cutoff;
TTL policy and the runtime clock belong above the SQL boundary.

Candidate ordering is:

1. newest ``Response.fetched_at``;
2. newest ``Response.created_at``;
3. greatest ``Response.id`` as a deterministic final tie-breaker.

This module deliberately does not:

- inspect the acquiring Request's cache mode or TTL;
- calculate TTL;
- inspect or create Resolutions;
- inspect payload-store availability or integrity;
- perform external acquisition;
- decide what to do when no candidate exists.

Those decisions belong to the DataIO runtime.

All functions operate on a caller-provided Psycopg connection and do not
control transactions.
"""

from __future__ import annotations

import re
from datetime import datetime

from psycopg import Connection, sql

from mxm.dataio.models import ResponseStatus
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.sql_timestamps import (
    SqlTimestampError,
    ts_ns_to_db_datetime,
)
from mxm.types.timestamps import TSNSScalar

type ExecutableQuery = sql.SQL | sql.Composed


_SHA256_PATTERN = re.compile(
    r"^[0-9a-f]{64}$",
    flags=re.ASCII,
)


class ReuseLookupError(RuntimeError):
    """Reuse candidate state cannot be represented safely at the SQL boundary."""


def fetch_reuse_candidate_response_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    source: str,
    request_hash: str,
    as_of_bucket: str | None,
    cache_tag: str | None,
    minimum_fetched_at: TSNSScalar | None = None,
) -> str | None:
    """Return the newest metadata-eligible Response ID, if one exists.

    ``request_hash`` identifies the logical external question.

    ``source``, ``as_of_bucket``, and ``cache_tag`` define the reuse namespace
    in which an acquisition must have occurred.

    ``minimum_fetched_at`` is an optional inclusive freshness cutoff supplied
    by the runtime. When absent, candidate age is unrestricted.

    This function returns only the selected Response identity. Entity
    reconstruction remains the responsibility of ``responses.py``.
    """

    _validate_non_empty_text(
        source,
        field="source",
    )

    _validate_hash(
        request_hash,
    )

    _validate_optional_text(
        as_of_bucket,
        field="as_of_bucket",
    )

    _validate_optional_text(
        cache_tag,
        field="cache_tag",
    )

    parameters: list[object] = [
        source,
        request_hash,
        as_of_bucket,
        cache_tag,
        ResponseStatus.OK.value,
    ]

    if minimum_fetched_at is None:
        freshness_clause = sql.SQL("")
    else:
        freshness_clause = sql.SQL(
            """
            AND response.fetched_at >= %s
            """
        )

        parameters.append(
            _ts_ns_to_db_datetime(
                minimum_fetched_at,
                field="minimum_fetched_at",
            )
        )

    query = sql.SQL(
        """
        SELECT
            response.id
        FROM {} AS response
        JOIN {} AS acquiring_request
          ON acquiring_request.id = response.request_id
        WHERE acquiring_request.source = %s
          AND acquiring_request.hash = %s
          AND acquiring_request.as_of_bucket IS NOT DISTINCT FROM %s
          AND acquiring_request.cache_tag IS NOT DISTINCT FROM %s
          AND response.status = %s
          {}
        ORDER BY
            response.fetched_at DESC,
            response.created_at DESC,
            response.id DESC
        LIMIT 1
        """
    ).format(
        sql.Identifier(
            schema,
            "responses",
        ),
        sql.Identifier(
            schema,
            "requests",
        ),
        freshness_clause,
    )

    rows = _fetch_rows(
        connection,
        query,
        tuple(parameters),
    )

    if not rows:
        return None

    if len(rows) != 1 or len(rows[0]) != 1:
        raise ReuseLookupError(
            f"Reuse candidate query returned an unexpected result: {rows!r}"
        )

    return _require_response_id(
        rows[0][0],
    )


# ---------------------------------------------------------------------
# PRIVATE QUERY HELPERS
# ---------------------------------------------------------------------


def _fetch_rows(
    connection: Connection[PostgresRow],
    query: ExecutableQuery,
    parameters: tuple[object, ...] | None = None,
) -> list[PostgresRow]:
    """Execute one query and return all result rows."""

    with connection.cursor() as cursor:
        if parameters is None:
            cursor.execute(
                query,
            )
        else:
            cursor.execute(
                query,
                parameters,
            )

        return cursor.fetchall()


# ---------------------------------------------------------------------
# TIMESTAMP BOUNDARY
# ---------------------------------------------------------------------


def _ts_ns_to_db_datetime(
    value: TSNSScalar,
    *,
    field: str,
) -> datetime:
    """Convert one reuse-query timestamp to the Psycopg representation."""

    try:
        return ts_ns_to_db_datetime(
            value,
            field=f"Reuse {field}",
        )
    except SqlTimestampError as err:
        raise ReuseLookupError(str(err)) from err


# ---------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------


def _validate_non_empty_text(
    value: object,
    *,
    field: str,
) -> None:
    """Require non-empty text."""

    if not isinstance(value, str) or not value:
        raise ReuseLookupError(f"Reuse {field} must be non-empty text, got {value!r}")


def _validate_optional_text(
    value: object,
    *,
    field: str,
) -> None:
    """Require text or NULL."""

    if value is not None and not isinstance(value, str):
        raise ReuseLookupError(f"Reuse {field} must be text or NULL, got {value!r}")


def _validate_hash(
    value: object,
) -> None:
    """Require a canonical logical-question SHA-256 hash."""

    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ReuseLookupError(
            "Reuse request_hash must be a 64-character lowercase hexadecimal "
            f"SHA-256 value, got {value!r}"
        )


def _require_response_id(
    value: object,
) -> str:
    """Require one persisted Response identity."""

    if not isinstance(value, str) or not value:
        raise ReuseLookupError(
            f"Reuse candidate Response id must be non-empty text, got {value!r}"
        )

    return value
