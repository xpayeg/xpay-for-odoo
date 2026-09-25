"""Webhook event-name registry: exact wire strings, the subscription list,
and the two event families the receiver routes on.

Unsubscribed/unknown event types are acknowledged 200 and ignored — the
receiver must stay forward-compatible; XPay adds event types without notice.
"""

from __future__ import annotations

CHECKOUT_SESSION_COMPLETED = "checkout.session.completed"
CHECKOUT_SESSION_EXPIRED = "checkout.session.expired"
CHECKOUT_SESSION_ASYNC_PAYMENT_SUCCEEDED = "checkout.session.async_payment_succeeded"
CHECKOUT_SESSION_ASYNC_PAYMENT_FAILED = "checkout.session.async_payment_failed"

CHARGE_REFUNDED = "charge.refunded"
REFUND_FAILED = "refund.failed"
REFUND_CREATED = "refund.created"

PAYMENT_INTENT_PAYMENT_FAILED = "payment_intent.payment_failed"

CHECKOUT_EVENTS = (
    CHECKOUT_SESSION_COMPLETED,
    CHECKOUT_SESSION_EXPIRED,
    CHECKOUT_SESSION_ASYNC_PAYMENT_SUCCEEDED,
    CHECKOUT_SESSION_ASYNC_PAYMENT_FAILED,
)

REFUND_EVENTS = (
    CHARGE_REFUNDED,
    REFUND_FAILED,
    REFUND_CREATED,
)

# A decline is order history, never a state transition: the receiver notes
# it and moves on. Kept as its own family rather than folded into
# CHECKOUT_EVENTS because its payload carries a payment intent, not a
# checkout session.
DECLINE_EVENTS = (PAYMENT_INTENT_PAYMENT_FAILED,)

# Every event type the module's webhook endpoint registers for and the
# receiver applies. Everything else is acknowledged 200 and ignored.
SUBSCRIBED = CHECKOUT_EVENTS + REFUND_EVENTS + DECLINE_EVENTS


def is_checkout_event(event_type: str) -> bool:
    return event_type in CHECKOUT_EVENTS


def is_refund_event(event_type: str) -> bool:
    return event_type in REFUND_EVENTS


def is_decline_event(event_type: str) -> bool:
    return event_type in DECLINE_EVENTS


def charge_refunds(charge: object) -> list:
    """The refunds a charge carries, newest first as the platform orders
    them (`ChargeResponse.refunds` is an array). A `{"data": [...]}`
    wrapper is accepted too, so a Stripe-shaped payload never crashes the
    receiver. Anything else is an empty list."""
    refunds = charge.get("refunds") if isinstance(charge, dict) else None
    if isinstance(refunds, dict):
        refunds = refunds.get("data")
    if not isinstance(refunds, list):
        return []
    return [refund for refund in refunds if isinstance(refund, dict)]
