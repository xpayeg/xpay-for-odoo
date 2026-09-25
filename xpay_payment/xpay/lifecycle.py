"""Pure payment-lifecycle rules: payload + stored state -> verdict -> action.

Every function here is a pure function of its arguments — no I/O, no locks,
no odoo. The Odoo layer is the only writer and runs these under its own
per-order lock; this module only ever answers "what does this mean" and
"what should happen next".
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable

from .events import CHECKOUT_SESSION_ASYNC_PAYMENT_FAILED

PAID = "paid"
AWAITING = "awaiting"
EXPIRED = "expired"
FAILED = "failed"
UNKNOWN = "unknown"


def verdict(session: dict, event_type: str | None = None) -> str:
    """The session's payment verdict, computed in a fixed order.

    Order is load-bearing: an absent `paymentStatus` is `UNKNOWN` before
    anything else is even considered; it is never treated as paid. Only
    then does an `async_payment_failed` event force `FAILED`, then an
    expired session forces `EXPIRED`, then `status`+`paymentStatus`
    together decide `PAID` vs `AWAITING`.
    """
    payment_status = session.get("paymentStatus")
    if payment_status is None:
        return UNKNOWN

    if event_type == CHECKOUT_SESSION_ASYNC_PAYMENT_FAILED:
        return FAILED

    if session.get("status") == "expired" or session.get("isExpired"):
        return EXPIRED

    status = session.get("status")
    if status == "complete" and payment_status == "paid":
        return PAID
    if status == "complete" and payment_status == "unpaid":
        return AWAITING

    return UNKNOWN


CURRENT = "current"
SUPERSEDED = "superseded"
FOREIGN = "foreign"


def ownership(session_id: str, stored_id: str | None, superseded_ids: Iterable[str]) -> str:
    """Ownership trichotomy against the transaction's own stored ids.
    Existence is never ownership: a session id must match the CURRENT
    stored id, or appear in the SUPERSEDED ledger, or it is FOREIGN."""
    # compare_digest refuses non-ASCII input; an id that cannot even be
    # compared is foreign, not a server error.
    try:
        if stored_id is not None and hmac.compare_digest(session_id, stored_id):
            return CURRENT
    except TypeError:
        return FOREIGN
    if session_id in superseded_ids:
        return SUPERSEDED
    return FOREIGN


SET_DONE = "set_done"
SET_PENDING = "set_pending"
SET_CANCELED = "set_canceled"
SET_ERROR = "set_error"
PARK = "park"
NONE = "none"

_OPEN_STATES = ("draft", "pending")


def plan(verdict_value: str, tx_state: str) -> str:
    """The action a verdict implies for a transaction currently in `tx_state`.

    Guarded by `tx_state` throughout: out-of-order webhook delivery must
    never reopen or reclose a transaction whose state has already moved on
    for an unrelated reason.
    """
    if verdict_value == PAID:
        return PARK if tx_state in ("cancel", "error") else SET_DONE
    if verdict_value == AWAITING:
        return SET_PENDING if tx_state == "draft" else NONE
    if verdict_value == EXPIRED:
        # Only from `draft`: in this module `pending` always means a
        # payment reference was already issued (AWAITING) — the platform
        # never expires such a session, so a late or misrouted `expired`
        # event must not cancel it.
        return SET_CANCELED if tx_state == "draft" else NONE
    if verdict_value == FAILED:
        return SET_ERROR if tx_state in _OPEN_STATES else NONE
    return NONE


def remember_event(processed: list[str], event_id: str, cap: int = 20) -> list[str]:
    """Append `event_id` to the processed list, deduping and keeping only
    the newest `cap` entries."""
    deduped = [seen for seen in processed if seen != event_id]
    deduped.append(event_id)
    return deduped[-cap:]


def is_replay(processed: list[str], event_id: str) -> bool:
    return event_id in processed
