"""Plain-SQL persistence operations for DataIO request occurrences.

This module owns the PostgreSQL representation of ``Request`` objects.

A Request is one occurrence of a logical external request. ``Request.id``
identifies that occurrence, while ``Request.hash`` identifies what was asked
of which external source.

Multiple request occurrences may therefore legitimately share one logical
request hash.

All functions operate on a caller-provided Psycopg connection. They do not
open, commit, or roll back transactions. Transaction ownership belongs to the
higher-level DataIO operation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import cast

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from mxm.dataio.models import (
    CacheMode,
    Request,
    RequestMethod,
)
from mxm.dataio.sql.postgres import PostgresRow
from mxm.types import JSONLike, JSONObj

type ExecutableQuery = sql.SQL | sql.Composed


_SHA256_PATTERN = re.compile(
    r"^[0-9a-f]{64}$",
    flags=re.ASCII,
)


class RequestPersistenceError(RuntimeError):
    """Base error for invalid or inconsistent persisted request state."""


class RequestConflictError(RequestPersistenceError):
    """Raised when one request occurrence ID identifies different state."""


# ---------------------------------------------------------------------
# REQUEST READS
# ---------------------------------------------------------------------


def fetch_request_by_id(
    connection: Connection[PostgresRow],
    *,
    schema: str,
    request_id: str,
) -> Request | None:
    """Return one persisted request occurrence by ID, if present.

    Args:
        connection:
            Active Psycopg connection owned by the caller.
        schema:
            PostgreSQL schema containing the ``requests`` table.
        request_id:
            Request-occurrence identifier to retrieve.

    Returns:
        The persisted request occurrence, or ``None`` when absent.
    """

    _validate_identifier(
        request_id,
        field="request_id",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            session_id,
            source,
            kind,
            method,
            params,
            body,
            hash,
            cache_mode,
            ttl_seconds,
            as_of_bucket,
            cache_tag,
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
    """Return request occurrences sharing one logical request hash.

    The returned mapping is keyed by request-occurrence ID.

    Multiple returned requests are expected and legitimate: the hash
    identifies the logical request, not the individual occurrence.

    Results are deterministically ordered by creation time and then request
    ID before being reconstructed into the mapping.

    Args:
        connection:
            Active Psycopg connection owned by the caller.
        schema:
            PostgreSQL schema containing the ``requests`` table.
        request_hash:
            Logical request identity to retrieve.

    Returns:
        Matching request occurrences keyed by request ID.
    """

    _validate_hash(request_hash)

    query = sql.SQL(
        """
        SELECT
            id,
            session_id,
            source,
            kind,
            method,
            params,
            body,
            hash,
            cache_mode,
            ttl_seconds,
            as_of_bucket,
            cache_tag,
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
    """Persist one request occurrence idempotently.

    A request occurrence absent from the database is inserted.

    A request already present with the same occurrence ID and identical state
    is accepted as an idempotent no-op.

    A request already present with the same occurrence ID but different state
    raises ``RequestConflictError``.

    A different request occurrence with the same logical request hash is
    legitimate and is persisted independently.

    Args:
        connection:
            Active Psycopg connection owned by the caller.
        schema:
            PostgreSQL schema containing the ``requests`` table.
        request:
            Request occurrence to persist.

    Raises:
        RequestConflictError:
            If the request occurrence ID already identifies different state.
        RequestPersistenceError:
            If the requested occurrence is invalid or absent after insertion.
    """

    _validate_request(request)

    query = sql.SQL(
        """
        INSERT INTO {} (
            id,
            session_id,
            source,
            kind,
            method,
            params,
            body,
            hash,
            cache_mode,
            ttl_seconds,
            as_of_bucket,
            cache_tag,
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

    body_value = Jsonb(request.body) if request.body is not None else None

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                request.id,
                request.session_id,
                request.source,
                request.kind,
                request.method.value,
                params_value,
                body_value,
                request.hash,
                request.cache_mode.value,
                request.ttl_seconds,
                request.as_of_bucket,
                request.cache_tag,
                request.created_at,
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
            "Persisted request conflicts with requested occurrence for "
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
    """Reconstruct requests while rejecting duplicate occurrence identities."""

    requests: dict[str, Request] = {}

    for row in rows:
        request = _request_from_row(row)

        if request.id in requests:
            raise RequestPersistenceError(
                f"Request query returned duplicate request occurrence ID {request.id!r}"
            )

        requests[request.id] = request

    return requests


def _request_from_row(
    row: PostgresRow,
) -> Request:
    """Reconstruct one validated request occurrence from a database row."""

    if len(row) != 13:
        raise RequestPersistenceError(
            f"Request query returned an unexpected row shape: {row!r}"
        )

    request_id = _require_text(
        row[0],
        field="id",
    )

    session_id = _require_text(
        row[1],
        field="session_id",
    )

    source = _require_text(
        row[2],
        field="source",
    )

    kind = _require_text(
        row[3],
        field="kind",
    )

    method_text = _require_text(
        row[4],
        field="method",
    )

    params = _optional_json_object(
        row[5],
        field="params",
    )

    body = _optional_json_like(
        row[6],
        field="body",
    )

    persisted_hash = _require_hash(row[7])

    cache_mode_text = _require_text(
        row[8],
        field="cache_mode",
    )

    ttl_seconds = _optional_non_negative_number(
        row[9],
        field="ttl_seconds",
    )

    as_of_bucket = _optional_text(
        row[10],
        field="as_of_bucket",
    )

    cache_tag = _optional_text(
        row[11],
        field="cache_tag",
    )

    created_at = _require_datetime(
        row[12],
        field="created_at",
    )

    try:
        method = RequestMethod(method_text)
    except ValueError as err:
        raise RequestPersistenceError(
            f"Persisted request method is not recognised: {method_text!r}"
        ) from err

    try:
        cache_mode = CacheMode(cache_mode_text)
    except ValueError as err:
        raise RequestPersistenceError(
            f"Persisted request cache_mode is not recognised: {cache_mode_text!r}"
        ) from err

    request = Request(
        id=request_id,
        session_id=session_id,
        source=source,
        kind=kind,
        method=method,
        params=params,
        body=body,
        cache_mode=cache_mode,
        ttl_seconds=ttl_seconds,
        as_of_bucket=as_of_bucket,
        cache_tag=cache_tag,
        created_at=created_at,
    )

    if request.hash != persisted_hash:
        raise RequestPersistenceError(
            "Persisted request hash is inconsistent with logical request "
            f"identity: request_id={request.id!r}, "
            f"persisted_hash={persisted_hash!r}, "
            f"computed_hash={request.hash!r}"
        )

    _validate_request(request)

    return request


# ---------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------


def _validate_request(
    request: Request,
) -> None:
    """Validate persistence-level request-occurrence invariants."""

    _validate_identifier(
        request.id,
        field="id",
    )

    _validate_identifier(
        request.session_id,
        field="session_id",
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

    if request.body is not None:
        _validate_json_like(
            request.body,
            field="body",
        )

    if request.ttl_seconds is not None:
        _validate_non_negative_number(
            request.ttl_seconds,
            field="ttl_seconds",
        )

    _validate_datetime(
        request.created_at,
        field="created_at",
    )

    _validate_hash(request.hash)

    # ``hash`` is mutable dataclass state even though it is generated during
    # construction. Reconstructing the dataclass gives us the canonical hash
    # without duplicating logical-identity computation in the SQL layer.
    canonical_request = replace(request)

    if request.hash != canonical_request.hash:
        raise RequestPersistenceError(
            "Request hash is inconsistent with logical request identity: "
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

    if (
        not isinstance(
            value,
            str,
        )
        or not value
    ):
        raise RequestPersistenceError(
            f"Request {field} must be non-empty text, got {value!r}"
        )


def _validate_hash(
    value: object,
) -> None:
    """Require a lowercase hexadecimal SHA-256 logical-request hash."""

    if (
        not isinstance(
            value,
            str,
        )
        or _SHA256_PATTERN.fullmatch(value) is None
    ):
        raise RequestPersistenceError(
            "Request hash must be a 64-character lowercase hexadecimal "
            f"SHA-256 value, got {value!r}"
        )


def _require_hash(
    value: object,
) -> str:
    """Require and return a persisted logical-request hash."""

    _validate_hash(value)

    return cast(
        str,
        value,
    )


def _require_text(
    value: object,
    *,
    field: str,
) -> str:
    """Require a non-empty persisted text value."""

    if (
        not isinstance(
            value,
            str,
        )
        or not value
    ):
        raise RequestPersistenceError(
            f"Persisted request {field} must be non-empty text, got {value!r}"
        )

    return value


def _optional_text(
    value: object,
    *,
    field: str,
) -> str | None:
    """Require a persisted text or NULL value."""

    if value is None:
        return None

    if not isinstance(
        value,
        str,
    ):
        raise RequestPersistenceError(
            f"Persisted request {field} must be text or NULL, got {value!r}"
        )

    return value


def _validate_datetime(
    value: object,
    *,
    field: str,
) -> datetime:
    """Require a timezone-aware datetime."""

    if not isinstance(
        value,
        datetime,
    ):
        raise RequestPersistenceError(
            f"Request {field} must be a datetime, got {value!r}"
        )

    if value.tzinfo is None or value.utcoffset() is None:
        raise RequestPersistenceError(
            f"Request {field} must be timezone-aware, got {value!r}"
        )

    return value


def _require_datetime(
    value: object,
    *,
    field: str,
) -> datetime:
    """Require a timezone-aware persisted datetime."""

    if not isinstance(
        value,
        datetime,
    ):
        raise RequestPersistenceError(
            f"Persisted request {field} must be a datetime, got {value!r}"
        )

    if value.tzinfo is None or value.utcoffset() is None:
        raise RequestPersistenceError(
            f"Persisted request {field} must be timezone-aware, got {value!r}"
        )

    return value


def _validate_non_negative_number(
    value: object,
    *,
    field: str,
) -> float:
    """Require a non-negative numeric value."""

    if isinstance(
        value,
        bool,
    ) or not isinstance(
        value,
        int | float,
    ):
        raise RequestPersistenceError(f"Request {field} must be numeric, got {value!r}")

    result = float(value)

    if result < 0:
        raise RequestPersistenceError(
            f"Request {field} must be non-negative, got {value!r}"
        )

    return result


def _optional_non_negative_number(
    value: object,
    *,
    field: str,
) -> float | None:
    """Require a non-negative persisted numeric value or NULL."""

    if value is None:
        return None

    return _validate_non_negative_number(
        value,
        field=field,
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


def _optional_json_like(
    value: object,
    *,
    field: str,
) -> JSONLike | None:
    """Require a persisted JSON value or NULL."""

    if value is None:
        return None

    _validate_json_like(
        value,
        field=field,
    )

    return cast(
        JSONLike,
        value,
    )


def _validate_json_object(
    value: object,
    *,
    field: str,
) -> None:
    """Require a JSON object with string keys."""

    if not isinstance(
        value,
        dict,
    ):
        raise RequestPersistenceError(
            f"Request {field} must be a JSON object, got {value!r}"
        )

    raw = cast(
        dict[object, object],
        value,
    )

    for key, item in raw.items():
        if not isinstance(
            key,
            str,
        ):
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

    if isinstance(
        value,
        list,
    ):
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

    if isinstance(
        value,
        dict,
    ):
        raw = cast(
            dict[object, object],
            value,
        )

        for key, item in raw.items():
            if not isinstance(
                key,
                str,
            ):
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
