"""`/payment/xpay/connect/callback` over real HTTP: a Connect failure must
land on the provider (`xpay_connect_error`), never silently down a URL
query flag nothing reads back."""

from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.tests import tagged

from ..services import connect_errors, connect_service
from ..xpay.errors import XPayTransportError
from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestConnectCallback(XPayCommon, PaymentHttpCommon):
    def _authenticate_as_admin(self, **flow):
        admin = self.env.ref("base.user_admin")
        self.authenticate(
            admin.login,
            admin.login,
            session_extra={connect_service.SESSION_KEY: flow} if flow else None,
        )

    def test_callback_with_oauth_error_sets_the_alert(self):
        self._authenticate_as_admin(
            provider_id=self.provider.id,
            plane="test",
            state="state_1",
            verifier="verifier_1",
            client_id="client_1",
        )

        url = self._build_url("/payment/xpay/connect/callback?error=access_denied&state=state_1")
        response = self.url_open(url)

        self.assertEqual(response.status_code, 200)
        self.provider.invalidate_recordset()
        self.assertTrue(self.provider.xpay_connect_error)
        self.assertIn("XPay", self.provider.xpay_connect_error)
        self.assertEqual(self._snapshot()["connect_error_code"], "access_denied")

    def test_callback_success_clears_a_previous_connect_error(self):
        self.provider._xpay_snapshot_update(
            {
                "connect_error_code": "access_denied",
                "connect_error_message": "XPay: the connection was canceled before it finished.",
                "connect_error_at": "2026-01-01 00:00:00",
            }
        )
        self.provider.invalidate_recordset()
        self.assertTrue(self.provider.xpay_connect_error)

        transport = self._use_transport()
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})
        self._authenticate_as_admin(
            provider_id=self.provider.id,
            plane="test",
            state="state_2",
            verifier="verifier_1",
            client_id="client_1",
        )

        url = self._build_url("/payment/xpay/connect/callback?code=auth_code_1&state=state_2")
        response = self.url_open(url)

        self.assertEqual(response.status_code, 200)
        self.provider.invalidate_recordset()
        self.assertFalse(self.provider.xpay_connect_error)

    def test_callback_records_a_new_error_despite_an_earlier_unrelated_incomplete_flag(self):
        # A same-plane provisioning failure raises XPaySetupIncomplete and is
        # deliberately not recorded here (its own banner covers it). A token
        # exchange failure is a different, ordinary UserError and must still
        # get its own visible error, even though the provider already
        # carries an incomplete flag left by an earlier, unrelated failure.
        self.provider._xpay_snapshot_update({"setup_incomplete_step": connect_errors.STEP_WEBHOOK})
        self.provider.invalidate_recordset()

        transport = self._use_transport()
        transport.fail(XPayTransportError("timed out"))
        self._authenticate_as_admin(
            provider_id=self.provider.id,
            plane="test",
            state="state_3",
            verifier="verifier_1",
            client_id="client_1",
        )

        url = self._build_url("/payment/xpay/connect/callback?code=auth_code_1&state=state_3")
        response = self.url_open(url)

        self.assertEqual(response.status_code, 200)
        self.provider.invalidate_recordset()
        self.assertTrue(self.provider.xpay_connect_error)
        self.assertEqual(self._snapshot()["connect_error_code"], "connect_failed")
        # The earlier, unrelated flag is untouched by this ambiguous failure.
        self.assertEqual(self._snapshot()["setup_incomplete_step"], connect_errors.STEP_WEBHOOK)

    def test_callback_without_a_client_id_in_the_flow_is_treated_as_stale(self):
        # Every flow this module starts now carries a client id alongside
        # state and verifier; one that lacks it is refused the same way
        # as a missing flow, before the provider is even looked up.
        self._authenticate_as_admin(
            provider_id=self.provider.id, plane="test", state="state_4", verifier="verifier_1"
        )

        url = self._build_url("/payment/xpay/connect/callback?code=auth_code_1&state=state_4")
        response = self.url_open(url)

        self.assertEqual(response.status_code, 403)

    def test_unconnected_provider_computes_the_error_field_without_raising(self):
        self.provider.write(
            {
                "xpay_restricted_key": False,
                "xpay_publishable_key": False,
                "xpay_webhook_secret": False,
                "xpay_account_json": False,
                "state": "disabled",
            }
        )
        self.provider.invalidate_recordset()
        self.assertFalse(self.provider.xpay_is_connected)
        self.assertFalse(self.provider.xpay_connect_error)
