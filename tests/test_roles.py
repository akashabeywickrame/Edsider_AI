"""Each role kept to its own section, on the API itself (service.allow):
a teacher cannot work the data-entry routes, an employee cannot act as a
teacher, and only admin/superadmin can assign papers or see the teacher
list - by calling the API directly, not only by typing an address the
page would not show them. Real requests through TestClient, so the
middleware and every dependency run; the Easy Auth header stands in for a
real sign-in, as in test_signin_gate.py."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import service
from service import StaffIn, add_staff_member

SUPER = {"identity": "boss@example.com", "name": "Boss", "role": "superadmin"}
WHO = "x-ms-client-principal-name"
PEOPLE = {"employee": "emp@example.com", "teacher": "tea@example.com",
          "admin": "adm@example.com", "superadmin": "sup@example.com"}


def test_every_data_route_names_who_may_use_it():
    """Written as "everything except this short list", so a newly added
    route that forgets its role rule fails here instead of being open to
    every signed-in person."""
    open_to_all = {"/health", "/me", "/docs", "/docs/oauth2-redirect", "/redoc",
                   "/openapi.json", "/{full_path:path}"}
    role_checks = {service.require_superadmin, service.require_assigner}
    unguarded = []
    for route in service.app.routes:
        path = getattr(route, "path", None)
        if path is None or path in open_to_all:
            continue
        calls = []
        stack = list(getattr(route, "dependant", None).dependencies)
        while stack:
            d = stack.pop()
            calls.append(d.call)
            stack.extend(d.dependencies)
        if not any(c in role_checks or hasattr(c, "allowed_roles") for c in calls):
            unguarded.append(f"{sorted(route.methods)} {path}")
    assert not unguarded


@pytest.fixture
def client(local_db, monkeypatch):
    for var in ("EDSIDER_REQUIRE_SIGNIN", "EDSIDER_BOOTSTRAP_ADMINS", "EDSIDER_API_KEY",
                "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    for role, email in PEOPLE.items():
        add_staff_member(StaffIn(email=email, name=role.title(), role=role), user=SUPER)
    return TestClient(service.app)


def as_role(client, role, method, path):
    return client.request(method, path, headers={WHO: PEOPLE[role]}).status_code


# A route from each section that needs no data to answer.
DATA_ENTRY = ("GET", "/decisions")
TEACHER = ("GET", "/teachers/me/documents")
ADMIN = ("GET", "/teachers")


def test_a_teacher_cannot_use_data_entry(client):
    assert as_role(client, "teacher", *DATA_ENTRY) == 403
    assert as_role(client, "teacher", *TEACHER) == 200


def test_an_employee_cannot_use_teacher_review(client):
    assert as_role(client, "employee", *TEACHER) == 403
    assert as_role(client, "employee", *DATA_ENTRY) == 200


@pytest.mark.parametrize("role", ["employee", "teacher"])
def test_only_admins_open_the_admin_section(client, role):
    assert as_role(client, role, *ADMIN) == 403
    assert as_role(client, role, "POST", "/intake/documents/1/assign") == 403


@pytest.mark.parametrize("role", ["admin", "superadmin"])
def test_admins_oversee_every_section(client, role):
    for route in (DATA_ENTRY, TEACHER, ADMIN):
        assert as_role(client, role, *route) == 200, route


def test_manage_users_is_still_superadmin_only(client):
    assert as_role(client, "admin", "GET", "/admin/users") == 403
    assert as_role(client, "superadmin", "GET", "/admin/users") == 200


def test_a_refusal_says_why(client):
    r = client.get(DATA_ENTRY[1], headers={WHO: PEOPLE["teacher"]})
    assert "Your role cannot use this part" in r.json()["detail"]


def test_a_local_run_with_no_sign_in_is_still_open(client):
    assert client.get(DATA_ENTRY[1]).status_code == 200
    assert client.get(ADMIN[1]).status_code == 200
