# Changelog

All notable changes to this project will be documented in this file.

The format is based on **Keep a Changelog**, and this project adheres to **Semantic Versioning**.

## [0.3.0] – 2025-10-27

### Added
- **Policy-driven caching system** with `CacheMode` enum:
  - `default` – use cached data if fresh, otherwise refetch  
  - `only_if_cached` – never hit network; raise on cache miss  
  - `bypass` – always fetch new data  
  - `revalidate` – stub for future ETag support  
  - `never` – fetch but never persist (ephemeral requests)
- **Time-aware caching** via optional `ttl_seconds` field controlling cache freshness.
- **Bucketed caching** using `as_of_bucket` for versioned or daily snapshots.
- **Cache tag partitioning** (`cache_tag`) to distinguish parallel caches (e.g. locale "en"/"de").
- **Response provenance** now mirrors request metadata (`cache_mode`, `ttl_seconds`, `as_of_bucket`, `cache_tag`, `fetched_at`) for self-contained auditability.
- **Store.get_cached_response_by_request_hash_and_bucket()** method for precise lookup by request hash + bucket.
- **CacheStore protocol stub** (`mxm_dataio/cache.py`) introducing forward-compatibility for separating ephemeral and archival storage.
- Comprehensive **pytest suite** covering TTL expiration, bucket partitioning, and cache policy semantics.
- Updated **README.md** with a new “Caching and Volatility” section.

### Changed
- `DataIoSession.fetch()` and `send()` now respect `cache_mode`, `ttl`, and `as_of_bucket` policy knobs.
- `persist_result_as_response()` copies caching context into each `Response` for provenance.
- Default behavior remains identical to pre-0.3.0 (`cache_mode="default"`, no TTL, no bucket).

### Fixed
- Eliminated stale-cache reuse beyond TTL expiry.
- Correctly raise `RuntimeError` on cache miss under `CacheMode.ONLY_IF_CACHED`.
- Verified deterministic hash composition excludes TTL and cache-mode policy (only `as_of_bucket` and `cache_tag` affect the key).

### Internal
- All new code validated with `pytest -q` and `pyright --strict`.
- Project formatting aligned with `ruff` and `black`.

---

> **Release summary:**  
> `mxm-dataio v0.3.0` introduces a fully time-aware, bucketed caching system—turning the ingestion layer into a deterministic archival and volatility-aware cache for the MXM ecosystem.
## [0.2.2] – 2025-10-22
### Changed
- Simplified configuration structure:
  - Removed redundant top-level `mxm_dataio:` subtree.
  - Reduced defaults to a minimal `paths` and `cache` section only.
  - Updated environment and profile YAMLs to use nested keys (`dataio:`) for correct OmegaConf merging.
- Updated `config.py` to expose a single `dataio_view(cfg)` helper based on `mxm_config.make_view`.
- Refactored `Store` and `DataIoSession` to accept an `MXMConfig` directly, supporting dot-access instead of raw mappings.
- Adapted all tests for the new config shape and `MXMConfig` interface.

### Fixed
- Environment and profile overrides now merge correctly (e.g. `prod → use_cache: false`, `research → db_path`).
- Eliminated dotted-key override bug in YAML merging.

### Internal
- All tests green again under the new configuration model.
- Aligned structure with `mxm-datakraken` to support package-subtree config and view slicing.
## [0.2.1] – 2025-10-22

### Added
- Introduced **`make_view()`** helper in `mxm-config` for creating typed, read-only sub-config views of the global MXM configuration tree.  
  Enables downstream packages to obtain focused configuration scopes (`mxm_dataio`, `mxm_dataraken`, etc.) via `mxm_config.make_view(cfg, "mxm_dataio")`.
- Added package-specific view helpers to `mxm-dataio`:
  - `dataio_view(cfg)` – returns the full `mxm_dataio` subtree.
  - `dataio_paths_view(cfg)` – returns the `mxm_dataio.paths` view.
  - `dataio_http_view(cfg)` – returns the `mxm_dataio.http` view.
- New hermetic test suite verifying correct YAML loading via local `MXM_CONFIG_HOME`
  and validating all view helpers with `pyright` and `pytest`.

### Changed
- Simplified `mxm_dataio/config/config.py` to remove implicit auto-loading and rely on explicit `load_config()` and view helpers.
- Updated default configuration YAMLs to the unified **package-subtree layout** (`mxm_dataio:` block under top level).
- Rewrote `README.md` for clarity, shortening and aligning documentation style with current MXM standards.

### Fixed
- Ensured all tests run without dependence on globally installed configs.
- Verified full `pyright`, `ruff`, and `black` compliance.

## [0.2.0] — 2025-10-21

### Added
- **`py.typed` marker** to declare the package as type-checked and fully typed for downstream consumers.
- Comprehensive **Pyright coverage** across all modules (`models.py`, `adapters.py`, `api.py`, and `store.py`).
- Extended test coverage for `AdapterResult` metadata persistence:
  - `.meta.json` sidecars (content type, headers, transport metadata).
  - Cache idempotence and deterministic persistence validation.

### Changed
- **Unified adapter protocol**:  
  All adapter capabilities (`Fetcher`, `Sender`, `Streamer`) now return a single, rich data envelope:
  ```python
  AdapterResult(data: bytes, *, content_type=None, encoding=None,
                transport_status=None, url=None, elapsed_ms=None,
                headers=None, adapter_meta=None)
  ```
  replacing the former `bytes | dict` behavior.  
  Adapters must now explicitly return `AdapterResult` objects.

- **Refactored `DataIoSession`** for uniform persistence:  
  - Always writes payloads as `.bin` files and sidecar metadata as `.meta.json`.  
  - Simplified and unified caching logic.  
  - `fetch()` and `send()` now share a common adapter-result handling path.

- **Improved type precision and clarity** in `store.py`:  
  - `_instances` properly annotated (`dict[str, Store]`).  
  - `get_instance()` explicitly returns `Store`.  
  - Removed all Pyright “unknown” and “partially typed” errors.

### Deprecated
- None.

### Removed
- Support for raw-bytes adapter returns (`fetch() -> bytes`)  
  and mapping-returns (`send() -> dict`).  
  These are replaced by the new, strictly-typed `AdapterResult` interface.

### Fixed
- Eliminated all Pyright warnings and type-inference issues across the codebase.  
- Resolved circular import risk between `adapters` and `models` by relocating `AdapterResult` to `models.py`.

### Notes
- **Breaking change:** Adapters returning raw `bytes` are no longer supported.  
  Implement `AdapterResult` return values for full compatibility.
- The new protocol ensures all I/O operations are **deterministic, auditable, and replayable**.  
  Each persisted response now has:
  - A binary payload (`<checksum>.bin`)
  - A JSON sidecar (`<checksum>.meta.json`)
- Tests confirm identical cache semantics and integrity of persisted sessions.

### Upgrade guidance
To migrate:
1. Update custom adapters to return an `AdapterResult` instead of raw bytes.  
   Example:
   ```python
   from mxm_dataio.models import AdapterResult
   return AdapterResult(data=response_bytes, content_type="application/json")
   ```
2. No changes needed for consumers of `DataIoSession` or `Store`.
3. After upgrading, run `pytest` and `pyright` to confirm compliance.

---

[0.2.0]: https://example.com/releases/tag/v0.2.0

## [0.1.4] — 2025-10-19
### Added
- **GitHub Actions workflow** `.github/workflows/release.yml`  
  Automates build and publication to **PyPI** when a new version tag is pushed.  
  The workflow builds both wheel and sdist, verifies imports, and uploads securely using Poetry.

### Changed
- Updated dependency on **`mxm-config`** to use the latest official **PyPI release** (`>=0.2.5`).
- Minor adjustments to project metadata to align with publishing standards.

### Notes
- This is the **first PyPI-published version** of `mxm-dataio`.  
  No functional changes to the core package logic.
- The internal dependency graph (`mxm-dataio` → `mxm-config`) now fully resolves from public releases.

### Upgrade guidance
No code changes are required.  
You can install this version directly from PyPI:
```bash
pip install -U mxm-dataio
## [0.1.3] - 2025-10-16
### Changed
- Pin mxm-config dependency by git **rev** (commit SHA) instead of tag to avoid dulwich/tag resolution issues.

## [0.1.2] - 2025-10-16

### Changed
- Relax dependency on **mxm-config** to `^0.2.2` so consumers can use the new `MXMConfig` protocol and `make_subconfig` helper.
- Documentation: examples now show passing a minimal DataIO config via `make_subconfig(...)` at package boundaries.

### Notes
- No runtime behavior changes in DataIO; this is a compatibility/packaging bump to align with mxm-config ≥ 0.2.2.

[0.1.2]: https://example.com/releases/tag/v0.1.2

## [0.1.1] - 2025-10-09

### Changed
- README: add CI/license badges and minor doc tweaks.
- Bump version to 0.1.1.
---

## [0.1.0] - 2025-10-09

### Added
- **Core datamodel** in `models.py`:
  - `Session`, `Request`, `Response` dataclasses.
  - Enums: `SessionMode`, `RequestMethod`, `ResponseStatus`.
  - Deterministic request hashing, payload checksums, JSON serialization helpers.
- **Persistence layer** in `store.py`:
  - SQLite-backed metadata; payloads written to `responses/<checksum>.bin`.
  - Sidecar metadata support via `<checksum>.meta.json` with deterministic, Unicode-friendly JSON.
  - Per-config singleton `Store.get_instance(cfg)`.
  - Basic queries: `list_sessions`, `get_latest_session_id`.
- **Adapters & capabilities** in `adapters.py`:
  - Base adapter protocol + capability protocols: `Fetcher`, `Sender` (with stubs for `Streamer`).
  - `AdapterResult` envelope (bytes + transport metadata) for richer returns without DB migrations.
- **Runtime registry** in `registry.py`:
  - `register`, `resolve_adapter`, `unregister`, `clear_registry`, `list_registered`, `describe_registry`.
- **High-level API** in `api.py`:
  - `DataIoSession` context manager (one adapter per session).
  - Create `Request`s, execute `.fetch()` / `.send()`, and persist `Response`s automatically.
  - Optional caching by request hash (returns most recent stored response).
- **Indexes** for performance (SQLite):
  - `requests(hash)`, `requests(session_id)`, `responses(request_id)`, `responses(created_at)`, `responses(checksum)`.
- **Store helpers**:
  - `mark_session_ended(session_id, ended_at)` to finalize sessions.
  - `get_cached_response_by_request_hash(request_hash)` to centralize cache lookups.
- **Tests**:
  - Models, store (incl. extended robustness), registry, API behavior.
  - Metadata sidecars and `AdapterResult` paths (fetch/send).
  - Strict type checks (Pyright) and linting (Ruff), Black formatting.
- **Documentation**:
  - Comprehensive `README.md`: architecture, design, adapters, registry, `DataIoSession`, config via `mxm-config`, examples, roadmap.

### Changed
- Centralized cache retrieval and session end-time updates in `Store` (moved from `api.py`).
- Simplified `_extract_bytes_and_meta` to trust type hints (`bytes | AdapterResult`).
- Removed unused internal helper in `api.py` after refactor.

### Deprecated
- None.

### Removed
- None.

### Fixed
- Sidecar JSON now uses `ensure_ascii=False` for readable Unicode while remaining deterministic.

---

## Links

- Compare changes: `[Unreleased]` vs `0.1.0` (add repo URL once published).

[Unreleased]: https://example.com/compare/v0.1.0...HEAD
[0.1.0]: https://example.com/releases/tag/v0.1.0
