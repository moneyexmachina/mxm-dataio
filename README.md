# mxm-dataio

Unified ingestion, caching, and audit layer for the Money Ex Machina (MXM) ecosystem. `mxm-dataio` records every interaction with an external system—who/what/when, the exact bytes returned, and optional transport metadata—so downstream packages are reproducible and auditable.

## Purpose & scope

**What it is**
- A lightweight, protocol-agnostic layer that models external interactions as `Session → Request → Response`, persists metadata in SQLite, and payloads as files.
- A small registry + adapter interface so applications can plug in sources (web APIs, files, brokers).
- A simple runtime API (`DataIoSession`) to run fetch/send operations with automatic persistence and optional caching.

**What it’s not**
- Not a domain database for market data or reference data.
- Not a parsing/ETL framework. Domain packages (e.g., `mxm-marketdata`, `mxm-refdata`) parse/normalize and store into their own schemas, while linking back to `mxm-dataio` for provenance.

## Architecture at a glance

```
App (e.g. mxm-datakraken)
  │
  ├─ mxm-config → cfg (machine/env/profile-specific paths)
  │
  └─ mxm-dataio
      ├─ models.py        (Session, Request, Response)
      ├─ store.py         (SQLite metadata + filesystem payloads)
      ├─ adapters.py      (MXMDataIoAdapter + Fetcher/Sender/Streamer, AdapterResult)
      ├─ registry.py      (register/resolve adapters)
      └─ api.py           (DataIoSession: runtime orchestration)
```

**Storage layout**
```
${paths.data_root}/
  dataio.sqlite
  responses/
    <sha256>.bin         # raw bytes (exact payload)
    <sha256>.meta.json   # optional sidecar metadata (transport info)
```

## Core concepts

- **Session** — groups related Requests (e.g., a daily run). Fields include `source`, `mode`, `as_of`, `started_at`, `ended_at`.
- **Request** — deterministic identity of an external call: `kind`, `method`, `params`, optional `body`, and a stable `hash` used for caching.
- **Response** — what came back: `status`, `checksum`, `size_bytes`, `sequence` (for future streaming), `path` of payload.
- **Adapter** — small class that knows how to talk to a system, via capabilities:
  - `Fetcher.fetch(request) -> bytes | AdapterResult`
  - `Sender.send(request, payload: bytes) -> bytes | dict | AdapterResult`
  - `Streamer.stream(request)` (future)
- **Registry** — process-local map of `source` → adapter instance.
- **DataIoSession** — context manager that opens a Session, creates Requests, resolves the adapter, executes I/O, persists Responses, and optionally returns cached results by Request hash.

## Design principles

- **Protocol-agnostic & dependency-light**: stdlib only (sqlite3, pathlib, json, hashlib).
- **Deterministic & auditable**: stable hashing, checksums, reproducible layout.
- **Queryable & fast**: SQLite indexes on hot paths (`requests.hash`, `requests.session_id`, `responses.request_id`, `responses.created_at`, `responses.checksum`) for snappy cache lookups and listing.
- **Separation of concerns**:
  - `mxm-dataio` archives raw bytes + provenance.
  - Domain packages parse/normalize into queryable schemas, while storing `response_id`/`checksum` for provenance.
- **Extensible**: adapters are tiny; sidecar metadata via `AdapterResult` requires no DB migration.

## Adapters & registry

```python
# adapters.py (excerpt)
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from mxm_dataio.models import Request

@runtime_checkable
class MXMDataIoAdapter(Protocol):
    source: str
    def describe(self) -> str: ...
    def close(self) -> None: ...

@runtime_checkable
class Fetcher(MXMDataIoAdapter, Protocol):
    def fetch(self, request: Request) -> bytes: ...

@runtime_checkable
class Sender(MXMDataIoAdapter, Protocol):
    # DataIoSession.send accepts any of these:
    # - bytes               (raw)
    # - dict[str, Any]      (stored as deterministic JSON)
    # - AdapterResult       (bytes + metadata sidecar)
    def send(self, request: Request, payload: bytes) -> Any: ...

@dataclass(slots=True)
class AdapterResult:
    data: bytes
    content_type: str | None = None
    encoding: str | None = None
    transport_status: int | None = None
    url: str | None = None
    elapsed_ms: int | None = None
    headers: dict[str, str] | None = None
    adapter_meta: dict[str, Any] | None = None
    def meta_dict(self) -> dict[str, Any]:
        return {
            k: v for k, v in {
                "content_type": self.content_type,
                "encoding": self.encoding,
                "transport_status": self.transport_status,
                "url": self.url,
                "elapsed_ms": self.elapsed_ms,
                "headers": self.headers,
                "adapter_meta": self.adapter_meta,
            }.items() if v is not None
        }
```

```python
# registry.py (excerpt)
from mxm_dataio.adapters import MXMDataIoAdapter

def register(name: str, adapter: MXMDataIoAdapter) -> None: ...
def resolve_adapter(name: str) -> MXMDataIoAdapter: ...
def unregister(name: str) -> None: ...
def clear_registry() -> None: ...
def list_registered() -> list[str]: ...
def describe_registry() -> str: ...
```

## Runtime API: DataIoSession

```python
from mxm_dataio.api import DataIoSession
from mxm_dataio.models import RequestMethod

with DataIoSession(source="justetf", cfg=cfg) as io:
    req = io.request(kind="etf_profile", params={"isin": "LU0274211480"})
    resp = io.fetch(req)  # Adapter.fetch → bytes or AdapterResult
    print(resp.status, resp.path)  # 'ok', .../responses/<checksum>.bin
```

- **Capability checks** — `.fetch()` requires a `Fetcher` adapter; `.send()` requires a `Sender`. Raises `TypeError` otherwise.
- **Caching** — enabled by default (`use_cache=True`). If a previous Request with the same `hash` exists, returns the most recent Response (no duplicate I/O).
- **Sidecar metadata** — if an adapter returns `AdapterResult`, its `meta_dict()` is written to `responses/<checksum>.meta.json` (deterministic JSON, readable Unicode).

**Store helpers** (used internally by `DataIoSession`):
- `Store.mark_session_ended(session_id, ended_at)` — finalize session end time.
- `Store.get_cached_response_by_request_hash(request_hash)` — return the latest cached `Response` for an identical Request.

## Configuration (via mxm-config)

Each application owns where its data lives.

```python
from mxm_config import load_config
cfg = load_config("mxm-datakraken", env="dev", profile="default")

# cfg["paths"] should include:
#   data_root      -> base folder for this app/env/profile
#   db_path        -> e.g. ${data_root}/dataio.sqlite
#   responses_dir  -> e.g. ${data_root}/responses
```

Recommended defaults inside the *app’s* `config/default.yaml`:

```yaml
paths:
  data_root: ${paths.data_root_base}/${mxm_env}/datakraken/${mxm_profile}
  db_path: ${paths.data_root}/dataio.sqlite
  responses_dir: ${paths.data_root}/responses
```

## Quick start examples

### 1) Register an adapter and fetch

```python
from mxm_config import load_config
from mxm_dataio.registry import register
from mxm_dataio.api import DataIoSession
from mxm_dataio.adapters import AdapterResult
from mxm_dataio.models import Request

class JustETFFetcher:
    source = "justetf"
    def fetch(self, request: Request) -> AdapterResult:
        data = b'{"name":"Example ETF"}'
        return AdapterResult(
            data=data,
            content_type="application/json",
            transport_status=200,
            url="https://api.justetf.example/etf?isin=LU0274211480",
            headers={"x-ratelimit-remaining": "99"},
        )
    def describe(self) -> str: return "JustETF demo fetcher"
    def close(self) -> None: pass

cfg = load_config("mxm-datakraken", env="dev", profile="default")
register("justetf", JustETFFetcher())

with DataIoSession(source="justetf", cfg=cfg) as io:
    req = io.request(kind="etf_profile", params={"isin": "LU0274211480"})
    resp = io.fetch(req)

print(resp.status, resp.path)
# Sidecar metadata at .../responses/<checksum>.meta.json
```

### 2) Send with metadata

```python
from mxm_dataio.models import RequestMethod, Request

class BrokerSender:
    source = "ibkr"
    def send(self, request: Request, payload: bytes) -> AdapterResult:
        # pretend we placed an order
        return AdapterResult(
            data=b'{"order_id":12345,"status":"accepted"}',
            content_type="application/json",
            transport_status=202,
            adapter_meta={"env": "paper"},
        )
    def describe(self) -> str: return "Broker demo sender"
    def close(self) -> None: pass

register("ibkr", BrokerSender())
with DataIoSession(source="ibkr", cfg=cfg) as io:
    req = io.request(kind="place_order", method=RequestMethod.POST, body={"symbol":"CLZ5"})
    resp = io.send(req, payload=b'{"qty":1,"side":"BUY"}')
```

### 3) Read payload + sidecar metadata

```python
from pathlib import Path
from mxm_dataio.store import Store

store = Store.get_instance(cfg)
raw = Path(resp.path).read_bytes()
meta = store.read_metadata(resp.checksum)  # raises if none was written
```

### 4) Caching behavior

```python
with DataIoSession(source="justetf", cfg=cfg, use_cache=True) as io:
    r1 = io.request(kind="k", params={"x": 1}); resp1 = io.fetch(r1)
    r2 = io.request(kind="k", params={"x": 1}); resp2 = io.fetch(r2)
assert resp2.id == resp1.id  # reused cached Response
```

## Testing & quality

- **Test suites** (fast, isolated, tmp paths):
  - `tests/test_store.py` + `test_store_extended.py` — schema, atomicity, payload I/O, singleton, robustness.
  - `tests/test_registry.py` — register/resolve/duplicate/unregister/describe.
  - `tests/test_api.py` — lifecycle, fetch/send persistence, cache, capability guards.
  - `tests/test_store_metadata.py` + `tests/test_api_adapterresult.py` — sidecar metadata roundtrip, AdapterResult paths.
- **Style & Type**: Black, Ruff, Pyright `--strict`.
- No network in tests; adapters are dummies.

## Design decisions (why this way?)

- **No SQLAlchemy** in `mxm-dataio`: the metadata model is tiny, stable, and well-served by sqlite3. Domain packages may use ORMs for their richer schemas.
- **Sidecar metadata (AdapterResult)**: captures transport facts (status, headers, URL, elapsed) without DB migrations; JSON is deterministic and Unicode-friendly.
- **One adapter per DataIoSession**: keeps audit trail clear and deterministic.
- **Caching by Request hash**: avoids duplicate I/O for identical requests; still returns the raw bytes exactly as previously archived.

## Roadmap

- Replay helpers in `Store` (e.g., list/iterate responses by request; as-of replay).
- CLI tools: list sessions/requests, inspect responses, dump payloads.
- Migrations: schema versioning via a small `meta` table.
- Compression / TTL: optional payload compression, retention policies.
- Streaming: finalize `Streamer` as `AsyncIterator[bytes]`, persist sequenced frames.
- Reference adapters: `LocalFileFetcher`, minimal `HttpFetcher` (stdlib).

## Repository layout

```
mxm_dataio/
  __init__.py
  api.py
  adapters.py
  models.py
  registry.py
  store.py
tests/
  test_models.py
  test_store.py
  test_store_extended.py
  test_registry.py
  test_api.py
  test_store_metadata.py
  test_api_adapterresult.py
config/
  default.yaml    # (in consumer apps; shown here for reference)
```

## Versioning & compatibility

- Python 3.11+ recommended (tested with latest stable).
- No external runtime dependencies besides `mxm-config` for path resolution.
- Semantic versioning once published; internal changes guarded by tests.

## Using with domain packages

- **mxm-datakraken** (reference data): adapters fetch raw web/regulator data; package parses into normalized entities; every entity stores `source_response_id`.
- **mxm-marketdata** (prices/volumes/CA): adapters fetch OHLCV; package writes columnar/DB; rows carry `source_response_id` for audit/replay.
- **mxm-refdata**: reconciliation logic; may replay past `Response`s by `as_of`.

## License
MIT License. See [LICENSE](LICENSE).
