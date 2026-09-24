import json

from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.tests import tagged

from ..xpay import events
from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestWebhook(XPayCommon, PaymentHttpCommon):
    def _tx(self, **values):
        tx = self._create_transaction("direct", **values)
        tx.xpay_session_id = "cs_1"
        return tx

    def _raw_post(self, body, header):
        url = self._build_url(f"/payment/xpay/webhook/{self.provider.id}")
        headers = {"Content-Type": "application/json"}
        if header is not None:
            headers["XPay-Signature"] = header
        return self.url_open(url, data=body, headers=headers, method="POST")

    def _post(self, event, *, secret=None):
        body = json.dumps(event).encode()
        used_secret = secret if secret is not None else self.provider.xpay_webhook_secret
        header = self.sign_header(used_secret, body)
        return self._raw_post(body, header)

    def _health(self, key):
        self.provider.invalidate_recordset()
        return self.provider._xpay_snapshot().get(key)

    # -- Status table -----------------------------------------------------

    def test_unknown_provider_is_404(self):
        body = b'{"type": "x", "data": {"object": {}}}'
        header = self.sign_header("whsec_test_1", body)
        url = self._build_url("/payment/xpay/webhook/999999")
        response = self.url_open(url, data=body, headers={"XPay-Signature": header}, method="POST")
        self.assertEqual(response.status_code, 404)

    def test_missing_secret_is_500(self):
        self.provider.xpay_webhook_secret = False
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(session_id="cs_1", status="complete", payment_status="paid"),
        )
        response = self._post(event, secret="whatever-since-none-is-configured")
        self.assertEqual(response.status_code, 500)

    def test_missing_signature_header_is_401_and_writes_no_health(self):
        # An unsigned request is a probe, not a delivery: routine log, no
        # health write in either direction.
        event = self.make_event(events.CHECKOUT_SESSION_COMPLETED, self.make_session())
        response = self._raw_post(json.dumps(event).encode(), None)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self._health("webhook_last_success_at"))
        self.assertFalse(self._health("webhook_last_failure_at"))

    def test_invalid_signature_is_401_and_records_a_health_failure(self):
        event = self.make_event(events.CHECKOUT_SESSION_COMPLETED, self.make_session())
        response = self._raw_post(json.dumps(event).encode(), "t=1,v1=deadbeef")
        self.assertEqual(response.status_code, 401)
        self.assertTrue(self._health("webhook_last_failure_at"))
        self.assertTrue(self._health("webhook_last_failure_reason"))

    def test_malformed_json_is_400(self):
        body = b"not json"
        header = self.sign_header("whsec_test_1", body)
        response = self._raw_post(body, header)
        self.assertEqual(response.status_code, 400)

    def test_missing_type_or_object_is_400(self):
        body = json.dumps({"data": {}}).encode()
        header = self.sign_header("whsec_test_1", body)
        response = self._raw_post(body, header)
        self.assertEqual(response.status_code, 400)

    def test_data_that_is_not_an_object_is_400(self):
        # `data` present but not a JSON object: a 400, never a crash the
        # platform would keep redelivering.
        body = json.dumps({"type": events.CHECKOUT_SESSION_COMPLETED, "data": []}).encode()
        header = self.sign_header("whsec_test_1", body)
        response = self._raw_post(body, header)
        self.assertEqual(response.status_code, 400)

    def test_unknown_event_type_is_200_ignored(self):
        event = self.make_event("customer.created", {"id": "cus_1"})
        response = self._post(event)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ignored"], "unknown_event")

    def test_no_transaction_is_404(self):
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(
                session_id="cs_does_not_exist", status="complete", payment_status="paid"
            ),
        )
        response = self._post(event)
        self.assertEqual(response.status_code, 404)

    def test_replayed_event_id_is_200_no_op(self):
        tx = self._tx()
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(session_id="cs_1", status="complete", payment_status="paid"),
            event_id="evt_replay_1",
        )
        first = self._post(event)
        self.assertEqual(first.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.xpay_processed_event_ids, ["evt_replay_1"])

        second = self._post(event)
        self.assertEqual(second.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.xpay_processed_event_ids, ["evt_replay_1"])

    def test_superseded_paid_session_is_located_and_parked(self):
        tx = self._tx()
        tx.xpay_session_id = "cs_new"
        tx.xpay_superseded_session_ids = ["cs_old"]
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(session_id="cs_old", status="complete", payment_status="paid"),
        )
        response = self._post(event)
        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "draft")  # parked, not silently completed

    def test_paid_event_on_draft_tx_ends_done_with_provider_reference_set(self):
        tx = self._tx()
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(
                session_id="cs_1",
                status="complete",
                payment_status="paid",
                payment_intent=self.make_payment_intent(intent_id="pi_1", charge_id="ch_1"),
            ),
        )
        response = self._post(event)
        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.provider_reference, "pi_1")
        self.assertEqual(tx.xpay_charge_id, "ch_1")

    def test_completed_unpaid_ends_pending(self):
        tx = self._tx()
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(session_id="cs_1", status="complete", payment_status="unpaid"),
        )
        response = self._post(event)
        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "pending")

    def test_async_payment_succeeded_from_pending_ends_done(self):
        tx = self._tx()
        tx._set_pending()
        event = self.make_event(
            events.CHECKOUT_SESSION_ASYNC_PAYMENT_SUCCEEDED,
            self.make_session(
                session_id="cs_1",
                status="complete",
                payment_status="paid",
                payment_intent=self.make_payment_intent(),
            ),
        )
        response = self._post(event)
        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "done")

    def test_expired_ends_cancel(self):
        tx = self._tx()
        event = self.make_event(
            events.CHECKOUT_SESSION_EXPIRED,
            self.make_session(
                session_id="cs_1", status="expired", payment_status="unpaid", is_expired=True
            ),
        )
        response = self._post(event)
        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "cancel")

    def test_expired_against_a_pending_transaction_leaves_it_pending(self):
        # `pending` means a payment reference was already issued (a
        # deferred method like Fawry); the platform never expires such a
        # session, so a late/misrouted `expired` event must not cancel it.
        tx = self._tx()
        tx._set_pending()
        event = self.make_event(
            events.CHECKOUT_SESSION_EXPIRED,
            self.make_session(
                session_id="cs_1", status="expired", payment_status="unpaid", is_expired=True
            ),
        )
        response = self._post(event)
        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "pending")

    def test_async_payment_failed_ends_error(self):
        tx = self._tx()
        event = self.make_event(
            events.CHECKOUT_SESSION_ASYNC_PAYMENT_FAILED,
            self.make_session(session_id="cs_1", status="complete", payment_status="unpaid"),
        )
        response = self._post(event)
        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "error")

    def test_health_fields_updated_on_success(self):
        self._tx()
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(session_id="cs_1", status="complete", payment_status="paid"),
        )
        self._post(event)
        self.assertTrue(self._health("webhook_last_success_at"))

    def test_health_fields_updated_on_500(self):
        self.provider.xpay_webhook_secret = False
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(session_id="cs_1", status="complete", payment_status="paid"),
        )
        self._post(event, secret="anything")
        self.assertTrue(self._health("webhook_last_failure_at"))
        self.assertTrue(self._health("webhook_last_failure_reason"))

    def test_health_fields_not_updated_on_404(self):
        event = self.make_event(
            events.CHECKOUT_SESSION_COMPLETED,
            self.make_session(session_id="cs_missing", status="complete", payment_status="paid"),
        )
        self._post(event)
        self.assertFalse(self._health("webhook_last_success_at"))
        self.assertFalse(self._health("webhook_last_failure_at"))
