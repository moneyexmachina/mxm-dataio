# mxm-dataio

![Version](https://img.shields.io/github/v/release/moneyexmachina/mxm-dataio)
![License](https://img.shields.io/github/license/moneyexmachina/mxm-dataio)
![Python](https://img.shields.io/badge/python-3.13+-blue)
[![Checked with pyright](https://microsoft.github.io/pyright/img/pyright_badge.svg)](https://microsoft.github.io/pyright/)

## Purpose

`mxm-dataio` provides a unified ingestion, caching, and audit layer for the
Money Ex Machina ecosystem.

It records each external interaction as a Request, its acquired Response, and
the Resolution connecting them. Structured records are persisted in
PostgreSQL, while exact payload bytes are persisted through a `PayloadStore`.

The system is designed for deterministic reproducibility, offline caching, and transparent data provenance.

## Installation

```bash
pip install mxm-dataio
```

## Usage

### Composition

```python
from mxm.dataio import compose_dataio

dataio = compose_dataio(
    database=database,
    payload_store=payload_store,
    timestamp_source=timestamp_source,
)
```

The application owns configuration, secrets, dependency construction, adapter
selection, and adapter lifecycle. The `DataIO` façade binds the configured
database, payload store, and clock, then delegates request resolution to the
internal resolution workflow.

## Overview

`mxm-dataio` is a lightweight ingestion and audit backbone.

It provides:

- deterministic request identity
- persistent raw payload storage
- structured metadata in PostgreSQL
- adapter-based I/O abstraction

## Architecture

```
mxm-dataio/
├── DataIO
├── Request / Response / Resolution
├── adapters
├── payload stores
└── PostgreSQL repositories
```

Each interaction:

```
Request ──> Response
   │           │
   └─> Resolution
```

Each Request occurrence carries its source, cache policy, TTL, and opaque reuse
partition coordinates. Its logical-question hash remains derived only from
`kind + params`.

## Core model

| Concept | Role |
|--------|------|
| Request | Logical question occurrence and resolution context |
| Response | External observation and payload identity |
| Resolution | Acquired-or-reused provenance |
| Adapter | I/O implementation |
| PayloadStore | Exact payload-byte persistence |

## Configuration

Configuration remains application-owned and is not part of the DataIO
capability.

## Adapters

Adapters implement fetch/send logic while `mxm-dataio` handles persistence.

```python
from mxm.dataio.adapters import Fetcher
from mxm.dataio.models import AdapterResult, Request

class ExampleFetcher:
    source = "example"

    def fetch(self, request: Request) -> AdapterResult:
        ...
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

