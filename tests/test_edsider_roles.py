"""Data entry inside EdSider's Assess pages: EdSider's web app forwards the
ID token its B2C sign-in issued to EdSider's own web client, and the role
comes from EdSider (service._edsider_identity) - only its Data Entry
Operators get in, as employee. Tokens are signed with a throwaway key, as
in test_id_tokens.py; EdSider's database is replaced by a dict."""

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import id_tokens, store
from src.edsider import send as edsider_send
from src.edsider import staff as edsider_staff
import service
from service import StaffIn, add_staff_member

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
B2C_ISS = next(iter(id_tokens.DEFAULT_B2C))
WEB_CLIENT = id_tokens.DEFAULT_EDSIDER_WEB_CLIENTS[0]
HEADER = "X-EdsiderAI-Id-Token"
SUPER = {"identity": "boss@example.com", "name": "Boss", "role": "superadmin"}

DEOP = {"user_id": 501, "staff_id": 77, "role_code": "DEOP", "name": "Deo Operator"}
MASAD = {"user_id": 502, "staff_id": 78, "role_code": "MASAD", "name": "Main Admin"}


def _token(email, aud=WEB_CLIENT, iss=B2C_ISS):
    now = int(time.time())
    return jwt.encode({"iss": iss, "aud": aud, "iat": now, "exp": now + 3600,
                       "email": email}, KEY, algorithm="RS256")


@pytest.fixture
def edsider(monkeypatch):
    """EdSider's staff, by email - what edsider_person would read."""
    people = {}
    monkeypatch.setattr(id_tokens, "_signing_key", lambda token, url: KEY.public_key())
    monkeypatch.setattr(edsider_staff, "edsider_person", lambda email: people.get(email))
    monkeypatch.setattr(service, "_edsider_rows", {})
    for var in ("EDSIDER_TOKEN_AUDIENCES", "EDSIDER_WEB_CLIENT_IDS", "EDSIDER_REQUIRE_SIGNIN",
                "EDSIDER_BOOTSTRAP_ADMINS", "EDSIDER_API_KEY", "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    return people


@pytest.fixture
def client(local_db, edsider):
    return TestClient(service.app)


# ---- the token ----

def test_a_token_for_edsiders_web_app_is_an_edsider_web_sign_in(edsider):
    who = id_tokens.identity_from_id_token(_token("Op@InfyInnos.com"))
    assert who == {"email": "op@infyinnos.com", "edsider_web": True}


def test_single_sign_on_into_this_apps_own_pages_is_not(edsider, monkeypatch):
    # Issued by the same B2C, but to this app's registration - the
    # standalone app's EdSider sign-in, which keeps the staff list.
    monkeypatch.setenv("EDSIDER_TOKEN_AUDIENCES", "edsider-ai-client")
    who = id_tokens.identity_from_id_token(_token("op@infyinnos.com", aud="edsider-ai-client"))
    assert who["edsider_web"] is False


# ---- through the API ----

def test_an_edsider_data_entry_operator_gets_in_as_employee(client, edsider):
    edsider["deo@infyinnos.com"] = DEOP
    r = client.get("/me", headers={HEADER: _token("deo@infyinnos.com")})
    assert r.status_code == 200
    assert {k: r.json()[k] for k in ("identity", "name", "role")} == {
        "identity": "deo@infyinnos.com", "name": "Deo Operator", "role": "employee"}
    # Their EdSider ids are kept, so a paper they send names them.
    with store.connect() as conn:
        row = store.lookup_staff(conn, "deo@infyinnos.com")
    assert (row["edsider_user_id"], row["edsider_staff_id"]) == (501, 77)


@pytest.mark.parametrize("person", [MASAD, None, {**DEOP, "role_code": "TEACH"}])
def test_anyone_edsider_does_not_list_as_a_deop_is_refused(client, edsider, person):
    if person:
        edsider["someone@infyinnos.com"] = person
    r = client.get("/me", headers={HEADER: _token("someone@infyinnos.com")})
    assert r.status_code == 403
    assert "not a Data Entry Operator in EdSider" in r.json()["detail"]


def test_this_apps_own_roles_do_not_count_for_an_edsider_web_sign_in(client, edsider, monkeypatch):
    # A superadmin here (bootstrap list, and a role set on Manage users) is
    # still only what EdSider says inside EdSider's pages.
    monkeypatch.setenv("EDSIDER_BOOTSTRAP_ADMINS", "boss@infyinnos.com")
    add_staff_member(StaffIn(email="boss@infyinnos.com", name="Boss", role="superadmin"), user=SUPER)
    assert client.get("/me", headers={HEADER: _token("boss@infyinnos.com")}).status_code == 403
    edsider["boss@infyinnos.com"] = DEOP
    assert client.get("/me", headers={HEADER: _token("boss@infyinnos.com")}).json()["role"] == "employee"


def test_a_deop_cannot_use_the_admin_routes(client, edsider):
    edsider["deo@infyinnos.com"] = DEOP
    assert client.get("/teachers", headers={HEADER: _token("deo@infyinnos.com")}).status_code == 403


def test_edsider_unreachable_is_a_503_not_a_refusal(client, edsider, monkeypatch):
    def down(email):
        raise OSError("connection refused")
    monkeypatch.setattr(edsider_staff, "edsider_person", down)
    r = client.get("/me", headers={HEADER: _token("deo@infyinnos.com")})
    assert r.status_code == 503


def test_no_edsider_database_set_up_is_a_503(client, edsider, monkeypatch):
    def unset(email):
        raise edsider_send.SendNotSetUp("EDSIDER_SEND_DB")
    monkeypatch.setattr(edsider_staff, "edsider_person", unset)
    assert client.get("/me", headers={HEADER: _token("deo@infyinnos.com")}).status_code == 503


def test_the_standalone_apps_edsider_sign_in_still_uses_the_staff_list(client, edsider, monkeypatch):
    monkeypatch.setenv("EDSIDER_TOKEN_AUDIENCES", "edsider-ai-client")
    add_staff_member(StaffIn(email="ad@infyinnos.com", name="Ad Min", role="admin"), user=SUPER)
    r = client.get("/me", headers={HEADER: _token("ad@infyinnos.com", aud="edsider-ai-client")})
    assert r.status_code == 200 and r.json()["role"] == "admin"
