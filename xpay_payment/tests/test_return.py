from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.tests import tagged

from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestReturn(XPayCommon, PaymentHttpCommon):
    def _tx(self):
        tx = self._create_transaction("direct")
        tx.xpay_session_id = "cs_1"
        return tx

    def test_return_route_reads_session_server_side_and_applies(self):
        transport = self._use_transport()
        tx = self._tx()
        transport.queue(
            200, self.make_session(session_id="cs_1", status="complete", payment_status="paid")
        )

        url = self._build_url(f"/payment/xpay/return?reference={tx.reference}&session_id=cs_1")
        response = self.url_open(url)

        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "done")

    def test_mismatched_session_id_param_is_ignored_not_a_gate(self):
        transport = self._use_transport()
        tx = self._tx()
        transport.queue(
            200, self.make_session(session_id="cs_1", status="complete", payment_status="paid")
        )

        url = self._build_url(
            f"/payment/xpay/return?reference={tx.reference}&session_id=WRONG_ID_ENTIRELY"
        )
        response = self.url_open(url)

        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "done")

    def test_api_failure_leaves_state_and_still_redirects(self):
        transport = self._use_transport()
        tx = self._tx()
        transport.queue(500, {"error": {"code": "internal_error", "message": "boom"}})

        url = self._build_url(f"/payment/xpay/return?reference={tx.reference}&session_id=cs_1")
        response = self.url_open(url)

        self.assertEqual(response.status_code, 200)
        tx.invalidate_recordset()
        self.assertEqual(tx.state, "draft")

    def test_unknown_reference_still_redirects(self):
        url = self._build_url("/payment/xpay/return?reference=does-not-exist&session_id=cs_1")
        response = self.url_open(url)
        self.assertEqual(response.status_code, 200)
