"""The sign-in gate (service.py's require_signed_in_staff middleware) and
the API serving the built page itself (FRONTEND_DIR) - through real HTTP
requests, so middleware, CORS and routing are all in the path. The
TestClient is used without `with`, so the app's startup (which loads the
embedding model and the bank) never runs; none of these routes need it.
"""

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


@pytest.fixture
def client(local_db, monkeypatch):
    monkeypatch.delenv("EDSIDER_REQUIRE_SIGNIN", raising=False)
    monkeypatch.delenv("EDSIDER_BOOTSTRAP_ADMINS", raising=False)
    monkeypatch.delenv("EDSIDER_API_KEY", raising=False)
    return TestClient(service.app)


@pytest.fixture
def built_page(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>the app</html>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("outside", encoding="utf-8")
    monkeypatch.setattr(service, "FRONTEND_DIR", tmp_path.resolve())
    return tmp_path


# ---- who gets in ----

def test_a_local_run_with_no_sign_in_is_still_open(client):
    r = client.get("/me")
    assert r.status_code == 200 and r.json()["identity"] == "reviewer"


def test_with_require_signin_no_identity_is_refused_everywhere_but_health(client, monkeypatch):
    monkeypatch.setenv("EDSIDER_REQUIRE_SIGNIN", "true")
    assert client.get("/me").status_code == 401
    assert client.get("/teachers").status_code == 401
    assert client.get("/admin/users").status_code == 401


def test_a_signed_in_stranger_is_refused_on_any_api_route(client):
    # Not just /me - a route that never asked who is calling is gated too.
    r = client.get("/teachers", headers={WHO: "stranger@example.com"})
    assert r.status_code == 403
    assert r.json()["detail"].startswith("stranger@example.com is not allowed")


def test_a_listed_person_gets_in_and_is_named(client, monkeypatch):
    monkeypatch.setenv("EDSIDER_REQUIRE_SIGNIN", "true")
    add_staff_member(StaffIn(email="emp@example.com", name="Emp One", role="employee"), user=SUPER)
    r = client.get("/me", headers={WHO: "Emp@Example.com"})
    assert r.status_code == 200
    me = r.json()
    assert {k: me[k] for k in ("identity", "name", "role", "masad")} == {
        "identity": "emp@example.com", "name": "Emp One", "role": "employee", "masad": False}


def test_a_refusal_still_carries_cors_headers(client):
    # Otherwise the browser hides the reason from the page.
    r = client.get("/teachers", headers={WHO: "stranger@example.com",
                                         "Origin": "http://localhost:5173"})
    assert r.status_code == 403
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ---- the page served from the same address ----

def test_health_needs_no_sign_in_even_when_required(client, monkeypatch):
    monkeypatch.setenv("EDSIDER_REQUIRE_SIGNIN", "true")
    assert client.get("/health").status_code == 200


def test_the_page_and_its_files_load_without_sign_in(client, built_page, monkeypatch):
    monkeypatch.setenv("EDSIDER_REQUIRE_SIGNIN", "true")
    for path in ("/", "/sign-in", "/app/admin/users"):
        r = client.get(path)
        assert r.status_code == 200 and "the app" in r.text, path
        assert r.headers["cache-control"] == "no-cache"
    assert client.get("/assets/app.js").text == "console.log(1)"


def test_an_unknown_address_is_a_404_not_the_page(client, built_page):
    r = client.get("/no-such-api-route")
    assert r.status_code == 404 and "the app" not in r.text


def test_a_path_cannot_climb_out_of_the_built_page(client, built_page):
    for path in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"):
        r = client.get(path)
        assert "outside" not in r.text, path


def test_with_no_built_page_the_api_serves_no_page(client, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "FRONTEND_DIR", (tmp_path / "missing").resolve())
    assert client.get("/sign-in").status_code == 404
