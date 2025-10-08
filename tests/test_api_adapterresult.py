"""Tests for AdapterResult handling in mxm_dataio.api.DataIoSession."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mxm_dataio.adapters import AdapterResult
from mxm_dataio.api import DataIoSession
from mxm_dataio.models import Request, RequestMethod, ResponseStatus
from mxm_dataio.registry import clear_registry, register
from mxm_dataio.store import Store

# --------------------------------------------------------------------------- #
# Dummy adapters exercising different return shapes
# --------------------------------------------------------------------------- #


class FetcherWithMeta:
    """Returns AdapterResult (data + meta) from fetch()."""

    source = "fetch_meta"

    def fetch(self, request: Request) -> AdapterResult:
        data = f"F-META:{request.hash}".encode("utf-8")
        return AdapterResult(
            data=data,
            content_type="application/octet-stream",
            transport_status=200,
            url="https://example.test/resource",
            elapsed_ms=5,
            headers={"x-test": "1"},
            adapter_meta={"note": "ok"},
        )

    def describe(self) -> str:
        return "Fetcher returning AdapterResult"

    def close(self) -> None:
        pass


class FetcherRawBytes:
    """Returns raw bytes from fetch()."""

    source = "fetch_bytes"

    def fetch(self, request: Request) -> bytes:
        return f"F-BYTES:{request.hash}".encode("utf-8")

    def describe(self) -> str:
        return "Fetcher returning raw bytes"

    def close(self) -> None:
        pass


class SenderWithMeta:
    """Returns AdapterResult (data + meta) from send()."""

    source = "send_meta"

    def send(self, request: Request, payload: bytes) -> AdapterResult:
        _ = request
        data = b"SENT:" + payload
        return AdapterResult(
            data=data,
            content_type="application/octet-stream",
            transport_status=202,
            adapter_meta={"ack": True},
        )

    def describe(self) -> str:
        return "Sender returning AdapterResult"

    def close(self) -> None:
        pass


class SenderDictResult:
    """Returns a mapping (treated as JSON payload) from send()."""

    source = "send_dict"

    def send(self, request: Request, payload: bytes) -> dict[str, Any]:
        _ = request
        return {"accepted": True, "len": len(payload)}

    def describe(self) -> str:
        return "Sender returning dict"

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:  # noqa: PT019
    clear_registry()
    yield
    clear_registry()


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


def test_fetch_adapterresult_writes_sidecar(
    store_cfg: dict[str, Any], store: Store
) -> None:
    register("fetch_meta", FetcherWithMeta())

    with DataIoSession(source="fetch_meta", cfg=store_cfg) as io:
        req = io.request(kind="k", params={"p": 1})
        resp = io.fetch(req)

    assert resp.status == ResponseStatus.OK
    assert resp.checksum is not None
    sidecar = store.responses_dir / f"{resp.checksum}.meta.json"
    assert sidecar.exists()

    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    assert meta["content_type"] == "application/octet-stream"
    assert meta["transport_status"] == 200
    assert meta["adapter_meta"] == {"note": "ok"}


def test_fetch_raw_bytes_has_no_sidecar(
    store_cfg: dict[str, Any], store: Store
) -> None:
    register("fetch_bytes", FetcherRawBytes())

    with DataIoSession(source="fetch_bytes", cfg=store_cfg) as io:
        req = io.request(kind="k", params={"p": 2})
        resp = io.fetch(req)

    assert resp.status == ResponseStatus.OK
    assert resp.checksum is not None
    assert not (store.responses_dir / f"{resp.checksum}.meta.json").exists()


def test_send_adapterresult_writes_sidecar_ack(
    store_cfg: dict[str, Any], store: Store
) -> None:
    register("send_meta", SenderWithMeta())

    with DataIoSession(source="send_meta", cfg=store_cfg) as io:
        req = io.request(kind="post", method=RequestMethod.POST, body={"x": 1})
        resp = io.send(req, payload=b"abc")

    assert resp.status == ResponseStatus.ACK
    assert resp.checksum is not None
    sidecar = store.responses_dir / f"{resp.checksum}.meta.json"
    assert sidecar.exists()
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    assert meta["transport_status"] == 202
    assert meta["adapter_meta"] == {"ack": True}


def test_send_dict_result_stores_json_payload_no_sidecar(
    store_cfg: dict[str, Any], store: Store
) -> None:
    register("send_dict", SenderDictResult())

    with DataIoSession(source="send_dict", cfg=store_cfg) as io:
        req = io.request(kind="post", method=RequestMethod.POST, body={"y": 2})
        resp = io.send(req, payload={"hello": "world"})

    assert resp.status == ResponseStatus.ACK
    assert resp.path is not None
    payload = Path(resp.path).read_bytes()
    meta_path = store.responses_dir / f"{resp.checksum}.meta.json"
    assert not meta_path.exists()

    # Payload is deterministic JSON bytes of the mapping returned by the adapter
    obj = json.loads(payload.decode("utf-8"))
    assert obj == {"accepted": True, "len": len(b'{"hello":"world"}')}


def test_cache_hit_with_adapterresult_reuses_response(
    store_cfg: dict[str, Any], store: Store
) -> None:
    register("fetch_meta", FetcherWithMeta())

    with DataIoSession(source="fetch_meta", cfg=store_cfg) as io:
        r1 = io.request(kind="k", params={"same": 1})
        resp1 = io.fetch(r1)
        # Identical request → cache hit
        r2 = io.request(kind="k", params={"same": 1})
        resp2 = io.fetch(r2)

    assert resp2.id == resp1.id
    # Sidecar still present and unchanged
    sidecar = store.responses_dir / f"{resp1.checksum}.meta.json"
    assert sidecar.exists()
    before = sidecar.read_text(encoding="utf-8")
    after = sidecar.read_text(encoding="utf-8")
    assert before == after


def test_use_cache_false_new_response_same_payload(
    store_cfg: dict[str, Any], store: Store
) -> None:
    register("fetch_meta", FetcherWithMeta())

    with DataIoSession(source="fetch_meta", cfg=store_cfg, use_cache=False) as io:
        r1 = io.request(kind="k", params={"same": 2})
        resp1 = io.fetch(r1)
        r2 = io.request(kind="k", params={"same": 2})
        resp2 = io.fetch(r2)

    # Different response ids because caching is disabled
    assert resp1.id != resp2.id
    # But identical payload → same checksum/path; sidecar remains idempotent
    assert resp1.checksum == resp2.checksum
    assert resp1.path == resp2.path
    sidecar = store.responses_dir / f"{resp1.checksum}.meta.json"
    assert sidecar.exists()
