"""`/payment/xpay/return` (thank-you-page re-check) and
`/payment/xpay/webhook/<provider>` (the payment authority).
"""

import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ..services import order_sync
from ..services.logging import get_logger, log
from ..xpay import events, signature
from ..xpay.errors import Codes, SignatureError, XPayApiError

_logger = get_logger(__name__)


class XPayMainController(http.Controller):
    @http.route(
        "/payment/xpay/return",
        type="http",
        auth="public",
        website=True,
        sitemap=False,
        methods=["GET"],
    )
    def xpay_return(self, reference=None, session_id=None, **kwargs):
        # The session_id query parameter is never read back into a lookup —
        # it is a screenshot/support aid only; a mismatch is ignored, not a
        # gate, because the server-side session read below is the truth.
        tx = request.env["payment.transaction"]
        if reference:
            tx = (
                request.env["payment.transaction"]
                .sudo()
                .search([("provider_code", "=", "xpay"), ("reference", "=", reference)], limit=1)
            )

        if tx and tx.xpay_session_id:
            try:
                # Money boundary: a same-plane reconnect whose setup never
                # finished (connect_service._provision) must not re-check a
                # session on the new, half-provisioned key. ValidationError
                # is caught below like any other failure: tx is left as is.
                tx.provider_id._xpay_require_setup_complete()
                session = tx.provider_id._xpay_client().get_checkout_session(
                    tx.xpay_session_id, shopper_facing=True
                )
                tx._handle_notification_data(
                    "xpay",
                    {
                        "event_type": None,
                        "event_id": None,
                        "session": session,
                        "source": "return",
                    },
                )
            except (order_sync.OrderLockBusy, XPayApiError, ValidationError) as exc:
                log(
                    _logger,
                    "error",
                    "return.apply_failed",
                    tx_reference=tx.reference,
                    error=type(exc).__name__,
                )

        return request.redirect("/payment/status")

    @http.route(
        "/payment/xpay/webhook/<int:provider_id>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def xpay_webhook(self, provider_id, **kwargs):
        provider = request.env["payment.provider"].sudo().browse(provider_id).exists()
        if not provider or provider.code != "xpay":
            return self._json(404, {"error": "not_found"})
        plane = provider._xpay_plane()

        raw_body = request.httprequest.get_data()
        header = request.httprequest.headers.get(signature.HEADER_NAME)
        secret = provider.xpay_webhook_secret or ""

        try:
            signature.verify(header, raw_body, secret)
        except SignatureError as exc:
            if exc.code == Codes.WEBHOOK_NOT_CONFIGURED:
                log(_logger, "error", "webhook.signature_rejected", plane=plane, code=exc.code)
                self._record_health(provider, plane, success=False, reason=exc.code)
                return self._json(500, {"error": exc.code})
            if exc.code == Codes.WEBHOOK_SIGNATURE_MISSING:
                # The endpoint is public; a request with no signature header
                # at all is not a delivery and proves nothing (a probe, a
                # scanner) — routine-level log, no health write.
                log(_logger, "info", "webhook.unsigned_probe", plane=plane)
                return self._json(401, {"error": exc.code})
            # A header IS present: this is the platform (or someone who
            # already knows the integration is installed) and its verdict is
            # reportable.
            log(_logger, "error", "webhook.signature_rejected", plane=plane, code=exc.code)
            self._record_health(provider, plane, success=False, reason=exc.code)
            return self._json(401, {"error": exc.code})

        payload = self._decode(raw_body)
        data = payload.get("data") if isinstance(payload, dict) else None
        obj = data.get("object") if isinstance(data, dict) else None
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("type"), str)
            or not payload["type"]
            or not isinstance(obj, dict)
        ):
            return self._json(400, {"error": Codes.WEBHOOK_PAYLOAD_MALFORMED})

        event_type = payload["type"]
        event_id = payload.get("id") if isinstance(payload.get("id"), str) else None

        if event_type not in events.SUBSCRIBED:
            self._record_health(provider, plane, success=True)
            return self._json(200, {"received": True, "ignored": "unknown_event"})

        if events.is_decline_event(event_type):
            return self._handle_decline(provider, plane, obj, event_id)

        if event_type == events.CHARGE_REFUNDED:
            # A charge can carry more than one refund; each entry is its
            # own refund and is applied (or mirrored in) on its own, not
            # only the newest.
            return self._apply_charge_refunds(provider, plane, obj, event_id)

        payment_data = self._payment_data_for(event_type, event_id, obj)
        tx = (
            request.env["payment.transaction"]
            .sudo()
            ._get_tx_from_notification_data("xpay", payment_data)
        )

        if not tx and events.is_refund_event(event_type):
            # No child transaction carries this refund id: it may be a
            # dashboard-issued (or another-integration-issued) refund this
            # store has never seen. `_get_tx_from_notification_data` stays a
            # pure lookup; the mirror-in-a-new-child creation lives here.
            return self._mirror_external_refund(provider, plane, obj, event_id)

        if not tx:
            return self._json(404, {"error": Codes.WEBHOOK_TRANSACTION_NOT_FOUND})

        try:
            tx._handle_notification_data("xpay", payment_data)
        except order_sync.OrderLockBusy:
            self._record_health(provider, plane, success=False, reason=Codes.ORDER_LOCK_BUSY)
            return self._json(500, {"error": Codes.ORDER_LOCK_BUSY})
        except Exception:
            # Returning a response commits the request's transaction, so a
            # partially applied event must be rolled back first: the 500
            # makes the platform redeliver it against a clean state.
            request.env.cr.rollback()
            log(_logger, "error", "webhook.apply_failed", plane=plane, event_type=event_type)
            self._record_health(provider, plane, success=False, reason="apply_failed")
            return self._json(500, {"error": "internal"})

        self._record_health(provider, plane, success=True)
        return self._json(200, {"received": True})

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _decode(raw_body):
        try:
            return json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _payment_data_for(event_type, event_id, obj):
        # `charge.refunded` is handled by `_apply_charge_refunds` before this
        # is ever called, so every event reaching here is either a checkout
        # event or a single-refund one.
        base = {"event_type": event_type, "event_id": event_id, "source": "webhook"}
        if events.is_checkout_event(event_type):
            return {**base, "session": obj}
        return {**base, "refund": obj}

    def _mirror_external_refund(self, provider, plane, refund, event_id):
        """`refund.created`/`refund.failed` naming a refund id no child owns
        yet: find the source payment by the refund's own `chargeId`/
        `paymentIntentId` and mirror it in."""
        if not isinstance(refund, dict):
            return self._json(404, {"error": Codes.WEBHOOK_TRANSACTION_NOT_FOUND})

        source_tx = self._find_refund_source(
            provider,
            charge_id=refund.get("chargeId"),
            payment_intent_id=refund.get("paymentIntentId"),
        )
        if not source_tx:
            return self._json(404, {"error": Codes.WEBHOOK_TRANSACTION_NOT_FOUND})

        try:
            result = order_sync.mirror_external_refund(source_tx, refund, event_id)
        except order_sync.OrderLockBusy:
            self._record_health(provider, plane, success=False, reason=Codes.ORDER_LOCK_BUSY)
            return self._json(500, {"error": Codes.ORDER_LOCK_BUSY})
        except Exception:
            request.env.cr.rollback()
            log(_logger, "error", "webhook.apply_failed", plane=plane, event_type="refund")
            self._record_health(provider, plane, success=False, reason="apply_failed")
            return self._json(500, {"error": "internal"})

        if result == "invalid":
            # A refund with no id or no amount is a malformed payload, not a
            # store failure: 400 like the other shape checks, nothing stored.
            return self._json(400, {"error": Codes.WEBHOOK_PAYLOAD_MALFORMED})

        self._record_health(provider, plane, success=True)
        return self._json(200, {"received": True})

    def _apply_charge_refunds(self, provider, plane, charge, event_id):
        """`charge.refunds` carries every refund the charge has, newest
        first: each entry is looked up by its own id — applied to the child
        that already owns it, or mirrored in when none does — instead of
        only ever acting on the newest one."""
        refunds = events.charge_refunds(charge)
        if not refunds:
            return self._json(404, {"error": Codes.WEBHOOK_TRANSACTION_NOT_FOUND})

        domain = [("provider_code", "=", "xpay"), ("operation", "=", "refund")]
        any_matched = False
        any_invalid_shape = False

        for entry in refunds:
            refund_id = entry.get("id") if isinstance(entry, dict) else None
            child = request.env["payment.transaction"]
            if isinstance(refund_id, str) and refund_id:
                child = (
                    request.env["payment.transaction"]
                    .sudo()
                    .search(domain + [("provider_reference", "=", refund_id)], limit=1)
                )
            try:
                if child:
                    order_sync.apply_locked(
                        child, {"refund": entry, "event_id": event_id, "source": "webhook"}
                    )
                    any_matched = True
                    continue

                source_tx = self._find_refund_source(
                    provider,
                    charge_id=charge.get("id"),
                    payment_intent_id=entry.get("paymentIntentId")
                    if isinstance(entry, dict)
                    else None,
                )
                if not source_tx:
                    continue
                result = order_sync.mirror_external_refund(source_tx, entry, event_id)
                if result == "invalid":
                    any_invalid_shape = True
                else:
                    any_matched = True
            except order_sync.OrderLockBusy:
                # Returning a response commits the request's transaction, so
                # a partially applied event must be rolled back first: the
                # 500 makes the platform redeliver it against a clean state,
                # and every entry this loop already applied is idempotent to
                # replay.
                request.env.cr.rollback()
                self._record_health(provider, plane, success=False, reason=Codes.ORDER_LOCK_BUSY)
                return self._json(500, {"error": Codes.ORDER_LOCK_BUSY})
            except Exception:
                request.env.cr.rollback()
                log(
                    _logger,
                    "error",
                    "webhook.apply_failed",
                    plane=plane,
                    event_type=events.CHARGE_REFUNDED,
                )
                self._record_health(provider, plane, success=False, reason="apply_failed")
                return self._json(500, {"error": "internal"})

        if any_matched:
            self._record_health(provider, plane, success=True)
            return self._json(200, {"received": True})
        if any_invalid_shape:
            return self._json(400, {"error": Codes.WEBHOOK_PAYLOAD_MALFORMED})
        return self._json(404, {"error": Codes.WEBHOOK_TRANSACTION_NOT_FOUND})

    @staticmethod
    def _find_refund_source(provider, *, charge_id, payment_intent_id):
        """The DONE payment this refund belongs to, by the charge id first
        and the refund's own payment intent id as a fallback."""
        domain = [("provider_id", "=", provider.id)]
        source_tx = request.env["payment.transaction"]
        if isinstance(charge_id, str) and charge_id:
            source_tx = source_tx.sudo().search(
                domain + [("xpay_charge_id", "=", charge_id)], limit=1
            )
        if not source_tx and isinstance(payment_intent_id, str) and payment_intent_id:
            source_tx = (
                request.env["payment.transaction"]
                .sudo()
                .search(domain + [("xpay_payment_intent_id", "=", payment_intent_id)], limit=1)
            )
        if not source_tx or source_tx.state != "done":
            return request.env["payment.transaction"]
        return source_tx

    def _handle_decline(self, provider, plane, intent, event_id):
        session_id = intent.get("checkoutSessionId")
        if not isinstance(session_id, str) or not session_id:
            nested = intent.get("checkoutSession")
            session_id = nested.get("id") if isinstance(nested, dict) else None

        if not isinstance(session_id, str) or not session_id:
            self._record_health(provider, plane, success=True)
            return self._json(200, {"received": True, "ignored": "no_session_reference"})

        tx = (
            request.env["payment.transaction"]
            .sudo()
            .search(
                [
                    ("provider_id", "=", provider.id),
                    ("provider_code", "=", "xpay"),
                    ("xpay_session_id", "=", session_id),
                ],
                limit=1,
            )
        )
        if not tx:
            return self._json(404, {"error": Codes.WEBHOOK_TRANSACTION_NOT_FOUND})

        try:
            order_sync.note_declined(tx, intent, event_id)
        except order_sync.OrderLockBusy:
            self._record_health(provider, plane, success=False, reason=Codes.ORDER_LOCK_BUSY)
            return self._json(500, {"error": Codes.ORDER_LOCK_BUSY})

        self._record_health(provider, plane, success=True)
        return self._json(200, {"received": True})

    @staticmethod
    def _record_health(provider, plane, *, success, reason=None):
        now = fields.Datetime.to_string(fields.Datetime.now())
        if success:
            provider.sudo()._xpay_snapshot_update({"webhook_last_success_at": now})
        else:
            provider.sudo()._xpay_snapshot_update(
                {"webhook_last_failure_at": now, "webhook_last_failure_reason": reason or ""}
            )

    @staticmethod
    def _json(status, body):
        return request.make_json_response(body, status=status)
