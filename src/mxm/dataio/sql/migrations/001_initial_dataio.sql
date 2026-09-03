CREATE TABLE {schema}.sessions (
    id text PRIMARY KEY,
    source text NOT NULL,
    mode text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,

    CONSTRAINT sessions_id_non_empty
        CHECK (id <> ''),

    CONSTRAINT sessions_source_non_empty
        CHECK (source <> ''),

    CONSTRAINT sessions_mode_non_empty
        CHECK (mode <> ''),

    CONSTRAINT sessions_id_source_unique
        UNIQUE (id, source)
);


CREATE TABLE {schema}.requests (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    source text NOT NULL,

    kind text NOT NULL,
    method text NOT NULL,
    params jsonb,
    body jsonb,

    hash text NOT NULL,

    cache_mode text NOT NULL,
    ttl_seconds double precision,
    as_of_bucket text,
    cache_tag text,

    created_at timestamptz NOT NULL,

    CONSTRAINT requests_id_non_empty
        CHECK (id <> ''),

    CONSTRAINT requests_source_non_empty
        CHECK (source <> ''),

    CONSTRAINT requests_kind_non_empty
        CHECK (kind <> ''),

    CONSTRAINT requests_method_non_empty
        CHECK (method <> ''),

    CONSTRAINT requests_cache_mode_non_empty
        CHECK (cache_mode <> ''),

    CONSTRAINT requests_hash_sha256
        CHECK (hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT requests_ttl_seconds_non_negative
        CHECK (
            ttl_seconds IS NULL
            OR ttl_seconds >= 0
        ),

    CONSTRAINT requests_params_object
        CHECK (
            params IS NULL
            OR jsonb_typeof(params) = 'object'
        ),

    CONSTRAINT requests_session_source_fk
        FOREIGN KEY (session_id, source)
        REFERENCES {schema}.sessions (id, source)
);

CREATE TABLE {schema}.responses (
    id text PRIMARY KEY,
    request_id text NOT NULL,

    status text NOT NULL,
    sequence integer,

    created_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL,

    payload_checksum text NOT NULL,
    size_bytes bigint NOT NULL,

    media_type text,
    encoding text,
    elapsed_ms bigint,
    adapter_meta jsonb,

    CONSTRAINT responses_id_non_empty
        CHECK (id <> ''),

    CONSTRAINT responses_status_non_empty
        CHECK (status <> ''),

    CONSTRAINT responses_sequence_non_negative
        CHECK (
            sequence IS NULL
            OR sequence >= 0
        ),

    CONSTRAINT responses_payload_checksum_sha256
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),

    CONSTRAINT responses_size_bytes_non_negative
        CHECK (size_bytes >= 0),

    CONSTRAINT responses_elapsed_ms_non_negative
        CHECK (
            elapsed_ms IS NULL
            OR elapsed_ms >= 0
        ),

    CONSTRAINT responses_adapter_meta_object
        CHECK (
            adapter_meta IS NULL
            OR jsonb_typeof(adapter_meta) = 'object'
        ),

    CONSTRAINT responses_request_fk
        FOREIGN KEY (request_id)
        REFERENCES {schema}.requests (id)
);

CREATE TABLE {schema}.resolutions (
    request_id text PRIMARY KEY,
    response_id text NOT NULL,
    kind text NOT NULL,
    resolved_at timestamptz NOT NULL,

    CONSTRAINT resolutions_kind_valid
        CHECK (
            kind IN (
                'acquired',
                'reused'
            )
        ),

    CONSTRAINT resolutions_request_fk
        FOREIGN KEY (request_id)
        REFERENCES {schema}.requests (id),

    CONSTRAINT resolutions_response_fk
        FOREIGN KEY (response_id)
        REFERENCES {schema}.responses (id)
);


CREATE INDEX requests_session_id_idx
    ON {schema}.requests (session_id);

CREATE INDEX requests_hash_created_at_idx
    ON {schema}.requests (hash, created_at DESC);

CREATE INDEX responses_request_id_idx
    ON {schema}.responses (request_id);

CREATE INDEX responses_payload_checksum_idx
    ON {schema}.responses (payload_checksum);

CREATE INDEX responses_fetched_at_idx
    ON {schema}.responses (fetched_at DESC);

CREATE INDEX resolutions_response_id_idx
    ON {schema}.resolutions (response_id);
