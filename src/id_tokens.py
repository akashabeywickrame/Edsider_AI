"""Who is calling, from a Google or Microsoft ID token the page forwards.

Why this exists: the page (eds-dev-ai-web) and the API (eds-dev-ai-api)
are separate App Services. Easy Auth signs a person in to the PAGE's app
and tells only that app who they are; a browser never carries that
sign-in to the API, so every API call arrived anonymous - treated as a
local run, full access, the staff list never consulted. Anyone with any
Google account could use every page.

The page can read its own Easy Auth session (/.auth/me), which holds the
ID token Google or Microsoft issued at sign-in, and send it along. That
token is signed by Google/Microsoft, so the API checks it itself rather
than trusting the page: signature against the provider's published keys,
that it was issued for THIS app (audience), and that it has not expired.
Only then is its email used - and the staff list still decides.

Not in store.py: no database here, only the providers' public keys.
"""

import os

import jwt
from jwt import PyJWKClient

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_KEYS = "https://www.googleapis.com/oauth2/v3/certs"
MICROSOFT_ISSUER_PREFIX = "https://login.microsoftonline.com/"
MICROSOFT_KEYS = "https://login.microsoftonline.com/common/discovery/v2.0/keys"

# Which apps' tokens are accepted - the client IDs the eds-dev-ai-web
# login redirects themselves name (public identifiers, not secrets):
# its Google OAuth client and its Microsoft app registration. A token
# Google issued to some OTHER app carries a real email too, so without
# this check any site a person ever signed in to could replay their
# token here. Another environment sets EDSIDER_TOKEN_AUDIENCES instead.
DEFAULT_AUDIENCES = (
    "326928994683-7jb55oe1agr77kqsolfea9k0u21eh5th.apps.googleusercontent.com",
    "cc17167b-b571-4af2-b16a-c30d3834e932",
)

# A minute's grace either side for clock differences between machines.
LEEWAY_SECONDS = 60

_key_clients = {}


class InvalidToken(Exception):
    """The token was not accepted - the message says why, for the log."""


def audiences():
    configured = os.environ.get("EDSIDER_TOKEN_AUDIENCES", "")
    listed = [a.strip() for a in configured.split(",") if a.strip()]
    return listed or list(DEFAULT_AUDIENCES)


def _signing_key(token, keys_url):
    # PyJWKClient caches the provider's keys, so this is a network fetch
    # once in a while, not per request.
    client = _key_clients.get(keys_url)
    if client is None:
        client = _key_clients[keys_url] = PyJWKClient(keys_url, cache_keys=True, lifespan=3600)
    return client.get_signing_key_from_jwt(token).key


def email_from_id_token(token, key_for=None):
    """The verified, lower-cased email in a Google or Microsoft ID token,
    or InvalidToken. `key_for(token, keys_url)` is for tests."""
    key_for = key_for or _signing_key
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise InvalidToken(f"not a token: {e}")
    issuer = unverified.get("iss", "")
    if issuer in GOOGLE_ISSUERS:
        keys_url = GOOGLE_KEYS
    elif issuer.startswith(MICROSOFT_ISSUER_PREFIX):
        keys_url = MICROSOFT_KEYS
    else:
        raise InvalidToken(f"not issued by Google or Microsoft: {issuer!r}")

    try:
        claims = jwt.decode(
            token, key_for(token, keys_url), algorithms=["RS256"],
            audience=audiences(), issuer=issuer, leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "aud", "iss"], "verify_iat": False},
        )
    except jwt.PyJWTError as e:
        raise InvalidToken(str(e))
    if keys_url == GOOGLE_KEYS:
        # An unverified Google email could be one the person merely typed.
        if claims.get("email_verified") not in (True, "true"):
            raise InvalidToken("Google has not verified this email")
        email = claims.get("email")
    else:
        # Microsoft puts the sign-in address in preferred_username; email
        # is only present when the account has one set.
        email = claims.get("email") or claims.get("preferred_username")
    if not email or "@" not in email:
        raise InvalidToken("no email in the token")
    return email.strip().lower()

