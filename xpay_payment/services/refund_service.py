"""Submit a refund request to XPay for a refund (child) transaction.

`POST /refunds`' `amount` is in the charge's PROCESSING currency; the child
refund transaction's own `amount` is in the order's PRESENTMENT currency.
The two agree only when the order was priced and processed in the same
currency; otherwise the presentment mirror recorded on the source
transaction at payment time (`xpay_presentment_json`) is the only way to
state the request in the currency the platform expects, and to check its
answer against something other than our own echoed number.
"""

from odoo import _
from odoo.exceptions import ValidationError

from ..xpay import idempotency, money
from ..xpay.errors import Codes, XPayApiError, XPayTransportError
from . import order_sync
from .logging import get_logger, log

_logger = get_logger(__name__)

_STATUS_PENDING = ("PENDING", "REQUIRES_ACTION")
_STATUS_FAILED = ("FAILED", "CANCELED")

# Answers the platform does not cache under the idempotency key it was
# sent with: a retry must reuse that same key rather than treat the
# attempt as refused. 408/429/503 are the transient HTTP statuses; a 409
# in-flight answer on a key already in use is the same case under a
# different shape, since the first attempt's own outcome is still pending.
# The same code at 400 is a different, definitive answer: the body sent
# under a reused key did not match the one first bound to it, which a
# retry with the same body — the only kind this module ever sends — can
# never fix.
_TRANSIENT_REFUND_STATUSES = frozenset({408, 429, 503})


def _is_transient_refund_failure(exc):
    return (
        isinstance(exc, XPayTransportError)
        or exc.http_status in _TRANSIENT_REFUND_STATUSES
        or (exc.code == Codes.IDEMPOTENCY_KEY_IN_USE and exc.http_status == 409)
    )


def submit_refund(refund_tx):
    """POST /refunds for `refund_tx` (a payment.transaction with
    operation == 'refund') and apply the response to it."""
    refund_tx.ensure_one()
    source_tx = refund_tx.source_transaction_id
    # Reading the source's completed and rejected refund counts below
    # decides this attempt's idempotency key; two submissions racing on the
    # same source without a lock could read the same counts and collide.
    # A lock failure means another submission is already in flight, not a
    # refusal of this one, so it must not mark this child rejected.
    try:
        order_sync.lock(source_tx)
    except order_sync.OrderLockBusy as exc:
        raise ValidationError(
            _("Another refund on this payment is in progress. Please retry in a moment.")
        ) from exc

    refund_children = source_tx.child_transaction_ids.filtered(
        lambda child: child.operation == "refund"
    )
    # The lock above only ever catches two submissions racing at the same
    # instant; a sibling that already got past it and is now waiting on
    # the platform's own answer is a different case the lock cannot see.
    # The completed-refund ledger just below cannot see it either — it
    # counts only children the platform has already answered — so left
    # unchecked, this attempt could compute a fullness or a count against
    # a ledger that is a moment away from changing under it.
    pending_sibling = refund_children.filtered(
        lambda child: child.id != refund_tx.id and child.state == "pending"
    )
    if pending_sibling:
        raise ValidationError(
            _("Another refund on this payment is in progress. Please retry in a moment.")
        )

    provider = refund_tx.provider_id
    # Money boundary: a same-plane reconnect whose setup never finished
    # must not submit a refund on the new, half-provisioned key.
    provider._xpay_require_setup_complete()
    currency = refund_tx.currency_id
    currency_code = currency.name.upper()

    amount_minor = _to_minor(currency, abs(refund_tx.amount))
    source_amount_minor = _to_minor(currency, source_tx.amount)

    # A sibling that failed on transport (no response at all) got no answer
    # from the platform, so it may have gone through anyway; unlike a
    # definitive rejection, it can never be ruled settled. Until it resolves,
    # only the identical amount may proceed — that is this same attempt
    # replaying its own key, never a second, different refund racing an
    # outcome that has not landed yet.
    unsettled_siblings = refund_children.filtered(
        lambda child: child.id != refund_tx.id and child.xpay_refund_awaiting_answer
    )
    mismatched_unsettled = unsettled_siblings.filtered(
        lambda child: _to_minor(child.currency_id, abs(child.amount)) != amount_minor
    )
    if mismatched_unsettled:
        raise ValidationError(
            _("Another refund on this payment is in progress. Please retry in a moment.")
        )

    presentment = source_tx.xpay_presentment_json
    presentment = presentment if isinstance(presentment, dict) else {}

    if presentment:
        processing_currency = (presentment.get("processing_currency") or "").upper()
        processing_amount_minor = presentment.get("processing_amount_minor")
        rate = presentment.get("rate")
    else:
        processing_currency = currency_code
        processing_amount_minor = source_amount_minor
        rate = None

    body = {
        "paymentIntentId": source_tx.xpay_payment_intent_id,
        "reason": "REQUESTED_BY_CUSTOMER",
    }

    completed_children = refund_children.filtered(lambda child: child.state == "done")
    recorded_count = len(completed_children)
    rejected_count = len(refund_children.filtered(lambda child: child.xpay_refund_rejected))
    completed_presentment_minor = sum(
        _to_minor(currency, abs(child.amount)) for child in completed_children
    )
    completed_processing_minor = sum(
        child.xpay_processing_amount_minor for child in completed_children
    )

    # An amount is always stated, in the charge's processing currency.
    # Leaving it out would let the platform refund whatever it currently
    # holds back, which is not always what this module asked for: it can
    # include a fee the platform passed through that was never approved
    # here, or leave out a refund issued from the platform's own dashboard
    # that this module has not recorded yet. Stating the amount keeps this
    # module the one place that decides what a refund actually moves.
    #
    # A refund is full when the completed children's amounts plus this one
    # account for the whole source amount — checked against this module's
    # own completed-refund ledger, never by comparing this request's own
    # amount to the source's total on its own, which would convert only
    # this slice on the last step of a multi-step refund and strand a
    # fraction of a unit that a fresh conversion of just that slice loses.
    is_full = completed_presentment_minor + amount_minor == source_amount_minor

    if is_full:
        # Reuses the processing-currency figures captured at payment time
        # and already sent, verbatim, rather than recomputing anything, so
        # nothing here can lose a fraction of a unit to truncation the way
        # a fresh conversion would.
        sent_processing_minor = processing_amount_minor - completed_processing_minor
    elif presentment:
        try:
            sent_processing_minor = money.presentment_to_processing(
                amount_minor, currency_code, processing_currency, rate
            )
        except TypeError as exc:
            # The mirror exists but carries no usable locked rate: converting
            # would either crash or guess, and a guess here is money moved on
            # a number nobody signed off on.
            log(_logger, "error", "refund.no_locked_rate", tx_reference=refund_tx.reference)
            raise ValidationError(
                _(
                    "This refund has no locked exchange rate. Refund the full amount instead, or"
                    " issue the partial refund from your XPay dashboard."
                )
            ) from exc
    else:
        sent_processing_minor = amount_minor

    body["amount"] = sent_processing_minor
    expected_processing_minor = sent_processing_minor
    refund_tx.xpay_processing_amount_minor = sent_processing_minor

    key = idempotency.bind_to_body(
        idempotency.refund_key(source_tx.reference, recorded_count, rejected_count, amount_minor),
        body,
    )

    refund_tx.write({"xpay_refund_awaiting_answer": True, "xpay_refund_idempotency_key": key})
    try:
        refund = provider._xpay_client().create_refund(body, idempotency_key=key)
    except XPayApiError as exc:
        # A transport failure or a transient answer carries no definitive
        # refusal, so the same key must still replay it on the next
        # attempt; anything else is a definite answer from the platform,
        # and the platform caches that answer under this same key for a
        # day, so the next attempt needs a different one or it would just
        # replay this same refusal.
        if not _is_transient_refund_failure(exc):
            refund_tx.xpay_refund_rejected = True
            # A definitive refusal under this key answers for every earlier
            # attempt still waiting under the same key: none of them was
            # honoured either.
            _settle_awaiting(refund_tx, refund_children)
        log(
            _logger,
            "error",
            "refund.request_failed",
            tx_reference=refund_tx.reference,
            code=exc.code,
        )
        # ValidationError, not UserError: `_refund()` (payment/models/
        # payment_transaction.py) only catches ValidationError around this
        # call, turning it into a clean `_set_error` on the refund
        # transaction instead of an uncaught exception.
        if exc.code == Codes.RESOURCE_INVALID_STATE:
            raise ValidationError(
                _(
                    "XPay is still processing a previous refund on this payment. Please retry"
                    " in a moment."
                )
            ) from exc
        # exc.message is text echoed from XPay's own API response — never a
        # shopper-facing surface. The code and full message are already in
        # the log line above.
        raise ValidationError(
            _("XPay could not process this refund (%(code)s). Please try again.", code=exc.code)
        ) from exc

    # Any response settles this attempt, and with it every earlier attempt
    # under the same key: the platform holds one outcome per key, and this
    # is it, so a sibling whose own response was lost has its answer now.
    _settle_awaiting(refund_tx, refund_children)

    refund_id = refund.get("id")
    if isinstance(refund_id, str) and refund_id:
        refund_tx.provider_reference = refund_id

    status = refund.get("status")
    status = status.upper() if isinstance(status, str) else None
    if status == "SUCCEEDED":
        if _amount_mismatch(refund, expected_processing_minor, processing_currency):
            log(
                _logger,
                "critical",
                "refund.amount_mismatch",
                tx_reference=refund_tx.reference,
                expected_minor=expected_processing_minor,
                expected_currency=processing_currency,
                actual_amount=refund.get("amount"),
                actual_currency=refund.get("currency"),
            )
            refund_tx._set_error(
                _("XPay's refund confirmation did not match the amount requested.")
            )
            return
        refund_tx._set_done()
    elif status in _STATUS_PENDING:
        refund_tx._set_pending()
    elif status in _STATUS_FAILED:
        # A refund the platform creates and then refuses is still a
        # definitive answer, exactly like an error response: the next
        # attempt's idempotency key must account for it, or it would just
        # replay this same refusal.
        refund_tx.xpay_refund_rejected = True
        refund_tx._set_error(_("XPay reported that this refund failed or was canceled."))
    else:
        refund_tx._set_error(_("XPay returned an unrecognized refund status."))


def _settle_awaiting(refund_tx, refund_children):
    """Clear the awaiting-answer flag on this attempt and on every sibling
    that was sent under the same key and never heard back."""
    refund_tx.xpay_refund_awaiting_answer = False
    key = refund_tx.xpay_refund_idempotency_key
    same_key_siblings = refund_children.filtered(
        lambda child: child.id != refund_tx.id
        and child.xpay_refund_awaiting_answer
        and child.xpay_refund_idempotency_key == key
    )
    if same_key_siblings:
        same_key_siblings.write({"xpay_refund_awaiting_answer": False})


def _to_minor(currency, amount):
    return money.to_minor(f"{currency.round(amount):.{currency.decimal_places}f}", currency.name)


def _amount_mismatch(refund, expected_minor, expected_currency):
    """True unless the response states exactly the expected processing-
    currency amount: the platform echoes back the same integer this module
    sent, so an exact match is what a correct response looks like."""
    # The platform serialises the amount as a number on GET and as text on
    # the POST /refunds answer; both are the same integer minor amount.
    actual_minor = money.parse_minor(refund.get("amount"))
    actual_currency = refund.get("currency")
    if (
        actual_minor is None
        or not isinstance(expected_minor, int)
        or not isinstance(actual_currency, str)
        or not actual_currency
    ):
        return True
    if actual_currency.upper() != expected_currency:
        return True
    return actual_minor != expected_minor
