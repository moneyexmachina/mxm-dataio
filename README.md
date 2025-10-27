# mxm-dataio
![Version](https://img.shields.io/github/v/release/moneyexmachina/mxm-dataio)
![License](https://img.shields.io/github/license/moneyexmachina/mxm-dataio)
![Python](https://img.shields.io/badge/python-3.12+-blue)
[![Checked with pyright](https://microsoft.github.io/pyright/img/pyright_badge.svg)](https://microsoft.github.io/pyright/)


**Unified ingestion, caching, and audit layer for Money Ex Machina.**

## Overview

`mxm-dataio` is Money Ex Machina’s lightweight **ingestion and audit backbone**.  
It records every external interaction (`Session → Request → Response`),  
persists exact payload bytes, and stores structured metadata in SQLite.

It is designed for **deterministic reproducibility**, **offline caching**,  
and **transparent provenance** across all MXM data sources.

## Architecture at a glance

```
mxm-dataio/
├── DataIoSession      → runtime context (one logical run)
├── Request / Response → atomic data transactions
├── adapters/          → pluggable fetch/send implementations
└── store/             → SQLite-backed metadata and byte storage
```

Each interaction is represented as:

```
Session ─┬─> Request ──> Response
          └─> Request ──> Response
```

Raw bytes and parsed metadata are stored under:
```
<root>/responses/<session>/<hash>.json
<root>/blobs/<session>/<hash>.bin
```

## Core model

| Concept | Role |
|----------|------|
| **Session** | Groups a set of related requests; ensures atomic persistence. |
| **Request** | Deterministic identity of an operation (method + URL + params + headers). |
| **Response** | Archived payload, metadata, and audit fields. |
| **Adapter** | Tiny class implementing `fetch()` or `send()` returning an `AdapterResult`. |
| **Registry** | Runtime mapping from adapter name → adapter instance. |

## Runtime API

### DataIoSession

The main entry point for ingestion or submission tasks.

```python
from mxm_dataio.api import DataIoSession
from mxm_dataio.adapters import HttpFetcher
from mxm_config import load_config
from mxm_dataio.config.config import dataio_view

cfg = load_config(package="mxm-dataio", env="dev", profile="default")
dio_cfg = dataio_view(cfg)

# Register an adapter under a source name
register("http", HttpFetcher())  # implements Fetcher

# Use the session with that source name
with DataIoSession(source="http", cfg=dio_cfg) as io:
    req = io.request(kind="demo", params={"q": "mxm"})
    resp = io.fetch(req)
    print(resp.status, resp.checksum, resp.path)

```

`AdapterResult` objects contain both the raw payload and normalized metadata:
```python

from typing import Any

class AdapterResult:
    data: bytes
    content_type: str | None
    transport_status: int | None
    url: str | None
    elapsed_ms: int | None
    headers: dict[str, str] | None
    adapter_meta: dict[str, Any] | None
```

## Configuration

`mxm-dataio` reads its settings from the **`dataio` subtree**
of the global MXM config. Downstream packages obtain read-only
views via `mxm_config.make_view`.


## Adapters

Adapters provide I/O logic while `mxm-dataio` handles persistence.

Example (simplified):

```python
from typing import Any
from mxm_dataio.adapters import BaseFetcher
from mxm_dataio.types import AdapterResult
import requests

class HttpFetcher(BaseFetcher):
    def fetch(self, url: str, **params) -> AdapterResult:
        r = requests.get(url, params=params)
        return AdapterResult(
            payload=r.content,
            meta={"url": r.url, "headers": dict(r.headers)},
            content_type=r.headers.get("content-type"),
            status_code=r.status_code,
        )
```

Adapters can be registered dynamically:
```python
from mxm_dataio.registry import register_adapter
register_adapter("http", HttpFetcher())
```

### Caching and Volatility

Recent versions introduce a **policy-driven caching system** supporting:

- **Volatile sources** (e.g. JustETF) that change daily  
- **Eternal sources** (e.g. FCA FIRDS) that never mutate after release  
- Fine-grained control via `cache_mode`, `ttl_seconds`, and `as_of_bucket`

Each request/response pair now carries explicit provenance metadata.

#### CacheMode semantics

| Mode | Behavior |
|------|-----------|
| `default` | Use cached data if available and not expired; otherwise refetch |
| `only_if_cached` | Never hit network; raise on cache miss |
| `bypass` | Always refetch and persist new response |
| `revalidate` | Future ETag support; currently same as `default` |
| `never` | Fetch but never persist (ephemeral or side-effect requests) |

#### Provenance fields

| Field | Type | Meaning |
|--------|------|---------|
| `cache_mode` | Enum\[str] | Policy governing cache use |
| `ttl_seconds` | float \| None | Time-to-live in seconds; older entries are refetched |
| `as_of_bucket` | str \| None | Logical “time partition” (e.g. `"2025-10-27"`) |
| `cache_tag` | str \| None | Optional sub-partition (e.g. language `"en"`) |

Example embedded in saved JSON:

```json
"_provenance": {
  "response_id": "resp-123",
  "checksum": "sha256:…",
  "fetched_at": "2025-10-27T10:45:12Z",
  "cache_mode": "default",
  "ttl_seconds": 86400,
  "as_of_bucket": "2025-10-27",
  "cache_tag": "en"
}
```

#### Example usage

```python
from mxm_dataio.api import DataIoSession

with DataIoSession(
    source="justetf",
    cfg=cfg,
    cache_mode="default",
    ttl=86400,
    as_of_bucket="2025-10-27",
) as s:
    req = s.request(kind="http", params={"u": "A"})
    resp = s.fetch(req)
    print(resp.checksum, resp.as_of_bucket)
```

This enables reproducible daily snapshots for volatile sources while preserving
eternal datasets indefinitely.

## Quick examples

### Fetch and cache a resource

```python
session = DataIoSession(cfg=dio_cfg)
result = session.fetch("https://example.com/data.json", fetcher="http")
print(result.status_code)
```

The payload and metadata are stored automatically in SQLite + filesystem.
Subsequent identical requests are served from cache unless `force_refresh=True`.

### Send data to an API

```python
result = session.send("https://api.example.com/upload", data=b"...", sender="http")
print(result.status_code)
```

## Design principles

- **Deterministic:** identical inputs yield identical request IDs.  
- **Auditable:** all payloads and headers persisted for replay.  
- **Minimal dependencies:** pure Python, no ORM or framework assumptions.  
- **Composable:** adapters plug into any MXM package via registry.  
- **Readable data:** SQLite + JSON + raw bytes, human-inspectable.  

## Testing & quality

All tests are pure-Python and hermetic—no network calls.  
Configuration YAMLs are loaded directly from the repo using a temporary  
`MXM_CONFIG_HOME` fixture. The project is validated with:

```
pytest -q
pyright --strict
ruff check .
black --check .
```

## Roadmap

- Async adapters (`aiohttp`, websockets).  
- Multi-backend persistence (S3, DuckDB).  
- Delta auditing and content hashing improvements.  
- CLI for session inspection and cache management.  

## Repository layout

```
mxm_dataio/
  adapters/       → built-in adapter implementations
  config/         → default YAMLs and view helpers
  store/          → persistence backend
  types.py        → protocol and dataclasses
tests/            → pytest suite (hermetic)
```

## License

MIT © Money Ex Machina
Unified ingestion, caching, and audit layer for the Money Ex Machina (MXM) ecosystem. `mxm-dataio` records every interaction with an external system—who/what/when, the exact bytes returned, and optional transport metadata—so downstream packages are reproducible and auditable.
