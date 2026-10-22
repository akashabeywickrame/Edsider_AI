"""EdSider's reference lists through the API: what was copied is what the
page gets, and a refresh that cannot reach EdSider says so and keeps the
last copy (see scripts/sync.py sync_lookups, store.sync_edsider_lookups)."""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
import service


def _copy(rows):
    with store.connect() as conn:
        store.sync_edsider_lookups(conn, rows)


def test_the_lists_route_returns_what_was_copied(local_db):
    _copy([{"kind": "grade", "id": 12, "name": "12", "short_code": "G12", "extra": None},
           {"kind": "exam_category", "id": 1, "name": "Model - Term", "short_code": "MT", "extra": None}])
    out = service.edsider_lists()
    assert out.lists["grade"] == [{"id": 12, "name": "12", "short_code": "G12", "extra": None}]
    assert out.lists["exam_category"][0]["name"] == "Model - Term"
    assert out.synced_at


def test_a_refresh_that_cannot_reach_edsider_keeps_the_last_copy(local_db, monkeypatch):
    _copy([{"kind": "grade", "id": 12, "name": "12", "short_code": "G12", "extra": None}])
    for var in ("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    with pytest.raises(HTTPException) as err:
        service.sync_edsider_lists()
    assert err.value.status_code == 502
    assert service.edsider_lists().lists["grade"][0]["id"] == 12
