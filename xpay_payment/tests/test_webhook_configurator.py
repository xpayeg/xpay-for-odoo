from odoo.exceptions import UserError
from odoo.fields import Datetime
from odoo.tests import tagged

from ..services import webhook_configurator
from ..xpay import events
from ..xpay.errors import XPayApiError, XPayTransportError
from .common import XPayCommon, build


@tagged("post_install", "-at_install")
class TestWebhookConfigurator(XPayCommon):
    # -- Admin actions --------------------------------------------------------

    def test_action_reconfigure_webhook_is_rate_limited(self):
        self.provider._xpay_snapshot_update(
            {
                "webhook_endpoint_id": "we_old",
                "webhook_reconfigured_at": Datetime.to_string(Datetime.now()),
            }
        )
        with self.assertRaises(UserError):
            self.provider.action_xpay_reconfigure_webhook()

    def test_action_reconfigure_webhook_creates_the_new_endpoint_before_deleting_the_old_one(self):
        # A reused op id would replay the platform's cached response for
        # the endpoint about to be decommissioned (Idempotent-Replayed);
        # create-before-delete additionally means a failed create never
        # leaves the store without a live endpoint.
        self.provider._xpay_snapshot_update(
            {"webhook_endpoint_id": "we_old", "webhook_op_id": "op-old"}
        )
        transport = self._use_transport()
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})  # ensure_endpoint's own dedupe list
        transport.queue(200, {})  # DELETE the old endpoint, only after the create above
        transport.queue(  # reconcile_enabled_events' list: already matches, no PATCH
            200, {"data": [{"id": "we_new", "enabledEvents": list(events.SUBSCRIBED)}]}
        )

        self.provider.action_xpay_reconfigure_webhook()

        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertEqual(transport.calls[1]["method"], "GET")
        self.assertEqual(transport.calls[2]["method"], "DELETE")
        self.assertIn("we_old", transport.calls[2]["url"])
        snapshot = self._snapshot()
        self.assertEqual(snapshot["webhook_endpoint_id"], "we_new")
        self.assertNotEqual(snapshot["webhook_op_id"], "op-old")
        self.assertTrue(snapshot["webhook_reconfigured_at"])
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_new")
        # enabledEvents already matched: no PATCH was issued.
        self.assertNotIn("PATCH", [c["method"] for c in transport.calls])

    def test_action_reconfigure_webhook_sends_a_different_idempotency_key_than_the_original_create(
        self,
    ):
        transport = self._use_transport()
        # The original create that set up the endpoint being reconfigured.
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_old", secret="whsec_old"))
        transport.queue(200, {"data": []})
        webhook_configurator.ensure_endpoint(self.provider)
        original_key = transport.calls[0]["headers"]["Idempotency-Key"]

        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})  # ensure_endpoint's own dedupe list
        transport.queue(200, {})  # DELETE the old endpoint
        transport.queue(200, {"data": [{"id": "we_new", "enabledEvents": list(events.SUBSCRIBED)}]})

        self.provider.action_xpay_reconfigure_webhook()

        reconfigure_key = transport.calls[2]["headers"]["Idempotency-Key"]
        self.assertNotEqual(reconfigure_key, original_key)
        snapshot = self._snapshot()
        self.assertEqual(snapshot["webhook_endpoint_id"], "we_new")
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_new")

    def test_action_reconfigure_webhook_leaves_the_old_endpoint_when_the_create_fails(self):
        self.provider._xpay_snapshot_update({"webhook_endpoint_id": "we_old"})
        transport = self._use_transport()
        transport.queue(400, {"error": {"code": "invalid_request", "message": "nope"}})

        with self.assertRaises(XPayApiError):
            self.provider.action_xpay_reconfigure_webhook()

        self.assertEqual(len(transport.calls), 1)  # no DELETE followed the failed create
        snapshot = self._snapshot()
        self.assertEqual(snapshot["webhook_endpoint_id"], "we_old")
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_test_1")

    def test_ensure_endpoint_retries_after_a_transport_failure_with_the_same_key(self):
        # No endpoint id was ever persisted for this op id, so the retry is
        # the SAME intended creation and must reuse it, not mint a new one.
        # A plain try/except, not `self.assertRaises` as a context manager:
        # Odoo's own override wraps that context in a savepoint and rolls
        # it back on the expected exception, which would erase the op-id
        # write this test asserts survives the failure.
        transport = self._use_transport()
        transport.fail(XPayTransportError("connection reset"))
        try:
            webhook_configurator.ensure_endpoint(self.provider)
            self.fail("expected an XPayTransportError")
        except XPayTransportError:
            pass
        first_key = transport.calls[0]["headers"]["Idempotency-Key"]
        first_op_id = self._snapshot()["webhook_op_id"]
        self.assertFalse(self._snapshot().get("webhook_endpoint_id"))

        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})
        webhook_configurator.ensure_endpoint(self.provider)

        retry_key = transport.calls[1]["headers"]["Idempotency-Key"]
        self.assertEqual(retry_key, first_key)
        self.assertEqual(self._snapshot()["webhook_op_id"], first_op_id)

    def test_ensure_endpoint_retries_with_a_fresh_key_after_the_endpoint_cap(self):
        # The rejected create's key is cached by the platform as a
        # definitive answer for a day: replaying it on retry would return
        # that same rejection instead of trying again against the slot
        # `_free_a_slot` just freed.
        transport = self._use_transport()
        transport.queue(
            400,
            {
                "error": {
                    "code": "invalid_request",
                    "message": "You have reached the maximum number of webhook endpoints",
                }
            },
        )
        url = webhook_configurator._endpoint_url(self.provider)
        transport.queue(
            200,
            {
                "data": [
                    build(
                        "WebhookEndpointResponse",
                        id="we_old",
                        url=url,
                        createdAt="2020-01-01T00:00:00Z",
                    )
                ]
            },
        )
        transport.queue(200, {})  # DELETE the freed slot
        transport.queue(201, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})  # dedupe list after the successful retry

        webhook_configurator.ensure_endpoint(self.provider)

        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertEqual(transport.calls[1]["method"], "GET")
        self.assertEqual(transport.calls[2]["method"], "DELETE")
        self.assertEqual(transport.calls[3]["method"], "POST")
        first_key = transport.calls[0]["headers"]["Idempotency-Key"]
        retry_key = transport.calls[3]["headers"]["Idempotency-Key"]
        self.assertNotEqual(retry_key, first_key)
        self.assertEqual(self._snapshot()["webhook_endpoint_id"], "we_new")
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_new")

    # -- enabledEvents reconciliation -----------------------------------------

    def test_reconcile_enabled_events_patches_when_they_differ(self):
        self.provider._xpay_snapshot_update(
            {"webhook_endpoint_id": "we_1", "webhook_op_id": "op-1"}
        )
        transport = self._use_transport()
        transport.queue(
            200, {"data": [{"id": "we_1", "enabledEvents": ["checkout.session.completed"]}]}
        )
        transport.queue(200, {"id": "we_1", "enabledEvents": list(events.SUBSCRIBED)})

        webhook_configurator.reconcile_enabled_events(self.provider)

        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(transport.calls[1]["method"], "PATCH")
        self.assertEqual(
            sorted(transport.calls[1]["json_body"]["enabledEvents"]), sorted(events.SUBSCRIBED)
        )

    def test_reconcile_enabled_events_is_a_noop_when_they_already_match(self):
        self.provider._xpay_snapshot_update(
            {"webhook_endpoint_id": "we_1", "webhook_op_id": "op-1"}
        )
        transport = self._use_transport()
        transport.queue(200, {"data": [{"id": "we_1", "enabledEvents": list(events.SUBSCRIBED)}]})

        webhook_configurator.reconcile_enabled_events(self.provider)

        self.assertEqual(len(transport.calls), 1)  # only the list call, no PATCH

    def test_reconcile_enabled_events_is_a_noop_without_a_recorded_endpoint(self):
        transport = self._use_transport()
        webhook_configurator.reconcile_enabled_events(self.provider)
        self.assertEqual(transport.calls, [])

    # -- ensure_endpoint response validation ---------------------------------

    def test_ensure_endpoint_rejects_a_response_missing_the_secret(self):
        transport = self._use_transport()
        # A malformed create response (id present, secret missing) must not
        # be persisted: writing a falsy secret and then deduping with
        # keep_id=None would delete every other endpoint at this URL,
        # including the one just "created", leaving the store with no
        # working endpoint and no usable secret.
        transport.queue(200, {"id": "we_new"})

        with self.assertRaises(XPayApiError):
            webhook_configurator.ensure_endpoint(self.provider)

        self.assertFalse(self._snapshot().get("webhook_endpoint_id"))
        self.assertEqual(len(transport.calls), 1)  # no dedupe/list call followed

    def test_ensure_endpoint_rejects_a_response_missing_the_id(self):
        transport = self._use_transport()
        transport.queue(200, {"secret": "whsec_new"})

        with self.assertRaises(XPayApiError):
            webhook_configurator.ensure_endpoint(self.provider)

        self.assertFalse(self._snapshot().get("webhook_endpoint_id"))
        # The secret this store already had (set up by XPayCommon) must be
        # left untouched, not overwritten with the malformed response's data.
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_test_1")
        self.assertEqual(len(transport.calls), 1)

    def test_endpoint_url_has_no_plane_segment(self):
        self.assertEqual(
            webhook_configurator._endpoint_url(self.provider),
            f"https://xpay-test.example/payment/xpay/webhook/{self.provider.id}",
        )
