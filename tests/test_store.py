"""Tests for mxm_dataio.store.

Covers schema creation, idempotent inserts, transaction handling,
payload I/O, and linkage between Session, Request, and Response.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mxm_dataio.models import Request, Response, ResponseStatus, Session
from mxm_dataio.store import Store

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def store_cfg(tmp_path: Path) -> dict[str, Any]:
    """Provide a temporary config dictionary for testing."""
    return {
        "paths": {
            "data_root": str(tmp_path),
            "db_path": str(tmp_path / "test.sqlite"),
            "responses_dir": str(tmp_path / "responses"),
        }
    }


@pytest.fixture()
def store(store_cfg: dict[str, Any]) -> Store:
    """Return a new Store instance for the temporary config."""
    return Store(store_cfg)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_schema_creation(store: Store) -> None:
    """Verify that schema tables are created correctly."""
    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            )
        }
    expected = {"sessions", "requests", "responses"}
    assert expected.issubset(tables)


def test_insert_session_idempotent(store: Store) -> None:
    """Inserting the same session twice should not duplicate rows."""
    s = Session(source="test")
    store.insert_session(s)
    store.insert_session(s)
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert count == 1


def test_insert_request_and_response_linkage(store: Store) -> None:
    """Inserted response should link to its request via request_id."""
    s = Session(source="test")
    r = Request(session_id=s.id, kind="fetch", params={"x": 1})
    store.insert_session(s)
    store.insert_request(r)

    payload = b"abc"
    path = store.write_payload(payload)
    resp = Response.from_bytes(r.id, ResponseStatus.OK, payload, str(path))
    store.insert_response(resp)

    with store.connect() as conn:
        q = conn.execute(
            "SELECT request_id FROM responses WHERE id = ?", (resp.id,)
        ).fetchone()
    assert q is not None
    assert q[0] == r.id


def test_payload_roundtrip(store: Store) -> None:
    """Payloads written to disk can be read back and verified."""
    data = b"hello world"
    path = store.write_payload(data)
    checksum = path.stem
    out = store.read_payload(checksum)
    assert out == data


def test_payload_checksum_mismatch(store: Store) -> None:
    """Corrupted file should raise checksum mismatch."""
    data = b"original"
    path = store.write_payload(data)
    # Corrupt the file
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        store.read_payload(path.stem)


def test_transaction_rollback(store: Store) -> None:
    """Ensure rollback happens when an exception is raised inside connect()."""
    try:
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, source, mode, as_of, started_at) VALUES (?, ?, ?, ?, ?)",
                ("bad", "x", "sync", "now", "now"),
            )
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE id='bad'").fetchall()
    assert rows == []


def test_get_instance_singleton(store_cfg: dict[str, Any]) -> None:
    """Store.get_instance should return the same object for same config."""
    s1 = Store.get_instance(store_cfg)
    s2 = Store.get_instance(store_cfg)
    assert s1 is s2

    other_cfg = {
        "paths": {
            **store_cfg["paths"],
            "db_path": str(Path(store_cfg["paths"]["data_root"]) / "other.sqlite"),
        }
    }
    s3 = Store.get_instance(other_cfg)
    assert s3 is not s1


def test_insert_request_idempotent(store: Store) -> None:
    """Reinserting the same request hash should not create duplicates."""
    s = Session(source="test")
    r = Request(session_id=s.id, kind="fetch", params={"a": 1})
    store.insert_session(s)
    store.insert_request(r)
    store.insert_request(r)
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert count == 1


def test_list_sessions_returns_recent_first(store: Store) -> None:
    """list_sessions should return sessions ordered by start time descending."""
    s1 = Session(source="one")
    s2 = Session(source="two")
    store.insert_session(s1)
    store.insert_session(s2)
    sessions = store.list_sessions()
    assert sessions[0][0] == s2.id  # newest first


def test_get_latest_session_id(store: Store) -> None:
    """get_latest_session_id should return the most recent session for a source."""
    s1 = Session(source="x")
    s2 = Session(source="x")
    store.insert_session(s1)
    store.insert_session(s2)
    latest = store.get_latest_session_id("x")
    assert latest == s2.id
