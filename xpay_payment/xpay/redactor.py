"""Deep redaction of secrets and PII before anything reaches a log record.

Conservative by design: false positives are acceptable, false negatives are
not. `redact()` never mutates its input — every call returns a new structure.
"""

from __future__ import annotations

import re

SECRET_KEYS = frozenset(
    {
        "api_key",
        "secret",
        "secret_key",
        "client_secret",
        "clientsecret",
        "webhook_secret",
        "whsec",
        "restricted_key",
        "xpay_restricted_key",
        "xpay_webhook_secret",
        "code_verifier",
        "code_challenge",
        "authorization",
        "cookie",
        "set-cookie",
        "password",
        "pass",
        "token",
        "access_token",
        "refresh_token",
        "card_number",
        "cardnumber",
        "pan",
        "cvv",
        "cvc",
        "card_cvv",
        "security_code",
    }
)

# xpay_publishable_key is deliberately absent from both sets: it is the one
# XPay key meant to reach a browser, so it is not a secret and not PII.

PII_KEYS = frozenset(
    {
        "email",
        "billing_email",
        "phone",
        "phone_number",
        "billing_phone",
        "name",
        "first_name",
        "last_name",
        "billing_first_name",
        "billing_last_name",
        "address",
        "billing_address",
        "shipping_address",
    }
)

_MASK = "[REDACTED]"

# A run of 13-19 digits (optionally space/dash separated), checked with Luhn
# below before masking — an arbitrary long number (an order id, say) is not
# scrubbed just because it happens to be the right length.
_PAN_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_API_KEY_PARAM = re.compile(
    r"((?:x-)?api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,&}]+", re.IGNORECASE
)
# rk_/sk_ only: pk_ (publishable) is not secret, so its shape is never scrubbed.
_SECRET_KEY_SHAPED = re.compile(r"((?:sk|rk)_(?:live|test)_)[A-Za-z0-9]+")
_WHSEC_SHAPED = re.compile(r"(whsec_)[A-Za-z0-9_-]+")

_KEY_PREFIX = re.compile(r"^(?:sk|rk|pk|whsec)_(?:live_|test_)?")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _scrub_string(text: str) -> str:
    def replace_pan(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return _MASK if _luhn_valid(digits) else match.group(0)

    text = _PAN_CANDIDATE.sub(replace_pan, text)
    text = _BEARER.sub(r"\1[REDACTED]", text)
    text = _API_KEY_PARAM.sub(r"\1[REDACTED]", text)
    text = _SECRET_KEY_SHAPED.sub(r"\1[REDACTED]", text)
    text = _WHSEC_SHAPED.sub(r"\1[REDACTED]", text)
    return text


def _mask_pii(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def redact(value, *, max_depth: int = 10):
    """Recursively redact `value`. Returns a new structure; never mutates
    the input. `max_depth` guards against a pathological (or adversarial)
    payload recursing without bound."""
    if max_depth < 0:
        return "[TRUNCATED]"

    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            lowered = key.lower() if isinstance(key, str) else key
            if lowered in SECRET_KEYS:
                out[key] = _MASK
            elif lowered in PII_KEYS:
                if isinstance(val, (dict, list)):
                    out[key] = redact(val, max_depth=max_depth - 1)
                elif val is None:
                    out[key] = val
                else:
                    out[key] = _mask_pii(str(val))
            else:
                out[key] = redact(val, max_depth=max_depth - 1)
        return out
    if isinstance(value, list):
        return [redact(item, max_depth=max_depth - 1) for item in value]
    if isinstance(value, str):
        return _scrub_string(value)
    return value


def mask_key(key: str) -> str:
    """A key safe to log: recognizable prefix, elided middle, last 4 chars —
    `"rk_test_...4aE6"` -> `"rk_test_…4aE6"`."""
    match = _KEY_PREFIX.match(key)
    prefix = match.group(0) if match else key[:8]
    return f"{prefix}…{key[-4:]}"
