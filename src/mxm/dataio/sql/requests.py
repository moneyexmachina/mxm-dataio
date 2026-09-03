"""Plain-SQL persistence operations for DataIO request occurrences.

This module owns the PostgreSQL representation of ``Request`` objects.

A Request is one immutable occurrence of an external question made within a
DataIO Session.

``Request.id`` identifies the individual occurrence.

``Request.hash`` identifies the question itself: kind, method, params, and
body. It deliberately excludes Session-owned context such as source, cache
policy, TTL, as-of bucket, and cache tag.

Multiple request occurrences, including occurrences belonging to different
sessions, may therefore legitimately share one question hash.

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

from mxm.dataio.models import Request, RequestMethod
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
    """Return one persisted request occurrence by ID, if present."""

    _validate_identifier(
        request_id,
        field="request_id",
    )

    query = sql.SQL(
        """
        SELECT
            id,
            session_id,
            kind,
            method,
            params,
            body,
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
    """Return request occurrences sharing one question hash.

    The returned mapping is keyed by request-occurrence ID.

    The hash identifies the question only. It does not establish reuse
    eligibility: Session-owned source and resolution context must also be
    considered by the higher-level resolution logic.

    Results are deterministically ordered by creation time and request ID.
    """

    _validate_hash(request_hash)

    query = sql.SQL(
        """
        SELECT
            id,
            session_id,
            kind,
            method,
            params,
            body,
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
    """Persist one immutable request occurrence idempotently.

    A request occurrence absent from the database is inserted.

    A request already present with the same occurrence ID and identical state
    is accepted as an idempotent no-op.

    A request already present with the same occurrence ID but different state
    raises ``RequestConflictError``.

    A different request occurrence with the same question hash is legitimate
    and is persisted independently.
    """

    _validate_request(request)

    query = sql.SQL(
        """
        INSERT INTO {} (
            id,
            session_id,
            kind,
            method,
            params,
            body,
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
                request.kind,
                request.method.value,
                params_value,
                body_value,
                request.hash,
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

    if len(row) != 8:
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

    kind = _require_text(
        row[2],
        field="kind",
    )

    method_text = _require_text(
        row[3],
        field="method",
    )

    params = _optional_json_object(
        row[4],
        field="params",
    )

    body = _optional_json_like(
        row[5],
        field="body",
    )

    persisted_hash = _require_hash(
        row[6],
    )

    created_at = _require_datetime(
        row[7],
        field="created_at",
    )

    try:
        method = RequestMethod(
            method_text,
        )
    except ValueError as err:
        raise RequestPersistenceError(
            f"Persisted request method is not recognised: {method_text!r}"
        ) from err

    request = Request(
        id=request_id,
        session_id=session_id,
        kind=kind,
        method=method,
        params=params,
        body=body,
        created_at=created_at,
    )

    if request.hash != persisted_hash:
        raise RequestPersistenceError(
            "Persisted request hash is inconsistent with question identity: "
            f"request_id={request.id!r}, "
            f"persisted_hash={persisted_hash!r}, "
            f"computed_hash={request.hash!r}"
        )

    _validate_request(
        request,
    )

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

    _validate_datetime(
        request.created_at,
        field="created_at",
    )

    _validate_hash(
        request.hash,
    )

    # Request is frozen, but its JSON constituents may reference mutable
    # containers. Reconstructing the dataclass recomputes the canonical
    # question hash and detects any nested mutation before persistence.
    canonical_request = replace(
        request,
    )

    if request.hash != canonical_request.hash:
        raise RequestPersistenceError(
            "Request hash is inconsistent with question identity: "
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
    """Require a lowercase hexadecimal SHA-256 question hash."""

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
    """Require and return a persisted question hash."""

    _validate_hash(
        value,
    )

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
