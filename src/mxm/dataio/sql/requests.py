"""Plain-SQL persistence operations for DataIO Request occurrences.

This module owns the PostgreSQL representation of ``Request`` objects.

A Request is one immutable occurrence of a logical external data question and
the resolution context under which that occurrence is satisfied.

``Request.id`` identifies the individual occurrence.

``Request.hash`` identifies the logical question itself: ``kind`` plus
canonical ``params``. It deliberately excludes resolution context such as
source, cache policy, TTL, as-of bucket, and cache tag.

Transport details such as HTTP method, request body placement, SDK invocation,
authentication, and pagination belong to the source adapter and are not part
of the persisted Request model.

Multiple Request occurrences with different resolution contexts may therefore
legitimately share one question hash.

Model timestamps use the canonical MXM ``TSNSScalar`` representation.
PostgreSQL stores timestamps as ``timestamptz``. The representation bridge
between those forms lives in ``sql_timestamps``; this module translates
timestamp-boundary failures into Request persistence errors.

All functions operate on a caller-provided Psycopg connection. They do not
open, commit, or roll back transactions. Transaction ownership belongs to the
higher-level DataIO operation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import cast

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from mxm.dataio.models import CacheMode, Request
from mxm.dataio.sql.postgres import PostgresRow
from mxm.dataio.sql.sql_timestamps import (
    SqlTimestampError,
    require_db_timestamp,
    ts_ns_to_db_datetime,
    validate_sql_timestamp,
)
from mxm.types import JSONObj
from mxm.types.timestamps import TSNSScalar

type ExecutableQuery = sql.SQL | sql.Composed


_SHA256_PATTERN = re.compile(
    r"^[0-9a-f]{64}$",
    flags=re.ASCII,
)


class RequestPersistenceError(RuntimeError):
    """Base error for invalid or inconsistent persisted Request state."""


class RequestConflictError(RequestPersistenceError):
    """Raised when one Request occurrence ID identifies different state."""


# ---------------------------------------------------------------------
# REQUEST READS
# ---------------------------------------------------------------------


def fetch_request_by_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    request_id: str,
) -> Request | None:
    """Return one persisted Request occurrence by ID, if present."""

    _validate_identifier(
        request_id,
        field="request_id",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            source,
            kind,
            params,
            cache_mode,
            ttl_seconds,
            as_of_bucket,
            cache_tag,
            hash,
            created_at
        FROM {}
        WHERE id = %s
        """
    ).format(
        sql.Identifier(
            schema,
            "requests",
        )
    )

    rows = _fetch_rows(
        connection,
        query,
        (request_id,),
    )

    if not rows:
        return None

    if len(rows) != 1:
        raise RequestPersistenceError(
            "Request query returned multiple rows for "
            f"request_id {request_id!r}: {rows!r}"
        )

    return _request_from_row(rows[0])


def fetch_requests_by_hash(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    request_hash: str,
) -> dict[str, Request]:
    """Return Request occurrences sharing one logical-question hash.

    The returned mapping is keyed by Request occurrence ID.

    The hash identifies the logical question only. It does not establish cache
    reuse eligibility: source and cache-partition context must also be
    considered by the higher-level reuse logic.

    Results are deterministically ordered by creation time and Request ID.
    """

    _validate_hash(request_hash)

    query = sql.SQL(
        """
        SELECT
            id,
            source,
            kind,
            params,
            cache_mode,
            ttl_seconds,
            as_of_bucket,
            cache_tag,
            hash,
            created_at
        FROM {}
        WHERE hash = %s
        ORDER BY
            created_at,
            id
        """
    ).format(
        sql.Identifier(
            schema,
            "requests",
        )
    )

    rows = _fetch_rows(
        connection,
        query,
        (request_hash,),
    )

    return _requests_from_rows(rows)


# ---------------------------------------------------------------------
# REQUEST WRITES
# ---------------------------------------------------------------------


def insert_request(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    request: Request,
) -> None:
    """Persist one immutable Request occurrence idempotently.

    A Request occurrence absent from the database is inserted.

    A Request already present with the same occurrence ID and identical state
    is accepted as an idempotent no-op.

    A Request already present with the same occurrence ID but different state
    raises ``RequestConflictError``.

    A different Request occurrence with the same logical-question hash is
    legitimate and is persisted independently.
    """

    _validate_request(request)

    query = sql.SQL(
        """
        INSERT INTO {} (
            id,
            source,
            kind,
            params,
            cache_mode,
            ttl_seconds,
            as_of_bucket,
            cache_tag,
            hash,
            created_at
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s
        )
        ON CONFLICT (id) DO NOTHING
        """
    ).format(
        sql.Identifier(
            schema,
            "requests",
        )
    )

    params_value = Jsonb(request.params) if request.params is not None else None

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                request.id,
                request.source,
                request.kind,
                params_value,
                request.cache_mode.value,
                request.ttl_seconds,
                request.as_of_bucket,
                request.cache_tag,
                request.hash,
                _ts_ns_to_db_datetime(
                    request.created_at,
                    field="created_at",
                ),
            ),
        )

    persisted_request = fetch_request_by_id(
        connection,
        schema=schema,
        request_id=request.id,
    )

    if persisted_request is None:
        raise RequestPersistenceError(
            f"Request was not present after insertion: request_id={request.id!r}"
        )

    if persisted_request != request:
        raise RequestConflictError(
            "Persisted Request conflicts with requested occurrence for "
            f"request_id {request.id!r}: "
            f"persisted={persisted_request!r}, "
            f"requested={request!r}"
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
            cursor.execute(query)
        else:
            cursor.execute(
                query,
                parameters,
            )

        return cursor.fetchall()


# ---------------------------------------------------------------------
# ROW RECONSTRUCTION
# ---------------------------------------------------------------------


def _requests_from_rows(
    rows: Sequence[PostgresRow],
) -> dict[str, Request]:
    """Reconstruct Requests while rejecting duplicate occurrence identities."""

    requests: dict[str, Request] = {}

    for row in rows:
        request = _request_from_row(row)

        if request.id in requests:
            raise RequestPersistenceError(
                f"Request query returned duplicate Request occurrence ID {request.id!r}"
            )

        requests[request.id] = request

    return requests


def _request_from_row(
    row: PostgresRow,
) -> Request:
    """Reconstruct one validated Request occurrence from a PostgreSQL row."""

    if len(row) != 10:
        raise RequestPersistenceError(
            f"Request query returned an unexpected row shape: {row!r}"
        )

    request_id = _require_text(
        row[0],
        field="id",
    )

    source = _require_text(
        row[1],
        field="source",
    )

    kind = _require_text(
        row[2],
        field="kind",
    )

    params = _optional_json_object(
        row[3],
        field="params",
    )

    cache_mode_text = _require_text(
        row[4],
        field="cache_mode",
    )

    ttl_seconds = _optional_non_negative_float(
        row[5],
        field="ttl_seconds",
    )

    as_of_bucket = _optional_text(
        row[6],
        field="as_of_bucket",
    )

    cache_tag = _optional_text(
        row[7],
        field="cache_tag",
    )

    persisted_hash = _require_hash(
        row[8],
    )

    created_at = _require_db_timestamp(
        row[9],
        field="created_at",
    )

    try:
        cache_mode = CacheMode(cache_mode_text)
    except ValueError as err:
        raise RequestPersistenceError(
            f"Persisted Request cache_mode is not recognised: {cache_mode_text!r}"
        ) from err

    request = Request(
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

    if request.hash != persisted_hash:
        raise RequestPersistenceError(
            "Persisted Request hash is inconsistent with logical-question "
            "identity: "
            f"request_id={request.id!r}, "
            f"persisted_hash={persisted_hash!r}, "
            f"computed_hash={request.hash!r}"
        )

    _validate_request(
        request,
    )

    return request


# ---------------------------------------------------------------------
# TIMESTAMP BOUNDARY ERROR TRANSLATION
# ---------------------------------------------------------------------


def _validate_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Validate one Request timestamp for PostgreSQL persistence."""

    try:
        return validate_sql_timestamp(
            value,
            field=f"Request {field}",
        )
    except SqlTimestampError as err:
        raise RequestPersistenceError(str(err)) from err


def _ts_ns_to_db_datetime(
    value: TSNSScalar,
    *,
    field: str,
) -> datetime:
    """Convert one Request timestamp to the Psycopg representation."""

    try:
        return ts_ns_to_db_datetime(
            value,
            field=f"Request {field}",
        )
    except SqlTimestampError as err:
        raise RequestPersistenceError(str(err)) from err


def _require_db_timestamp(
    value: object,
    *,
    field: str,
) -> TSNSScalar:
    """Convert one required persisted Request timestamp."""

    try:
        return require_db_timestamp(
            value,
            field=f"Persisted Request {field}",
        )
    except SqlTimestampError as err:
        raise RequestPersistenceError(str(err)) from err


# ---------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------


def _validate_request(
    request: Request,
) -> None:
    """Validate persistence-level Request-occurrence invariants."""

    _validate_identifier(
        request.id,
        field="id",
    )

    _validate_identifier(
        request.source,
        field="source",
    )

    _validate_identifier(
        request.kind,
        field="kind",
    )

    if request.params is not None:
        _validate_json_object(
            request.params,
            field="params",
        )

    _validate_optional_non_negative_float(
        request.ttl_seconds,
        field="ttl_seconds",
    )

    _validate_optional_text(
        request.as_of_bucket,
        field="as_of_bucket",
    )

    _validate_optional_text(
        request.cache_tag,
        field="cache_tag",
    )

    _validate_timestamp(
        request.created_at,
        field="created_at",
    )

    _validate_hash(
        request.hash,
    )

    # Request is frozen, but its JSON constituents may reference mutable
    # containers. Reconstructing the dataclass recomputes the canonical
    # logical-question hash and therefore detects nested mutation before
    # persistence.
    canonical_request = replace(
        request,
    )

    if request.hash != canonical_request.hash:
        raise RequestPersistenceError(
            "Request hash is inconsistent with logical-question identity: "
            f"request_id={request.id!r}, "
            f"stored_hash={request.hash!r}, "
            f"computed_hash={canonical_request.hash!r}"
        )


def _validate_identifier(
    value: object,
    *,
    field: str,
) -> None:
    """Require a non-empty text identifier."""

    if not isinstance(value, str) or not value:
        raise RequestPersistenceError(
            f"Request {field} must be non-empty text, got {value!r}"
        )


def _require_text(
    value: object,
    *,
    field: str,
) -> str:
    """Require a non-empty persisted text value."""

    if not isinstance(value, str) or not value:
        raise RequestPersistenceError(
            f"Persisted Request {field} must be non-empty text, got {value!r}"
        )

    return value


def _validate_optional_non_negative_float(
    value: object,
    *,
    field: str,
) -> None:
    """Require a finite non-negative numeric value or NULL."""

    if value is None:
        return

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise RequestPersistenceError(
            f"Request {field} must be a finite non-negative number or NULL, "
            f"got {value!r}"
        )


def _optional_non_negative_float(
    value: object,
    *,
    field: str,
) -> float | None:
    """Require a persisted finite non-negative numeric value or NULL."""

    if value is None:
        return None

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise RequestPersistenceError(
            f"Persisted Request {field} must be a finite non-negative number "
            f"or NULL, got {value!r}"
        )

    return float(value)


def _validate_optional_text(
    value: object,
    *,
    field: str,
) -> None:
    """Require text or NULL."""

    if value is not None and not isinstance(value, str):
        raise RequestPersistenceError(
            f"Request {field} must be text or NULL, got {value!r}"
        )


def _optional_text(
    value: object,
    *,
    field: str,
) -> str | None:
    """Require persisted text or NULL."""

    if value is None:
        return None

    if not isinstance(value, str):
        raise RequestPersistenceError(
            f"Persisted Request {field} must be text or NULL, got {value!r}"
        )

    return value


def _validate_hash(
    value: object,
) -> None:
    """Require a lowercase hexadecimal SHA-256 logical-question hash."""

    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise RequestPersistenceError(
            "Request hash must be a 64-character lowercase hexadecimal "
            f"SHA-256 value, got {value!r}"
        )


def _require_hash(
    value: object,
) -> str:
    """Require and return a persisted logical-question hash."""

    _validate_hash(
        value,
    )

    return cast(
        str,
        value,
    )


# ---------------------------------------------------------------------
# JSON VALIDATION
# ---------------------------------------------------------------------


def _optional_json_object(
    value: object,
    *,
    field: str,
) -> JSONObj | None:
    """Require a persisted JSON object or NULL."""

    if value is None:
        return None

    _validate_json_object(
        value,
        field=field,
    )

    return cast(
        JSONObj,
        value,
    )


def _validate_json_object(
    value: object,
    *,
    field: str,
) -> None:
    """Require a JSON object with string keys."""

    if not isinstance(value, dict):
        raise RequestPersistenceError(
            f"Request {field} must be a JSON object, got {value!r}"
        )

    raw = cast(
        dict[object, object],
        value,
    )

    for key, item in raw.items():
        if not isinstance(key, str):
            raise RequestPersistenceError(
                f"Request {field} must contain only string keys, got {key!r}"
            )

        _validate_json_like(
            item,
            field=f"{field}.{key}",
        )


def _validate_json_like(
    value: object,
    *,
    field: str,
) -> None:
    """Require a value representable by the DataIO JSON model."""

    if value is None:
        return

    if isinstance(
        value,
        str | bool | int | float,
    ):
        return

    if isinstance(value, list):
        raw_items = cast(
            list[object],
            value,
        )

        for index, item in enumerate(raw_items):
            _validate_json_like(
                item,
                field=f"{field}[{index}]",
            )

        return

    if isinstance(value, dict):
        raw = cast(
            dict[object, object],
            value,
        )

        for key, item in raw.items():
            if not isinstance(key, str):
                raise RequestPersistenceError(
                    f"Request {field} JSON object must contain only string "
                    f"keys, got {key!r}"
                )

            _validate_json_like(
                item,
                field=f"{field}.{key}",
            )

        return

    raise RequestPersistenceError(
        f"Request {field} must contain JSON-compatible values, got {value!r}"
    )
