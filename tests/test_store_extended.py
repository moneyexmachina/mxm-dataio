"""
Extended tests for mxm_dataio.store.

These tests validate persistence, concurrency, and robustness aspects
of the Store class beyond basic functional coverage.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import os
from datetime import datetime
from pathlib import Path

import pytest
from mxm_config import MXMConfig, make_subconfig

from mxm_dataio.models import Session
from mxm_dataio.store import Store

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def store_cfg_view(tmp_path: Path) -> MXMConfig:
    """
    Provide a temporary **dataio view** (MXMConfig) for tests.

    Shape expected by Store:
        paths.root, paths.db_path, paths.responses_dir
    """
    return make_subconfig(
        {
            "paths": {
                "root": str(tmp_path),
                "db_path": str(tmp_path / "test.sqlite"),
                "responses_dir": str(tmp_path / "responses"),
            }
        }
    )


@pytest.fixture()
def store(store_cfg_view: MXMConfig) -> Store:
    """Return a new Store instance for the temporary config view."""
    return Store(store_cfg_view)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_store_persistence_across_instances(store_cfg_view: MXMConfig) -> None:
    """Data inserted by one Store instance should be visible to another."""
    store1 = Store.get_instance(store_cfg_view)
    session = Session(source="persist")
    store1.insert_session(session)

    # New instance reading the same DB
    store2 = Store(store_cfg_view)
    with store2.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE id=?", (session.id,)
        ).fetchall()
    assert len(rows) == 1, "Session should persist across instances"


def test_singleton_thread_safety(store_cfg_view: MXMConfig) -> None:
    """get_instance should return the same object even under concurrency."""

    def create_store(_: object) -> Store:
        return Store.get_instance(store_cfg_view)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(create_store, itertools.repeat(None, 5)))

    first = results[0]
    assert all(s is first for s in results), "All threads should share one instance"


def test_datetime_roundtrip(store: Store) -> None:
    """Timestamps should be stored and retrievable as ISO UTC datetimes."""
    s = Session(source="timecheck")
    store.insert_session(s)

    with store.connect() as conn:
        row = conn.execute(
            "SELECT started_at FROM sessions WHERE id=?", (s.id,)
        ).fetchone()

    assert row is not None
    dt = datetime.fromisoformat(row[0])
    assert dt.tzinfo is not None, "Timestamp must preserve timezone info"


def test_missing_payload_raises(store: Store) -> None:
    """Attempting to read a non-existent payload should raise FileNotFoundError."""
    fake_checksum = "00" * 32
    with pytest.raises(FileNotFoundError):
        store.read_payload(fake_checksum)


def test_large_payload_roundtrip(store: Store) -> None:
    """Large binary payloads should write and read back correctly."""
    data = os.urandom(5 * 1024 * 1024)  # 5 MB
    path = store.write_payload(data)
    assert path.exists()
    out = store.read_payload(path.stem)
    assert out == data
    assert len(out) == len(data)


def test_indexes_created(store: Store) -> None:
    with store.connect() as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list('requests')")}
        assert "idx_requests_hash" in names
        assert "idx_requests_session" in names

        names = {row[1] for row in conn.execute("PRAGMA index_list('responses')")}
        assert "idx_responses_request" in names
        assert "idx_responses_created" in names
        # keep this if your schema creates it:
        assert "idx_responses_checksum" in names


def test_store_helpers_end_and_cache(store: Store) -> None:
    from datetime import datetime, timezone

    from mxm_dataio.models import (
        Request,
        RequestMethod,
        Response,
        ResponseStatus,
        Session,
    )

    sess = Session(source="unit")
    store.insert_session(sess)

    end = datetime.now(tz=timezone.utc).replace(microsecond=0)
    store.mark_session_ended(sess.id, end)
    with store.connect() as conn:
        ended = conn.execute(
            "SELECT ended_at FROM sessions WHERE id = ?", (sess.id,)
        ).fetchone()[0]
    assert ended == end.isoformat()

    req = Request(
        session_id=sess.id, kind="k", method=RequestMethod.GET, params={"a": 1}
    )
    store.insert_request(req)
    p = store.write_payload(b"abc")
    resp = Response.from_bytes(
        request_id=req.id, status=ResponseStatus.OK, data=b"abc", path=str(p)
    )
    store.insert_response(resp)

    cached = store.get_cached_response_by_request_hash(req.hash)
    assert cached is not None and cached.id == resp.id


def test_mark_session_ended_accepts_none(store: Store) -> None:
    from mxm_dataio.models import Session

    sess = Session(source="unit")
    store.insert_session(sess)

    store.mark_session_ended(sess.id, None)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT ended_at FROM sessions WHERE id=?", (sess.id,)
        ).fetchone()
    assert row is not None
    assert row[0] is None
