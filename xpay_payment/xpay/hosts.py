"""XPay platform hosts and the allowlist for any URL a browser is sent to.

Credentials and base URLs resolve server-side only; this module never reads
either from request input. The allowlist is the sole control on API-returned
redirect/hosted-checkout URLs — nothing else in the stack validates them.
"""

from __future__ import annotations

from urllib.parse import urlsplit

API_BASE = "https://api.xpay.app"
CHECKOUT_BASE = "https://checkout.xpay.app"
SDK_URL = "https://checkout.xpay.app/v1/sdk.js"

ALLOWED_HOSTS = ("checkout.xpay.app", "api.xpay.app", "xpay.app")


def oauth_base(api_base: str) -> str:
    """The OAuth issuer, derived from the API base so the two can never point
    at different environments."""
    return api_base.rstrip("/") + "/api/auth"


def is_allowed_xpay_url(url: str) -> bool:
    """True when a browser may be sent to `url`.

    HTTPS only, host is one of `ALLOWED_HOSTS` or a subdomain of one.
    Backslashes and userinfo are rejected on sight — a parser-differential
    trap: some parsers read ``https://evil.com\\@xpay.app/`` as host
    ``xpay.app``, but a browser (WHATWG treats ``\\`` as ``/``) navigates to
    ``evil.com``. Neither ever appears in a legitimate XPay URL.
    """
    if "\\" in url:
        return False

    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        return False

    host = parts.hostname
    if not host:
        return False
    scheme = parts.scheme.lower()

    if scheme != "https":
        return False

    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS)
