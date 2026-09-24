from datetime import timedelta

from odoo.exceptions import ValidationError
from odoo.fields import Datetime
from odoo.tests import tagged

from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestProviderState(XPayCommon):
    """One credential set, `state` follows the key's plane, and the form's
    computed status texts."""

    # -- Plane and state ------------------------------------------------------

    def test_plane_comes_from_the_key_prefix(self):
        self.assertEqual(self.provider._xpay_plane(), "test")
        self.provider.write({"xpay_restricted_key": "rk_live_abcdef123456", "state": "enabled"})
        self.assertEqual(self.provider._xpay_plane(), "live")
        self.provider.write({"xpay_restricted_key": False, "state": "disabled"})
        self.assertIsNone(self.provider._xpay_plane())

    def test_state_cannot_be_enabled_on_a_test_key(self):
        with self.assertRaises(ValidationError):
            self.provider.state = "enabled"

    def test_state_cannot_be_test_on_a_live_key(self):
        with self.assertRaises(ValidationError):
            self.provider.write({"xpay_restricted_key": "rk_live_abcdef123456", "state": "test"})

    def test_state_cannot_be_on_without_a_key(self):
        with self.assertRaises(ValidationError):
            self.provider.write({"xpay_restricted_key": False, "state": "test"})

    def test_disabled_never_needs_a_key(self):
        self.provider.write({"xpay_restricted_key": False, "state": "disabled"})
        self.assertFalse(self.provider.xpay_is_connected)

    def test_status_texts(self):
        self.assertTrue(self.provider.xpay_is_connected)
        self.assertEqual(self.provider.xpay_plane, "test")
        self.assertIn("XPay Test Merchant", self.provider.xpay_connection_status)
        self.assertIn("test account", self.provider.xpay_connection_status)
        self.assertIn("Not set up", self.provider.xpay_webhook_status)

        self.provider._xpay_snapshot_update(
            {
                "webhook_endpoint_id": "we_1",
                "webhook_last_success_at": Datetime.to_string(Datetime.now()),
            }
        )
        self.assertIn("Healthy", self.provider.xpay_webhook_status)

        self.provider._xpay_snapshot_update(
            {
                "webhook_last_failure_at": Datetime.to_string(
                    Datetime.now() + timedelta(seconds=5)
                ),
                "webhook_last_failure_reason": "webhook_signature_invalid",
            }
        )
        self.assertIn("Failing", self.provider.xpay_webhook_status)
        self.assertIn("webhook_signature_invalid", self.provider.xpay_webhook_status)

        self.provider.write({"xpay_restricted_key": False, "state": "disabled"})
        self.assertEqual(self.provider.xpay_connection_status, "Not connected")

    def test_snapshot_update_merges_one_level_deep(self):
        self.provider._xpay_snapshot_update({"webhook_endpoint_id": "we_1"})
        self.provider._xpay_snapshot_update({"webhook_last_failure_reason": "x"})
        snapshot = self._snapshot()
        self.assertEqual(snapshot["webhook_endpoint_id"], "we_1")
        self.assertEqual(snapshot["webhook_last_failure_reason"], "x")
        self.assertEqual(snapshot["account"]["id"], "acct_test_1")  # untouched
