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
from pathlib import Path
from typing import Any, ClassVar, Generator

from mxm_dataio.models import Request, Response, Session

# --------------------------------------------------------------------------- #
# Store class
# --------------------------------------------------------------------------- #


class Store:
    """Manage SQLite metadata and payload file persistence.

    A Store instance is tied to a specific MXM configuration, resolved
    from mxm-config.  Each configuration (identified by its database
    path) has at most one Store instance per process.
    """

    _instances: ClassVar[dict[str, Store]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, cfg: dict[str, Any]):
        """Initialize the Store from a resolved configuration object."""
        paths = cfg["paths"]
        self.data_root = Path(paths["data_root"])
        self.db_path = Path(paths.get("db_path", self.data_root / "dataio.sqlite"))
        self.responses_dir = Path(
            paths.get("responses_dir", self.data_root / "responses")
        )

        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------ #
    # Singleton factory
    # ------------------------------------------------------------------ #

    @classmethod
    def get_instance(cls, cfg: dict[str, Any]) -> Store:
        """Return the singleton Store for the given configuration."""
        db_key = str(cfg["paths"]["db_path"])
        with cls._lock:
            if db_key not in cls._instances:
                cls._instances[db_key] = cls(cfg)
            return cls._instances[db_key]

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

    # ------------------------------------------------------------------ #
    # Insert methods
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

    def insert_request(self, request: Request) -> None:
        """Insert or ignore a Request record."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO requests
                (id, session_id, kind, method, params_json, body_json, hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                (id, request_id, status, sequence, checksum, path, created_at, size_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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

    def write_metadata(self, checksum: str, meta: dict[str, Any]) -> Path:
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

    def read_metadata(self, checksum: str) -> dict[str, Any]:
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
    def _safe_json(data: Any | None) -> str | None:
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
                "SELECT id, source, mode, started_at FROM sessions ORDER BY started_at DESC"
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
