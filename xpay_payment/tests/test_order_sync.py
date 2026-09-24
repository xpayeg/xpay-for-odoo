from odoo import api
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from ..services import order_sync, refund_service
from ..xpay.errors import XPayTransportError
from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestOrderSync(XPayCommon):
    def _tx(self, **values):
        tx = self._create_transaction("direct", **values)
        tx.xpay_session_id = "cs_1"
        return tx

    def test_lock_busy_raises(self):
        # Real cross-connection contention needs a row genuinely committed
        # and a genuinely independent connection on each side of the race —
        # self.env.cr's single long-lived test transaction is neither
        # (Odoo's test harness forbids committing it, and a plain
        # TransactionCase may not even see a row another connection just
        # committed). Mirrors odoo/addons/onboarding/tests/
        # test_onboarding_concurrency.py: fixture created and committed
        # through its own fresh registry cursor, contention exercised
        # through two more, independent of self.env entirely.
        admin_partner_id = self.env.ref("base.partner_admin").id

        with self.registry.cursor() as cr1:
            env1 = api.Environment(cr1, api.SUPERUSER_ID, {})
            fixture_tx = env1["payment.transaction"].create(
                {
                    "provider_id": self.provider.id,
                    "payment_method_id": self.payment_method_id,
                    "reference": "lock-busy-test-ref",
                    "amount": 100.0,
                    "currency_id": self.currency.id,
                    "partner_id": admin_partner_id,
                }
            )
            tx_id = fixture_tx.id
        # cr1 committed the fixture on clean `with` exit.

        cr2 = self.registry.cursor()
        try:
            cr2.execute("SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE", [tx_id])

            with self.registry.cursor() as cr3:
                env3 = api.Environment(cr3, api.SUPERUSER_ID, {})
                victim_tx = env3["payment.transaction"].browse(tx_id)
                with self.assertRaises(order_sync.OrderLockBusy):
                    order_sync.lock(victim_tx)
        finally:
            cr2.rollback()
            cr2.close()

    def test_submit_refund_refuses_without_an_api_call_when_the_source_row_is_locked(self):
        # Same contention shape as test_lock_busy_raises, exercising
        # submit_refund's own lock instead of order_sync.lock directly. The
        # refund child is created up front, alongside the source, so cr3
        # only ever reads and lock-checks the source row under contention —
        # never inserts against it, which would otherwise wait on cr2's
        # lock instead of failing fast.
        admin_partner_id = self.env.ref("base.partner_admin").id
        transport = self._use_transport()

        with self.registry.cursor() as cr1:
            env1 = api.Environment(cr1, api.SUPERUSER_ID, {})
            source_tx = env1["payment.transaction"].create(
                {
                    "provider_id": self.provider.id,
                    "payment_method_id": self.payment_method_id,
                    "reference": "submit-lock-source-ref",
                    "amount": 100.0,
                    "currency_id": self.currency.id,
                    "partner_id": admin_partner_id,
                }
            )
            source_tx.xpay_session_id = "cs_lock_1"
            source_tx.xpay_payment_intent_id = "pi_lock_1"
            source_tx.xpay_charge_id = "ch_lock_1"
            source_tx._set_done()
            refund_tx = source_tx._create_child_transaction(100.0, is_refund=True)
            refund_id = refund_tx.id
        # cr1 committed the fixture (source and its already-created refund
        # child) on clean `with` exit.

        cr2 = self.registry.cursor()
        try:
            cr2.execute(
                "SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE",
                [source_tx.id],
            )

            with self.registry.cursor() as cr3:
                env3 = api.Environment(cr3, api.SUPERUSER_ID, {})
                refund_tx3 = env3["payment.transaction"].browse(refund_id)
                try:
                    refund_service.submit_refund(refund_tx3)
                    self.fail("submit_refund should refuse while the source row is locked")
                except ValidationError as exc:
                    self.assertIn("progress", str(exc).lower())
                self.assertFalse(refund_tx3.xpay_refund_rejected)
        finally:
            cr2.rollback()
            cr2.close()

        self.assertEqual(transport.calls, [])

    def _registered_buyer(self):
        partner = self.env["res.partner"].create({"name": "Reg Buyer", "email": "reg@example.com"})
        self.env["res.users"].create(
            {
                "name": "Reg Buyer",
                "login": f"reg-{partner.id}@example.com",
                "partner_id": partner.id,
                "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
            }
        )
        return partner

    def test_paid_session_customer_id_is_remembered_on_a_registered_shopper(self):
        buyer = self._registered_buyer()
        tx = self._tx(partner_id=buyer.id)
        session = self.make_session(session_id="cs_1", status="complete", payment_status="paid")
        session["customerId"] = "cus_test_new"
        session["livemode"] = False

        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_c1"}
        )

        self.assertEqual(action, "set_done")
        self.assertEqual(buyer._xpay_customer_id("test"), "cus_test_new")
        self.assertIsNone(buyer._xpay_customer_id("live"))

    def test_customer_id_in_the_nested_shape_is_read_too(self):
        buyer = self._registered_buyer()
        tx = self._tx(partner_id=buyer.id)
        session = self.make_session(session_id="cs_1", status="complete", payment_status="paid")
        session["customer"] = {"id": "cus_live_nested"}
        session["livemode"] = True

        order_sync.apply_locked(tx, {"session": session, "event_type": None, "event_id": "evt_c2"})

        self.assertEqual(buyer._xpay_customer_id("live"), "cus_live_nested")

    def test_guest_customer_id_is_not_stored(self):
        tx = self._tx()  # the default partner has no user: a guest
        session = self.make_session(session_id="cs_1", status="complete", payment_status="paid")
        session["customerId"] = "cus_test_guest"

        order_sync.apply_locked(tx, {"session": session, "event_type": None, "event_id": "evt_c3"})

        self.assertFalse(tx.partner_id.xpay_customer_ids)

    def test_paid_after_cancel_parks_with_no_state_change(self):
        tx = self._tx()
        tx._set_canceled()
        session = self.make_session(session_id="cs_1", status="complete", payment_status="paid")

        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_1"}
        )

        self.assertEqual(action, "parked")
        self.assertEqual(tx.state, "cancel")
        if "sale_order_ids" in tx._fields and tx.sale_order_ids:
            self.assertTrue(tx.sale_order_ids.message_ids)

    def test_superseded_paid_parks(self):
        tx = self._tx()
        tx.xpay_superseded_session_ids = ["cs_old"]
        session = self.make_session(session_id="cs_old", status="complete", payment_status="paid")

        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_2"}
        )

        self.assertEqual(action, "superseded_paid")
        self.assertEqual(tx.state, "draft")

    def test_superseded_completed_unpaid_is_ignored(self):
        tx = self._tx()
        tx.xpay_superseded_session_ids = ["cs_old"]
        session = self.make_session(session_id="cs_old", status="complete", payment_status="unpaid")

        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_3"}
        )

        self.assertEqual(action, "superseded_ignored")
        self.assertEqual(tx.state, "draft")

    def test_absent_payment_status_is_a_no_op(self):
        tx = self._tx()
        session = self.make_session(session_id="cs_1")
        session.pop("paymentStatus")

        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_4"}
        )

        self.assertEqual(action, "none")
        self.assertEqual(tx.state, "draft")

    def test_foreign_session_is_ignored(self):
        tx = self._tx()
        session = self.make_session(session_id="cs_other", status="complete", payment_status="paid")

        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_5"}
        )

        self.assertEqual(action, "foreign")
        self.assertEqual(tx.state, "draft")

    def test_replay_is_a_no_op(self):
        tx = self._tx()
        session = self.make_session(session_id="cs_1", status="complete", payment_status="paid")
        order_sync.apply_locked(tx, {"session": session, "event_type": None, "event_id": "evt_6"})
        self.assertEqual(tx.state, "done")

        # A different (still PAID) session payload under the SAME event id
        # must not be re-applied — if it were, this would still just be
        # _set_done() again (a no-op transition), so the real proof is the
        # returned action, not the state.
        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_6"}
        )

        self.assertEqual(action, "replay")
        self.assertEqual(tx.state, "done")

    def test_paid_records_payment_intent_and_charge_ids(self):
        tx = self._tx()
        session = self.make_session(
            session_id="cs_1",
            status="complete",
            payment_status="paid",
            payment_intent=self.make_payment_intent(intent_id="pi_1", charge_id="ch_1"),
        )

        order_sync.apply_locked(tx, {"session": session, "event_type": None, "event_id": "evt_7"})

        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_1")
        self.assertEqual(tx.xpay_charge_id, "ch_1")
        self.assertEqual(tx.provider_reference, "pi_1")

    def test_paid_session_with_a_presentment_mirror_records_the_locked_rate(self):
        tx = self._tx(currency_id=self.currency_usd.id, amount=110.00)
        session = self.make_session(
            session_id="cs_1",
            status="complete",
            payment_status="paid",
            currency="EGP",
            amount_subtotal=561110,
            presentment=self.make_presentment_details(
                amount=11000, currency="USD", exchange_rate=51.01
            ),
        )

        order_sync.apply_locked(tx, {"session": session, "event_type": None, "event_id": "evt_8"})

        self.assertEqual(tx.state, "done")
        self.assertEqual(
            tx.xpay_presentment_json,
            {
                "presentment_currency": "USD",
                "presentment_amount_minor": 11000,
                "rate": "51.01",
                "processing_currency": "EGP",
                "processing_amount_minor": 561110,
            },
        )

    def test_park_on_a_done_transaction_keeps_its_own_ids_and_names_the_duplicate(self):
        tx = self._tx()
        if "sale_order_ids" in tx._fields:
            order = self.env["sale.order"].create({"partner_id": tx.partner_id.id})
            tx.sale_order_ids = [(4, order.id)]
        session_a = self.make_session(
            session_id="cs_1",
            status="complete",
            payment_status="paid",
            payment_intent=self.make_payment_intent(intent_id="pi_a", charge_id="ch_a"),
        )
        order_sync.apply_locked(tx, {"session": session_a, "event_type": None, "event_id": "evt_a"})
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_a")
        self.assertEqual(tx.xpay_charge_id, "ch_a")

        tx.xpay_superseded_session_ids = ["cs_old"]
        session_b = self.make_session(
            session_id="cs_old",
            status="complete",
            payment_status="paid",
            payment_intent=self.make_payment_intent(intent_id="pi_b", charge_id="ch_b"),
        )

        action = order_sync.apply_locked(
            tx, {"session": session_b, "event_type": None, "event_id": "evt_b"}
        )

        self.assertEqual(action, "superseded_paid")
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_a")
        self.assertEqual(tx.xpay_charge_id, "ch_a")
        if "sale_order_ids" in tx._fields:
            note = "".join(tx.sale_order_ids.message_ids.mapped("body"))
            self.assertIn("pi_b", note)
            self.assertNotIn("pi_a", note)

    def test_park_on_a_not_yet_done_transaction_still_records_ids_as_today(self):
        tx = self._tx()
        tx._set_canceled()
        session = self.make_session(
            session_id="cs_1",
            status="complete",
            payment_status="paid",
            payment_intent=self.make_payment_intent(intent_id="pi_c", charge_id="ch_c"),
        )

        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_c"}
        )

        self.assertEqual(action, "parked")
        self.assertEqual(tx.state, "cancel")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_c")
        self.assertEqual(tx.xpay_charge_id, "ch_c")

    def test_park_on_a_pending_transaction_keeps_its_own_ids_and_names_the_duplicate(self):
        tx = self._tx()
        session_a = self.make_session(session_id="cs_1", status="complete", payment_status="unpaid")
        order_sync.apply_locked(
            tx, {"session": session_a, "event_type": None, "event_id": "evt_p1"}
        )
        self.assertEqual(tx.state, "pending")
        # A pending transaction already holds a real issued reference of
        # its own (a deferred method's reference is issued before the
        # transaction is done); set distinctive ids to prove they survive.
        tx.xpay_payment_intent_id = "pi_pending"
        tx.xpay_charge_id = "ch_pending"

        tx.xpay_superseded_session_ids = ["cs_old"]
        session_b = self.make_session(
            session_id="cs_old",
            status="complete",
            payment_status="paid",
            payment_intent=self.make_payment_intent(intent_id="pi_b", charge_id="ch_b"),
        )

        action = order_sync.apply_locked(
            tx, {"session": session_b, "event_type": None, "event_id": "evt_p2"}
        )

        self.assertEqual(action, "superseded_paid")
        self.assertEqual(tx.state, "pending")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_pending")
        self.assertEqual(tx.xpay_charge_id, "ch_pending")

    def test_park_after_pending_then_error_keeps_the_issued_reference_ids(self):
        # A transaction that already holds a real issued reference must
        # keep it regardless of what state it later moves to — guarding on
        # `state in (done, pending)` would miss this: the transaction is
        # neither by the time the duplicate arrives, but the reference it
        # already holds is still the real one.
        tx = self._tx()
        session_a = self.make_session(
            session_id="cs_1",
            status="complete",
            payment_status="unpaid",
            payment_intent=self.make_payment_intent(intent_id="pi_issued", charge_id="ch_issued"),
        )
        order_sync.apply_locked(
            tx, {"session": session_a, "event_type": None, "event_id": "evt_pe1"}
        )
        self.assertEqual(tx.state, "pending")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_issued")
        self.assertEqual(tx.xpay_charge_id, "ch_issued")

        tx._set_error("a later attempt failed")

        tx.xpay_superseded_session_ids = ["cs_old"]
        session_b = self.make_session(
            session_id="cs_old",
            status="complete",
            payment_status="paid",
            payment_intent=self.make_payment_intent(intent_id="pi_dup", charge_id="ch_dup"),
        )

        action = order_sync.apply_locked(
            tx, {"session": session_b, "event_type": None, "event_id": "evt_pe2"}
        )

        self.assertEqual(action, "superseded_paid")
        self.assertEqual(tx.state, "error")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_issued")
        self.assertEqual(tx.xpay_charge_id, "ch_issued")

    def test_current_sessions_late_paid_report_overwrites_ids_a_superseded_park_filled_first(self):
        # The superseded park below is the ONLY report that has arrived so
        # far, so it fills the still-empty slot with ITS OWN ids — the
        # existing, correct behaviour for an empty row. The current
        # session's own late report must still win when it finally
        # arrives, even though the slot it would have filled is no longer
        # empty.
        tx = self._tx()
        if "sale_order_ids" in tx._fields:
            order = self.env["sale.order"].create({"partner_id": tx.partner_id.id})
            tx.sale_order_ids = [(4, order.id)]
        tx._set_canceled()

        tx.xpay_superseded_session_ids = ["cs_old"]
        session_old = self.make_session(
            session_id="cs_old",
            status="complete",
            payment_status="paid",
            payment_intent=self.make_payment_intent(intent_id="pi_old", charge_id="ch_old"),
        )
        action_old = order_sync.apply_locked(
            tx, {"session": session_old, "event_type": None, "event_id": "evt_old"}
        )
        self.assertEqual(action_old, "superseded_paid")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_old")
        self.assertEqual(tx.xpay_charge_id, "ch_old")

        session_current = self.make_session(
            session_id="cs_1",
            status="complete",
            payment_status="paid",
            payment_intent=self.make_payment_intent(intent_id="pi_current", charge_id="ch_current"),
        )
        action_current = order_sync.apply_locked(
            tx, {"session": session_current, "event_type": None, "event_id": "evt_current"}
        )

        self.assertEqual(action_current, "parked")
        self.assertEqual(tx.state, "cancel")
        self.assertEqual(tx.xpay_payment_intent_id, "pi_current")
        self.assertEqual(tx.xpay_charge_id, "ch_current")
        if "sale_order_ids" in tx._fields:
            note = "".join(tx.sale_order_ids.message_ids.mapped("body"))
            self.assertIn("pi_old", note)
            self.assertIn("pi_current", note)

    def test_mirror_naming_neither_charge_nor_intent_is_foreign(self):
        # Both fields are required on every refund the platform sends; a
        # payload naming neither is malformed, not merely unconfirmed, and
        # must not be trusted just because some OUTER correlation (e.g. the
        # charge id `_apply_charge_refunds` used to find this source at
        # all) is not this function's own concern.
        tx = self._tx()
        tx.xpay_payment_intent_id = "pi_1"
        tx.xpay_charge_id = "ch_1"
        tx._set_done()
        refund = self.make_refund(refund_id="re_neither", status="SUCCEEDED", amount=10000)
        del refund["chargeId"]
        del refund["paymentIntentId"]

        action = order_sync.mirror_external_refund(tx, refund, "evt_neither")

        self.assertEqual(action, "foreign")
        children = (
            self.env["payment.transaction"]
            .sudo()
            .search([("source_transaction_id", "=", tx.id), ("operation", "=", "refund")])
        )
        self.assertFalse(children, "A foreign report must not create a child.")

    def test_mirror_of_a_foreign_report_touches_nothing(self):
        transport = self._use_transport()
        tx = self._tx()
        tx.xpay_payment_intent_id = "pi_1"
        tx.xpay_charge_id = "ch_1"
        tx._set_done()
        transport.fail(XPayTransportError("response lost"))
        unsettled = tx._refund(amount_to_refund=100.0)
        self.assertEqual(unsettled.state, "error")
        self.assertEqual(unsettled.xpay_processing_amount_minor, 10000)

        # Same amount as the sibling's own stored figure -- an adoption
        # match if ownership were not checked first -- but this refund
        # carries neither chargeId nor paymentIntentId, so it is foreign
        # and never reaches the adoption check at all.
        refund = self.make_refund(refund_id="re_foreign_no_own", status="SUCCEEDED", amount=10000)
        del refund["chargeId"]
        del refund["paymentIntentId"]

        action = order_sync.mirror_external_refund(tx, refund, "evt_foreign_no_own")

        self.assertEqual(action, "foreign")
        unsettled.invalidate_recordset()
        self.assertEqual(
            unsettled.state, "error", "A foreign report must not complete the sibling."
        )
        self.assertFalse(unsettled.provider_reference)
        self.assertEqual(
            unsettled.xpay_processing_amount_minor,
            10000,
            "A foreign report must not touch the sibling's stored processing amount.",
        )
        children = (
            self.env["payment.transaction"].sudo().search([("source_transaction_id", "=", tx.id)])
        )
        self.assertEqual(len(children), 1, "A foreign report must not create a second child.")
