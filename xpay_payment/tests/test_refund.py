import json

from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.tests import tagged

from ..xpay import events, idempotency, money
from ..xpay.errors import XPayTransportError
from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestRefund(XPayCommon, PaymentHttpCommon):
    def _paid_tx(self, **values):
        tx = self._create_transaction("direct", **values)
        tx.xpay_session_id = "cs_1"
        tx.xpay_payment_intent_id = "pi_1"
        tx.xpay_charge_id = "ch_1"
        tx._set_done()
        return tx

    def test_refund_request_body_and_idempotency(self):
        # "EGP exact": no presentment mirror (order priced and processed in
        # the same currency), so the request amount and the verified
        # response amount are the same minor-unit figure exactly.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_1", status="SUCCEEDED", amount=25000))

        refund_tx = tx._send_refund_request(amount_to_refund=250.0)

        call = transport.last_call
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{tx.provider_id._xpay_api_base()}/refunds?liveMode=false")
        self.assertEqual(call["json_body"]["paymentIntentId"], "pi_1")
        self.assertEqual(call["json_body"]["amount"], 25000)
        self.assertEqual(call["json_body"]["reason"], "REQUESTED_BY_CUSTOMER")
        self.assertEqual(
            call["headers"]["Idempotency-Key"],
            idempotency.bind_to_body(
                idempotency.refund_key(tx.reference, 0, 0, 25000), call["json_body"]
            ),
        )

        self.assertEqual(refund_tx.state, "done")
        self.assertEqual(refund_tx.provider_reference, "re_1")
        self.assertEqual(refund_tx.xpay_processing_amount_minor, 25000)

    def test_full_refund_without_a_presentment_mirror_sends_the_source_amount(self):
        transport = self._use_transport()
        tx = self._paid_tx()  # amount = self.amount = 750.00 EGP
        transport.queue(200, self.make_refund(refund_id="re_2", status="SUCCEEDED", amount=75000))

        refund_tx = tx._send_refund_request(amount_to_refund=750.0)

        self.assertEqual(transport.last_call["json_body"]["amount"], 75000)
        self.assertEqual(refund_tx.state, "done")

    def test_full_refund_with_a_presentment_mirror_sends_the_stored_processing_amount(self):
        # The stored figure (561111) deliberately differs from what
        # converting the presentment amount fresh would give (561110, see
        # test_usd_presentment_partial_refund_converts_to_the_processing_currency),
        # so this proves the stored value is sent verbatim rather than
        # recomputed.
        transport = self._use_transport()
        tx = self._paid_tx(currency_id=self.currency_usd.id, amount=110.00)
        tx.xpay_presentment_json = {
            "presentment_currency": "USD",
            "presentment_amount_minor": 11000,
            "rate": "51.01",
            "processing_currency": "EGP",
            "processing_amount_minor": 561111,
        }
        transport.queue(
            200,
            self.make_refund(
                refund_id="re_full", status="SUCCEEDED", amount=561111, currency="EGP"
            ),
        )

        refund_tx = tx._send_refund_request(amount_to_refund=110.00)

        self.assertEqual(transport.last_call["json_body"]["amount"], 561111)
        self.assertEqual(refund_tx.state, "done")

    def test_amount_exceeding_the_refundable_balance_is_refused_with_nothing_moved(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            400,
            {
                "error": {
                    "code": "amount_invalid",
                    "message": "requested amount exceeds the refundable balance",
                }
            },
        )

        refund_tx = tx._send_refund_request(amount_to_refund=100.0)

        self.assertEqual(refund_tx.state, "error")
        self.assertFalse(refund_tx.provider_reference)

    def test_retry_after_a_lost_response_replays_the_same_key(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))

        first_attempt = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(first_attempt.state, "error")
        key_1 = transport.last_call["headers"]["Idempotency-Key"]

        transport.queue(
            200, self.make_refund(refund_id="re_retry", status="SUCCEEDED", amount=10000)
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)

        key_2 = transport.last_call["headers"]["Idempotency-Key"]
        self.assertEqual(
            key_1, key_2, "A retry with the same amount must replay the failed attempt's key."
        )
        self.assertEqual(retry.state, "done")

    def test_two_successful_refunds_in_sequence_carry_different_keys(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_a", status="SUCCEEDED", amount=10000))
        first = tx._send_refund_request(amount_to_refund=100.0)
        key_1 = transport.last_call["headers"]["Idempotency-Key"]

        transport.queue(200, self.make_refund(refund_id="re_b", status="SUCCEEDED", amount=10000))
        second = tx._send_refund_request(amount_to_refund=100.0)
        key_2 = transport.last_call["headers"]["Idempotency-Key"]

        self.assertEqual(first.state, "done")
        self.assertEqual(second.state, "done")
        self.assertNotEqual(
            key_1,
            key_2,
            "A refund following a completed one is a new refund and needs its own key.",
        )

    def test_a_definitive_rejection_then_retry_sends_a_different_key(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            400, {"error": {"code": "amount_invalid", "message": "exceeds refundable balance"}}
        )
        first = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(first.state, "error")
        self.assertTrue(first.xpay_refund_rejected)
        key_1 = transport.last_call["headers"]["Idempotency-Key"]

        transport.queue(
            200, self.make_refund(refund_id="re_after_rejection", status="SUCCEEDED", amount=10000)
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)
        key_2 = transport.last_call["headers"]["Idempotency-Key"]

        self.assertNotEqual(
            key_1, key_2, "A retry after a definitive rejection must be a fresh attempt."
        )
        self.assertEqual(retry.state, "done")

    def test_a_transport_failure_then_retry_still_sends_the_same_key(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        first = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(first.state, "error")
        self.assertFalse(first.xpay_refund_rejected)
        key_1 = transport.last_call["headers"]["Idempotency-Key"]

        transport.queue(
            200,
            self.make_refund(refund_id="re_after_transport_fail", status="SUCCEEDED", amount=10000),
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)
        key_2 = transport.last_call["headers"]["Idempotency-Key"]

        self.assertEqual(
            key_1, key_2, "A transport failure carries no answer, so the key must still replay."
        )
        self.assertEqual(retry.state, "done")

    def test_a_409_in_flight_answer_then_retry_sends_the_same_key(self):
        # `idempotency_key_in_use` means the platform is still working on
        # the first attempt under this key, not that it refused it — the
        # opposite of test_409_resource_invalid_state_gives_a_retry_later_message,
        # which is the platform's own per-charge lock, a real rejection.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            409,
            {
                "error": {
                    "code": "idempotency_key_in_use",
                    "message": "a request is already in flight with this key",
                }
            },
        )
        first = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(first.state, "error")
        self.assertFalse(first.xpay_refund_rejected)
        key_1 = transport.last_call["headers"]["Idempotency-Key"]

        transport.queue(
            200, self.make_refund(refund_id="re_after_in_flight", status="SUCCEEDED", amount=10000)
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)
        key_2 = transport.last_call["headers"]["Idempotency-Key"]

        self.assertEqual(
            key_1, key_2, "An in-flight answer is not a refusal, so the key must still replay."
        )
        self.assertEqual(retry.state, "done")

    def test_submit_refund_refuses_without_an_api_call_when_a_sibling_refund_is_pending(self):
        # The completed-refund ledger only ever sees children the platform
        # has already answered; a sibling still waiting on that answer
        # must still block a second submission, or this attempt could
        # compute its fullness or its idempotency key against a ledger a
        # moment away from changing under it.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_pending", status="PENDING"))
        pending_sibling = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(pending_sibling.state, "pending")
        calls_before = len(transport.calls)

        second = tx._send_refund_request(amount_to_refund=50.0)

        self.assertEqual(second.state, "error")
        self.assertIn("progress", second.state_message.lower())
        self.assertFalse(second.xpay_refund_rejected)
        self.assertEqual(len(transport.calls), calls_before)

    def test_submit_refund_refuses_when_an_unsettled_sibling_is_a_different_amount(self):
        # A transport failure carries no answer at all, so the platform may
        # already have completed the request that never got a response:
        # until that is resolved, only the identical amount (the retry) may
        # proceed. A different amount would risk two refunds outstanding on
        # the platform at once, so it is refused before any call is made.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        unsettled = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(unsettled.state, "error")
        self.assertFalse(unsettled.xpay_refund_rejected)
        self.assertFalse(unsettled.provider_reference)
        calls_before = len(transport.calls)

        second = tx._send_refund_request(amount_to_refund=50.0)

        self.assertEqual(second.state, "error")
        self.assertIn("progress", second.state_message.lower())
        self.assertFalse(second.xpay_refund_rejected)
        self.assertEqual(len(transport.calls), calls_before)

    def test_submit_refund_proceeds_when_the_unsettled_sibling_is_the_same_amount(self):
        # The same amount as the unsettled sibling is the genuine retry: it
        # must replay the identical idempotency key, not be blocked by its
        # own earlier, still-unresolved attempt.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        unsettled = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(unsettled.state, "error")
        key_1 = transport.last_call["headers"]["Idempotency-Key"]

        transport.queue(
            200,
            self.make_refund(refund_id="re_same_amount_retry", status="SUCCEEDED", amount=10000),
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)
        key_2 = transport.last_call["headers"]["Idempotency-Key"]

        self.assertEqual(retry.state, "done")
        self.assertEqual(
            key_1, key_2, "The same amount as the unsettled sibling is the retry, not a new refund."
        )

    def test_a_locally_refused_attempt_never_counts_as_unsettled(self):
        # A refusal made here, before anything left this module, is still
        # persisted by Odoo as a child in error. It must never pass for an
        # attempt the platform may have honoured, or a refused attempt at
        # one amount and a lost response at another would lock every later
        # refund out for good.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        lost = tx._send_refund_request(amount_to_refund=100.0)
        self.assertTrue(lost.xpay_refund_awaiting_answer)
        key_lost = transport.last_call["headers"]["Idempotency-Key"]

        refused = tx._send_refund_request(amount_to_refund=50.0)
        self.assertEqual(refused.state, "error")
        self.assertFalse(refused.xpay_refund_awaiting_answer)

        transport.queue(
            200, self.make_refund(refund_id="re_retry", status="SUCCEEDED", amount=10000)
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(retry.state, "done")
        self.assertEqual(transport.last_call["headers"]["Idempotency-Key"], key_lost)
        lost.invalidate_recordset()
        self.assertFalse(
            lost.xpay_refund_awaiting_answer, "The retry's answer settles the lost attempt."
        )

        calls_before = len(transport.calls)
        transport.queue(
            200, self.make_refund(refund_id="re_other", status="SUCCEEDED", amount=5000)
        )
        other = tx._send_refund_request(amount_to_refund=50.0)
        self.assertEqual(other.state, "done")
        self.assertEqual(len(transport.calls), calls_before + 1)
        self.assertNotEqual(transport.last_call["headers"]["Idempotency-Key"], key_lost)

    def test_a_definitive_rejection_settles_the_lost_attempt_under_the_same_key(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        lost = tx._send_refund_request(amount_to_refund=100.0)
        self.assertTrue(lost.xpay_refund_awaiting_answer)

        transport.queue(
            400, {"error": {"code": "amount_invalid", "message": "exceeds refundable balance"}}
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(retry.state, "error")
        self.assertTrue(retry.xpay_refund_rejected)
        lost.invalidate_recordset()
        self.assertFalse(lost.xpay_refund_awaiting_answer)

    def test_adoption_never_picks_a_never_sent_child(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        tx._send_refund_request(amount_to_refund=100.0)  # awaiting an answer
        refused = tx._send_refund_request(amount_to_refund=50.0)  # refused here, never sent
        self.assertFalse(refused.xpay_refund_awaiting_answer)

        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[
                self.make_refund(refund_id="re_dashboard_50", status="SUCCEEDED", amount=5000)
            ],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_never_sent")

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        refused.invalidate_recordset()
        self.assertFalse(refused.provider_reference)
        self.assertEqual(refused.state, "error")
        mirrored = (
            self.env["payment.transaction"]
            .sudo()
            .search(
                [
                    ("source_transaction_id", "=", tx.id),
                    ("provider_reference", "=", "re_dashboard_50"),
                ]
            )
        )
        self.assertEqual(len(mirrored), 1)
        self.assertNotEqual(mirrored, refused)

    def test_two_lost_attempts_under_one_key_are_settled_by_the_one_report(self):
        # Two retries of the same amount that both lose their response were
        # sent under one key, so the platform holds one outcome for both;
        # its report must adopt one and settle the other, never leave both
        # waiting and every other amount refused.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        first = tx._send_refund_request(amount_to_refund=100.0)
        transport.fail(XPayTransportError("response lost"))
        second = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(first.xpay_refund_idempotency_key, second.xpay_refund_idempotency_key)
        self.assertTrue(first.xpay_refund_awaiting_answer and second.xpay_refund_awaiting_answer)

        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[self.make_refund(refund_id="re_one_key", status="SUCCEEDED", amount=10000)],
        )
        response = self._post_webhook(
            self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_one_key")
        )

        self.assertEqual(response.status_code, 200)
        first.invalidate_recordset()
        second.invalidate_recordset()
        self.assertEqual(first.state, "done")
        self.assertEqual(first.provider_reference, "re_one_key")
        self.assertFalse(first.xpay_refund_awaiting_answer)
        self.assertFalse(second.xpay_refund_awaiting_answer)
        self.assertFalse(second.provider_reference)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(len(children), 2, "No third child should be created.")

        transport.queue(200, self.make_refund(refund_id="re_next", status="SUCCEEDED", amount=5000))
        other = tx._send_refund_request(amount_to_refund=50.0)
        self.assertEqual(other.state, "done")

    def test_a_400_idempotency_key_in_use_is_definitive_and_marks_the_child_rejected(self):
        # The same code the platform uses for its own in-flight answer
        # (409) also appears at 400 when the body sent under a reused key
        # does not match the one first bound to it — a different,
        # definitive rejection, not a pending first attempt, so a retry
        # with the identical body could never resolve it.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            400,
            {
                "error": {
                    "code": "idempotency_key_in_use",
                    "message": "a request was already made with this key and a different body",
                }
            },
        )

        refund_tx = tx._send_refund_request(amount_to_refund=100.0)

        self.assertEqual(refund_tx.state, "error")
        self.assertTrue(refund_tx.xpay_refund_rejected)

    def test_refund_confirmation_with_the_amount_as_text_is_accepted(self):
        # The platform may answer with the amount as text, "245000", not
        # 245000. Same figure, so the refund is done.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            200, dict(self.make_refund(refund_id="re_t", status="SUCCEEDED"), amount="75000")
        )

        refund_tx = tx._send_refund_request(amount_to_refund=750.0)

        self.assertEqual(refund_tx.state, "done")

    def test_refund_confirmation_with_a_wrong_amount_as_text_is_still_refused(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            200, dict(self.make_refund(refund_id="re_w", status="SUCCEEDED"), amount="74000")
        )

        refund_tx = tx._send_refund_request(amount_to_refund=750.0)

        self.assertEqual(refund_tx.state, "error")

    def test_usd_presentment_partial_refund_converts_to_the_processing_currency(self):
        # The order was priced in USD and processed (settled) in EGP at a
        # locked rate — the presentment mirror `refund_service` reads back
        # is exactly what `order_sync._record_presentment` would have
        # written at payment time.
        transport = self._use_transport()
        tx = self._paid_tx(currency_id=self.currency_usd.id, amount=110.00)
        tx.xpay_presentment_json = {
            "presentment_currency": "USD",
            "presentment_amount_minor": 11000,
            "rate": "51.01",
            "processing_currency": "EGP",
            "processing_amount_minor": 561110,
        }
        expected_processing_minor = money.presentment_to_processing(5500, "USD", "EGP", "51.01")
        transport.queue(
            200,
            self.make_refund(
                refund_id="re_3",
                status="SUCCEEDED",
                amount=expected_processing_minor,
                currency="EGP",
            ),
        )

        refund_tx = tx._send_refund_request(amount_to_refund=55.00)

        body = transport.last_call["json_body"]
        self.assertEqual(body["amount"], expected_processing_minor)
        self.assertEqual(refund_tx.state, "done")

    def test_a_second_partial_refund_completing_the_order_sends_the_exact_remainder(self):
        # $50 then $60 of a $110 order: the second refund brings the
        # completed total to the whole order, so it must send whatever the
        # source has left in the processing currency, not a fresh
        # conversion of its own $60 slice, which loses a fraction of a unit
        # the way test_full_refund_with_a_presentment_mirror_sends_the_stored_processing_amount
        # proves for a lone full refund.
        transport = self._use_transport()
        tx = self._paid_tx(currency_id=self.currency_usd.id, amount=110.00)
        tx.xpay_presentment_json = {
            "presentment_currency": "USD",
            "presentment_amount_minor": 11000,
            "rate": "51.01",
            "processing_currency": "EGP",
            "processing_amount_minor": 561111,
        }
        first_processing_minor = money.presentment_to_processing(5000, "USD", "EGP", "51.01")
        transport.queue(
            200,
            self.make_refund(
                refund_id="re_step1",
                status="SUCCEEDED",
                amount=first_processing_minor,
                currency="EGP",
            ),
        )
        first = tx._send_refund_request(amount_to_refund=50.00)
        self.assertEqual(first.state, "done")

        remainder = 561111 - first_processing_minor
        transport.queue(
            200,
            self.make_refund(
                refund_id="re_step2", status="SUCCEEDED", amount=remainder, currency="EGP"
            ),
        )
        second = tx._send_refund_request(amount_to_refund=60.00)

        body = transport.last_call["json_body"]
        self.assertEqual(body["amount"], remainder)
        self.assertNotEqual(
            body["amount"],
            money.presentment_to_processing(6000, "USD", "EGP", "51.01"),
            "A fresh conversion of only the second slice would strand a unit.",
        )
        self.assertEqual(second.state, "done")

    def test_partial_refund_without_a_locked_rate_is_refused_cleanly(self):
        transport = self._use_transport()
        tx = self._paid_tx(currency_id=self.currency_usd.id, amount=110.00)
        tx.xpay_presentment_json = {
            "presentment_currency": "USD",
            "presentment_amount_minor": 11000,
            "rate": None,
            "processing_currency": "EGP",
            "processing_amount_minor": 561110,
        }

        refund_tx = tx._send_refund_request(amount_to_refund=55.00)

        self.assertEqual(refund_tx.state, "error")
        self.assertIn("exchange rate", refund_tx.state_message.lower())
        self.assertEqual(transport.calls, [])

    def test_refund_amount_mismatch_sets_error_and_does_not_mark_done(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        # An amount that does not match what this module actually sent.
        transport.queue(200, self.make_refund(refund_id="re_4", status="SUCCEEDED", amount=99000))

        refund_tx = tx._send_refund_request(amount_to_refund=100.0)

        self.assertEqual(refund_tx.state, "error")

    def test_refund_status_mapping_pending(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(status="PENDING"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "pending")

    def test_refund_status_mapping_failed(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(status="FAILED"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "error")

    def test_a_sync_failed_answer_is_a_definitive_rejection_and_retry_sends_a_different_key(self):
        # The platform can create a refund and answer with status FAILED as
        # an ordinary success response (never an error branch), so this is
        # a separate definitive-rejection path from the 400/409 one above.
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            200, self.make_refund(refund_id="re_sync_failed", status="FAILED", amount=10000)
        )

        first = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(first.state, "error")
        self.assertTrue(first.xpay_refund_rejected)
        self.assertFalse(
            first.xpay_refund_awaiting_answer,
            "Its own answer settled it; it is not still waiting.",
        )
        key_1 = transport.last_call["headers"]["Idempotency-Key"]

        transport.queue(
            200,
            self.make_refund(refund_id="re_after_sync_failed", status="SUCCEEDED", amount=10000),
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)
        key_2 = transport.last_call["headers"]["Idempotency-Key"]

        self.assertNotEqual(
            key_1,
            key_2,
            "A sync FAILED status is a definitive rejection, so a retry needs a fresh key.",
        )
        self.assertEqual(retry.state, "done")

    def test_refund_request_failure_does_not_leak_the_raw_api_message(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            400,
            {
                "error": {
                    "code": "parameter_invalid",
                    "message": "sensitive gateway detail: card ending 4242 declined",
                }
            },
        )

        refund_tx = tx._send_refund_request(amount_to_refund=100.0)

        self.assertEqual(refund_tx.state, "error")
        self.assertIn("parameter_invalid", refund_tx.state_message)
        self.assertNotIn("sensitive gateway detail", refund_tx.state_message)
        self.assertNotIn("4242", refund_tx.state_message)

    def test_409_resource_invalid_state_gives_a_retry_later_message(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(409, {"error": {"code": "resource_invalid_state", "message": "locked"}})

        refund_tx = tx._send_refund_request(amount_to_refund=100.0)

        self.assertEqual(refund_tx.state, "error")
        self.assertIn("retry", refund_tx.state_message.lower())

    def test_webhook_refund_failed_updates_the_child(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_1", status="PENDING"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "pending")

        event = self.make_event(
            events.REFUND_FAILED,
            self.make_refund(refund_id="re_1", status="FAILED", amount=10000),
        )
        body = json.dumps(event).encode()
        header = self.sign_header("whsec_test_1", body)
        url = self._build_url(f"/payment/xpay/webhook/{self.provider.id}")
        response = self.url_open(url, data=body, headers={"XPay-Signature": header})

        self.assertEqual(response.status_code, 200)
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.state, "error")

    def test_webhook_refund_failed_naming_a_foreign_charge_is_ignored(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_1", status="PENDING"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "pending")

        # Same refund id as the child already stores, but a chargeId that
        # names a different charge than the one this refund's source ever
        # recorded: not a report about this payment, whatever matched it.
        event = self.make_event(
            events.REFUND_FAILED,
            self.make_refund(
                refund_id="re_1", status="FAILED", amount=10000, charge_id="ch_foreign"
            ),
        )
        body = json.dumps(event).encode()
        header = self.sign_header("whsec_test_1", body)
        url = self._build_url(f"/payment/xpay/webhook/{self.provider.id}")
        response = self.url_open(url, data=body, headers={"XPay-Signature": header})

        self.assertEqual(response.status_code, 200)
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.state, "pending")

    def test_webhook_charge_refunded_updates_the_child(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_1", status="PENDING"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "pending")

        # `ChargeResponse.refunds` is an array, newest first.
        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[self.make_refund(refund_id="re_1", status="SUCCEEDED", amount=10000)],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge)
        body = json.dumps(event).encode()
        header = self.sign_header("whsec_test_1", body)
        url = self._build_url(f"/payment/xpay/webhook/{self.provider.id}")
        response = self.url_open(url, data=body, headers={"XPay-Signature": header})

        self.assertEqual(response.status_code, 200)
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.state, "done")

    def test_charge_refunded_applies_the_childs_own_refund_not_the_newest(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_old", status="PENDING"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "pending")

        # A newer refund of the same charge failed; this child owns re_old
        # and must follow re_old, not the newest entry.
        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[
                self.make_refund(refund_id="re_new", status="FAILED", amount=5000),
                self.make_refund(refund_id="re_old", status="SUCCEEDED", amount=10000),
            ],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge)
        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.state, "done")
        self.assertEqual(refund_tx.provider_reference, "re_old")

    def test_charge_refunded_without_the_childs_refund_changes_nothing(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_old", status="PENDING"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)

        charge = self.make_charge(
            charge_id="ch_1", refunds=[self.make_refund(refund_id="re_new", status="FAILED")]
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge)
        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.state, "pending")
        self.assertEqual(refund_tx.provider_reference, "re_old")

    def test_charge_refunded_with_a_wrong_amount_does_not_complete_an_errored_child(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "error")

        # An amount that does not match what this module actually sent.
        refund = self.make_refund(refund_id="re_1", status="SUCCEEDED", amount=99000)
        refund_tx._process_notification_data(
            {"event_type": events.CHARGE_REFUNDED, "refund": refund, "source": "webhook"}
        )

        self.assertEqual(refund_tx.state, "error")
        self.assertEqual(refund_tx.provider_reference, "re_1")

    def test_charge_refunded_with_a_matching_amount_completes_an_errored_child(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "error")

        refund = self.make_refund(refund_id="re_1", status="SUCCEEDED", amount=10000)
        refund_tx._process_notification_data(
            {"event_type": events.CHARGE_REFUNDED, "refund": refund, "source": "webhook"}
        )

        self.assertEqual(refund_tx.state, "done")
        self.assertEqual(refund_tx.provider_reference, "re_1")

    def test_charge_refunded_naming_a_foreign_charge_is_ignored(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_1", status="PENDING"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "pending")

        # Same refund id as the child already stores, but a chargeId that
        # names a different charge than the one this refund's source ever
        # recorded: not a report about this payment, whatever matched it.
        refund = self.make_refund(
            refund_id="re_1", status="SUCCEEDED", amount=10000, charge_id="ch_foreign"
        )
        refund_tx._process_notification_data(
            {"event_type": events.CHARGE_REFUNDED, "refund": refund, "source": "webhook"}
        )

        self.assertEqual(refund_tx.state, "pending")

    def test_webhook_charge_refunded_applies_every_entry_not_only_the_newest(self):
        # Two refund children built directly, not through two submissions:
        # a second submission while a sibling is still pending on the
        # platform is refused outright (see
        # test_submit_refund_refuses_without_an_api_call_when_a_sibling_refund_is_pending),
        # so this is the only way to put two children in "pending" side by
        # side for the webhook fan-out this test actually exercises.
        tx = self._paid_tx()
        older = tx._create_child_transaction(100.0, is_refund=True, provider_reference="re_old")
        older.xpay_processing_amount_minor = 10000
        older._set_pending()
        newer = tx._create_child_transaction(200.0, is_refund=True, provider_reference="re_new")
        newer.xpay_processing_amount_minor = 20000
        newer._set_pending()

        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[
                self.make_refund(refund_id="re_new", status="FAILED", amount=20000),
                self.make_refund(refund_id="re_old", status="SUCCEEDED", amount=10000),
            ],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_multi_1")

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        older.invalidate_recordset()
        newer.invalidate_recordset()
        self.assertEqual(older.state, "done")
        self.assertEqual(newer.state, "error")

    def test_webhook_after_a_lost_response_adopts_the_errored_child_instead_of_a_second_one(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        refund_tx = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, "error")
        self.assertFalse(refund_tx.provider_reference)
        self.assertEqual(refund_tx.xpay_processing_amount_minor, 10000)

        # The platform actually processed the request that got no response;
        # its own report of that same refund arrives as a webhook.
        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[self.make_refund(refund_id="re_recovered", status="SUCCEEDED", amount=10000)],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_recovered")

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(
            len(children), 1, "The webhook must settle the existing attempt, not create a second."
        )
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.id, children.id)
        self.assertEqual(refund_tx.state, "done")
        self.assertEqual(refund_tx.provider_reference, "re_recovered")

    def test_a_refund_failed_webhook_adopting_a_lost_response_marks_it_rejected(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        lost = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(lost.state, "error")
        self.assertFalse(lost.provider_reference)
        self.assertFalse(lost.xpay_refund_rejected)
        self.assertTrue(lost.xpay_refund_awaiting_answer)

        # The platform actually processed the request that got no
        # response, and this time reports it as failed rather than
        # succeeded.
        event = self.make_event(
            events.REFUND_FAILED,
            self.make_refund(refund_id="re_recovered_failed", status="FAILED", amount=10000),
            event_id="evt_recovered_failed",
        )
        response = self._post_webhook(event)
        self.assertEqual(response.status_code, 200)

        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(
            len(children), 1, "The webhook must settle the existing attempt, not create a second."
        )
        lost.invalidate_recordset()
        self.assertEqual(lost.id, children.id)
        self.assertEqual(lost.provider_reference, "re_recovered_failed")
        self.assertEqual(lost.state, "error")
        self.assertTrue(
            lost.xpay_refund_rejected,
            "A refund the platform created and then reported failed is a definitive refusal too.",
        )
        self.assertFalse(lost.xpay_refund_awaiting_answer)

        transport.queue(
            200,
            self.make_refund(
                refund_id="re_after_adoption_rejection", status="SUCCEEDED", amount=10000
            ),
        )
        retry = tx._send_refund_request(amount_to_refund=100.0)
        self.assertEqual(retry.state, "done")
        self.assertNotEqual(
            retry.xpay_refund_idempotency_key,
            lost.xpay_refund_idempotency_key,
            "A retry after a webhook-reported rejection must be a fresh attempt.",
        )

    def test_a_child_rejected_by_a_webhook_is_never_an_adoption_candidate_for_a_later_report(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.fail(XPayTransportError("response lost"))
        lost = tx._send_refund_request(amount_to_refund=100.0)  # stored processing amount 10000

        # The platform's own report of that lost attempt: adopts `lost`
        # and marks it a definitive rejection.
        first_report = self.make_event(
            events.REFUND_FAILED,
            self.make_refund(refund_id="re_first_report", status="FAILED", amount=10000),
            event_id="evt_first_report",
        )
        self.assertEqual(self._post_webhook(first_report).status_code, 200)
        lost.invalidate_recordset()
        self.assertTrue(lost.xpay_refund_rejected)

        # A second, unrelated refund at the exact same amount must mirror
        # a new child, never settle the one the platform already rejected.
        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[
                self.make_refund(refund_id="re_second_report", status="SUCCEEDED", amount=10000)
            ],
        )
        second_report = self.make_event(
            events.CHARGE_REFUNDED, charge, event_id="evt_second_report"
        )
        response = self._post_webhook(second_report)

        self.assertEqual(response.status_code, 200)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(len(children), 2, "The rejected child must not be adopted a second time.")
        lost.invalidate_recordset()
        self.assertEqual(lost.provider_reference, "re_first_report")
        self.assertEqual(lost.state, "error")
        mirrored = children - lost
        self.assertEqual(mirrored.provider_reference, "re_second_report")
        self.assertEqual(mirrored.state, "done")

    def test_adoption_refuses_when_two_exact_siblings_were_sent_under_different_keys(self):
        # Reachable: a first attempt's response is lost, its own report is
        # later mirrored in from the dashboard (settling it under its own
        # key), and a same-amount retry is then sent under a fresh key and
        # itself loses its response too -- two awaiting children left
        # behind at the same stored amount, sent under two different keys.
        tx = self._paid_tx()
        first = tx._create_child_transaction(
            100.0,
            is_refund=True,
            xpay_refund_awaiting_answer=True,
            xpay_processing_amount_minor=10000,
            xpay_refund_idempotency_key="key_one",
        )
        first._set_error("response lost")
        second = tx._create_child_transaction(
            100.0,
            is_refund=True,
            xpay_refund_awaiting_answer=True,
            xpay_processing_amount_minor=10000,
            xpay_refund_idempotency_key="key_two",
        )
        second._set_error("response lost")

        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[self.make_refund(refund_id="re_ambiguous", status="SUCCEEDED", amount=10000)],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_ambiguous")

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(
            len(children), 2, "Neither sibling should be adopted and no child should be created."
        )
        first.invalidate_recordset()
        second.invalidate_recordset()
        self.assertFalse(first.provider_reference)
        self.assertEqual(first.state, "error")
        self.assertTrue(first.xpay_refund_awaiting_answer)
        self.assertFalse(second.provider_reference)
        self.assertEqual(second.state, "error")
        self.assertTrue(second.xpay_refund_awaiting_answer)

    def test_adoption_never_picks_a_rejected_sibling(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(
            400, {"error": {"code": "amount_invalid", "message": "exceeds refundable balance"}}
        )
        rejected = tx._send_refund_request(amount_to_refund=100.0)  # stored processing amount 10000
        self.assertEqual(rejected.state, "error")
        self.assertTrue(rejected.xpay_refund_rejected)

        # Exact amount match, but the only candidate is rejected: a new
        # child must be mirrored in instead of settling the rejected one.
        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[self.make_refund(refund_id="re_new_mirror", status="SUCCEEDED", amount=10000)],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_rejected_skip")

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(len(children), 2)
        rejected.invalidate_recordset()
        self.assertFalse(rejected.provider_reference)
        self.assertEqual(rejected.state, "error")
        mirrored = children - rejected
        self.assertEqual(mirrored.provider_reference, "re_new_mirror")
        self.assertEqual(mirrored.state, "done")

    def test_adoption_refuses_a_matching_amount_in_a_foreign_currency(self):
        transport = self._use_transport()
        tx = self._paid_tx()  # EGP order, no presentment mirror
        transport.fail(XPayTransportError("response lost"))
        errored = tx._send_refund_request(amount_to_refund=100.0)  # stored processing amount 10000
        self.assertEqual(errored.state, "error")

        # The same number the errored attempt is waiting on, but reported
        # in a currency other than the source's own currency, with no
        # presentment breakdown: not a candidate for adoption, and not
        # safe to mirror into a new child either, however well the amount
        # lines up.
        charge = self.make_charge(
            charge_id="ch_1",
            refunds=[
                self.make_refund(
                    refund_id="re_foreign_ccy", status="SUCCEEDED", amount=10000, currency="USD"
                )
            ],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_foreign_ccy")

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(
            len(children), 1, "A currency mismatch with no presentment breakdown must not mirror."
        )
        errored.invalidate_recordset()
        self.assertFalse(errored.provider_reference, "The errored attempt must not be adopted.")
        self.assertEqual(errored.state, "error")

    def test_dashboard_refund_with_no_presentment_and_a_foreign_currency_is_unmirrorable(self):
        # Priced in USD, processed in EGP (the presentment mirror records
        # the split); the dashboard-issued refund reports only the
        # processing-currency (EGP) figure, with no presentment breakdown
        # to read a USD amount from.
        tx = self._paid_tx(currency_id=self.currency_usd.id, amount=110.00)
        tx.xpay_presentment_json = {
            "presentment_currency": "USD",
            "presentment_amount_minor": 11000,
            "rate": "51.01",
            "processing_currency": "EGP",
            "processing_amount_minor": 561111,
        }
        order = None
        if "sale_order_ids" in tx._fields:
            order = self.env["sale.order"].create({"partner_id": tx.partner_id.id})
            tx.sale_order_ids = [(4, order.id)]

        refund = self.make_refund(
            refund_id="re_no_presentment", status="SUCCEEDED", amount=561111, currency="EGP"
        )
        charge = self.make_charge(charge_id="ch_1", refunds=[refund])
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_unmirrorable")

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertFalse(children, "No child must be created for an unmirrorable refund.")
        if order is not None:
            note = "".join(order.message_ids.mapped("body"))
            self.assertIn("re_no_presentment", note)

    def test_webhook_refund_completes_on_a_processing_currency_match_despite_a_presentment_mismatch(
        self,
    ):
        transport = self._use_transport()
        tx = self._paid_tx()  # EGP order, amount = self.amount = 750.00
        tx.xpay_presentment_json = {
            "presentment_currency": "EGP",
            "presentment_amount_minor": 75000,
            "rate": "0.0198",
            "processing_currency": "USD",
            "processing_amount_minor": 1485,
        }
        transport.queue(
            200, self.make_refund(refund_id="re_1", status="PENDING", amount=1485, currency="USD")
        )
        refund_tx = tx._send_refund_request(amount_to_refund=750.0)
        self.assertEqual(refund_tx.state, "pending")
        self.assertEqual(refund_tx.xpay_processing_amount_minor, 1485)

        # The presentment mirror on this delivery is off by forty piasters
        # — a presentment-currency check would refuse it — but the
        # processing-currency amount this module actually asked for matches
        # exactly.
        refund = self.make_refund(
            refund_id="re_1",
            status="SUCCEEDED",
            amount=1485,
            currency="USD",
            presentment=self.make_refund_presentment_details(amount=75040, currency="EGP"),
        )
        charge = self.make_charge(charge_id="ch_1", refunds=[refund])
        event = self.make_event(events.CHARGE_REFUNDED, charge)
        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.state, "done")

    def test_webhook_refund_parks_on_a_processing_currency_mismatch(self):
        transport = self._use_transport()
        tx = self._paid_tx()
        tx.xpay_presentment_json = {
            "presentment_currency": "EGP",
            "presentment_amount_minor": 75000,
            "rate": "0.0198",
            "processing_currency": "USD",
            "processing_amount_minor": 1485,
        }
        transport.queue(
            200, self.make_refund(refund_id="re_1", status="PENDING", amount=1485, currency="USD")
        )
        refund_tx = tx._send_refund_request(amount_to_refund=750.0)
        self.assertEqual(refund_tx.state, "pending")

        # A dollar off in the PROCESSING currency must not complete the
        # refund, even though nothing here touches the presentment
        # currency at all.
        refund = self.make_refund(refund_id="re_1", status="SUCCEEDED", amount=1585, currency="USD")
        charge = self.make_charge(charge_id="ch_1", refunds=[refund])
        event = self.make_event(events.CHARGE_REFUNDED, charge)
        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.state, "pending")

    def test_refund_created_completes_a_pending_child_whose_presentment_echo_is_one_unit_off(
        self,
    ):
        # A converted partial refund: the platform projects the processing
        # amount back to the order currency at the locked rate, and two
        # truncations can leave that projection one minor unit under the
        # child's own amount. There is no framework amount check to trip
        # over here; this module's own processing-currency check is what
        # decides, and it must complete the refund despite the order-
        # currency discrepancy.
        transport = self._use_transport()
        tx = self._paid_tx()
        tx.xpay_presentment_json = {
            "presentment_currency": "EGP",
            "presentment_amount_minor": 75000,
            "rate": "0.0198",
            "processing_currency": "USD",
            "processing_amount_minor": 1485,
        }
        transport.queue(
            200, self.make_refund(refund_id="re_1", status="PENDING", amount=1485, currency="USD")
        )
        refund_tx = tx._send_refund_request(amount_to_refund=750.0)
        self.assertEqual(refund_tx.state, "pending")

        refund = self.make_refund(
            refund_id="re_1",
            status="SUCCEEDED",
            amount=1485,
            currency="USD",
            presentment=self.make_refund_presentment_details(amount=74999, currency="EGP"),
        )
        event = self.make_event(events.REFUND_CREATED, refund)
        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        refund_tx.invalidate_recordset()
        self.assertEqual(refund_tx.state, "done")

    # -- Dashboard-initiated refunds -----------------------------------------

    def _post_webhook(self, event):
        body = json.dumps(event).encode()
        header = self.sign_header("whsec_test_1", body)
        url = self._build_url(f"/payment/xpay/webhook/{self.provider.id}")
        return self.url_open(url, data=body, headers={"XPay-Signature": header})

    def test_dashboard_charge_refunded_with_no_child_creates_and_marks_done(self):
        tx = self._paid_tx()
        tx.xpay_charge_id = "ch_dashboard_1"
        charge = self.make_charge(
            charge_id="ch_dashboard_1",
            refunds=[
                self.make_refund(
                    refund_id="re_dashboard_1",
                    status="SUCCEEDED",
                    amount=10000,
                    charge_id="ch_dashboard_1",
                )
            ],
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_dashboard_1")

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 200)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(len(children), 1)
        self.assertEqual(children.provider_reference, "re_dashboard_1")
        self.assertEqual(children.state, "done")
        self.assertEqual(children.operation, "refund")
        self.assertAlmostEqual(children.amount, -100.0)

    def test_dashboard_refund_without_an_id_is_400_and_creates_nothing(self):
        tx = self._paid_tx()
        tx.xpay_charge_id = "ch_dashboard_1"
        refund = self.make_refund(refund_id="re_x", status="SUCCEEDED", amount=10000)
        del refund["id"]
        event = self.make_event(
            events.CHARGE_REFUNDED,
            self.make_charge(charge_id="ch_dashboard_1", refunds=[refund]),
        )

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 400)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertFalse(children)

    def test_dashboard_refund_without_an_amount_is_400_and_creates_nothing(self):
        tx = self._paid_tx()
        tx.xpay_charge_id = "ch_dashboard_1"
        refund = {"id": "re_no_amount", "status": "SUCCEEDED", "currency": "EGP"}
        event = self.make_event(
            events.CHARGE_REFUNDED,
            self.make_charge(charge_id="ch_dashboard_1", refunds=[refund]),
        )

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 400)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertFalse(children)

    def test_dashboard_charge_refunded_replay_does_not_create_a_second_child(self):
        tx = self._paid_tx()
        tx.xpay_charge_id = "ch_dashboard_2"
        charge = self.make_charge(
            charge_id="ch_dashboard_2",
            refunds={
                "data": [
                    self.make_refund(
                        refund_id="re_dashboard_2",
                        status="SUCCEEDED",
                        amount=5000,
                        charge_id="ch_dashboard_2",
                    )
                ]
            },
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge, event_id="evt_dashboard_2")

        first = self._post_webhook(event)
        second = self._post_webhook(event)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(len(children), 1)

    def test_dashboard_charge_refunded_unknown_charge_is_404(self):
        charge = self.make_charge(
            charge_id="ch_does_not_exist",
            refunds={"data": [self.make_refund(refund_id="re_unknown", status="SUCCEEDED")]},
        )
        event = self.make_event(events.CHARGE_REFUNDED, charge)

        response = self._post_webhook(event)

        self.assertEqual(response.status_code, 404)

    # -- Refunding through a different installed provider -------------------

    def test_a_demo_provider_refund_completes_alongside_this_module(self):
        # The base method takes an optional amount argument that every
        # other provider's own override forwards unchanged, so this
        # override must forward it the same way, or refunding any OTHER
        # provider's transaction raises a TypeError as long as this module
        # is installed, whether or not that transaction was ours.
        demo_module = self.env["ir.module.module"].sudo().search([("name", "=", "payment_demo")])
        if demo_module.state != "installed":
            self.skipTest("payment_demo is not installed")

        demo_provider = self.env.ref("payment.payment_provider_demo")
        demo_tx = (
            self.env["payment.transaction"]
            .sudo()
            .create(
                {
                    "provider_id": demo_provider.id,
                    "payment_method_id": self.env.ref("payment_demo.payment_method_demo").id,
                    "amount": 50.0,
                    "currency_id": self.currency_egp.id,
                    "reference": "demo-tx-1",
                    "operation": "online_direct",
                    "partner_id": self.partner.id,
                }
            )
        )
        demo_tx._set_done()

        demo_refund_tx = demo_tx._send_refund_request(amount_to_refund=50.0)
        self.assertEqual(demo_refund_tx.state, "done")

        transport = self._use_transport()
        tx = self._paid_tx()
        transport.queue(200, self.make_refund(refund_id="re_1", status="SUCCEEDED", amount=10000))
        xpay_refund_tx = tx._send_refund_request(amount_to_refund=100.0)

        self.assertEqual(xpay_refund_tx.state, "done")
        self.assertEqual(xpay_refund_tx.provider_reference, "re_1")
