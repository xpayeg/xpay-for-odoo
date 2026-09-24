import json

from odoo import api, fields, models

from ..services import checkout_service, order_sync, refund_service
from ..xpay import money


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    # provider_reference holds the payment intent id for payment
    # transactions and the refund id for refund transactions (operation
    # == 'refund'), matching the base field's own per-provider convention.
    xpay_session_id = fields.Char(readonly=True, index=True, copy=False)
    xpay_session_attempt = fields.Integer(default=0)
    xpay_superseded_session_ids = fields.Json(default=lambda self: [])
    xpay_processed_event_ids = fields.Json(default=lambda self: [])
    xpay_payment_intent_id = fields.Char()
    xpay_charge_id = fields.Char()
    # {processing_currency, processing_amount_minor, presentment_currency,
    # presentment_amount_minor, rate} captured at payment time from the
    # session's presentmentDetails, when the merchant prices in a currency
    # other than XPay's processing currency. Read by refund_service to
    # convert a partial refund's order-currency amount into the processing
    # currency the /refunds `amount` field is in.
    xpay_presentment_json = fields.Json()
    # On a refund (operation == 'refund') child only: the processing-
    # currency minor amount this module actually sent for this attempt, or
    # that the platform reported for one mirrored in from its own record.
    # The order-currency `amount` on the child is not exact enough to check
    # a webhook's own processing-currency report against, or to add up a
    # ledger of completed refunds against the source's frozen total, when
    # the order was priced in a different currency than it settled in.
    xpay_processing_amount_minor = fields.Integer(readonly=True)
    # True once the platform has definitively refused this refund attempt:
    # an error response to the request, or a refund the platform created
    # anyway and then reported failed or canceled, whether on the sync
    # answer or a later webhook. Never a transport failure, which carries
    # no answer at all. The next attempt's idempotency key must account for
    # it, or a retry would just replay the same refusal the platform caches
    # under that key for a day.
    xpay_refund_rejected = fields.Boolean(readonly=True)
    # True from the moment a refund request leaves this module until the
    # platform's answer for it is known: its own response, or the answer a
    # later attempt receives for the very same key. An attempt refused here
    # before anything was sent never sets it, so it can never be mistaken
    # for one the platform may have honoured.
    xpay_refund_awaiting_answer = fields.Boolean(readonly=True)
    # The idempotency key the request was sent under. Attempts that share a
    # key share the platform's one outcome for it, which is how a webhook
    # settles every attempt a single report answers for.
    xpay_refund_idempotency_key = fields.Char(readonly=True)

    def _get_specific_processing_values(self, processing_values):
        if self.provider_code != "xpay":
            return super()._get_specific_processing_values(processing_values)
        session_data = checkout_service.session_for(self)
        return {
            "client_secret": session_data["client_secret"],
            "session_id": session_data["session_id"],
            "return_url": session_data["return_url"],
        }

    def _send_refund_request(self):
        if self.provider_code != "xpay":
            return super()._send_refund_request()
        refund_service.submit_refund(self)
        return None

    @api.model
    def _search_by_reference(self, provider_code, payment_data):
        if provider_code != "xpay":
            return super()._search_by_reference(provider_code, payment_data)

        domain = [("provider_code", "=", "xpay")]

        session = payment_data.get("session")
        if isinstance(session, dict) and isinstance(session.get("id"), str) and session["id"]:
            tx = self.search(domain + [("xpay_session_id", "=", session["id"])], limit=1)
            if tx:
                return tx
            # Not the CURRENT session of any transaction; it may still be a
            # SUPERSEDED one (ownership is never existence, but a
            # superseded-paid event must still be findable). Odoo has no
            # domain operator for JSON-array containment, so this one case
            # reads the column directly.
            return self._search_by_superseded_session_id(session["id"])

        refund = payment_data.get("refund")
        if isinstance(refund, dict) and isinstance(refund.get("id"), str) and refund["id"]:
            return self.search(
                domain + [("provider_reference", "=", refund["id"]), ("operation", "=", "refund")],
                limit=1,
            )

        return self.browse()

    def _search_by_superseded_session_id(self, session_id):
        # provider_code is a related, non-stored field (no column of its
        # own), so the provider's own code is checked via a join instead.
        self.env.cr.execute(
            """
            SELECT pt.id
              FROM payment_transaction pt
              JOIN payment_provider pp ON pp.id = pt.provider_id
             WHERE pp.code = 'xpay' AND pt.xpay_superseded_session_ids @> %s::jsonb
             ORDER BY pt.id DESC
             LIMIT 1
            """,
            [json.dumps([session_id])],
        )
        row = self.env.cr.fetchone()
        return self.browse(row[0]) if row else self.browse()

    def _extract_amount_data(self, payment_data):
        if self.provider_code != "xpay":
            return super()._extract_amount_data(payment_data)

        session = payment_data.get("session")
        if isinstance(session, dict):
            try:
                amount_minor, currency_code = money.session_charge(session)
            except ValueError:
                return None
            return {
                "amount": money.to_odoo_amount(amount_minor, currency_code),
                "currency_code": currency_code,
                "precision_digits": money.decimals(currency_code),
            }

        # A refund report is checked by this module itself, in the charge's
        # processing currency and against the exact integer this module
        # sent, before any completion (order_sync._apply_refund_update).
        # The framework's own check would instead compare the order-currency
        # figure the platform derives from that integer, and a converted
        # partial refund can legitimately land one minor unit away from the
        # child's amount after two truncations. Opting out here leaves the
        # stronger check as the only one that decides. A charge payload
        # carries no single amount to check either.
        return None

    def _apply_updates(self, payment_data):
        if self.provider_code != "xpay":
            return super()._apply_updates(payment_data)
        order_sync.apply_locked(self, payment_data)
        return None
