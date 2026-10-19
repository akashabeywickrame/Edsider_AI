"""Forwarded Google/Microsoft ID tokens (src/id_tokens.py) and the gate
that uses them (service.py). Tokens are signed with a throwaway RSA key
made here, and the provider's key lookup is replaced with that key's
public half - everything else (signature, audience, issuer, expiry, the
staff list) is the real code."""

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import id_tokens
import service
from service import StaffIn, add_staff_member

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
GOOGLE_AUD = id_tokens.DEFAULT_AUDIENCES[0]
MS_AUD = id_tokens.DEFAULT_AUDIENCES[1]
SUPER = {"identity": "boss@example.com", "name": "Boss", "role": "superadmin"}
HEADER = "X-EdsiderAI-Id-Token"


def _token(key=KEY, **claims):
    now = int(time.time())
    base = {"iss": "https://accounts.google.com", "aud": GOOGLE_AUD, "iat": now,
            "exp": now + 3600, "email": "teacher@gmail.com", "email_verified": True}
    base.update(claims)
    return jwt.encode(base, key, algorithm="RS256")


def _ms_token(**claims):
    claims.setdefault("iss", "https://login.microsoftonline.com/e5b930c2-fcdd-4f7c-aa1a-dc6a03c6dc2b/v2.0")
    claims.setdefault("aud", MS_AUD)
    claims.setdefault("email_verified", None)
    claims.setdefault("email", None)
    return _token(**claims)


def _our_key(token, keys_url):
    return KEY.public_key()


@pytest.fixture(autouse=True)
def provider_keys(monkeypatch):
    monkeypatch.setattr(id_tokens, "_signing_key", _our_key)
    monkeypatch.delenv("EDSIDER_TOKEN_AUDIENCES", raising=False)


# ---- the token itself ----

def test_a_google_token_gives_its_verified_email():
    assert id_tokens.email_from_id_token(_token(email="Teacher@Gmail.com")) == "teacher@gmail.com"


def test_a_microsoft_token_uses_preferred_username():
    assert id_tokens.email_from_id_token(
        _ms_token(preferred_username="Akash@InfyInnos.com")) == "akash@infyinnos.com"


@pytest.mark.parametrize("bad, why", [
    (lambda: _token(key=OTHER_KEY), "signed with someone else's key"),
    (lambda: _token(aud="some-other-app.apps.googleusercontent.com"), "issued for another app"),
    (lambda: _token(exp=int(time.time()) - 600, iat=int(time.time()) - 4200), "expired"),
    (lambda: _token(email_verified=False), "Google has not verified the email"),
    (lambda: _token(iss="https://evil.example.com"), "not Google or Microsoft"),
    (lambda: "not-a-token", "garbage"),
])
def test_a_token_that_must_not_be_trusted_is_refused(bad, why):
    with pytest.raises(id_tokens.InvalidToken):
        id_tokens.email_from_id_token(bad())


def test_another_environment_can_be_added(monkeypatch):
    monkeypatch.setenv("EDSIDER_TOKEN_AUDIENCES", "qa-client-id")
    assert id_tokens.email_from_id_token(_token(aud="qa-client-id")) == "teacher@gmail.com"
    # Added to the built-in list, never replacing it: a mistyped setting
    # must not lock out an environment the code already knows.
    assert id_tokens.email_from_id_token(_token()) == "teacher@gmail.com"


def test_preprod_sign_ins_are_accepted(monkeypatch):
    # The client IDs pre-prod's own /.auth/login/* redirects name - without
    # them every pre-prod sign-in was refused and looped back to sign-in.
    monkeypatch.delenv("EDSIDER_TOKEN_AUDIENCES", raising=False)
    for aud in ("918577417431-pivh4gbds789a35mea310kupeavrkvj3.apps.googleusercontent.com",
                "b518f1fb-147e-4ad4-a0d3-0299afd61090"):
        assert id_tokens.email_from_id_token(_token(aud=aud)) == "teacher@gmail.com"


# ---- through the API, as the deployed page calls it ----

@pytest.fixture
def client(local_db, monkeypatch):
    for var in ("EDSIDER_REQUIRE_SIGNIN", "EDSIDER_BOOTSTRAP_ADMINS", "EDSIDER_API_KEY",
                "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    return TestClient(service.app)


def test_any_google_account_is_refused_unless_it_is_on_the_staff_list(client):
    r = client.get("/teachers", headers={HEADER: _token(email="anyone@gmail.com")})
    assert r.status_code == 403
    assert r.json()["detail"].startswith("anyone@gmail.com is not allowed")


def test_a_google_account_a_superadmin_added_gets_in_with_its_role(client):
    add_staff_member(StaffIn(email="emp@gmail.com", name="Emp One", role="employee"), user=SUPER)
    r = client.get("/me", headers={HEADER: _token(email="emp@gmail.com")})
    assert r.status_code == 200
    assert r.json() == {"identity": "emp@gmail.com", "name": "Emp One", "role": "employee"}


def test_an_expired_or_forged_token_is_a_401_so_the_page_signs_in_again(client):
    for token in (_token(exp=int(time.time()) - 600, iat=int(time.time()) - 4200),
                  _token(key=OTHER_KEY)):
        r = client.get("/me", headers={HEADER: token})
        assert r.status_code == 401 and "sign in again" in r.json()["detail"]


def test_on_azure_sign_in_is_required_without_any_setting(client, monkeypatch):
    # App Service sets WEBSITE_SITE_NAME itself - deploying is enough.
    monkeypatch.setenv("WEBSITE_SITE_NAME", "eds-dev-ai-api")
    assert client.get("/me").status_code == 401
    assert client.get("/teachers").status_code == 401
    assert client.get("/health").status_code == 200


def test_an_explicit_setting_still_wins_on_azure(client, monkeypatch):
    monkeypatch.setenv("WEBSITE_SITE_NAME", "eds-dev-ai-api")
    monkeypatch.setenv("EDSIDER_REQUIRE_SIGNIN", "false")
    assert client.get("/me").json()["identity"] == "reviewer"


def test_easy_auths_own_header_wins_over_a_forwarded_token(client):
    add_staff_member(StaffIn(email="admin@example.com", name="Ad Min", role="admin"), user=SUPER)
    r = client.get("/me", headers={"x-ms-client-principal-name": "admin@example.com",
                                   HEADER: _token(email="anyone@gmail.com")})
    assert r.json()["identity"] == "admin@example.com"
