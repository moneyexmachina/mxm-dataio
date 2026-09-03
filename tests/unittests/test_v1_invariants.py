from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

import mxm.dataio.api as api_mod
from mxm.config import MXMConfig
from mxm.dataio.adapters import Fetcher
from mxm.dataio.api import CacheMode, DataIoSession
from mxm.dataio.models import AdapterResult, Request
from mxm.dataio.store import Store


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = str(root)
        self.db_path = str(root / "dataio.sqlite")
        self.responses_dir = str(root / "responses")


class _Config:
    def __init__(self, root: Path) -> None:
        self.paths = _Paths(root)


class CountingFetcher(Fetcher):
    """Fetcher whose payload changes on every real external observation."""

    source = "source-a"

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, request: Request) -> AdapterResult:
        _ = request
        self.calls += 1
        return AdapterResult(
            data=f"observation-{self.calls}".encode(),
            headers={"X-Request-ID": str(self.calls)},
        )

    def describe(self) -> str:
        return "Counting fetcher"

    def close(self) -> None:
        pass


class IdenticalPayloadFetcher(Fetcher):
    """Fetcher returning identical bytes but observation-specific metadata."""

    source = "source-a"

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, request: Request) -> AdapterResult:
        _ = request
        self.calls += 1
        return AdapterResult(
            data=b"identical-payload",
            headers={"X-Request-ID": str(self.calls)},
        )

    def describe(self) -> str:
        return "Identical-payload fetcher"

    def close(self) -> None:
        pass


@pytest.fixture()
def cfg(tmp_path: Path) -> MXMConfig:
    root = tmp_path / "dataio"
    root.mkdir()
    return cast(MXMConfig, _Config(root))


@pytest.fixture()
def store(cfg: MXMConfig) -> Store:
    return Store(cfg)


def _patch_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Fetcher,
) -> None:
    def resolve_adapter(_: str) -> Fetcher:
        return fetcher

    monkeypatch.setattr(
        api_mod,
        "resolve_adapter",
        resolve_adapter,
        raising=True,
    )


def _persisted_request_ids(store: Store) -> set[str]:
    with store.connect() as conn:
        rows = conn.execute("SELECT id FROM requests").fetchall()
    return {cast(str, row[0]) for row in rows}


def _persisted_response_rows(
    store: Store,
) -> list[tuple[str, str, str | None]]:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, request_id, checksum
            FROM responses
            ORDER BY created_at
            """
        ).fetchall()

    return [
        (
            cast(str, row[0]),
            cast(str, row[1]),
            cast(str | None, row[2]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Logical request identity
# ---------------------------------------------------------------------------


def test_equivalent_requests_have_same_logical_hash_but_distinct_ids(
    cfg: MXMConfig,
) -> None:
    with DataIoSession("source-a", cfg) as io:
        q1 = io.request(kind="http", params={"u": "A"})
        q2 = io.request(kind="http", params={"u": "A"})

    assert q1.hash == q2.hash
    assert q1.id != q2.id


def test_logical_hash_ignores_cache_mode_and_ttl(
    cfg: MXMConfig,
) -> None:
    with DataIoSession(
        "source-a",
        cfg,
        cache_mode=CacheMode.DEFAULT,
        ttl=1.0,
        as_of_bucket="bucket",
        cache_tag="tag",
    ) as io:
        q1 = io.request(kind="http", params={"u": "A"})

    with DataIoSession(
        "source-a",
        cfg,
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl=999.0,
        as_of_bucket="bucket",
        cache_tag="tag",
    ) as io:
        q2 = io.request(kind="http", params={"u": "A"})

    assert q1.hash == q2.hash


def test_as_of_bucket_partitions_logical_identity(
    cfg: MXMConfig,
) -> None:
    with DataIoSession("source-a", cfg, as_of_bucket="A") as io:
        q1 = io.request(kind="http", params={"u": "A"})

    with DataIoSession("source-a", cfg, as_of_bucket="B") as io:
        q2 = io.request(kind="http", params={"u": "A"})

    assert q1.hash != q2.hash


def test_cache_tag_partitions_logical_identity(
    cfg: MXMConfig,
) -> None:
    with DataIoSession("source-a", cfg, cache_tag="en") as io:
        q1 = io.request(kind="http", params={"u": "A"})

    with DataIoSession("source-a", cfg, cache_tag="de") as io:
        q2 = io.request(kind="http", params={"u": "A"})

    assert q1.hash != q2.hash


@pytest.mark.xfail(
    strict=True,
    reason="ED005: source is not yet part of logical request identity",
)
def test_source_partitions_logical_identity(
    cfg: MXMConfig,
) -> None:
    with DataIoSession("source-a", cfg) as io:
        q1 = io.request(kind="http", params={"u": "A"})

    with DataIoSession("source-b", cfg) as io:
        q2 = io.request(kind="http", params={"u": "A"})

    assert q1.hash != q2.hash


# ---------------------------------------------------------------------------
# Request occurrence persistence
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "ED005: UNIQUE(request.hash) plus INSERT OR IGNORE currently collapses "
        "equivalent request occurrences"
    ),
)
def test_equivalent_request_occurrences_are_both_persisted(
    cfg: MXMConfig,
    store: Store,
) -> None:
    with DataIoSession("source-a", cfg, store=store) as io:
        q1 = io.request(kind="http", params={"u": "A"})
        q2 = io.request(kind="http", params={"u": "A"})

    assert q1.hash == q2.hash
    assert q1.id != q2.id

    persisted = _persisted_request_ids(store)

    assert q1.id in persisted
    assert q2.id in persisted


# ---------------------------------------------------------------------------
# Observation semantics
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "ED005: repeated equivalent request occurrences are currently collapsed, "
        "so the second BYPASS response can reference a non-persisted request"
    ),
)
def test_bypass_creates_two_valid_request_observation_chains(
    cfg: MXMConfig,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CountingFetcher()
    _patch_fetcher(monkeypatch, fetcher)

    with DataIoSession(
        "source-a",
        cfg,
        store=store,
        cache_mode=CacheMode.BYPASS,
    ) as io:
        q1 = io.request(kind="http", params={"u": "A"})
        r1 = io.fetch(q1)

        q2 = io.request(kind="http", params={"u": "A"})
        r2 = io.fetch(q2)

    assert fetcher.calls == 2

    assert q1.hash == q2.hash
    assert q1.id != q2.id

    assert r1.id != r2.id
    assert r1.request_id == q1.id
    assert r2.request_id == q2.id

    request_ids = _persisted_request_ids(store)
    assert q1.id in request_ids
    assert q2.id in request_ids

    response_rows = _persisted_response_rows(store)
    persisted_links = {
        (response_id, request_id) for response_id, request_id, _ in response_rows
    }

    assert (r1.id, q1.id) in persisted_links
    assert (r2.id, q2.id) in persisted_links


def test_default_reuses_existing_observation_without_external_fetch(
    cfg: MXMConfig,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CountingFetcher()
    _patch_fetcher(monkeypatch, fetcher)

    with DataIoSession(
        "source-a",
        cfg,
        store=store,
        cache_mode=CacheMode.DEFAULT,
    ) as io:
        q1 = io.request(kind="http", params={"u": "A"})
        r1 = io.fetch(q1)

        q2 = io.request(kind="http", params={"u": "A"})
        r2 = io.fetch(q2)

    assert q1.hash == q2.hash
    assert q1.id != q2.id
    assert fetcher.calls == 1

    # Current API already returns the original durable response on archive reuse.
    assert r2.id == r1.id


@pytest.mark.xfail(
    strict=True,
    reason=(
        "ED005: cache-hit request occurrence is currently collapsed by "
        "UNIQUE(request.hash)"
    ),
)
def test_default_cache_hit_retains_second_request_occurrence(
    cfg: MXMConfig,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CountingFetcher()
    _patch_fetcher(monkeypatch, fetcher)

    with DataIoSession(
        "source-a",
        cfg,
        store=store,
        cache_mode=CacheMode.DEFAULT,
    ) as io:
        q1 = io.request(kind="http", params={"u": "A"})
        r1 = io.fetch(q1)

        q2 = io.request(kind="http", params={"u": "A"})
        r2 = io.fetch(q2)

    assert fetcher.calls == 1
    assert r2.id == r1.id

    request_ids = _persisted_request_ids(store)
    assert q1.id in request_ids
    assert q2.id in request_ids


# ---------------------------------------------------------------------------
# Payload identity versus observation identity
# ---------------------------------------------------------------------------


def test_identical_external_payloads_have_same_checksum_but_distinct_responses(
    cfg: MXMConfig,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = IdenticalPayloadFetcher()
    _patch_fetcher(monkeypatch, fetcher)

    with DataIoSession(
        "source-a",
        cfg,
        store=store,
        cache_mode=CacheMode.BYPASS,
    ) as io:
        q1 = io.request(kind="http", params={"u": "A"})
        r1 = io.fetch(q1)

        q2 = io.request(kind="http", params={"u": "A"})
        r2 = io.fetch(q2)

    assert fetcher.calls == 2
    assert r1.id != r2.id
    assert r1.checksum == r2.checksum


@pytest.mark.xfail(
    strict=True,
    reason=(
        "ED005: response-specific metadata is currently keyed by payload "
        "checksum in a first-write-wins filesystem sidecar"
    ),
)
def test_identical_payload_observations_retain_independent_metadata(
    cfg: MXMConfig,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = IdenticalPayloadFetcher()
    _patch_fetcher(monkeypatch, fetcher)

    with DataIoSession(
        "source-a",
        cfg,
        store=store,
        cache_mode=CacheMode.BYPASS,
    ) as io:
        q1 = io.request(kind="http", params={"u": "A"})
        r1 = io.fetch(q1)

        q2 = io.request(kind="http", params={"u": "A"})
        r2 = io.fetch(q2)

    assert r1.id != r2.id
    assert r1.checksum == r2.checksum

    # Target invariant:
    #
    # observation_metadata(r1)["headers"]["X-Request-ID"] == "1"
    # observation_metadata(r2)["headers"]["X-Request-ID"] == "2"
    #
    # The current Store cannot express this: metadata is addressed only
    # by payload checksum. This explicit failure prevents us from silently
    # preserving the sidecar model.
    assert r1.checksum is not None
    first_metadata = store.read_metadata(r1.checksum)
    second_metadata = store.read_metadata(r2.checksum)

    assert first_metadata["headers"] == {"X-Request-ID": "1"}
    assert second_metadata["headers"] == {"X-Request-ID": "2"}


# ---------------------------------------------------------------------------
# Foreign-key integrity regression
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="ED005: SQLite foreign-key enforcement is not enabled",
)
def test_store_enforces_request_response_foreign_key_integrity(
    store: Store,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with store.connect() as conn:
            conn.execute(
                """
                INSERT INTO responses
                (
                    id,
                    request_id,
                    status,
                    sequence,
                    checksum,
                    path,
                    created_at,
                    size_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "response-with-missing-request",
                    "request-that-does-not-exist",
                    "ok",
                    None,
                    None,
                    None,
                    "2026-09-02T08:00:00+00:00",
                    None,
                ),
            )
