"""Versioned PostgreSQL schema migration support for MXM DataIO.

This module discovers SQL migrations packaged with ``mxm-dataio``, verifies
their identities, and applies pending migrations to the PostgreSQL schema owned
by a :class:`PostgresDatabase`.

Migration source is treated as immutable after application. The SHA-256
checksum is calculated over the unrendered packaged SQL source, so a migration
has the same identity when applied to operational and disposable test schemas.

The bootstrap migration establishes the owned schema and migration ledger. It
is deliberately not recorded in that ledger. Every subsequent migration is
executed and recorded atomically in its own transaction.

TODO(architecture):
    This generic PostgreSQL migration runner is intentionally duplicated from
    mxm-refdata for ED005. Once the pattern is proven across both packages,
    extract the shared migration infrastructure into a common package (likely
    mxm-infra), while keeping each package's migration resources and owned
    schema definitions local to that package.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import resources
from typing import Final, LiteralString, cast

from psycopg import sql

from mxm.dataio.sql.postgres import PostgresDatabase

type MigrationResource = tuple[str, str]
type MigrationLoader = Callable[[], Iterable[MigrationResource]]

_MIGRATION_PACKAGE: Final = "mxm.dataio.sql.migrations"
_BOOTSTRAP_FILENAME: Final = "000_bootstrap.sql"
_SCHEMA_PLACEHOLDER: Final = "{schema}"

_MIGRATION_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<version>[0-9]{3})_(?P<name>[a-z][a-z0-9_]*)\.sql$",
    flags=re.ASCII,
)

_MIGRATION_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9]{3}$",
    flags=re.ASCII,
)
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{64}$",
    flags=re.ASCII,
)


class MigrationError(RuntimeError):
    """Base class for migration discovery and application failures."""


class MigrationDiscoveryError(MigrationError):
    """Raised when packaged migration resources are invalid."""


class MigrationChecksumMismatchError(MigrationError):
    """Raised when an applied migration no longer matches packaged source."""


class MigrationStateError(MigrationError):
    """Raised when the database migration ledger is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable packaged SQL migration."""

    version: str
    name: str
    filename: str
    sql_text: str
    checksum: str

    @classmethod
    def from_resource(
        cls,
        filename: str,
        sql_text: str,
    ) -> Migration:
        """Construct and validate a migration from one packaged resource.

        Args:
            filename:
                Resource filename following ``NNN_name.sql``.
            sql_text:
                Unrendered SQL source loaded from the package.

        Returns:
            A validated migration with a stable SHA-256 checksum.

        Raises:
            MigrationDiscoveryError:
                If the filename, source text, or schema placeholder is invalid.
        """

        match = _MIGRATION_FILENAME_PATTERN.fullmatch(filename)

        if match is None:
            raise MigrationDiscoveryError(
                f"Invalid migration filename. Expected 'NNN_name.sql', got {filename!r}"
            )

        if not sql_text.strip():
            raise MigrationDiscoveryError(f"Migration {filename!r} contains no SQL")

        if _SCHEMA_PLACEHOLDER not in sql_text:
            raise MigrationDiscoveryError(
                f"Migration {filename!r} does not contain the required "
                f"{_SCHEMA_PLACEHOLDER!r} placeholder"
            )

        checksum = hashlib.sha256(
            sql_text.encode("utf-8"),
        ).hexdigest()

        return cls(
            version=match.group("version"),
            name=match.group("name"),
            filename=filename,
            sql_text=sql_text,
            checksum=checksum,
        )

    @property
    def is_bootstrap(self) -> bool:
        """Return whether this is the distinguished bootstrap migration."""

        return self.filename == _BOOTSTRAP_FILENAME


@dataclass(frozen=True, slots=True)
class MigrationInspection:
    """Observed migration state for one PostgreSQL DataIO schema."""

    initialised: bool
    packaged_versions: tuple[str, ...]
    applied_versions: tuple[str, ...]
    pending_versions: tuple[str, ...]

    @property
    def current(self) -> bool:
        """Return whether the schema has every packaged migration applied."""

        return self.initialised and not self.pending_versions


def _load_packaged_migration_resources() -> list[MigrationResource]:
    """Load SQL migration resources from the installed package.

    Returns:
        Migration filenames and their UTF-8 source text. Non-SQL package
        resources, such as ``__init__.py``, are ignored.
    """

    package_root = resources.files(_MIGRATION_PACKAGE)
    migration_resources: list[MigrationResource] = []

    for resource in package_root.iterdir():
        if not resource.is_file() or not resource.name.endswith(".sql"):
            continue

        migration_resources.append(
            (
                resource.name,
                resource.read_text(encoding="utf-8"),
            )
        )

    return migration_resources


def _render_migration_sql(
    migration: Migration,
    *,
    schema: str,
) -> sql.Composed:
    """Render a migration using a safely quoted PostgreSQL schema identifier.

    Only the exact ``{schema}`` marker is replaced. Other braces in SQL
    literals, JSON documents, or comments remain untouched.

    The SQL fragments originate from trusted packaged migration resources.
    """

    source_fragments = migration.sql_text.split(_SCHEMA_PLACEHOLDER)
    query_parts: list[sql.Composable] = []

    for index, fragment in enumerate(source_fragments):
        if index > 0:
            query_parts.append(sql.Identifier(schema))

        if fragment:
            query_parts.append(
                sql.SQL(
                    cast(LiteralString, fragment),
                )
            )

    return sql.Composed(query_parts)


class MigrationRunner:
    """Discover, verify, and apply PostgreSQL schema migrations."""

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        migration_loader: MigrationLoader | None = None,
    ) -> None:
        """Create a migration runner for one PostgreSQL schema.

        Args:
            database:
                PostgreSQL connection and transaction boundary.
            migration_loader:
                Optional migration-resource loader. Production use reads
                packaged SQL resources; unit tests may inject an in-memory
                loader without requiring package or filesystem mutation.
        """

        self._database = database
        self._migration_loader = migration_loader or _load_packaged_migration_resources

    def discover(self) -> list[Migration]:
        """Discover and validate all packaged migrations in version order.

        Returns:
            All migrations, including ``000_bootstrap.sql``.

        Raises:
            MigrationDiscoveryError:
                If bootstrap is missing, filenames are malformed, versions are
                duplicated, or migration source is invalid.
        """

        raw_resources = list(self._migration_loader())

        migrations = [
            Migration.from_resource(filename, sql_text)
            for filename, sql_text in raw_resources
        ]
        migrations.sort(
            key=lambda migration: (
                migration.version,
                migration.filename,
            )
        )

        self._validate_discovered_migrations(migrations)

        return migrations

    def inspect(self) -> MigrationInspection:
        """Inspect migration state without modifying PostgreSQL.

        Packaged migrations are discovered and validated first. The owned
        PostgreSQL schema is then inspected to determine whether bootstrap has
        established the migration ledger.

        A completely absent schema is treated as normal uninitialised state.
        An existing schema without its migration ledger is treated as
        inconsistent migration state.

        When the migration ledger exists, its contents are decoded and
        validated against packaged migrations using the same rules as
        ``migrate()``.

        Returns:
            Immutable migration-state observations in packaged version order.

        Raises:
            MigrationDiscoveryError:
                If packaged migrations are malformed.
            MigrationChecksumMismatchError:
                If an applied migration's packaged source has changed.
            MigrationStateError:
                If bootstrap state or the migration ledger is inconsistent.
            psycopg.Error:
                If PostgreSQL inspection fails.
        """

        migrations = self.discover()
        tracked_migrations = migrations[1:]

        packaged_versions = tuple(migration.version for migration in tracked_migrations)

        (
            schema_exists,
            ledger_exists,
        ) = self._inspect_bootstrap_state()

        if not schema_exists and not ledger_exists:
            return MigrationInspection(
                initialised=False,
                packaged_versions=packaged_versions,
                applied_versions=(),
                pending_versions=packaged_versions,
            )

        if not schema_exists or not ledger_exists:
            raise MigrationStateError(
                "PostgreSQL migration bootstrap state is inconsistent: "
                f"schema_exists={schema_exists!r}, "
                f"ledger_exists={ledger_exists!r}"
            )

        applied_migrations = self._read_applied_migrations()

        self._validate_applied_migrations(
            tracked_migrations,
            applied_migrations,
        )

        applied_versions = tuple(
            migration.version
            for migration in tracked_migrations
            if migration.version in applied_migrations
        )

        pending_versions = tuple(
            migration.version
            for migration in tracked_migrations
            if migration.version not in applied_migrations
        )

        return MigrationInspection(
            initialised=True,
            packaged_versions=packaged_versions,
            applied_versions=applied_versions,
            pending_versions=pending_versions,
        )

    def migrate(self) -> list[str]:
        """Apply all pending migrations and return newly applied versions.

        The bootstrap migration is run first and remains untracked. Applied
        migration checksums are verified before any pending migration is run.

        Each ordinary migration and its ledger insert execute in the same
        transaction. If either operation fails, neither remains committed.

        Returns:
            Versions newly applied during this invocation.

        Raises:
            MigrationDiscoveryError:
                If packaged migrations are malformed.
            MigrationChecksumMismatchError:
                If an applied migration's packaged source has changed.
            MigrationStateError:
                If the database records a migration absent from the package.
            psycopg.Error:
                If PostgreSQL execution fails.
        """

        migrations = self.discover()
        bootstrap = migrations[0]
        tracked_migrations = migrations[1:]

        self._apply_bootstrap(bootstrap)

        applied_migrations = self._read_applied_migrations()
        self._validate_applied_migrations(
            tracked_migrations,
            applied_migrations,
        )

        newly_applied: list[str] = []

        for migration in tracked_migrations:
            if migration.version in applied_migrations:
                continue

            self._apply_tracked_migration(migration)
            newly_applied.append(migration.version)

        return newly_applied

    def _validate_discovered_migrations(
        self,
        migrations: list[Migration],
    ) -> None:
        """Validate bootstrap presence, uniqueness, and version ordering."""

        if not migrations:
            raise MigrationDiscoveryError(
                "No packaged PostgreSQL migrations were discovered"
            )

        bootstrap_matches = [
            migration for migration in migrations if migration.is_bootstrap
        ]

        if len(bootstrap_matches) != 1:
            raise MigrationDiscoveryError(
                "Exactly one '000_bootstrap.sql' migration is required"
            )

        if migrations[0].filename != _BOOTSTRAP_FILENAME:
            raise MigrationDiscoveryError(
                "'000_bootstrap.sql' must be the first migration"
            )

        seen_versions: dict[str, str] = {}

        for migration in migrations:
            existing_filename = seen_versions.get(migration.version)

            if existing_filename is not None:
                raise MigrationDiscoveryError(
                    "Duplicate migration version "
                    f"{migration.version!r}: "
                    f"{existing_filename!r} and "
                    f"{migration.filename!r}"
                )

            seen_versions[migration.version] = migration.filename

        bootstrap = migrations[0]

        if bootstrap.version != "000" or bootstrap.name != "bootstrap":
            raise MigrationDiscoveryError(
                "Bootstrap migration must be named '000_bootstrap.sql'"
            )

        for migration in migrations[1:]:
            if migration.version == "000":
                raise MigrationDiscoveryError(
                    "Migration version '000' is reserved for bootstrap"
                )

    def _inspect_bootstrap_state(
        self,
    ) -> tuple[bool, bool]:
        """Return whether the owned schema and migration ledger exist."""

        query = sql.SQL(
            """
            SELECT
                EXISTS (
                    SELECT 1
                    FROM information_schema.schemata
                    WHERE schema_name = %s
                ),
                EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s
                      AND table_name = 'schema_migrations'
                )
            """
        )

        with self._database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (
                        self._database.schema,
                        self._database.schema,
                    ),
                )
                rows = cursor.fetchall()

        if len(rows) != 1:
            raise MigrationStateError(
                "Migration bootstrap inspection returned an unexpected "
                f"number of rows: {rows!r}"
            )

        row = rows[0]

        if len(row) != 2:
            raise MigrationStateError(
                "Migration bootstrap inspection returned an unexpected "
                f"row shape: {row!r}"
            )

        schema_exists = row[0]
        ledger_exists = row[1]

        if not isinstance(schema_exists, bool):
            raise MigrationStateError(
                "Migration bootstrap schema-existence result must be boolean, "
                f"got {schema_exists!r}"
            )

        if not isinstance(ledger_exists, bool):
            raise MigrationStateError(
                "Migration bootstrap ledger-existence result must be boolean, "
                f"got {ledger_exists!r}"
            )

        return (
            schema_exists,
            ledger_exists,
        )

    def _apply_bootstrap(
        self,
        migration: Migration,
    ) -> None:
        """Create the owned schema and migration ledger idempotently."""

        rendered_sql = _render_migration_sql(
            migration,
            schema=self._database.schema,
        )

        with self._database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(rendered_sql)

    def _read_applied_migrations(self) -> dict[str, str]:
        """Return applied migration versions mapped to their checksums."""

        table_identifier = sql.Identifier(
            self._database.schema,
            "schema_migrations",
        )
        query = sql.SQL("SELECT version, checksum FROM {} ORDER BY version").format(
            table_identifier
        )

        with self._database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()

        applied: dict[str, str] = {}

        for row in rows:
            if len(row) != 2:
                raise MigrationStateError(
                    f"Migration ledger returned an unexpected row shape: {row!r}"
                )

            version = row[0]
            checksum = row[1]

            if not isinstance(version, str):
                raise MigrationStateError(
                    f"Migration ledger version must be text, got {version!r}"
                )

            if not isinstance(checksum, str):
                raise MigrationStateError(
                    f"Migration ledger checksum must be text, got {checksum!r}"
                )

            if _MIGRATION_VERSION_PATTERN.fullmatch(version) is None:
                raise MigrationStateError(
                    f"Migration ledger contains an invalid version: {version!r}"
                )

            if _SHA256_PATTERN.fullmatch(checksum) is None:
                raise MigrationStateError(
                    "Migration ledger contains an invalid checksum for "
                    f"version {version!r}: {checksum!r}"
                )

            if version in applied:
                raise MigrationStateError(
                    f"Migration ledger contains duplicate version {version!r}"
                )

            applied[version] = checksum

        return applied

    def _validate_applied_migrations(
        self,
        packaged_migrations: list[Migration],
        applied_migrations: dict[str, str],
    ) -> None:
        """Verify that database migration state matches packaged source."""

        packaged_by_version = {
            migration.version: migration for migration in packaged_migrations
        }

        unknown_versions = sorted(set(applied_migrations) - set(packaged_by_version))

        if unknown_versions:
            rendered_versions = ", ".join(repr(version) for version in unknown_versions)
            raise MigrationStateError(
                "Database contains applied migration versions absent "
                f"from the package: {rendered_versions}"
            )

        for version, applied_checksum in applied_migrations.items():
            packaged_migration = packaged_by_version[version]

            if applied_checksum != packaged_migration.checksum:
                raise MigrationChecksumMismatchError(
                    "Checksum mismatch for applied migration "
                    f"{packaged_migration.filename!r}: "
                    f"database has {applied_checksum!r}, "
                    f"package has {packaged_migration.checksum!r}"
                )

    def _apply_tracked_migration(
        self,
        migration: Migration,
    ) -> None:
        """Execute and record one pending migration atomically."""

        rendered_sql = _render_migration_sql(
            migration,
            schema=self._database.schema,
        )
        ledger_identifier = sql.Identifier(
            self._database.schema,
            "schema_migrations",
        )
        record_query = sql.SQL(
            "INSERT INTO {} (version, checksum) VALUES (%s, %s)"
        ).format(ledger_identifier)

        with self._database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(rendered_sql)
                cursor.execute(
                    record_query,
                    (
                        migration.version,
                        migration.checksum,
                    ),
                )
