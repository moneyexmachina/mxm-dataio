"""Persistence layer for mxm-dataio.

This module provides the Store class, which manages metadata and
payload persistence for all external I/O interactions in the MXM
ecosystem.  It stores metadata in a local SQLite database and raw
payloads as files in a structured directory, using configuration paths
injected at runtime from mxm-config.

Design principles:
- One Store instance per configuration (singleton-per-config)
- Atomic commits with rollback on error
- Deterministic, reproducible file layout
- Zero external dependencies except mxm-config for path resolution
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Final, Generator, Optional

from mxm_config import MXMConfig

from mxm_dataio.models import Request, Response, Session
from mxm_dataio.types import JSONLike

# --------------------------------------------------------------------------- #
# Store class
# --------------------------------------------------------------------------- #


class Store:
    """Manage SQLite metadata and payload file persistence.

    A Store instance is tied to a specific MXM configuration, resolved
    from mxm-config.  Each configuration (identified by its database
    path) has at most one Store instance per process.

    Expects a **dataio** view as cfg. Reads only:

        cfg.paths.root            (required)
        cfg.paths.db_path         (optional)
        cfg.paths.responses_dir   (optional)

    Everything else in the view is ignored here.
    """

    _instances: ClassVar[dict[str, "Store"]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, cfg: MXMConfig) -> None:
        """Initialize the Store from a resolved configuration object."""

        # Required: paths.root
        try:
            root = cfg.paths.root  # type: ignore[attr-defined]
        except Exception as exc:
            raise ValueError(
                "dataio.paths.root is required on the passed config view"
            ) from exc
        self.data_root = Path(str(root))

        # Optional with defaults
        try:
            db_path = cfg.paths.db_path  # type: ignore[attr-defined]
        except Exception:
            db_path = self.data_root / "dataio.sqlite"
        self.db_path = Path(str(db_path))

        try:
            responses_dir = cfg.paths.responses_dir  # type: ignore[attr-defined]
        except Exception:
            responses_dir = self.data_root / "responses"
        self.responses_dir = Path(str(responses_dir))
        self.responses_dir.mkdir(parents=True, exist_ok=True)

        # Keep the view for other components that may need further knobs
        self.cfg = cfg

        self._ensure_schema()

    # ------------------------------------------------------------------ #
    # Singleton factory
    # ------------------------------------------------------------------ #

    @classmethod
    def get_instance(cls, cfg: MXMConfig) -> "Store":
        """
        Return the singleton Store for the given dataio view.

        Keyed by the (normalized) DB path. Uses the same fallback as __init__:
          - db_path := cfg.paths.db_path
          - else     cfg.paths.root / "dataio.sqlite"
        """
        # Compute db_path with the same semantics as __init__
        try:
            db_path = Path(str(cfg.paths.db_path))  # type: ignore[attr-defined]
        except Exception:
            root = Path(str(cfg.paths.root))  # type: ignore[attr-defined]
            db_path = root / "dataio.sqlite"

        # Normalize for a stable key (no FS requirement)
        # expanduser to unify ~; resolve(strict=False) to collapse ..
        # without touching FS
        key: Final[str] = db_path.expanduser().resolve(strict=False).as_posix()

        with cls._lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls(cfg)
                cls._instances[key] = inst
            return inst

    # ------------------------------------------------------------------ #
    # Database connection context
    # ------------------------------------------------------------------ #

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield a SQLite connection with automatic commit/rollback."""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Schema management
    # ------------------------------------------------------------------ #

    def _ensure_schema(self) -> None:
        """Create required tables if they do not yet exist."""
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    method TEXT NOT NULL,
                    params_json TEXT,
                    body_json TEXT,
                    hash TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS responses (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sequence INTEGER,
                    checksum TEXT,
                    path TEXT,
                    created_at TEXT NOT NULL,
                    size_bytes INTEGER,
                    FOREIGN KEY(request_id) REFERENCES requests(id)
                );
                """
            )
            # Indexes (idempotent and safe on existing DBs)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_hash ON requests(hash);"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_requests_session
                ON requests(session_id);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_responses_request
                ON responses(request_id);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_responses_created
                ON responses(created_at);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_responses_checksum
                ON responses(checksum);
                """
            )

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #

    def insert_session(self, session: Session) -> None:
        """Insert or ignore a Session record."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions
                (id, source, mode, as_of, started_at, ended_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.source,
                    session.mode.value,
                    session.as_of.isoformat(),
                    session.started_at.isoformat(),
                    session.ended_at.isoformat() if session.ended_at else None,
                ),
            )

    def mark_session_ended(self, session_id: str, ended_at: datetime | None) -> None:
        """Set ended_at for a session."""
        ts = ended_at.isoformat() if ended_at else None
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (ts, session_id),
            )

    def insert_request(self, request: Request) -> None:
        """Insert or ignore a Request record."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO requests
                (id, session_id, kind, method, params_json, body_json, hash,
                created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.id,
                    request.session_id,
                    request.kind,
                    request.method.value,
                    self._safe_json(request.params),
                    self._safe_json(request.body),
                    request.hash,
                    request.created_at.isoformat(),
                ),
            )

    def insert_response(self, response: Response) -> None:
        """Insert or ignore a Response record."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO responses
                (id, request_id, status, sequence, checksum, path, created_at,
                size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response.id,
                    response.request_id,
                    response.status.value,
                    response.sequence,
                    response.checksum,
                    response.path,
                    response.created_at.isoformat(),
                    response.size_bytes,
                ),
            )

    # --------------------------------------------------------------------- #
    # Caching & lookup
    # --------------------------------------------------------------------- #

    def get_cached_response_by_request_hash(
        self, request_hash: str
    ) -> Optional["Response"]:
        """Return the most recent Response for a previously-seen request hash,
        if any."""
        with self.connect() as conn:
            row_req = conn.execute(
                """SELECT id FROM requests WHERE hash = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (request_hash,),
            ).fetchone()
            if row_req is None:
                return None

            row_resp = conn.execute(
                """
                SELECT id, request_id, status, sequence, checksum, path,
                created_at, size_bytes
                FROM responses
                WHERE request_id = ?
                ORDER BY created_at DESC, COALESCE(sequence, -1) DESC
                LIMIT 1
                """,
                (row_req[0],),
            ).fetchone()

            if row_resp is None:
                return None

            # Local import to avoid circular deps
            from mxm_dataio.models import Response, ResponseStatus

            return Response(
                id=row_resp[0],
                request_id=row_resp[1],
                status=ResponseStatus(row_resp[2]),
                sequence=row_resp[3],
                checksum=row_resp[4],
                path=row_resp[5],
                created_at=datetime.fromisoformat(row_resp[6]),
                size_bytes=row_resp[7],
            )

    def get_cached_response_by_request_hash_and_bucket(
        self,
        request_hash: str,
        as_of_bucket: str | None = None,
    ) -> Optional["Response"]:
        """Return the most recent Response for a previously-seen request hash
        and bucket.

        Currently, the as_of_bucket is already encoded into the request hash,
        so this simply delegates to `get_cached_response_by_request_hash()`.
        The argument is accepted for forward compatibility, allowing future
        schema extensions with explicit bucket columns.
        """
        _ = as_of_bucket
        return self.get_cached_response_by_request_hash(request_hash)

    # ------------------------------------------------------------------ #
    # Payload management
    # ------------------------------------------------------------------ #

    def write_payload(self, data: bytes) -> Path:
        """Write payload bytes to the responses directory and return its path."""
        checksum = self._checksum(data)
        path = self.responses_dir / f"{checksum}.bin"
        if not path.exists():
            path.write_bytes(data)
        return path

    def read_payload(self, checksum: str) -> bytes:
        """Load payload bytes by checksum and verify integrity."""
        path = self.responses_dir / f"{checksum}.bin"
        data = path.read_bytes()
        if self._checksum(data) != checksum:
            raise ValueError(f"Checksum mismatch for {path}")
        return data

    def write_metadata(self, checksum: str, meta: dict[str, JSONLike]) -> Path:
        """Write response metadata as a sidecar JSON file next to the payload.

        Uses sorted keys and minified separators for determinism, and
        ensure_ascii=False for human-readable Unicode.
        """
        path = self.responses_dir / f"{checksum}.meta.json"
        if not path.exists():
            text = json.dumps(
                meta,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            path.write_text(text, encoding="utf-8")
        return path

    def read_metadata(self, checksum: str) -> dict[str, object]:
        import json

        path = self.responses_dir / f"{checksum}.meta.json"
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ #
    # Utility helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _checksum(data: bytes) -> str:
        """Return SHA-256 checksum of given bytes."""
        import hashlib

        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _safe_json(data: JSONLike | None) -> str | None:
        """Serialize a Python object to JSON or return None."""
        if data is None:
            return None
        import json

        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    # ------------------------------------------------------------------ #
    # Retrieval helpers
    # ------------------------------------------------------------------ #

    def list_sessions(self) -> list[tuple[str, str, str, str]]:
        """Return a list of (id, source, mode, started_at) for all sessions."""
        with self.connect() as conn:
            cur = conn.execute(
                """
                SELECT id, source, mode, started_at FROM sessions
                ORDER BY started_at DESC
                """
            )
            return list(cur.fetchall())

    def get_latest_session_id(self, source: str) -> str | None:
        """Return the most recent session ID for a given source, if any."""
        with self.connect() as conn:
            cur = conn.execute(
                """
                SELECT id FROM sessions
                WHERE source = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (source,),
            )
            row = cur.fetchone()
            return row[0] if row else None
