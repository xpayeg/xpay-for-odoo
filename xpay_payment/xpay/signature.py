"""XPay-Signature verification: `t=<unix>,v1=<hex>` HMAC-SHA256 over raw bytes.

The signature is the only authentication on the public webhook endpoint, so
verification fails closed by design: every branch that is not a proven-valid
signature raises. Verify against the raw request bytes, never a re-encoded
JSON round-trip — whitespace and key-order differences break the HMAC.
"""

from __future__ import annotations

import hmac
import re
import time

from .errors import Codes, SignatureError

HEADER_NAME = "XPay-Signature"
DEFAULT_TOLERANCE = 300

# A v1 signature is a lowercase hex-encoded HMAC-SHA256 digest: always 64
# ASCII hex characters. Validating the shape here, before hmac.compare_digest
# ever sees the value, keeps a malformed or non-ASCII header on the existing
# SignatureError path instead of raising an unhandled TypeError
# (hmac.compare_digest refuses non-ASCII str operands).
_HEX64 = re.compile(r"[0-9a-f]{64}")


def parse_header(header: str) -> tuple[int, list[str]]:
    """Split `t=<unix>,v1=<hex>[,v1=<hex>...]` into `(timestamp, signatures)`.

    A non-digit `t` value is ignored rather than accepted loosely — a
    negative, fractional, or otherwise non-integer timestamp never counts as
    a timestamp at all. Raises `SignatureError` (`WEBHOOK_SIGNATURE_INVALID`)
    when no valid timestamp or no `v1` entry survives.
    """
    timestamp: int | None = None
    signatures: list[str] = []

    for part in header.split(","):
        pair = part.strip().split("=", 1)
        if len(pair) != 2:
            continue
        key, value = pair[0].strip(), pair[1].strip()
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1":
            lowered = value.lower()
            if _HEX64.fullmatch(lowered):
                signatures.append(lowered)

    if timestamp is None or not signatures:
        raise SignatureError(Codes.WEBHOOK_SIGNATURE_INVALID, "XPay-Signature header is malformed")

    return timestamp, signatures


def compute(secret: str, timestamp: int, raw_body: bytes) -> str:
    """`v1 = HMAC-SHA256(secret, "<timestamp>.<raw body>")`, lowercase hex."""
    signed_payload = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode("utf-8"), signed_payload, "sha256").hexdigest()


def verify(
    header: str | None,
    raw_body: bytes,
    secret: str,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: int | None = None,
) -> None:
    """Verify a webhook signature header against the raw request body.

    Raises `SignatureError` on any failure, with a `code` distinguishing a
    configuration fault (`WEBHOOK_NOT_CONFIGURED`, answered 500 so the
    platform keeps retrying) from every other rejection (answered 401).
    """
    if not secret:
        raise SignatureError(
            Codes.WEBHOOK_NOT_CONFIGURED, "No webhook signing secret is configured"
        )
    if not header or not header.strip():
        raise SignatureError(Codes.WEBHOOK_SIGNATURE_MISSING, "XPay-Signature header is missing")

    timestamp, signatures = parse_header(header)

    current = now if now is not None else int(time.time())
    if abs(current - timestamp) > tolerance:
        raise SignatureError(
            Codes.WEBHOOK_TIMESTAMP_OUT_OF_TOLERANCE,
            "Webhook timestamp is outside the allowed tolerance",
        )

    expected = compute(secret, timestamp, raw_body)
    for candidate in signatures:
        # hash_equals-equivalent: constant-time compare closes the
        # byte-by-byte timing side channel on signature guessing.
        if hmac.compare_digest(expected, candidate):
            return

    raise SignatureError(Codes.WEBHOOK_SIGNATURE_INVALID, "Webhook signature does not match")
