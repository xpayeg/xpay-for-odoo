import json

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_round

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

    def _send_refund_request(self, amount_to_refund=None):
        # This hook runs on the SOURCE transaction and the base
        # implementation is what creates and returns the child, so the
        # base call happens for every provider. The submission and its
        # error handling run only for XPay, on the CHILD: a failed attempt
        # must persist as an error child with its awaiting-answer
        # bookkeeping intact rather than raise past this hook.
        refund_tx = super()._send_refund_request(amount_to_refund=amount_to_refund)
        if self.provider_code != "xpay":
            return refund_tx
        try:
            refund_service.submit_refund(refund_tx)
        except ValidationError as e:
            refund_tx._set_error(str(e))
        return refund_tx

    @api.model
    def _get_tx_from_notification_data(self, provider_code, notification_data):
        if provider_code != "xpay":
            return super()._get_tx_from_notification_data(provider_code, notification_data)

        domain = [("provider_code", "=", "xpay")]

        session = notification_data.get("session")
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

        refund = notification_data.get("refund")
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

    def _process_notification_data(self, notification_data):
        if self.provider_code != "xpay":
            return super()._process_notification_data(notification_data)
        # Odoo 18 has no framework hook that checks a notification's amount
        # and currency before completion, so the module checks a session
        # report itself here, before anything is applied.
        session = notification_data.get("session")
        if isinstance(session, dict):
            previous_state = self.state
            try:
                amount_minor, currency_code = money.session_charge(session)
            except ValueError:
                amount_minor = currency_code = None
            if amount_minor is not None:
                amount = money.to_odoo_amount(amount_minor, currency_code)
                precision = money.decimals(currency_code)
                tx_amount = float_round(
                    self.amount, precision_digits=precision, rounding_method="DOWN"
                )
                if self.currency_id.compare_amounts(amount, tx_amount) != 0:
                    self._set_error(
                        _(
                            "The amount from the payment data doesn't match the one from the"
                            " transaction."
                        )
                    )
                elif currency_code != self.currency_id.name:
                    self._set_error(
                        _(
                            "The currency from the payment data doesn't match the one from the"
                            " transaction."
                        )
                    )
            if self.state == "error" and self.state != previous_state:
                return None
        order_sync.apply_locked(self, notification_data)
        return None
