"""Sign-in and staff profiles: the `staff` table, service.py's
_resolve_identity / require_superadmin / require_assigner and the
/admin/users routes, and sync.py's sync_teachers() - called directly like
the rest of the API tests. No server, no Easy Auth: the Easy Auth header
is passed in as the plain value it would be.
"""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
import service
from service import (
    StaffIn, StaffUpdate, add_staff_member, current_identity,
    list_staff_members, remove_staff_member, require_superadmin,
    update_staff_member,
)

SUPER = {"identity": "boss@example.com", "name": "Boss", "role": "superadmin"}


class _FakeCursor:
    def __init__(self, rows):
        self.rows, self.sql = rows, None

    def execute(self, sql, params=None):
        self.sql = sql

    def __iter__(self):
        return iter(self.rows)

    def close(self):
        pass


class _FakePg:
    def __init__(self, rows):
        self.cur = _FakeCursor(rows)

    def cursor(self, *a, **k):
        return self.cur


def _sync(*teachers):
    from scripts.sync import sync_teachers
    return sync_teachers(_FakePg([{"email": e, "name": n} for e, n in teachers]))


def _staff(email):
    return {s.email: s for s in list_staff_members(user=SUPER)}.get(email)


# ---- signing in ----

def test_no_easy_auth_header_is_the_local_fallback(local_db):
    assert current_identity(None) == "reviewer"
    assert service.current_user(None)["role"] is None


def test_a_signed_in_email_not_on_the_list_is_refused(local_db):
    with pytest.raises(HTTPException) as exc:
        current_identity("stranger@example.com")
    assert exc.value.status_code == 403


def test_a_listed_email_signs_in_with_its_role_whatever_its_case(local_db):
    add_staff_member(StaffIn(email="N.Perera@Example.com", name="N. Perera", role="teacher"),
                     user=SUPER)
    assert service.current_user("n.perera@EXAMPLE.com") == {
        "identity": "n.perera@example.com", "name": "N. Perera", "role": "teacher"}


def test_bootstrap_emails_are_superadmin_with_an_empty_list(local_db, monkeypatch):
    monkeypatch.setenv("EDSIDER_BOOTSTRAP_ADMINS", "first@example.com, other@example.com")
    assert service.current_user("First@Example.com")["role"] == "superadmin"


@pytest.mark.parametrize("role", ["admin", "teacher", "employee"])
def test_only_a_superadmin_may_manage_staff(role):
    with pytest.raises(HTTPException) as exc:
        require_superadmin({"identity": "x", "name": "x", "role": role})
    assert exc.value.status_code == 403
    require_superadmin(SUPER)
    require_superadmin({"identity": "reviewer", "name": "reviewer", "role": None})


@pytest.mark.parametrize("role", ["teacher", "employee"])
def test_only_admins_may_refresh_the_teacher_list(role):
    with pytest.raises(HTTPException) as exc:
        service.require_assigner({"identity": "x", "name": "x", "role": role})
    assert exc.value.status_code == 403
    for ok in ("admin", "superadmin", None):
        service.require_assigner({"identity": "x", "name": "x", "role": ok})


# ---- managing staff ----

def test_add_update_remove(local_db):
    added = add_staff_member(
        StaffIn(email="e1@example.com", name="Emp One", role="employee",
                subjects=["ICT", " "]), user=SUPER)
    assert (added.added_by, added.effective_role, added.subjects) == (
        "boss@example.com", "employee", ["ICT"])
    assert added.added_at is not None

    # Re-adding corrects in place; subjects left out are kept.
    add_staff_member(StaffIn(email="e1@example.com", name="Employee One", role="admin"),
                     user=SUPER)
    s = _staff("e1@example.com")
    assert (s.name, s.effective_role, s.subjects) == ("Employee One", "admin", ["ICT"])

    remove_staff_member("E1@example.com", user=SUPER)
    assert _staff("e1@example.com") is None
    with pytest.raises(HTTPException):
        current_identity("e1@example.com")


def test_switching_someone_off_blocks_sign_in_and_keeps_the_row(local_db):
    add_staff_member(StaffIn(email="a@example.com", name="A", role="admin"), user=SUPER)
    off = update_staff_member("a@example.com", StaffUpdate(active=False), user=SUPER)
    assert (off.active, off.effective_role) == (False, None)
    with pytest.raises(HTTPException):
        current_identity("a@example.com")
    on = update_staff_member("a@example.com", StaffUpdate(active=True), user=SUPER)
    assert on.effective_role == "admin"


def test_a_superadmin_cannot_switch_themselves_off(local_db):
    add_staff_member(StaffIn(email="boss@example.com", name="Boss", role="superadmin"),
                     user=SUPER)
    with pytest.raises(HTTPException) as exc:
        update_staff_member("boss@example.com", StaffUpdate(active=False), user=SUPER)
    assert exc.value.status_code == 400


def test_editing_someone_unknown_is_a_404(local_db):
    with pytest.raises(HTTPException) as exc:
        update_staff_member("nobody@example.com", StaffUpdate(name="X"), user=SUPER)
    assert exc.value.status_code == 404


# ---- EdSider's own teachers ----

def test_sync_teachers_copies_edsider_teachers(local_db):
    from scripts.sync import sync_teachers
    pg = _FakePg([{"email": "t1@gmail.com", "name": "Tea One"},
                  {"email": "t2@gmail.com", "name": None}])
    assert sync_teachers(pg) == 2
    assert "'TEACH'" in pg.cur.sql
    assert {(t.identity, t.display_name, t.source) for t in service.list_teachers()} == {
        ("t1@gmail.com", "Tea One", "edsider"), ("t2@gmail.com", None, "edsider")}


def test_an_edsider_teacher_signs_in_as_teacher_without_being_added(local_db):
    _sync(("t1@gmail.com", "Tea One"))
    assert service.current_user("T1@Gmail.com") == {
        "identity": "t1@gmail.com", "name": "Tea One", "role": "teacher"}


def test_a_resync_drops_a_teacher_edsider_removed_but_keeps_their_profile(local_db):
    _sync(("a@gmail.com", "A"), ("b@gmail.com", "B"))
    _sync(("a@gmail.com", "A"))
    assert [t.identity for t in service.list_teachers()] == ["a@gmail.com"]
    with pytest.raises(HTTPException):
        current_identity("b@gmail.com")
    b = _staff("b@gmail.com")
    assert b is not None and b.in_edsider is False


def test_edsider_data_entry_operators_sign_in_and_fill_the_dropdown(local_db):
    """EdSider's DEOP staff come in with the same sync as its teachers,
    with every position they hold and their two EdSider ids."""
    from scripts.sync import sync_teachers
    pg = _FakePg([
        {"email": "t1@gmail.com", "name": "Tea One", "roles": ["ASSR", "TEACH"],
         "positions": ["ASSR|Assessor", "TEACH|Teacher"], "user_id": 292, "staff_id": 256},
        {"email": "op@gmail.com", "name": "Op One", "roles": ["ASSR", "DEOP"],
         "positions": ["ASSR|Assessor", "DEOP|Data Entry Operator"],
         "user_id": 317, "staff_id": 281}])
    assert sync_teachers(pg) == 1
    assert "'DEOP'" in pg.cur.sql
    assert service.current_user("Op@Gmail.com")["role"] == "employee"
    with store.connect() as conn:
        operators = [(o["identity"], o["positions"], o["source"])
                     for o in store.assignable_operators(conn)]
    assert operators == [("op@gmail.com", "Assessor, Data Entry Operator", "edsider")]
    with store.connect() as conn:
        row = store.lookup_staff(conn, "op@gmail.com")
    assert (row["edsider_user_id"], row["edsider_staff_id"]) == (317, 281)

    # No longer an operator in EdSider: out of the dropdown, cannot sign in.
    sync_teachers(_FakePg([{"email": "t1@gmail.com", "name": "Tea One", "roles": ["TEACH"],
                            "positions": ["TEACH|Teacher"]}]))
    with store.connect() as conn:
        assert store.assignable_operators(conn) == []
    with pytest.raises(HTTPException):
        current_identity("op@gmail.com")
    assert _staff("op@gmail.com").positions is None
    assert _staff("t1@gmail.com").positions == "Teacher"


def test_edsider_main_assessment_administrators_are_admins_here(local_db):
    """MASAD - EdSider's Main Assessment Administrator - is this app's
    admin: they assign papers and send them to EdSider."""
    from scripts.sync import sync_teachers
    pg = _FakePg([
        {"email": "t1@gmail.com", "name": "Tea One", "roles": ["TEACH"],
         "positions": ["TEACH|Teacher"], "user_id": 292, "staff_id": 256},
        {"email": "boss@gmail.com", "name": "Main Admin", "roles": ["MASAD", "TEACH"],
         "positions": ["MASAD|Main Assessment Administrator", "TEACH|Teacher"],
         "user_id": 5, "staff_id": 4}])
    sync_teachers(pg)
    assert "'MASAD'" in pg.cur.sql
    assert service.current_user("boss@gmail.com")["role"] == "admin"
    assert _staff("boss@gmail.com").edsider_admin

    # No longer MASAD in EdSider: a teacher again, not an admin.
    sync_teachers(_FakePg([{"email": "boss@gmail.com", "name": "Main Admin", "roles": ["TEACH"],
                            "positions": ["TEACH|Teacher"]}]))
    assert service.current_user("boss@gmail.com")["role"] == "teacher"


def test_an_empty_teacher_list_is_refused_not_applied(local_db):
    _sync(("a@gmail.com", "A"))
    with pytest.raises(ValueError):
        _sync()
    assert [t.identity for t in service.list_teachers()] == ["a@gmail.com"]


def test_a_superadmins_role_and_name_outrank_edsiders(local_db):
    _sync(("t1@gmail.com", "Tea One"))
    add_staff_member(StaffIn(email="t1@gmail.com", name="Ms Tea", role="admin"), user=SUPER)
    _sync(("t1@gmail.com", "Tea One"))
    assert service.current_user("t1@gmail.com") == {
        "identity": "t1@gmail.com", "name": "Ms Tea", "role": "admin"}
    # Removing what the superadmin granted puts them back to EdSider's say.
    remove_staff_member("t1@gmail.com", user=SUPER)
    assert service.current_user("t1@gmail.com")["role"] == "teacher"


def test_a_switched_off_edsider_teacher_cannot_sign_in(local_db):
    _sync(("t1@gmail.com", "Tea One"))
    update_staff_member("t1@gmail.com", StaffUpdate(active=False), user=SUPER)
    with pytest.raises(HTTPException):
        current_identity("t1@gmail.com")
    assert service.list_teachers() == []


def test_opening_the_teacher_app_is_recorded_on_the_same_profile(local_db):
    _sync(("t1@gmail.com", "Tea One"))
    service.touch_teacher(identity="t1@gmail.com")
    t1 = _staff("t1@gmail.com")
    assert t1.first_seen_at is not None and t1.name == "Tea One"
    assert len(list_staff_members(user=SUPER)) == 1


# ---- moving the three old tables into `staff` ----

def test_the_old_people_tables_are_copied_and_left_in_place(local_db):
    with store.connect() as conn:
        conn.execute("CREATE TABLE edsider_teachers (email TEXT PRIMARY KEY, name TEXT, "
                     "synced_at TIMESTAMPTZ DEFAULT now())")
        conn.execute("CREATE TABLE authorized_users (email TEXT PRIMARY KEY, name TEXT, "
                     "role TEXT, added_by TEXT, added_at TIMESTAMPTZ DEFAULT now())")
        conn.execute("CREATE TABLE teachers (identity TEXT PRIMARY KEY, display_name TEXT, "
                     "first_seen_at TIMESTAMPTZ DEFAULT now(), last_seen_at TIMESTAMPTZ DEFAULT now())")
        conn.execute("INSERT INTO edsider_teachers (email, name) VALUES "
                     "('t1@gmail.com', 'Tea One'), ('t2@gmail.com', 'Tea Two')")
        conn.execute("INSERT INTO authorized_users (email, name, role, added_by) VALUES "
                     "('t2@gmail.com', 'Ms Two', 'admin', 'boss@example.com'), "
                     "('e1@example.com', 'Emp', 'employee', 'boss@example.com')")
        conn.execute("INSERT INTO teachers (identity, display_name) VALUES "
                     "('t1@gmail.com', 'ignored'), ('N. Perera', NULL)")
        store._merge_old_people_tables(conn)
        # Twice is harmless - the copy upserts.
        store._merge_old_people_tables(conn)
        kept = conn.execute(
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = "
            "current_schema() AND table_name IN ('edsider_teachers','authorized_users','teachers')"
        ).fetchone()["n"]
    # Kept: a still-deployed older version may be reading them.
    assert kept == 3
    people = {s.email: s for s in list_staff_members(user=SUPER)}
    assert set(people) == {"t1@gmail.com", "t2@gmail.com", "e1@example.com", "N. Perera"}
    assert (people["t1@gmail.com"].name, people["t1@gmail.com"].effective_role) == ("Tea One", "teacher")
    assert people["t1@gmail.com"].first_seen_at is not None
    assert (people["t2@gmail.com"].name, people["t2@gmail.com"].effective_role,
            people["t2@gmail.com"].in_edsider) == ("Ms Two", "admin", True)
    assert people["e1@example.com"].effective_role == "employee"
    assert people["N. Perera"].effective_role is None
