# mxm-dataio

**Unified ingestion, caching, and audit layer for the Money Ex Machina ecosystem.**

`mxm-dataio` provides a minimal, protocol-agnostic structure for interacting with external data systems.  
It records every session, request, and response, enabling complete reproducibility and auditability of all data ingress into MXM — whether from web APIs, file systems, or trading interfaces like Interactive Brokers.

---

## 🧭 Purpose

The package defines a **universal ingestion model**:

```
Session(source="yahoo", as_of="2025-10-07")
    └── Request(kind="daily_bars", params={symbol: "CSPX.L", start: "2010-01-01"})
          └── Response(status="ok", checksum="...", bytes_path=".../responses/<checksum>.bin")
```

Each `Session` groups multiple `Request` objects, each of which produces a `Response`.  
The responses are stored as raw payloads (bytes or serialized objects), while all metadata is recorded in a local SQLite database.

This allows any data source — HTTP, file, streaming, or API — to be captured with the same structure and queried later.

---

## Design Principles

- **Protocol-agnostic**: works with HTTP, FTP, sockets, APIs, or any custom connector.  
- **Reproducible**: every interaction is timestamped, hashed, and stored.  
- **Composable**: adapters can be registered per source (`yahoo`, `ibkr`, etc.).  
- **Lightweight**: no runtime dependencies beyond Python standard library.  
- **Auditable**: full trail of what was fetched, when, and from where.  

---

## Project Layout

```text
mxm-dataio/
 ├── mxm_dataio/
 │    ├── __init__.py
 │    ├── models.py       # Session, Request, Response dataclasses
 │    ├── store.py        # SQLite + filesystem persistence
 │    ├── fetcher.py      # Generic HTTP fetcher
 │    ├── api.py          # Public context manager API (IngestSession)
 │    ├── adapters/       # Optional protocol adapters (http, ibkr, etc.)
 │    └── utils.py
 ├── configs/
 │    └── default.yaml    # Data root under ${mxm.data_root}/dataio/
 ├── tests/
 │    └── test_dataio_basic.py
 ├── pyproject.toml
 └── README.md
```


## Core Concepts

| Entity | Description | Typical fields |
|--------|--------------|----------------|
| **Session** | Logical group of requests made to a single source (e.g. “Yahoo daily fetch”). | `id`, `source`, `as_of`, `status` |
| **Request** | Individual call made under a session. | `endpoint`, `params_hash`, `started_at`, `status` |
| **Response** | Recorded reply from a request. | `status_code`, `checksum`, `bytes_path` |

---

## Example Usage

```python
from pathlib import Path
from mxm_dataio.api import IngestSession

root = Path("~/mxm-data/dev/dataio/default").expanduser()

with IngestSession(root=root, source="yahoo") as sess:
    resp = sess.request(
        endpoint="https://query1.finance.yahoo.com/v7/finance/download/CSPX.L",
        params={"period1": "1262304000", "interval": "1d"},
    )

print("Response stored at:", resp.bytes_path)
```

This produces:
- `dataio.sqlite` — the metadata database  
- `responses/<checksum>.bin` — raw response bytes  

## Adapter Interface

To extend `mxm-dataio` for any external system, implement a simple adapter:

```python
class Fetcher:
    def fetch(self, request: Request) -> bytes:
        ...
```

Register it in the global registry:

```python
from mxm_dataio.registry import register

register("ibkr", IBFetcher())
register("yahoo", YahooFetcher())
```

Now any `IngestSession(source="ibkr")` automatically routes requests through the `IBFetcher`.

## Integration Points

| MXM Package | How it uses `mxm-dataio` |
|--------------|--------------------------|
| **`mxm-datakraken`** | Records regulator and web-based reference data scrapes. |
| **`mxm-refdata`** | Optionally reconciles entities based on stored responses. |
| **`mxm-marketdata`** | Collects price, volume, and event data from APIs (Yahoo, IBKR, etc.) via the same ingestion interface. |

---

## 🧱 Storage Layout

```text
${mxm.data_root}/dataio/
 ├── dataio.sqlite          # Metadata DB
 └── responses/             # Raw payloads (by checksum)
      ├── 1a2b3c...bin
      └── ...
```

---

## Testing

```bash
poetry install
pytest -q
```


## License

All code © Money Ex Machina.  
Released under the MIT License unless otherwise specified.
