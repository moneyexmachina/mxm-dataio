from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

import pytest
from mxm_config import MXMConfig

import mxm_dataio.api as api_mod  # for monkeypatching resolve_adapter
from mxm_dataio.adapters import Fetcher
from mxm_dataio.api import CacheMode, DataIoSession
from mxm_dataio.cache import FileCacheStore
from mxm_dataio.models import AdapterResult, Request, ResponseStatus
from mxm_dataio.store import Store

# ---------- Minimal concrete config (MXMConfig is a Protocol; don't instantiate it) ---


@dataclass
class _CfgPaths:
    root: str
    db_path: Optional[str] = None
    responses_dir: Optional[str] = None


@dataclass
class _CfgConcrete:
    paths: _CfgPaths


# ---------- Dummy adapter that fully implements the abstract Fetcher contract ---------


class DummyFetcher(Fetcher):
    """A concrete Fetcher that increments a counter and returns unique payloads."""

    source: str = "dummy"

    def __init__(self) -> None:
        self.calls: int = 0

    # Match the base signature: returns str
    def describe(self) -> str:
        return "dummy fetcher"

    # Implement required abstract method
    def close(self) -> None:
        pass

    # Implement the required Fetcher method
    def fetch(self, request: Request) -> AdapterResult:
        _ = request
        self.calls += 1
        payload = f"payload-{self.calls}".encode("utf-8")
        return AdapterResult(
            data=payload,
            content_type="text/plain",
            url="http://example.test",
            elapsed_ms=1,
            headers={"X-Dummy": "1"},
        )


# ---------- Fixtures (module-local, explicitly typed) ----------


@pytest.fixture()
def tmp_cfg_dataio(tmp_path: Path) -> MXMConfig:
    root_dir = tmp_path / "dataio"
    root_dir.mkdir(parents=True, exist_ok=True)

    db_path = root_dir / "dataio.sqlite"
    responses_dir = root_dir / "responses"

    cfg = _CfgConcrete(
        paths=_CfgPaths(
            root=str(root_dir),
            db_path=str(db_path),
            responses_dir=str(responses_dir),
        )
    )
    return cast(MXMConfig, cfg)


@pytest.fixture()
def store_dataio(tmp_cfg_dataio: MXMConfig) -> Store:
    # Important: Store.get_instance keys off db path resolved from cfg
    return Store.get_instance(tmp_cfg_dataio)


@pytest.fixture()
def dummy_fetcher(monkeypatch: pytest.MonkeyPatch) -> DummyFetcher:
    """Provide a DummyFetcher and patch DataIoSession's resolve_adapter to return it."""
    fetcher = DummyFetcher()

    def _resolve_adapter_patched(source: str) -> Fetcher:
        # Ignore source; return our dummy fetcher
        return fetcher

    # Patch the symbol used inside DataIoSession
    monkeypatch.setattr(
        api_mod, "resolve_adapter", _resolve_adapter_patched, raising=True
    )
    return fetcher


# ---------- Helpers ----------


def _mk_session(
    source: str,
    cfg: MXMConfig,
    *,
    cache_mode: CacheMode = CacheMode.DEFAULT,
    ttl: float | None = None,
    as_of_bucket: str | None = None,
    cache_store: FileCacheStore | None = None,
    cache_tag: str | None = None,
) -> DataIoSession:
    return DataIoSession(
        source,
        cfg,
        cache_mode=cache_mode,
        ttl=ttl,
        as_of_bucket=as_of_bucket,
        cache_store=cache_store,
        cache_tag=cache_tag,
    )


# ============================== TESTS =======================================


def test_request_hash_includes_bucket(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    with _mk_session("dummy", tmp_cfg_dataio, as_of_bucket="2025-10-27") as s1:
        r1 = s1.request(kind="http", params={"u": "A"})
    with _mk_session("dummy", tmp_cfg_dataio, as_of_bucket="2025-10-28") as s2:
        r2 = s2.request(kind="http", params={"u": "A"})
    assert r1.hash != r2.hash


def test_default_mode_ttl(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    # First fetch (store + cache)
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.DEFAULT, ttl=1.0, as_of_bucket="D"
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        resp1 = s.fetch(req)
        assert resp1.status == ResponseStatus.OK
        assert dummy_fetcher.calls == 1

    # Fresh within TTL -> cache hit via archive lookup (no new fetch)
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.DEFAULT,
        ttl=10.0,
        as_of_bucket="D",
    ) as s:
        req2 = s.request(kind="http", params={"u": "A"})
        resp2 = s.fetch(req2)
        assert dummy_fetcher.calls == 1
        assert resp2.path == resp1.path

    # Expire TTL -> refetch
    time.sleep(1.1)
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.DEFAULT, ttl=1.0, as_of_bucket="D"
    ) as s:
        req3 = s.request(kind="http", params={"u": "A"})
        resp3 = s.fetch(req3)
        assert dummy_fetcher.calls == 2
        assert resp3.checksum != resp1.checksum


def test_only_if_cached_hits_or_raises(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    # Prime cache
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.DEFAULT, as_of_bucket="B"
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        s.fetch(req)
    assert dummy_fetcher.calls == 1

    # ONLY_IF_CACHED reuses even if TTL would be stale
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl=0.0,
        as_of_bucket="B",
    ) as s:
        req2 = s.request(kind="http", params={"u": "A"})
        _ = s.fetch(req2)
    assert dummy_fetcher.calls == 1

    # Different bucket -> miss -> raise
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.ONLY_IF_CACHED, as_of_bucket="C"
    ) as s:
        req3 = s.request(kind="http", params={"u": "A"})
        with pytest.raises(RuntimeError):
            s.fetch(req3)


def test_bypass_always_fetches(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.BYPASS, as_of_bucket="X"
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        r1 = s.fetch(req)
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.BYPASS, as_of_bucket="X"
    ) as s:
        req2 = s.request(kind="http", params={"u": "A"})
        r2 = s.fetch(req2)
    assert dummy_fetcher.calls >= 2
    assert r1.checksum != r2.checksum


def test_never_mode_is_ephemeral(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.NEVER, as_of_bucket="E"
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        r = s.fetch(req)
        assert r.path == "<ephemeral>"
    # A second session cannot reuse since we didn't persist
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.DEFAULT, as_of_bucket="E"
    ) as s:
        req2 = s.request(kind="http", params={"u": "A"})
        _ = s.fetch(req2)
    assert dummy_fetcher.calls == 2  # fetched again


def test_revalidate_behaves_like_default_without_support(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.REVALIDATE,
        ttl=999,
        as_of_bucket="R",
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        s.fetch(req)
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.REVALIDATE,
        ttl=999,
        as_of_bucket="R",
    ) as s:
        req2 = s.request(kind="http", params={"u": "A"})
        s.fetch(req2)
    assert dummy_fetcher.calls == 1  # cache hit


def test_provenance_fields_present(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.DEFAULT,
        ttl=86400,
        as_of_bucket="P",
        cache_tag="en",
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        resp = s.fetch(req)
        assert resp.fetched_at is not None
        assert resp.cache_mode == CacheMode.DEFAULT.value
        assert resp.ttl_seconds == 86400
        assert resp.as_of_bucket == "P"
        assert resp.cache_tag == "en"


def test_use_cache_shim(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    # use_cache=True ⇒ default (reuse)
    with DataIoSession("dummy", tmp_cfg_dataio, use_cache=True, as_of_bucket="S") as s:
        req = s.request(kind="http", params={"u": "A"})
        s.fetch(req)
    with DataIoSession("dummy", tmp_cfg_dataio, use_cache=True, as_of_bucket="S") as s:
        req2 = s.request(kind="http", params={"u": "A"})
        s.fetch(req2)
    assert dummy_fetcher.calls == 1

    # use_cache=False ⇒ bypass
    with DataIoSession("dummy", tmp_cfg_dataio, use_cache=False, as_of_bucket="S") as s:
        req3 = s.request(kind="http", params={"u": "A"})
        s.fetch(req3)
    assert dummy_fetcher.calls == 2


def test_store_lookup_bucket_wrapper(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    with _mk_session(
        "dummy", tmp_cfg_dataio, cache_mode=CacheMode.DEFAULT, as_of_bucket="L"
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        resp = s.fetch(req)
        assert resp.status == ResponseStatus.OK

    by_hash = store_dataio.get_cached_response_by_request_hash(req.hash)
    by_bucket = store_dataio.get_cached_response_by_request_hash_and_bucket(
        req.hash, "L"
    )
    assert by_hash is not None and by_bucket is not None
    assert by_hash.id == by_bucket.id


def test_file_cache_store_short_circuit(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
    tmp_path: Path,
) -> None:
    fcs = FileCacheStore(tmp_path / "fcs")

    # First call fetches and writes-through to archive and file cache
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_store=fcs,
        cache_mode=CacheMode.DEFAULT,
        ttl=999,
        as_of_bucket="Z",
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        s.fetch(req)
    assert dummy_fetcher.calls == 1

    # Second call is served by FileCacheStore; no new adapter call
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_store=fcs,
        cache_mode=CacheMode.DEFAULT,
        ttl=999,
        as_of_bucket="Z",
    ) as s:
        req2 = s.request(kind="http", params={"u": "A"})
        s.fetch(req2)
    assert dummy_fetcher.calls == 1


def test_hash_partitions_on_cache_tag(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    with _mk_session("dummy", tmp_cfg_dataio, as_of_bucket="T", cache_tag="en") as s1:
        r1 = s1.request(kind="http", params={"u": "A"})
    with _mk_session("dummy", tmp_cfg_dataio, as_of_bucket="T", cache_tag="de") as s2:
        r2 = s2.request(kind="http", params={"u": "A"})
    assert r1.hash != r2.hash


def test_cache_isolated_by_cache_tag(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    # Prime "en"
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.DEFAULT,
        as_of_bucket="T2",
        cache_tag="en",
    ) as s:
        req = s.request(kind="http", params={"u": "A"})
        s.fetch(req)
    assert dummy_fetcher.calls == 1

    # Same bucket, different tag -> new fetch
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.DEFAULT,
        as_of_bucket="T2",
        cache_tag="de",
    ) as s2:
        req2 = s2.request(kind="http", params={"u": "A"})
        s2.fetch(req2)
    assert dummy_fetcher.calls == 2


def test_hash_ignores_ttl_and_mode(
    tmp_cfg_dataio: MXMConfig,
    store_dataio: Store,
    dummy_fetcher: DummyFetcher,
) -> None:
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.DEFAULT,
        ttl=1.0,
        as_of_bucket="K",
        cache_tag=None,
    ) as s1:
        r1 = s1.request(kind="http", params={"u": "A"})
    with _mk_session(
        "dummy",
        tmp_cfg_dataio,
        cache_mode=CacheMode.ONLY_IF_CACHED,
        ttl=999.0,
        as_of_bucket="K",
        cache_tag=None,
    ) as s2:
        r2 = s2.request(kind="http", params={"u": "A"})
    assert r1.hash == r2.hash
