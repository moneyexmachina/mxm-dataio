# mxm-dataio

![Version](https://img.shields.io/github/v/release/moneyexmachina/mxm-dataio)
![License](https://img.shields.io/github/license/moneyexmachina/mxm-dataio)
![Python](https://img.shields.io/badge/python-3.13+-blue)
[![Checked with pyright](https://microsoft.github.io/pyright/img/pyright_badge.svg)](https://microsoft.github.io/pyright/)

## Purpose

`mxm-dataio` provides a unified ingestion, caching, and audit layer for the Money Ex Machina ecosystem.

It records every external interaction (`Session → Request → Response`), persists exact payload bytes, and stores structured metadata in SQLite.

The system is designed for deterministic reproducibility, offline caching, and transparent data provenance.

## Installation

```bash
pip install mxm-dataio
```

## Usage

### Basic session usage

```python
from mxm.dataio.api import DataIoSession
from mxm.dataio.adapters import HttpFetcher
from mxm.config import load_config
from mxm.dataio.config.config import dataio_view

cfg = load_config(package="mxm-dataio", env="dev", profile="default")
dio_cfg = dataio_view(cfg)

with DataIoSession(source="http", cfg=dio_cfg) as io:
    req = io.request(kind="demo", params={"q": "mxm"})
    resp = io.fetch(req)
    print(resp.status, resp.checksum, resp.path)
```

### Fetch and cache a resource

```python
session = DataIoSession(cfg=dio_cfg)
result = session.fetch("https://example.com/data.json", fetcher="http")
print(result.status_code)
```

### Send data to an API

```python
result = session.send("https://api.example.com/upload", data=b"...", sender="http")
print(result.status_code)
```

## Overview

`mxm-dataio` is a lightweight ingestion and audit backbone.

It provides:

- deterministic request identity
- persistent raw payload storage
- structured metadata in SQLite
- adapter-based I/O abstraction

## Architecture

```
mxm-dataio/
├── DataIoSession
├── Request / Response
├── adapters/
└── store/
```

Each interaction:

```
Session ─┬─> Request ──> Response
         └─> Request ──> Response
```

Storage layout:

```
<root>/responses/<session>/<hash>.json
<root>/blobs/<session>/<hash>.bin
```

## Core model

| Concept | Role |
|--------|------|
| Session | Groups related requests |
| Request | Deterministic operation identity |
| Response | Archived payload + metadata |
| Adapter | I/O implementation |
| Registry | Adapter mapping |

## Configuration

Configuration is loaded via `mxm-config` and read from the `dataio` subtree.

Default seed location:

```
src/mxm/dataio/_data/seed/dataio/
```

## Adapters

Adapters implement fetch/send logic while `mxm-dataio` handles persistence.

```python
from mxm.dataio.adapters import BaseFetcher
from mxm.dataio.types import AdapterResult
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

## Caching and Provenance

Supports policy-driven caching:

- volatile vs eternal data
- TTL-based expiry
- reproducible snapshots

Example provenance:

```json
{
  "_provenance": {
    "checksum": "sha256:…",
    "fetched_at": "2025-10-27T10:45:12Z",
    "cache_mode": "default"
  }
}
```

## Design principles

- Deterministic
- Auditable
- Minimal dependencies
- Composable
- Human-readable storage

## Development

```bash
make check
```

This runs:

- Ruff (lint + imports)
- Black (format)
- Isort (consistency)
- Pyright (typing)
- Pytest (tests)

## Roadmap

- Async adapters
- Multi-backend storage
- Delta auditing improvements
- CLI for inspection

## License

MIT License. See [LICENSE](LICENSE).

