"""OAuth 2.1 + PKCE ("Connect"): client registration, the authorize URL,
PKCE, and token-response validation.

The handshake's only deliverable is two freshly minted keys; the access
token itself is discarded by every caller and never returned from here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

REGISTRATION_MAX_AGE_DAYS = 6

_STATE_BYTES = 32
_VERIFIER_BYTES = 96  # base64url-encodes to 128 chars, RFC 7636's upper bound.


def generate_state() -> str:
    """An opaque, single-use anti-forgery token."""
    return secrets.token_urlsafe(_STATE_BYTES)


def generate_verifier() -> str:
    """A PKCE code verifier: 43-128 unreserved characters (RFC 7636 section 4.1)."""
    return secrets.token_urlsafe(_VERIFIER_BYTES)


def challenge(verifier: str) -> str:
    """S256 code challenge: `base64url(SHA-256(verifier))`, no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def registration_body(client_name: str, client_uri: str, redirect_uri: str) -> dict:
    """RFC 7591 dynamic client registration document for a public PKCE client."""
    return {
        "client_name": client_name,
        "client_uri": client_uri,
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
    }


def scope_for_plane(plane: str) -> str:
    return f"merchant.connect.{plane}"


def client_needs_registration(
    client_id: str | None,
    stored_uri: str | None,
    current_uri: str,
    registered_at,
    now,
    completed_at,
) -> bool:
    """Whether a Connect click must (re)register the OAuth client before
    authorizing.

    True when there is no client id, no stored redirect URI, or no
    registration timestamp: nothing to reuse. True when the stored
    redirect URI no longer matches this site's own current callback (the
    site moved host) — a moved host re-registers whether or not the old
    client ever completed a consent, since the platform would refuse a
    client registered under a redirect URI it no longer serves. Once a
    client has completed a consent it is reused indefinitely: the
    platform never reaps a client that has finished at least one
    authorization, and a fresh client id would leave the previous
    restricted key live at the platform, because a token exchange only
    retires the key minted for the SAME client id. A client that has
    never completed a consent is re-registered once it is at least
    `REGISTRATION_MAX_AGE_DAYS` old, a day ahead of the platform's own
    reap of never-completed clients, so this module never sends a
    browser to authorize a client id the platform has already dropped.

    `registered_at` and `completed_at` are each a `datetime` or `None`;
    `now` is a `datetime`."""
    if not client_id or not stored_uri or registered_at is None:
        return True
    if stored_uri != current_uri:
        return True
    if completed_at is not None:
        return False
    return (now - registered_at) >= timedelta(days=REGISTRATION_MAX_AGE_DAYS)


def states_match(candidate: str, expected: str) -> bool:
    """Constant-time compare of the callback's `state` against the one
    minted at `start`.

    `candidate` is attacker-controlled query input. `hmac.compare_digest()`
    raises `TypeError` when a `str` operand contains non-ASCII characters, so
    the shape is checked first — a candidate that fails the check is simply
    not a match, never an unhandled crash of the callback route.
    """
    return bool(candidate) and candidate.isascii() and hmac.compare_digest(candidate, expected)


def authorize_url(
    oauth_base: str,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
) -> str:
    """The `/oauth2/authorize` URL to redirect the browser to."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{oauth_base}/oauth2/authorize?{query}"


def token_request_form(*, code: str, redirect_uri: str, client_id: str, verifier: str) -> dict:
    """Form fields for `POST /oauth2/token` (`authorization_code` grant)."""
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }


@dataclass(frozen=True)
class ConnectResult:
    merchant_id: str
    plane: str
    restricted_key: str
    publishable_key: str


def parse_token_response(body: dict, expected_plane: str) -> ConnectResult:
    """Validate the token response: mode echo plus both key prefixes.

    Any failure means nothing gets written — a live answer to a test
    flow, or a malformed key, must never be provisioned.
    """
    if not isinstance(body, dict):
        raise ValueError("Token response is not an object")

    mode = body.get("xpay_mode")
    if mode != expected_plane:
        raise ValueError(
            f"Token response mode {mode!r} does not match requested plane {expected_plane!r}"
        )

    merchant_id = body.get("xpay_merchant_id")
    if not isinstance(merchant_id, str) or not merchant_id:
        raise ValueError("Token response is missing xpay_merchant_id")

    restricted_key = body.get("xpay_restricted_key")
    if not isinstance(restricted_key, str) or not restricted_key.startswith(
        f"rk_{expected_plane}_"
    ):
        raise ValueError(f"Restricted key is missing or not prefixed rk_{expected_plane}_")

    publishable_key = body.get("xpay_publishable_key")
    if not isinstance(publishable_key, str) or not publishable_key.startswith(
        f"pk_{expected_plane}_"
    ):
        raise ValueError(f"Publishable key is missing or not prefixed pk_{expected_plane}_")

    return ConnectResult(
        merchant_id=merchant_id,
        plane=mode,
        restricted_key=restricted_key,
        publishable_key=publishable_key,
    )


def classify_exchange_failure(http_status: int | None) -> str:
    """`'no_commit'` when a 5xx guarantees no key was minted (safe to retry
    blind); `'ambiguous'` otherwise — the authorization code is single-use,
    so a timeout or any other non-2xx may already have minted a key."""
    if http_status is not None and 500 <= http_status < 600:
        return "no_commit"
    return "ambiguous"
