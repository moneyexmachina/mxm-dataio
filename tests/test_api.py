"""Integration tests for mxm_dataio.api.DataIoSession."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from mxm_dataio.adapters import Fetcher, Sender
from mxm_dataio.api import DataIoSession
from mxm_dataio.models import AdapterResult, Request, RequestMethod, ResponseStatus
from mxm_dataio.registry import clear_registry, register
from mxm_dataio.store import Store

# --------------------------------------------------------------------------- #
# Dummy adapters (AdapterResult-based)
# --------------------------------------------------------------------------- #


class DummyFetcher(Fetcher):
    source = "dummy_fetch"

    def fetch(self, request: Request) -> AdapterResult:
        # Deterministic payload tied to request hash
        payload = f"PAYLOAD:{request.hash}".encode("utf-8")
        return AdapterResult(
            data=payload,
            transport_status=200,
            content_type="application/octet-stream",
            url="https://dummy.fetch.local/resource",
            elapsed_ms=5,
            headers={"X-Dummy": "fetch"},
            adapter_meta={"note": "dummy-fetch"},
        )

    def describe(self) -> str:
        return "Dummy fetch adapter"

    def close(self) -> None:
        pass


class DummySender(Sender):
    source = "dummy_send"

    def send(self, request: Request, payload: bytes) -> AdapterResult:
        _ = request
        # Echo the payload as the response 'data', and attach small metadata
        return AdapterResult(
            data=payload,
            transport_status=200,
            content_type="application/json",
            url="https://dummy.send.local/resource",
            elapsed_ms=7,
            headers={"Content-Type": "application/json"},
            adapter_meta={"ok": "1", "len": str(len(payload))},
        )

    def describe(self) -> str:
        return "Dummy send adapter"

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


_ = _clean_registry


@pytest.fixture()
def store_cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "paths": {
            "data_root": str(tmp_path),
            "db_path": str(tmp_path / "dataio.sqlite"),
            "responses_dir": str(tmp_path / "responses"),
        }
    }


@pytest.fixture()
def store(store_cfg: dict[str, Any]) -> Store:
    return Store.get_instance(store_cfg)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_session_lifecycle_updates_ended_at(
    store_cfg: dict[str, Any], store: Store
) -> None:
    register("dummy_fetch", DummyFetcher())

    with DataIoSession(source="dummy_fetch", cfg=store_cfg):
        pass  # open/close only

    with store.connect() as conn:
        row = conn.execute(
            "SELECT started_at, ended_at FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    started = datetime.fromisoformat(row[0])
    ended = datetime.fromisoformat(row[1])
    assert ended >= started


def test_fetch_persists_request_response_and_payload(
    store_cfg: dict[str, Any], store: Store
) -> None:
    register("dummy_fetch", DummyFetcher())

    with DataIoSession(source="dummy_fetch", cfg=store_cfg) as io:
        req = io.request(kind="demo", params={"a": 1})
        resp = io.fetch(req)

    assert resp.status == ResponseStatus.OK
    assert resp.path is not None
    payload_path = Path(resp.path)
    assert payload_path.exists()
    data = payload_path.read_bytes()
    assert data.startswith(b"PAYLOAD:")

    with store.connect() as conn:
        n_req = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        n_resp = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
    assert n_req == 1
    assert n_resp == 1


def test_fetch_cache_hit_returns_same_response(store_cfg: dict[str, Any]) -> None:
    register("dummy_fetch", DummyFetcher())

    with DataIoSession(source="dummy_fetch", cfg=store_cfg) as io:
        r1 = io.request(kind="demo", params={"x": 42})
        resp1 = io.fetch(r1)
        # A new Request with identical params should hit cache
        r2 = io.request(kind="demo", params={"x": 42})
        resp2 = io.fetch(r2)

    assert resp2.id == resp1.id  # cache returned the previously stored response


def test_send_persists_ack_and_json_payload(store_cfg: dict[str, Any]) -> None:
    register("dummy_send", DummySender())

    with DataIoSession(source="dummy_send", cfg=store_cfg) as io:
        req = io.request(kind="post_demo", method=RequestMethod.POST, body={"x": 1})
        resp = io.send(req, payload={"hello": "world"})

    assert resp.status == ResponseStatus.ACK
    assert resp.path is not None
    assert resp.checksum is not None

    # Payload on disk is the JSON we sent (deterministic encoder)
    payload_bytes = Path(resp.path).read_bytes()
    assert payload_bytes == b'{"hello":"world"}'

    # Sidecar metadata exists and contains adapter meta + content type
    store = Store.get_instance(store_cfg)
    meta = store.read_metadata(resp.checksum)
    assert meta["content_type"] == "application/json"
    assert meta["adapter_meta"]["ok"] == "1"
    assert meta["adapter_meta"]["len"] == str(len(b'{"hello":"world"}'))


def test_capability_mismatch_raises(store_cfg: dict[str, Any]) -> None:
    register("dummy_fetch", DummyFetcher())
    register("dummy_send", DummySender())

    with DataIoSession(source="dummy_fetch", cfg=store_cfg) as io:
        req = io.request(kind="k", params={})
        with pytest.raises(TypeError):
            io.send(req, payload=b"nope")

    with DataIoSession(source="dummy_send", cfg=store_cfg) as io:
        req = io.request(kind="k", params={})
        with pytest.raises(TypeError):
            io.fetch(req)


def test_request_requires_entered_session(store_cfg: dict[str, Any]) -> None:
    register("dummy_fetch", DummyFetcher())
    io = DataIoSession(source="dummy_fetch", cfg=store_cfg)
    with pytest.raises(RuntimeError):
        _ = io.request(kind="outside", params={})
