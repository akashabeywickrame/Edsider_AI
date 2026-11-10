"""Who is calling, from a Google, Microsoft or EdSider (Azure AD B2C) ID
token the page forwards.

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

# Which apps' tokens are accepted - the client IDs each environment's web
# app sends people to at sign-in (public identifiers, not secrets): its
# Google OAuth client and its Microsoft app registration, read off the
# /.auth/login/* redirects. A token Google issued to some OTHER app carries
# a real email too, so without this check any site a person ever signed in
# to could replay their token here. Every environment has its own pair, and
# a token from one missing here is refused as if forged - which is how
# pre-prod's sign-in looped back to the sign-in page. EDSIDER_TOKEN_AUDIENCES
# adds more (another environment) to these; it does not replace them, so a
# mistyped setting cannot lock out an environment listed here.
DEFAULT_AUDIENCES = (
    # dev (eds-dev-ai-web)
    "326928994683-7jb55oe1agr77kqsolfea9k0u21eh5th.apps.googleusercontent.com",
    "cc17167b-b571-4af2-b16a-c30d3834e932",
    # pre-prod (eds-preprod-ai-web)
    "918577417431-pivh4gbds789a35mea310kupeavrkvj3.apps.googleusercontent.com",
    "b518f1fb-147e-4ad4-a0d3-0299afd61090",
)

# EdSider's own web app, whose Assess pages now hold data entry: they
# forward the ID token EdSider's sign-in issued to EdSider's web client -
# not to this app's registration, as single sign-on above does. A B2C token
# for one of these is a sign-in from inside EdSider, whose role EdSider
# decides (service._edsider_identity). Dev's client is here;
# EDSIDER_WEB_CLIENT_IDS (comma-separated) adds other environments'.
DEFAULT_EDSIDER_WEB_CLIENTS = (
    "0d6203b7-f6ea-42ce-9fbc-1098d0458f54",     # dev (logindev.edsider.com)
)

# EdSider's own sign-in (Azure AD B2C, on EdSider's domain) - single sign-on:
# someone already signed in to EdSider who follows its Extract Paper button
# is signed in here without being asked again, once this app's Easy Auth
# lists EdSider as an OpenID Connect provider. Issuer -> its signing keys,
# read off the tenant's openid-configuration; dev's is here, and
# EDSIDER_B2C_ISSUERS / EDSIDER_B2C_KEYS (comma-separated, in the same
# order) add other environments' tenants. The client id of the app
# registration Easy Auth signs in with goes in EDSIDER_TOKEN_AUDIENCES.
DEFAULT_B2C = {
    # dev (logindev.edsider.com, edsiderdevnew, B2C_1A_signup_signin)
    "https://logindev.edsider.com/62c98af5-9dcb-443e-8a6c-b63c0350f910/v2.0/":
        "https://logindev.edsider.com/edsiderdevnew.onmicrosoft.com/"
        "b2c_1a_signup_signin/discovery/v2.0/keys",
}

# A minute's grace either side for clock differences between machines.
LEEWAY_SECONDS = 60

_key_clients = {}


class InvalidToken(Exception):
    """The token was not accepted - the message says why, for the log."""


def _listed(name):
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def audiences():
    configured = _listed("EDSIDER_TOKEN_AUDIENCES") + edsider_web_clients()
    return list(DEFAULT_AUDIENCES) + [a for a in configured if a not in DEFAULT_AUDIENCES]


def edsider_web_clients():
    """EdSider's web app's client ids - see DEFAULT_EDSIDER_WEB_CLIENTS."""
    listed = _listed("EDSIDER_WEB_CLIENT_IDS")
    return list(DEFAULT_EDSIDER_WEB_CLIENTS) + [c for c in listed
                                                if c not in DEFAULT_EDSIDER_WEB_CLIENTS]


def b2c_issuers():
    """{issuer: keys url} for every EdSider B2C tenant whose tokens count."""
    def listed(name):
        return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]
    return {**DEFAULT_B2C, **dict(zip(listed("EDSIDER_B2C_ISSUERS"), listed("EDSIDER_B2C_KEYS")))}


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
    return identity_from_id_token(token, key_for)["email"]


def identity_from_id_token(token, key_for=None):
    """{"email", "edsider_web"} - the verified email, and whether EdSider's
    sign-in (B2C) issued the token to EdSider's own web app, i.e. the caller
    is data entry inside EdSider's Assess pages: that sign-in takes its role
    from EdSider, not from this app's staff list (service._resolve_identity)."""
    key_for = key_for or _signing_key
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise InvalidToken(f"not a token: {e}")
    issuer = unverified.get("iss", "")
    b2c = b2c_issuers()
    if issuer in GOOGLE_ISSUERS:
        keys_url = GOOGLE_KEYS
    elif issuer in b2c:
        keys_url = b2c[issuer]
    elif issuer.startswith(MICROSOFT_ISSUER_PREFIX):
        keys_url = MICROSOFT_KEYS
    else:
        raise InvalidToken(f"not issued by Google, Microsoft or EdSider: {issuer!r}")

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
    elif issuer in b2c:
        # EdSider's sign-in verifies the address before issuing it; some
        # B2C policies send it as a one-item list instead.
        email = claims.get("email") or (claims.get("emails") or [None])[0]
    else:
        # Microsoft puts the sign-in address in preferred_username; email
        # is only present when the account has one set.
        email = claims.get("email") or claims.get("preferred_username")
    if not email or "@" not in email:
        raise InvalidToken("no email in the token")
    aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    edsider_web = issuer in b2c and any(a in edsider_web_clients() for a in aud)
    return {"email": email.strip().lower(), "edsider_web": edsider_web}

