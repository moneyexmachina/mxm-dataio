# mxm-dataio

![Version](https://img.shields.io/github/v/release/moneyexmachina/mxm-dataio)
![License](https://img.shields.io/github/license/moneyexmachina/mxm-dataio)
![Python](https://img.shields.io/badge/python-3.13+-blue)
[![Checked with pyright](https://microsoft.github.io/pyright/img/pyright_badge.svg)](https://microsoft.github.io/pyright/)

Reproducible and auditable external data acquisition for Money Ex Machina.

## Purpose
`mxm-dataio` records each external data request, any external response observed for it, and—when successfully satisfied—whether that response was newly acquired or reused.

It provides:

- deterministic logical-request identity;
- policy-aware response reuse;
- explicit source-adapter acquisition;
- exact content-addressed payload storage;
- PostgreSQL-backed Request, Response, and Resolution provenance;
- one composed runtime boundary for applications.

DataIO preserves adapter-boundary payload bytes without interpreting their domain content. Decoding, normalization, and dataset semantics remain application
responsibilities.

## Installation

```bash
pip install mxm-dataio
```

## Usage

Applications compose DataIO from concrete dependencies and pass the adapter
explicitly for each resolution:

```python
from mxm.dataio import CacheMissError, ResolveResult, compose_dataio
from mxm.dataio.models import CacheMode

dataio = compose_dataio(
    database=database,
    payload_store=payload_store,
    timestamp_source=timestamp_source,
)

result: ResolveResult = dataio.resolve(
    source="databento",
    kind="databento.timeseries.get_range",
    params={
        "dataset": "GLBX.MDP3",
        "schema": "ohlcv-1d",
        "symbols": "ES.c.0",
        "start": "2026-01-01",
        "end": "2026-02-01",
    },
    adapter=databento_adapter,
    cache_mode=CacheMode.DEFAULT,
)

payload = result.data
```

`ResolveResult` returns the attempted `Request`, the `Response` supplying the
payload, its `Resolution` when the request was successfully satisfied, and the
exact payload bytes. `CacheMissError` reports an `ONLY_IF_CACHED` request for
which no reusable response exists.

## Architecture

```text
application composition
    ↓
compose_dataio(...)
    ↓
DataIO.resolve(...)
    ↓
resolution workflow
    ├── reuse
    └── acquisition
```

The package-level composition boundary is:

```python
compose_dataio(
    *,
    database: PostgresDatabase,
    payload_store: PayloadStore,
    timestamp_source: Callable[[], TSNSScalar],
) -> DataIO
```

The application supplies:

- a configured `PostgresDatabase`;
- a concrete `PayloadStore`;
- a timestamp source;
- explicit source adapters.

The application owns configuration, secrets, adapter construction, and adapter
lifecycle. DataIO performs no adapter discovery and has no global registry.

`DataIO.resolve()` is a thin façade over the resolution workflow:

1. persist one Request occurrence;
2. attempt policy-aware reuse;
3. acquire through the explicit adapter when policy permits;
4. persist relational provenance;
5. return the exact payload bytes with the associated domain records.

No PostgreSQL transaction spans adapter or payload-store I/O. A successful new
Response and its ACQUIRED Resolution are committed atomically.

## Domain Model

| Concept | Role |
|---|---|
| `Request` | One logical-question occurrence and its reuse-policy context |
| `Response` | One acquired external observation and payload identity |
| `Resolution` | Records whether a Request was satisfied by acquisition or reuse |
| `ResolveResult` | Correlates the Request, Response, Resolution, and exact bytes |
| `Fetcher` | Source identity plus external acquisition behavior |
| `PayloadStore` | Immutable content-addressed payload persistence |

A Request owns `source`, `cache_mode`, `ttl_seconds`, `as_of_bucket`, and
`cache_tag`. Its deterministic hash is derived only from `kind` and `params`.

Only `ResponseStatus.OK` Responses can resolve Requests or be reused. An ERROR
Response remains a durable external observation with a stored payload, but it
does not receive a Resolution.

## Reuse Policy

- `DEFAULT` attempts reuse and acquires on a miss.
- `ONLY_IF_CACHED` attempts reuse and raises `CacheMissError` on a miss.
- `BYPASS` skips reusable-response selection and acquires.
- `ttl_seconds=None` applies no freshness cutoff.
- A finite non-negative TTL limits acceptable Response age whenever reuse is
  attempted.

Reuse is partitioned by source, logical-request hash, `as_of_bucket`, and
`cache_tag`. Missing or corrupt cached payloads are not reusable.

## Adapters

A fetch adapter implements the narrow structural contract:

```python
from mxm.dataio.models import AdapterResult, Request

class ExampleFetcher:
    source = "example"

    def fetch(self, request: Request) -> AdapterResult:
        ...
```

Adapters translate source-specific operations into `AdapterResult`. DataIO
preserves `AdapterResult.data` exactly, records generic acquisition metadata,
and treats source-specific metadata as opaque.

## Persistence

Structured Request, Response, and Resolution records are stored in PostgreSQL.
Exact payload bytes are stored through `PayloadStore` and addressed by their
SHA-256 checksum. Storage paths, object keys, URLs, and clients do not escape
the payload boundary.

The repository includes PostgreSQL and S3 integration coverage. Production
wiring to cloud PostgreSQL, S3, and the refounded `mxm-secrets` belongs to the
consuming application and is intentionally outside `mxm-dataio`.

## Integration Status

The DataIO capability architecture and package-level runtime boundary are
complete. `mxm-moneymachine` has not yet been migrated from the legacy
`DataIoSession` API to `DataIO.resolve()`.

## Design Principles

- **Explicit composition:** Dependencies and adapters are supplied by the
  application.

- **Deterministic identity:** Equivalent logical questions have stable hashes.

- **Auditable provenance:** Request occurrences, external observations, and
  successful resolutions are distinct records.

- **Exact payload preservation:** DataIO stores the bytes produced at the
  adapter boundary without semantic transformation.

- **Minimal implicit behavior:** Reuse policy, transaction boundaries, and
  source identity are explicit.

## Development

```bash
poetry install

make check
```

`make check` runs formatting checks, linting, strict type checking, and the
default unit-test suite. PostgreSQL and S3 integration suites are available
through their dedicated Make targets.

## License

MIT License. See [LICENSE](LICENSE).
