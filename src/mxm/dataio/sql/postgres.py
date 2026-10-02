"""PostgreSQL connection and transaction boundary for MXM DataIO.

TODO(architecture):
    This low-level PostgreSQL boundary is intentionally duplicated from
    mxm-refdata for ED005. Once the pattern is proven across both packages,
    extract the shared connection/transaction infrastructure into a common
    package (likely mxm-infra), while keeping package-specific schemas,
    migrations, repositories, and persistence logic local to each package.
This module provides the lowest-level PostgreSQL runtime abstraction used by
DataIO migrations, persistence, queries, and diagnostics.

It deliberately owns only:

- PostgreSQL connection URL validation and normalisation;
- validation of the owned PostgreSQL schema identifier;
- creation of Psycopg connections;
- commit, rollback, and connection-close lifecycle;
- basic database connectivity checking.

Schema migration, SQL query construction, DataIO interaction persistence, and
payload storage belong to higher-level modules.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, Final
from urllib.parse import quote

import psycopg
from psycopg import Connection

type PostgresRow = tuple[Any, ...]
type ConnectFactory = Callable[[str], Connection[PostgresRow]]

_SQLALCHEMY_PSYCOPG_PREFIX: Final = "postgresql+psycopg://"
_PSYCOPG_PREFIX: Final = "postgresql://"

_SCHEMA_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9_]{0,62}$",
    flags=re.ASCII,
)


def _normalise_connection_url(connection_url: str) -> str:
    """Return a Psycopg-compatible PostgreSQL connection URL.

    MXM runtime configuration may represent PostgreSQL URLs using SQLAlchemy's
    explicit Psycopg driver syntax::

        postgresql+psycopg://...

    Psycopg expects the ordinary PostgreSQL URI form::

        postgresql://...

    No other database dialects or PostgreSQL drivers are accepted.
    """

    normalised = connection_url.strip()

    if not normalised:
        raise ValueError("PostgreSQL connection URL must be non-empty")

    if normalised.startswith(_SQLALCHEMY_PSYCOPG_PREFIX):
        return _PSYCOPG_PREFIX + normalised.removeprefix(_SQLALCHEMY_PSYCOPG_PREFIX)

    if normalised.startswith(_PSYCOPG_PREFIX):
        return normalised

    raise ValueError(
        "Unsupported database connection URL. Expected a URL beginning with "
        f"{_SQLALCHEMY_PSYCOPG_PREFIX!r} or {_PSYCOPG_PREFIX!r}"
    )


def _validate_schema_name(schema: str) -> str:
    """Validate and return an unquoted PostgreSQL schema identifier.

    MXM schema names use lower-case ASCII identifiers only. Restricting the
    accepted syntax prevents callers from supplying qualified names, quoted
    identifiers, whitespace, or executable SQL fragments.

    PostgreSQL identifiers are limited to 63 bytes. The accepted character set
    is ASCII, so the 63-character limit is also the 63-byte limit.
    """

    if not _SCHEMA_NAME_PATTERN.fullmatch(schema):
        raise ValueError(
            "Invalid PostgreSQL schema name. Expected a lower-case ASCII "
            "identifier matching '^[a-z][a-z0-9_]{0,62}$', "
            f"got {schema!r}"
        )

    return schema


def _require_connection_text(
    value: str,
    *,
    field: str,
) -> str:
    """Validate and return one required PostgreSQL connection value."""

    resolved = value.strip()

    if not resolved:
        raise ValueError(f"PostgreSQL {field} must be non-empty")

    return resolved


def _default_connect(
    connection_url: str,
) -> Connection[PostgresRow]:
    """Open a transactional Psycopg connection."""

    return psycopg.connect(
        connection_url,
        autocommit=False,
    )


class PostgresDatabase:
    """Own PostgreSQL connection creation and transaction lifecycle.

    The class is intentionally independent of DataIO models and table
    definitions. Callers receive a normal Psycopg connection and remain
    responsible for creating cursors and executing their own SQL.

    A replaceable connection factory allows unit tests to exercise transaction
    behaviour without requiring a running PostgreSQL server.
    """

    def __init__(
        self,
        connection_url: str,
        *,
        schema: str = "dataio",
        connect_factory: ConnectFactory | None = None,
    ) -> None:
        """Create a PostgreSQL runtime boundary.

        Args:
            connection_url:
                PostgreSQL URI. Both ``postgresql://`` and the transitional
                SQLAlchemy-style ``postgresql+psycopg://`` prefix are accepted.
            schema:
                Explicitly owned PostgreSQL schema. It must be a lower-case
                unquoted identifier of at most 63 ASCII characters.
            connect_factory:
                Optional connection constructor used primarily by unit tests.
                Production use defaults to :func:`psycopg.connect`.

        Raises:
            ValueError:
                If the connection URL or schema name is invalid.
        """

        self._connection_url = _normalise_connection_url(connection_url)
        self._schema = _validate_schema_name(schema)
        self._connect = connect_factory or _default_connect

    @classmethod
    def from_config(
        cls,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str = "dataio",
        connect_factory: ConnectFactory | None = None,
    ) -> PostgresDatabase:
        """Create a PostgreSQL boundary from resolved connection parameters.

        This constructor accepts concrete PostgreSQL connection values after
        runtime configuration and secrets have been resolved by the
        composition root. PostgreSQL URI construction remains encapsulated
        inside this adapter.

        Args:
            host:
                PostgreSQL server hostname, IPv4 address, or IPv6 address.
            port:
                PostgreSQL TCP port.
            database:
                PostgreSQL database name.
            user:
                PostgreSQL user name.
            password:
                Resolved PostgreSQL password.
            schema:
                Explicitly owned PostgreSQL schema.
            connect_factory:
                Optional connection constructor used primarily by unit tests.

        Returns:
            A configured PostgreSQL runtime boundary.

        Raises:
            ValueError:
                If any connection parameter or the owned schema is invalid.
        """

        resolved_host = _require_connection_text(
            host,
            field="host",
        )
        resolved_database = _require_connection_text(
            database,
            field="database",
        )
        resolved_user = _require_connection_text(
            user,
            field="user",
        )
        resolved_password = _require_connection_text(
            password,
            field="password",
        )

        if not 1 <= port <= 65_535:
            raise ValueError(
                f"PostgreSQL port must be between 1 and 65535, got {port!r}"
            )

        rendered_host = (
            f"[{resolved_host}]"
            if ":" in resolved_host and not resolved_host.startswith("[")
            else resolved_host
        )

        connection_url = (
            "postgresql://"
            f"{quote(resolved_user, safe='')}:"
            f"{quote(resolved_password, safe='')}"
            f"@{rendered_host}:{port}/"
            f"{quote(resolved_database, safe='')}"
        )

        return cls(
            connection_url,
            schema=schema,
            connect_factory=connect_factory,
        )

    @property
    def schema(self) -> str:
        """Return the validated owned PostgreSQL schema name."""

        return self._schema

    def with_schema(
        self,
        schema: str,
    ) -> PostgresDatabase:
        """Return a database boundary for the same target with another schema.

        The connection target and connection factory are preserved. The new
        schema is validated independently by the normal constructor.

        This is useful when callers require an isolated PostgreSQL namespace,
        such as disposable integration-test schemas.
        """

        return PostgresDatabase(
            self._connection_url,
            schema=schema,
            connect_factory=self._connect,
        )

    @contextmanager
    def transaction(
        self,
    ) -> Generator[Connection[PostgresRow]]:
        """Yield one connection inside an explicit transaction boundary.

        A successful block is committed. Any exception raised by the caller or
        by the commit operation triggers a rollback and is then re-raised.

        The connection is closed in all cases. If rollback or close also fails
        while handling an existing exception, the cleanup failure is attached
        as a note without replacing the original error.
        """

        connection = self._connect(self._connection_url)
        pending_error: BaseException | None = None

        try:
            yield connection
            connection.commit()
        except BaseException as error:
            pending_error = error

            try:
                connection.rollback()
            except BaseException as rollback_error:
                error.add_note(f"PostgreSQL rollback also failed: {rollback_error!r}")

            raise
        finally:
            try:
                connection.close()
            except BaseException as close_error:
                if pending_error is None:
                    raise

                pending_error.add_note(
                    f"PostgreSQL connection close also failed: {close_error!r}"
                )

    def check_connection(self) -> bool:
        """Return whether PostgreSQL responds correctly to ``SELECT 1``.

        Connection and query exceptions deliberately propagate to the caller.
        Operational surfaces such as preflight can catch those exceptions and
        present them with the appropriate runtime context.
        """

        with self.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                row = cursor.fetchone()

        return row == (1,)
