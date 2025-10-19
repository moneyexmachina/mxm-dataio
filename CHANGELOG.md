# Changelog

All notable changes to this project will be documented in this file.

The format is based on **Keep a Changelog**, and this project adheres to **Semantic Versioning**.

## [Unreleased]

### Added
- CLI (planned for v0.2.0): inspect sessions/requests/responses, show metadata, dump payloads.
- Convenience store helpers (planned): `get_payload_and_metadata`, `has_metadata`.
- Reference adapters (planned): `LocalFileFetcher`, minimal stdlib `HttpFetcher`.
- Streaming design (planned): `Streamer.stream(request) -> AsyncIterator[bytes]` and sequenced persistence.

### Changed
- TBD based on feedback from `mxm-datakraken` JustETF integration.

### Deprecated
- None.

### Removed
- None.

### Fixed
- None.

---
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
