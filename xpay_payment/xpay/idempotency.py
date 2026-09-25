"""Deterministic idempotency keys for XPay writes.

Every key names a logical operation, never a random value, so a transport
retry of the same operation composes the same key and replays instead of
re-executing. The platform binds a key to the exact body it first saw —
callers must send an unchanged body whenever they reuse one of these keys.
"""

from __future__ import annotations

import hashlib
import json
import re

MAX_LENGTH = 255
_BODY_HASH_LENGTH = 12
_REF_DIGEST_LENGTH = 16

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

# `bind_to_body` always appends "_" + a 12-char body hash to whatever key
# these builders return (every call site pipes straight into it) — reserving
# that room here means bind_to_body's own truncation is a no-op for these
# keys, so a long reference is never cut twice: once here, once again in
# bind_to_body, which would otherwise re-truncate a discriminator suffix this
# module just went out of its way to protect.
_BUDGET = MAX_LENGTH - _BODY_HASH_LENGTH - 1


def _sanitize(reference: str) -> str:
    return _UNSAFE.sub("-", reference)


def _keyed(prefix: str, reference: str, suffix: str) -> str:
    """`prefix + reference + suffix`, kept within `_BUDGET`.

    `prefix` and `suffix` carry the discriminator that makes two logically
    different operations (a different attempt, a refund, a webhook op) produce
    different keys, so they are never truncated. When `reference` alone does
    not fit the remaining room, it is cut short and a digest of the FULL
    reference is appended in its place — a blind truncation of the whole
    composed string can drop the discriminator entirely, so two different
    operations on the same long reference would silently share one
    idempotency key.
    """
    room = _BUDGET - len(prefix) - len(suffix)
    if len(reference) <= room:
        return f"{prefix}{reference}{suffix}"
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:_REF_DIGEST_LENGTH]
    keep = max(room - len(digest) - 1, 0)
    return f"{prefix}{reference[:keep]}_{digest}{suffix}"


def session_key(reference: str, attempt: int, *, retry: bool = False) -> str:
    """One key per payment attempt on an order — a new attempt gets a fresh
    key. `retry` marks the one deliberate re-send of an attempt with a
    changed body (a stale customer link dropped): the same key with a
    different body would be refused as a fingerprint mismatch."""
    return _keyed("odoo_", _sanitize(reference), f"_s{attempt}{'r' if retry else ''}")


def refund_key(
    source_reference: str, recorded_count: int, rejected_count: int, amount_minor: int
) -> str:
    """One key per (source transaction, count of that source's completed
    refunds, count of its definitively rejected attempts, amount): a retry
    of the same logical refund after a lost response names the same
    completed and rejected counts and the same amount, so it replays; a
    refund that follows one already completed, or one the platform has
    definitively refused, sees one of the two counts move and gets a fresh
    key. The completed count must come from the source's own refund
    children, filtered to the ones that finished — a count that also
    includes attempts that errored out would hand a brand new refund the
    key an earlier, failed attempt already used. The rejected count is kept
    separate from the completed one: a transport failure (no answer at
    all) must keep replaying the same key, while a real refusal must not,
    since the platform caches that refusal under the key for a day."""
    return _keyed(
        "odoo_ref_",
        _sanitize(source_reference),
        f"_n{recorded_count}_x{rejected_count}_{amount_minor}",
    )


def webhook_key(op_id: str) -> str:
    """Keyed on a persisted operation id, never re-derived from wall-clock
    time — a transport retry of the same save must replay, not create a
    second endpoint."""
    return _keyed("odoo_wh_", _sanitize(op_id), "")


def bind_to_body(key: str, body: dict) -> str:
    """Suffix `key` with a hash of `body`'s canonical JSON serialization,
    keeping the total under `MAX_LENGTH`.

    The platform binds a key to the exact request body it first saw and
    refuses a mismatch (`idempotency_key_in_use`): body-binding keeps a
    transport retry of the same operation replayable while letting a
    legitimately changed body (a shopper editing an amount) compose a
    different key. `sort_keys=True` and no extra whitespace make the
    serialization deterministic across calls with the same body.
    """
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:_BODY_HASH_LENGTH]
    truncated = key[:_BUDGET]
    return f"{truncated}_{digest}"
