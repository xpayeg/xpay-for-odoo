"""The only writer of XPay-driven payment.transaction transitions.

Both async paths (webhook events) and the sync path (the return route)
funnel through `apply_locked`, so "payment done exactly once" is enforced
in one place.
"""

import psycopg2
from odoo import _

from ..xpay import lifecycle, money
from . import escalation
from .logging import get_logger, log

_logger = get_logger(__name__)

# A distinct object, never a plain string: comparing an Odoo recordset to a
# string with `==` logs the ORM's own type-mismatch warning on every single
# call, including the ordinary case where `_adopt_unresolved_sibling` returns
# an ordinary (non-ambiguous) recordset.
_ADOPTION_AMBIGUOUS = object()


class OrderLockBusy(Exception):
    """The transaction's row is held by another request right now."""


def lock(tx):
    """Take a blocking-free row lock on `tx`, then invalidate its cache so
    the next field read sees whatever the previous holder committed."""
    tx.ensure_one()
    try:
        with tx.env.cr.savepoint():
            tx.env.cr.execute(
                "SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE NOWAIT", [tx.id]
            )
    except psycopg2.errors.LockNotAvailable as exc:
        raise OrderLockBusy(f"payment.transaction {tx.id} is locked by another request") from exc
    tx.invalidate_recordset()


def apply_locked(tx, payment_data):
    """Apply one verified payload to `tx` under the per-transaction lock:
    dedupe by event id, check ownership, read the verdict, plan the
    transition, apply it through the native setters, then remember the
    event. Returns the action taken, mostly for tests.
    """
    lock(tx)

    processed = list(tx.xpay_processed_event_ids or [])
    event_id = payment_data.get("event_id")
    if event_id and lifecycle.is_replay(processed, event_id):
        return "replay"

    session = payment_data.get("session")
    if isinstance(session, dict):
        action = _apply_session_payload(tx, payment_data, session)
    else:
        _apply_refund_update(tx, payment_data)
        action = "refund_updated"

    if event_id:
        tx.xpay_processed_event_ids = lifecycle.remember_event(processed, event_id)

    return action


def note_declined(tx, intent, event_id):
    """Record a decline on `tx` from a payment_intent.payment_failed
    event: a note and a log row, never a state change, since the shopper
    may still succeed on a later attempt.
    """
    lock(tx)

    processed = list(tx.xpay_processed_event_ids or [])
    if event_id and lifecycle.is_replay(processed, event_id):
        return

    if tx.state != "done":
        error = intent.get("lastPaymentError") if isinstance(intent, dict) else None
        error = error if isinstance(error, dict) else {}
        code = error.get("declineCode") or error.get("code") or "unknown"
        message = error.get("merchantMessage") or error.get("message") or ""

        log(_logger, "info", "order_sync.payment_declined", tx_reference=tx.reference, code=code)

        note = _(
            "XPay payment attempt declined [%(code)s]: %(message)s The shopper can retry; the"
            " transaction is unchanged.",
            code=code,
            message=message or "—",
        )
        orders = tx.sale_order_ids if "sale_order_ids" in tx._fields else tx.browse()
        invoices = tx.invoice_ids if "invoice_ids" in tx._fields else tx.browse()
        for order in orders:
            order.message_post(body=note)
        for invoice in invoices:
            invoice.message_post(body=note)

    if event_id:
        tx.xpay_processed_event_ids = lifecycle.remember_event(processed, event_id)


def _apply_session_payload(tx, payment_data, session):
    incoming_id = session.get("id") or ""
    relation = lifecycle.ownership(
        incoming_id, tx.xpay_session_id, tx.xpay_superseded_session_ids or []
    )
    if relation == lifecycle.FOREIGN:
        return "foreign"

    verdict = lifecycle.verdict(session, payment_data.get("event_type"))

    if relation == lifecycle.SUPERSEDED:
        if verdict == lifecycle.PAID:
            _park(tx, session, "superseded_paid", is_current=False)
            return "superseded_paid"
        return "superseded_ignored"

    plan = lifecycle.plan(verdict, tx.state)
    return _apply_plan(tx, plan, session)


def _apply_plan(tx, plan, session):
    if plan == lifecycle.SET_DONE:
        _record_payment_ids(tx, session)
        _record_presentment(tx, session)
        _remember_customer(tx, session)
        tx._set_done()
        return "set_done"
    if plan == lifecycle.SET_PENDING:
        # A deferred payment method (e.g. Fawry) already carries a payment
        # intent id at reference-issued time — a reference was issued, even
        # though no money has moved yet; this id is later read as "real
        # money" once the transaction completes or gets parked.
        _record_payment_ids(tx, session)
        _record_presentment(tx, session)
        _remember_customer(tx, session)
        tx._set_pending()
        return "set_pending"
    if plan == lifecycle.SET_CANCELED:
        tx._set_canceled()
        return "set_canceled"
    if plan == lifecycle.SET_ERROR:
        tx._set_error(_("XPay reported that this payment failed."))
        return "set_error"
    if plan == lifecycle.PARK:
        _park(tx, session, "paid_after_cancel", is_current=True)
        return "parked"
    return "none"


def _record_payment_ids(tx, session):
    """Payment intent id (also the recorded `provider_reference`) and
    charge id off the session's `paymentIntent`, when present."""
    intent = session.get("paymentIntent") or {}
    intent_id = intent.get("id") if isinstance(intent, dict) else None
    latest_charge = intent.get("latestCharge") if isinstance(intent, dict) else None
    charge_id = latest_charge.get("id") if isinstance(latest_charge, dict) else None
    if isinstance(intent_id, str) and intent_id:
        tx.xpay_payment_intent_id = intent_id
        tx.provider_reference = intent_id
    if isinstance(charge_id, str) and charge_id:
        tx.xpay_charge_id = charge_id


def _remember_customer(tx, session):
    """Store the XPay customer id a paid (or reference-issued) session
    carries on a registered shopper's partner, per the session's OWN
    `livemode` stamp — never the provider's current state, since the
    record itself decides its plane — so the next checkout sends
    `customerId` instead of creating another customer. Guests are
    checkout-only records at XPay and are not stored."""
    customer_id = session.get("customerId")
    if not customer_id:
        customer = session.get("customer")
        if isinstance(customer, dict):
            customer_id = customer.get("id")
        elif isinstance(customer, str):
            customer_id = customer
    if not isinstance(customer_id, str) or not customer_id.startswith("cus_"):
        return
    partner = tx.partner_id
    if not partner or not partner._xpay_is_registered_shopper():
        return
    livemode = session.get("livemode")
    if livemode is True:
        plane = "live"
    elif livemode is False:
        plane = "test"
    else:
        plane = tx.provider_id._xpay_plane()
    if plane and partner._xpay_customer_id(plane) != customer_id:
        partner._xpay_remember_customer(plane, customer_id)
        log(
            _logger,
            "info",
            "customer.remembered",
            tx_reference=tx.reference,
            partner_id=partner.id,
            plane=plane,
        )


def _record_presentment(tx, session):
    """Snapshot the session's presentment mirror at payment time, when the
    merchant prices in a currency other than XPay's processing currency —
    refund_service reads this back to state a partial refund's `amount` in
    the processing currency."""
    presentment = session.get("presentmentDetails")
    if not isinstance(presentment, dict):
        return
    # The platform's own wire name for the locked rate is `exchangeRate`;
    # the module's internal snapshot keeps calling the field `rate`, so the
    # rename happens on the way in, once, here.
    rate = presentment.get("exchangeRate")
    tx.xpay_presentment_json = {
        "presentment_currency": presentment.get("currency"),
        "presentment_amount_minor": presentment.get("amountSubtotal"),
        "rate": str(rate) if rate is not None else None,
        "processing_currency": session.get("currency"),
        "processing_amount_minor": session.get("amountSubtotal"),
    }


def _apply_refund_update(tx, payment_data):
    """Map a refund/charge webhook payload's refund status onto the child
    (refund) transaction."""
    refund = payment_data.get("refund")
    if not isinstance(refund, dict):
        return

    refund_id = refund.get("id")

    # Ownership decides whether this report is about this payment at all;
    # writing the reported id onto the row before that check would bind
    # this child to a refund it was never actually about, whatever the
    # rest of the payload said.
    if not _refund_names_the_source(refund, tx.source_transaction_id):
        log(
            _logger,
            "info",
            "refund.webhook_foreign_source_ignored",
            tx_reference=tx.reference,
            refund_id=refund_id,
        )
        return

    if isinstance(refund_id, str) and refund_id:
        tx.provider_reference = refund_id

    status = refund.get("status")
    status = status.upper() if isinstance(status, str) else None
    if status == "SUCCEEDED":
        if not _refund_amount_matches(refund, tx):
            _flag_refund_amount_mismatch(tx, refund)
            return
        tx._set_done()
    elif status in ("PENDING", "REQUIRES_ACTION"):
        tx._set_pending()
    elif status in ("FAILED", "CANCELED"):
        # A refund the platform created and then reported failed or
        # canceled is a definitive refusal too, exactly like an error
        # response from submitting it: it must count toward the next
        # attempt's idempotency key, or a retry would just replay this
        # same refusal. An adopted child is already in state error here,
        # so `_set_error` below may be a no-op, but the flag still needs
        # setting.
        tx.xpay_refund_rejected = True
        tx._set_error(_("XPay reported that this refund failed or was canceled."))


def _refund_names_the_source(refund, source_tx):
    """A refund payload's own `chargeId`/`paymentIntentId` must agree with
    what the SOURCE transaction recorded at payment time, whichever of the
    two fields either side actually carries. Matching this child's own
    refund id is the primary correlation and is already checked by the
    caller; this is the second, independent one — a signature-verified
    event naming a different charge or intent is still not a report about
    this payment. Both fields are required on every refund the platform
    sends, so a payload carrying neither is malformed, not merely
    unconfirmed, and fails closed instead of passing by default."""
    charge_id = refund.get("chargeId")
    has_charge_id = isinstance(charge_id, str) and bool(charge_id)
    intent_id = refund.get("paymentIntentId")
    has_intent_id = isinstance(intent_id, str) and bool(intent_id)
    if not has_charge_id and not has_intent_id:
        return False
    if has_charge_id and source_tx.xpay_charge_id and charge_id != source_tx.xpay_charge_id:
        return False
    if (
        has_intent_id
        and source_tx.xpay_payment_intent_id
        and intent_id != source_tx.xpay_payment_intent_id
    ):
        return False
    return True


def _refund_amount_matches(refund, tx):
    """The refund's own amount and currency in the charge's PROCESSING
    currency, checked exactly against this child's own stored processing
    amount: the platform echoes back the same integer this module sent, so
    an exact match is what a correct report looks like. Refuses outright
    when the child carries no stored processing amount: every refund child
    this module ever creates or adopts stores one before it can reach a
    SUCCEEDED report."""
    processing_minor = tx.xpay_processing_amount_minor
    if not processing_minor:
        return False
    amount_minor = money.parse_minor(refund.get("amount"))
    currency = refund.get("currency")
    if amount_minor is None or not isinstance(currency, str) or not currency:
        return False
    if currency.upper() != _expected_processing_currency(tx):
        return False
    return amount_minor == processing_minor


def _expected_processing_currency(tx):
    """The charge's own processing currency, from the SOURCE transaction's
    presentment mirror when one was recorded at payment time (every refund
    child of one source settles in the same currency); with no mirror, the
    order was processed in its own presentment currency, so the child's own
    currency is that currency."""
    return _source_processing_currency(tx.source_transaction_id)


def _source_processing_currency(source_tx):
    """The processing currency every refund of SOURCE settles in: its
    presentment mirror's processing currency when payment time recorded
    one, else SOURCE's own currency."""
    presentment = source_tx.xpay_presentment_json
    presentment = presentment if isinstance(presentment, dict) else {}
    return (presentment.get("processing_currency") or source_tx.currency_id.name).upper()


def _flag_refund_amount_mismatch(tx, refund):
    """A refund report names this child's own refund id but disagrees with
    the amount or currency it was submitted for. Recording it as done would
    misstate what actually moved, so the state is left untouched; the child
    transaction carries no sale orders or invoices of its own, so the note
    and activity go on the source payment, which does."""
    source_tx = tx.source_transaction_id
    refund_id = refund.get("id")
    log(
        _logger,
        "critical",
        "refund.webhook_amount_mismatch",
        tx_reference=tx.reference,
        refund_id=refund_id,
        reported_amount=refund.get("amount"),
        reported_currency=refund.get("currency"),
    )

    message = _(
        "XPay reported refund %(refund_id)s with an amount or currency that does not match what"
        " transaction %(ref)s requested. Nothing was changed here. Review this refund in your"
        " XPay dashboard.",
        refund_id=refund_id or "—",
        ref=tx.reference,
    )
    escalation.escalate(
        source_tx, _("XPay: a refund report did not match what was requested"), message
    )


def _refund_has_presentment_amount(refund):
    """Whether `refund` carries a usable presentment-currency amount —
    i.e. whether `_presentment_amount_minor` read one from
    `presentmentDetails` rather than falling back to the top-level
    (processing-currency) `amount`."""
    presentment = refund.get("presentmentDetails")
    return (
        isinstance(presentment, dict) and money.parse_minor(presentment.get("amount")) is not None
    )


def _refund_currency_matches(refund, currency):
    """Whether the refund's own top-level `currency` is `currency` —
    case-insensitively, and never true for a missing or blank value."""
    reported = refund.get("currency")
    return (
        isinstance(reported, str) and bool(reported) and reported.upper() == currency.name.upper()
    )


def _flag_refund_unmirrorable(source_tx, refund):
    """A refund report cannot be safely mirrored into a child: it carries
    no presentment breakdown, and its own currency disagrees with
    SOURCE's, so there is no figure in SOURCE's currency to record. The
    child transaction that would normally carry the note and activity
    does not exist here, so both go on SOURCE's own orders and invoices,
    the same as `_flag_refund_amount_mismatch` and `_flag_adoption_ambiguous`."""
    refund_id = refund.get("id")
    log(
        _logger,
        "critical",
        "refund.unmirrorable_currency_mismatch",
        tx_reference=source_tx.reference,
        refund_id=refund_id,
        reported_currency=refund.get("currency"),
    )

    message = _(
        "XPay reported refund %(refund_id)s in a currency that does not match transaction"
        " %(ref)s, with no breakdown in the transaction's own currency. Nothing was recorded"
        " here. Review this refund in your XPay dashboard.",
        refund_id=refund_id or "—",
        ref=source_tx.reference,
    )
    escalation.escalate(source_tx, _("XPay: a refund report could not be recorded"), message)


def mirror_external_refund(source_tx, refund, event_id):
    """A `charge.refunded`/`refund.created`/`refund.failed` webhook names a
    refund id matching no child transaction: it was issued from the XPay
    dashboard (or another integration), not through this module — or it is
    this module's OWN attempt, reporting in after a lost response, in which
    case `_adopt_unresolved_sibling` finds the existing attempt instead of
    creating a second one. Runs under the SOURCE row's lock, with dedupe
    on the source keyed to this specific refund id — there is no child yet
    to dedupe against, and one event can carry more than one refund id.

    Returns "invalid" without touching anything when the refund carries no
    id, no usable presentment-or-body amount, or no usable top-level amount:
    a child without a stored refund id could never be matched by a later
    event (and would be mirrored again), and a missing amount must never
    become a zero or a crash the platform keeps redelivering. Returns
    "foreign" without touching anything when the refund's own `chargeId`/
    `paymentIntentId` disagrees with what SOURCE recorded: it is a report
    about a different payment, whatever id led the caller to SOURCE.
    Returns "ambiguous" without touching anything when more than one
    unresolved sibling matches the reported amount exactly but was sent
    under a different idempotency key: adopting one of them at random would
    settle an attempt that was never actually refunded, so a human is asked
    to reconcile them instead. Returns "unmirrorable" without creating a
    child when the refund carries no presentment breakdown and its own
    currency differs from SOURCE's: the reported figure is then a different
    currency's amount and cannot be re-expressed in SOURCE's currency
    without guessing an exchange rate."""
    refund_id = refund.get("id")
    amount_minor = _presentment_amount_minor(refund)
    processing_minor = money.parse_minor(refund.get("amount"))
    if (
        not isinstance(refund_id, str)
        or not refund_id
        or amount_minor is None
        or processing_minor is None
    ):
        return "invalid"

    lock(source_tx)

    processed = list(source_tx.xpay_processed_event_ids or [])
    dedupe_key = f"{event_id}_{refund_id}" if event_id else None
    if dedupe_key and lifecycle.is_replay(processed, dedupe_key):
        return "replay"

    if not _refund_names_the_source(refund, source_tx):
        log(
            _logger,
            "info",
            "refund.webhook_foreign_source_ignored",
            tx_reference=source_tx.reference,
            refund_id=refund_id,
        )
        if dedupe_key:
            source_tx.xpay_processed_event_ids = lifecycle.remember_event(processed, dedupe_key)
        return "foreign"

    sibling = _adopt_unresolved_sibling(source_tx, refund)
    if sibling is _ADOPTION_AMBIGUOUS:
        action = "ambiguous"
    elif sibling:
        sibling.xpay_processing_amount_minor = processing_minor
        # This report is the answer the sibling's own lost response never
        # delivered.
        sibling.xpay_refund_awaiting_answer = False
        _apply_refund_update(sibling, {"refund": refund})
        action = "adopted"
    elif not _refund_has_presentment_amount(refund) and not _refund_currency_matches(
        refund, source_tx.currency_id
    ):
        # No presentment breakdown to read SOURCE's own currency from, and
        # the refund's own currency disagrees with it: `amount_minor` is
        # then a different currency's figure, and treating it as SOURCE's
        # own would misstate what actually moved and inflate the completed
        # refund ledger.
        _flag_refund_unmirrorable(source_tx, refund)
        action = "unmirrorable"
    else:
        currency_code = source_tx.currency_id.name.upper()
        amount = money.to_odoo_amount(amount_minor, currency_code)
        child = source_tx._create_child_transaction(
            amount, is_refund=True, provider_reference=refund_id
        )
        child.xpay_processing_amount_minor = processing_minor
        _apply_refund_update(child, {"refund": refund})
        action = "mirrored"

    if dedupe_key:
        source_tx.xpay_processed_event_ids = lifecycle.remember_event(processed, dedupe_key)

    return action


def _adopt_unresolved_sibling(source_tx, refund):
    """A refund child of SOURCE that was actually sent and is still waiting
    for the platform's answer, with no `provider_reference` yet and in the
    source's own currency — this module's own attempt whose response never
    arrived. An attempt refused here before anything was sent never waits
    for an answer, so it is never a candidate. Adopting it keeps one child
    transaction per real refund: without it, a report of the very refund a
    lost response already attempted would create a second, phantom child
    instead, and would move the completed count out from under the attempt
    that is still sitting there in error. A rejected attempt is excluded
    outright: the platform already gave it a definitive, different answer,
    so this report belongs to someone else.

    A candidate matches when the report's own amount, read in SOURCE's own
    processing currency, equals the candidate's own stored processing
    amount exactly: the platform echoes back the same integer this module
    sent, so there is nothing left to allow for. Returns the child to
    adopt, an empty recordset when there is nothing to adopt, or the
    `_ADOPTION_AMBIGUOUS` sentinel — see `mirror_external_refund`."""
    candidates = source_tx.child_transaction_ids.filtered(
        lambda child: child.operation == "refund"
        and child.xpay_refund_awaiting_answer
        and not child.provider_reference
        and child.currency_id == source_tx.currency_id
    )
    if not candidates:
        return source_tx.browse()

    processing_minor = money.parse_minor(refund.get("amount"))
    processing_currency = _currency_upper(refund.get("currency"))
    if processing_minor is None or processing_currency != _source_processing_currency(source_tx):
        return source_tx.browse()

    matches = candidates.filtered(
        lambda child: child.xpay_processing_amount_minor == processing_minor
    )

    if not matches:
        return source_tx.browse()
    if len(matches) == 1:
        return matches

    # Several attempts sent under one key are one request to the platform,
    # and this report is its one outcome: adopt the earliest and settle the
    # others with it. Matches under different keys are distinct requests
    # with distinct outcomes, so none can be told apart from this report
    # alone.
    keys = set(matches.mapped("xpay_refund_idempotency_key"))
    if len(keys) == 1:
        adopted = matches.sorted("id")[:1]
        (matches - adopted).write({"xpay_refund_awaiting_answer": False})
        return adopted
    _flag_adoption_ambiguous(source_tx, refund)
    return _ADOPTION_AMBIGUOUS


def _flag_adoption_ambiguous(source_tx, refund):
    """More than one unresolved refund child of SOURCE matches this
    report's amount exactly, each sent under a different idempotency key:
    adopting any one of them risks settling an attempt that this report was
    never actually about, so none is adopted and a human resolves it
    instead."""
    refund_id = refund.get("id")
    log(
        _logger,
        "critical",
        "refund.adoption_ambiguous",
        tx_reference=source_tx.reference,
        refund_id=refund_id,
    )

    message = _(
        "XPay reported refund %(refund_id)s that matches more than one unresolved refund attempt"
        " on transaction %(ref)s, sent under different requests. Nothing was changed here."
        " Review this refund in your XPay dashboard.",
        refund_id=refund_id or "—",
        ref=source_tx.reference,
    )
    escalation.escalate(
        source_tx, _("XPay: a refund report matched more than one unresolved attempt"), message
    )


def _presentment_amount_minor(refund):
    """The refund's presentment-currency minor amount when the mirror
    carries one, else its (processing-currency) `amount` — the same
    presentment-first reading `money.session_charge` uses for sessions."""
    presentment = refund.get("presentmentDetails")
    if isinstance(presentment, dict):
        amount = money.parse_minor(presentment.get("amount"))
        if amount is not None:
            return amount
    return money.parse_minor(refund.get("amount"))


def _currency_upper(value):
    return value.upper() if isinstance(value, str) else None


def _session_identifiers(session):
    """Best-effort session id, payment intent id, charge id, and amount out
    of a session payload, for a log line or a merchant-facing note — never
    for a decision. A piece that cannot be read comes back `None` and
    prints as "—"; it never blocks anything."""
    if not isinstance(session, dict):
        return {
            "session_id": None,
            "intent_id": None,
            "charge_id": None,
            "amount": None,
            "currency": None,
        }
    intent = session.get("paymentIntent") or {}
    intent_id = intent.get("id") if isinstance(intent, dict) else None
    latest_charge = intent.get("latestCharge") if isinstance(intent, dict) else None
    charge_id = latest_charge.get("id") if isinstance(latest_charge, dict) else None
    try:
        amount_minor, currency = money.session_charge(session)
        amount = money.to_odoo_amount(amount_minor, currency)
    except ValueError:
        amount, currency = None, None
    return {
        "session_id": session.get("id"),
        "intent_id": intent_id,
        "charge_id": charge_id,
        "amount": amount,
        "currency": currency,
    }


def _park(tx, session, reason, is_current):
    """Paid-after-cancel and superseded-paid both land here: no state
    change, a critical log line, a note on every linked document, and an
    activity for a human to resolve. The module never silently completes
    or refunds money it cannot account for.

    `is_current` tells the two callers apart, because they earn different
    trust. `paid_after_cancel` reports the transaction's OWN session, so
    its ids are always the row's real reference and are always recorded,
    whatever already sits there. `superseded_paid` reports a DIFFERENT
    session that outlived its own replacement; its ids are recorded only
    into an EMPTY id slot, exactly as `_apply_plan` does for `SET_DONE` —
    a transaction that already carries either id was issued a real
    reference by an earlier payload (settled, or a deferred method's
    reference issued before completion), and that reference must survive
    whatever state the transaction later moves to. Guarding the superseded
    write on "is the slot empty" rather than "is this the current
    session" lets it note a plausible reference when nothing else is known
    yet, without that guess ever blocking the current session's own,
    later report from recording the real one. The duplicate's own
    identifiers always go in the log line, the note, and the activity too,
    never only onto the row."""
    duplicate = _session_identifiers(session)

    if isinstance(session, dict) and (
        is_current or (not tx.xpay_payment_intent_id and not tx.xpay_charge_id)
    ):
        _record_payment_ids(tx, session)

    log(
        _logger,
        "critical",
        "order_sync.park",
        tx_reference=tx.reference,
        reason=reason,
        session_id=duplicate["session_id"],
        payment_intent_id=duplicate["intent_id"],
        charge_id=duplicate["charge_id"],
        amount=duplicate["amount"],
        currency=duplicate["currency"],
    )

    message = _(
        "XPay reported a payment on transaction %(ref)s that Odoo did not apply automatically"
        " (%(reason)s): session %(session_id)s, payment intent %(intent_id)s, charge %(charge_id)s,"
        " amount %(amount)s %(currency)s. Review this payment in your XPay dashboard, then"
        " complete or refund it manually.",
        ref=tx.reference,
        reason=reason,
        session_id=duplicate["session_id"] or "—",
        intent_id=duplicate["intent_id"] or "—",
        charge_id=duplicate["charge_id"] or "—",
        amount=duplicate["amount"] if duplicate["amount"] is not None else "—",
        currency=duplicate["currency"] or "",
    )
    escalation.escalate(tx, _("XPay: review a payment on a closed order"), message)
