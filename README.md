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
from mxm_dataio import DataIoSession
from mxm_dataio.adapters import HttpFetcher
from mxm_config import load_config
from mxm_dataio.config import dataio_view

cfg = load_config(package="mxm-dataio", env="dev", profile="default")
dio_cfg = dataio_view(cfg)

session = DataIoSession(cfg=dio_cfg)

fetcher = HttpFetcher()
result = session.fetch("https://httpbin.org/get", fetcher)
print(result.status_code, len(result.payload))
```

`AdapterResult` objects contain both the raw payload and normalized metadata:
```python
from typing import Any

class AdapterResult:
    payload: bytes | str
    meta: dict[str, Any]
    content_type: str | None
    status_code: int | None
```

## Configuration

`mxm-dataio` reads its settings from the **`mxm_dataio` subtree**
of the global MXM config. Downstream packages obtain read-only
views via `mxm_config.make_view`.

### YAML shape

```yaml
# central paths (shared across packages)
paths:
  data_root: ${paths.data_root_base}/${mxm_env}/dataio/${mxm_profile}

# mxm-dataio-specific subtree
mxm_dataio:
  paths:
    root: ${paths.data_root}
    db_path: ${paths.data_root}/dataio.sqlite
  http:
    timeout_s: 20.0
    retries: { max_attempts: 3, backoff_ms: [500,1000,2000] }
    politeness_ms: 1000
    verify_ssl: true
    headers: { User-Agent: mxm-dataio/0.3 (+contact@moneyexmachina.com) }
  cache: { use_cache: true, write_cache: true, force_refresh: false }
  serialization: { response_format: raw, compression: none, hash_algo: sha256 }
  audit: { record_request: true, record_response: true, record_parse_log: true }
```

### View helpers

```python
from mxm_dataio.config import (
    dataio_view,        # full subtree
    dataio_paths_view,  # mxm_dataio.paths
    dataio_http_view,   # mxm_dataio.http
)

dio = dataio_view(cfg)
root = dataio_paths_view(cfg).root
timeout = dataio_http_view(cfg).timeout_s
```

Views are **read-only** and safe to share between modules.

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
