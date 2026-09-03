CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.schema_migrations (
    version text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT schema_migrations_version_non_empty
        CHECK (version <> ''),

    CONSTRAINT schema_migrations_checksum_sha256
        CHECK (checksum ~ '^[0-9a-f]{64}$')
);
