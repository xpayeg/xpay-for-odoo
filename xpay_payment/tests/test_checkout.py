from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from ..services import order_sync
from ..xpay import idempotency
from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestCheckout(XPayCommon):
    def test_get_processing_values_creates_session_with_exact_body(self):
        transport = self._use_transport()
        transport.queue(200, self.make_session(session_id="cs_1"))

        tx = self._create_transaction("direct")
        values = tx._get_processing_values()

        self.assertEqual(values["client_secret"], "cs_1_secret")
        self.assertEqual(values["session_id"], "cs_1")
        # The browser gets the real id; XPay gets the template it substitutes.
        self.assertIn("session_id=cs_1", values["return_url"])

        call = transport.last_call
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"], f"{tx.provider_id._xpay_api_base()}/checkout/sessions?liveMode=false"
        )
        body = call["json_body"]
        self.assertEqual(
            call["headers"]["Idempotency-Key"],
            idempotency.bind_to_body(idempotency.session_key(tx.reference, 1), body),
        )
        self.assertEqual(body["uiMode"], "custom")
        self.assertEqual(body["currency"], "EGP")
        self.assertEqual(
            body["lineItems"],
            [
                {
                    "quantity": 1,
                    "priceData": {
                        "currency": "EGP",
                        "unitAmount": 75000,
                        "productData": {"name": tx.reference},
                    },
                }
            ],
        )
        self.assertEqual(body["afterCompletion"]["type"], "redirect")
        self.assertIn(
            "session_id={CHECKOUT_SESSION_ID}", body["afterCompletion"]["redirect"]["url"]
        )
        # Who is paying: the transaction's snapshot of the billing partner
        # (guest case: details only, no creation flag).
        self.assertEqual(
            body["customerDetails"],
            {
                "name": "Norbert Buyer",
                "email": "norbert.buyer@example.com",
                "phone": "0032 12 34 56 78",
                "billingDetails": {
                    "address": {
                        "line1": tx.partner_address,
                        "city": "Sin City",
                        "postalCode": "1000",
                        "country": "BE",
                    }
                },
            },
        )
        self.assertNotIn("shipping", body["customerDetails"])
        self.assertNotIn("customerId", body)
        self.assertNotIn("customerCreation", body)
        self.assertEqual(body["metadata"]["integration"], "odoo")
        self.assertEqual(body["metadata"]["tx_reference"], tx.reference)
        self.assertIn("db_uuid", body["metadata"])

        tx.invalidate_recordset()
        self.assertEqual(tx.xpay_session_id, "cs_1")
        self.assertEqual(tx.xpay_session_attempt, 1)

    def test_customer_details_drop_empty_values_and_add_the_delivery_address(self):
        transport = self._use_transport()
        transport.queue(200, self.make_session(session_id="cs_1"))
        egypt = self.env.ref("base.eg")
        cairo = self.env["res.country.state"].search(
            [("country_id", "=", egypt.id), ("name", "=", "Cairo")], limit=1
        ) or self.env["res.country.state"].create(
            {"name": "Cairo", "code": "C", "country_id": egypt.id}
        )
        buyer = self.env["res.partner"].create(
            {"name": "Mo Mohamed", "email": "mo@example.com", "country_id": egypt.id}
        )
        delivery = self.env["res.partner"].create(
            {
                "name": "Mo at work",
                "parent_id": buyer.id,
                "type": "delivery",
                "phone": "+20 100 000 0000",
                "street": "24 Ezz",
                "street2": "Floor 3",
                "city": "Cairo",
                "state_id": cairo.id,
                "zip": "11756",
                "country_id": egypt.id,
            }
        )
        tx = self._create_transaction("direct", partner_id=buyer.id)
        if "sale_order_ids" in tx._fields:
            order = self.env["sale.order"].create(
                {"partner_id": buyer.id, "partner_shipping_id": delivery.id}
            )
            tx.sale_order_ids = [(4, order.id)]

        tx._get_processing_values()

        details = transport.last_call["json_body"]["customerDetails"]
        self.assertEqual(details["name"], "Mo Mohamed")
        self.assertEqual(details["email"], "mo@example.com")
        self.assertNotIn("phone", details)  # no phone: not sent as blank
        self.assertEqual(details["billingDetails"], {"address": {"country": "EG"}})
        if "sale_order_ids" in tx._fields:
            self.assertEqual(
                details["shipping"],
                {
                    "name": "Mo at work",
                    "phone": "+20 100 000 0000",
                    "address": {
                        "line1": "24 Ezz Floor 3",
                        "city": "Cairo",
                        "state": "Cairo",
                        "postalCode": "11756",
                        "country": "EG",
                    },
                },
            )

    # -- Customer linking -----------------------------------------------------

    def _registered_buyer(self, **partner_values):
        partner = self.env["res.partner"].create(
            {"name": "Reg Buyer", "email": "reg@example.com", **partner_values}
        )
        self.env["res.users"].create(
            {
                "name": "Reg Buyer",
                "login": f"reg-{partner.id}@example.com",
                "partner_id": partner.id,
                "groups_id": [(6, 0, [self.env.ref("base.group_portal").id])],
            }
        )
        return partner

    def test_registered_shopper_without_a_link_asks_xpay_to_create_the_customer(self):
        transport = self._use_transport()
        transport.queue(200, self.make_session(session_id="cs_1"))
        tx = self._create_transaction("direct", partner_id=self._registered_buyer().id)

        tx._get_processing_values()

        body = transport.last_call["json_body"]
        self.assertNotIn("customerId", body)
        self.assertEqual(body["customerCreation"], "always")
        self.assertEqual(body["customerDetails"]["email"], "reg@example.com")

    def test_registered_shopper_with_a_stored_link_sends_customer_id_only(self):
        transport = self._use_transport()
        transport.queue(200, self.make_session(session_id="cs_1"))
        buyer = self._registered_buyer()
        buyer._xpay_remember_customer("test", "cus_test_known")
        buyer._xpay_remember_customer("live", "cus_live_other")  # wrong plane: ignored
        tx = self._create_transaction("direct", partner_id=buyer.id)

        tx._get_processing_values()

        body = transport.last_call["json_body"]
        self.assertEqual(body["customerId"], "cus_test_known")
        self.assertNotIn("customerDetails", body)
        self.assertNotIn("customerCreation", body)

    def test_stale_customer_link_is_cleared_and_the_create_retried_once(self):
        transport = self._use_transport()
        buyer = self._registered_buyer()
        buyer._xpay_remember_customer("test", "cus_test_gone")
        tx = self._create_transaction("direct", partner_id=buyer.id)
        transport.queue(404, {"error": {"code": "resource_missing", "message": "No such customer"}})
        transport.queue(200, self.make_session(session_id="cs_1"))

        values = tx._get_processing_values()

        self.assertEqual(values["session_id"], "cs_1")
        first, second = transport.calls[-2], transport.calls[-1]
        self.assertEqual(first["json_body"]["customerId"], "cus_test_gone")
        self.assertNotIn("customerId", second["json_body"])
        self.assertEqual(second["json_body"]["customerCreation"], "always")
        self.assertNotEqual(
            first["headers"]["Idempotency-Key"], second["headers"]["Idempotency-Key"]
        )
        buyer.invalidate_recordset()
        self.assertIsNone(buyer._xpay_customer_id("test"))

    def test_other_errors_with_a_customer_link_are_not_retried(self):
        transport = self._use_transport()
        buyer = self._registered_buyer()
        buyer._xpay_remember_customer("test", "cus_test_known")
        tx = self._create_transaction("direct", partner_id=buyer.id)
        transport.queue(400, {"error": {"code": "invalid_request", "message": "nope"}})

        with self.assertRaises(UserError):
            tx._get_processing_values()

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(buyer._xpay_customer_id("test"), "cus_test_known")

    def test_a_later_paid_webhook_for_the_own_superseded_session_parks(self):
        transport = self._use_transport()
        transport.queue(200, self.make_session(session_id="cs_2"))
        tx = self._create_transaction("direct")
        tx._get_processing_values()
        tx.xpay_superseded_session_ids = ["cs_1"]

        session = self.make_session(session_id="cs_1", status="complete", payment_status="paid")
        action = order_sync.apply_locked(
            tx, {"session": session, "event_type": None, "event_id": "evt_own_superseded"}
        )

        self.assertEqual(action, "superseded_paid")
        self.assertEqual(tx.state, "draft")

    def test_unit_amount_over_the_int4_ceiling_is_refused_locally(self):
        transport = self._use_transport()
        tx = self._create_transaction("direct", amount=30000000000.0)

        with self.assertRaises(UserError):
            tx._get_processing_values()

        # Refused before any request was even sent.
        self.assertEqual(transport.calls, [])

    def _restrict_account_to(self, wire_types, *, currency="EGP"):
        snapshot = dict(
            self.provider.xpay_account_json,
            account=dict(
                self.xpay_account,
                supportedCurrencies=[
                    {"code": currency, "decimals": 2, "paymentMethodTypes": list(wire_types)}
                ],
            ),
        )
        self.provider.xpay_account_json = snapshot

    def test_session_refuses_a_method_the_account_does_not_enable_for_the_currency(self):
        transport = self._use_transport()
        self._restrict_account_to(["card"])
        valu = self.env.ref("xpay_payment.payment_method_valu")
        tx = self._create_transaction("direct", payment_method_id=valu.id)

        with self.assertRaises(ValidationError):
            tx._get_processing_values()

        # Refused before any request was even sent.
        self.assertEqual(transport.calls, [])

    def test_session_sends_only_the_payment_method_types_the_account_enables(self):
        transport = self._use_transport()
        self._restrict_account_to(["card"])
        transport.queue(200, self.make_session(session_id="cs_1"))
        tx = self._create_transaction("direct")

        tx._get_processing_values()

        self.assertEqual(transport.last_call["json_body"]["paymentMethodTypes"], ["card"])

    def test_session_create_failure_does_not_leak_the_raw_api_message(self):
        transport = self._use_transport()
        tx = self._create_transaction("direct")
        transport.queue(
            400,
            {
                "error": {
                    "code": "parameter_invalid",
                    "message": "sensitive gateway detail: card ending 4242 declined",
                }
            },
        )

        with self.assertRaises(UserError) as catcher:
            tx._get_processing_values()

        message = str(catcher.exception)
        self.assertIn("parameter_invalid", message)
        self.assertNotIn("sensitive gateway detail", message)
        self.assertNotIn("4242", message)

    def test_sibling_draft_transaction_session_is_expired_and_superseded(self):
        transport = self._use_transport()
        transport.queue(200, self.make_session(session_id="cs_1"))
        tx1 = self._create_transaction("direct", reference="Test Transaction 1")
        tx1._get_processing_values()

        so_model = self.env["ir.model.fields"].search(
            [("model", "=", "payment.transaction"), ("name", "=", "sale_order_ids")]
        )
        if not so_model:
            self.skipTest("sale_order_ids is not available without the sale module installed")

        order = self.env["sale.order"].create({"partner_id": self.partner.id})
        tx1.write({"sale_order_ids": [(6, 0, order.ids)]})

        transport.queue(200, {})  # expire call on the sibling
        transport.queue(200, self.make_session(session_id="cs_2"))
        tx2 = self._create_transaction(
            "direct", reference="Test Transaction 2", sale_order_ids=[(6, 0, order.ids)]
        )
        tx2._get_processing_values()

        tx1.invalidate_recordset()
        self.assertIn("cs_1", tx1.xpay_superseded_session_ids)
        self.assertFalse(tx1.xpay_session_id)

    def test_session_response_without_a_client_secret_is_refused(self):
        transport = self._use_transport()
        session = self.make_session(session_id="cs_1")
        session.pop("clientSecret", None)
        transport.queue(200, session)

        tx = self._create_transaction("direct")
        with self.assertRaises(UserError):
            tx._get_processing_values()
