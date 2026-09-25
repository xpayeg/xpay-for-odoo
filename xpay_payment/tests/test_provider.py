import json
import re
from datetime import timedelta

from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.exceptions import UserError
from odoo.fields import Datetime
from odoo.tests import tagged

from ..xpay import hosts
from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestProvider(XPayCommon):
    # -- Account-derived configuration --------------------------------------

    def test_supported_currencies_come_from_the_cached_account(self):
        currencies = self.provider._get_supported_currencies()
        self.assertEqual(set(currencies.mapped("name")), {"EGP", "KWD"})

    def test_supported_currencies_empty_when_not_connected(self):
        self.provider.write(
            {"xpay_restricted_key": False, "xpay_account_json": False, "state": "disabled"}
        )
        currencies = self.provider._get_supported_currencies()
        self.assertFalse(currencies)

    def test_default_payment_method_codes_from_account(self):
        codes = self.provider._get_default_payment_method_codes()
        self.assertEqual(codes, {"card", "valu", "fawry"})

    def test_inline_form_values_minor_amount_exact_for_egp(self):
        raw = self.provider._xpay_inline_form_values(750.00, self.currency_egp, "card", "en_US")
        values = json.loads(raw)
        self.assertEqual(values["minor_amount"], 75000)
        self.assertEqual(values["currency"], "EGP")
        self.assertEqual(values["mode"], "payment")

    def test_sdk_assets_template_script_src_matches_the_host_registry(self):
        # payment_xpay_templates.xml cannot reference a Python constant, so
        # its <script src> is a second, literal declaration of the SDK host.
        # This is the drift check the template's own comment refers to: if
        # xpay.hosts.SDK_URL ever changes without updating the template, this
        # test is what catches it.
        rendered = str(self.env["ir.qweb"]._render("xpay_payment.sdk_assets"))
        match = re.search(r'<script[^>]*\bsrc="([^"]+)"', rendered)
        self.assertIsNotNone(match, rendered)
        self.assertEqual(match.group(1), hosts.SDK_URL)

    def test_inline_form_values_minor_amount_exact_for_kwd_three_decimals(self):
        raw = self.provider._xpay_inline_form_values(750.00, self.currency_kwd, "card", "en_US")
        values = json.loads(raw)
        self.assertEqual(values["minor_amount"], 750000)

    def test_inline_form_values_locale_arabic(self):
        raw = self.provider._xpay_inline_form_values(750.00, self.currency_egp, "card", "ar_EG")
        self.assertEqual(json.loads(raw)["locale"], "ar")

    def test_inline_form_values_locale_defaults_to_english(self):
        raw = self.provider._xpay_inline_form_values(750.00, self.currency_egp, "card", "fr_FR")
        self.assertEqual(json.loads(raw)["locale"], "en")

    def test_inline_form_values_payment_method_types_intersect_account(self):
        raw = self.provider._xpay_inline_form_values(750.00, self.currency_egp, "fawry", "en_US")
        self.assertEqual(json.loads(raw)["payment_method_types"], ["fawry"])

    def test_inline_form_values_publishable_key_never_the_restricted_key(self):
        raw = self.provider._xpay_inline_form_values(750.00, self.currency_egp, "card", "en_US")
        values = json.loads(raw)
        self.assertEqual(values["publishable_key"], "pk_test_abcdef123456")

    def test_removal_values_clear_every_field(self):
        values = self.provider._get_removal_values()
        for field in (
            "xpay_publishable_key",
            "xpay_restricted_key",
            "xpay_webhook_secret",
            "xpay_account_json",
        ):
            self.assertFalse(values[field])

    def test_feature_support_fields(self):
        self.provider._compute_feature_support_fields()
        self.assertEqual(self.provider.support_refund, "partial")

    def test_client_raises_gateway_not_configured_when_no_key(self):
        self.provider.write({"xpay_restricted_key": False, "state": "disabled"})
        with self.assertRaises(UserError):
            self.provider._xpay_client()

    def test_client_user_agent_names_the_module_version_and_base_url(self):
        client = self.provider._xpay_client()
        base_url = self.provider.get_base_url()
        module = self.env["ir.module.module"].sudo().search([("name", "=", "xpay_payment")])
        self.assertTrue(module.installed_version)
        self.assertEqual(client._user_agent, f"xpay-odoo/{module.installed_version} ({base_url})")

    # -- Account cache freshness ---------------------------------------------

    def _age_the_cache(self, hours=3):
        stale = Datetime.to_string(Datetime.now() - timedelta(hours=hours))
        self.provider._xpay_snapshot_update({"fetched_at": stale})

    def test_account_cache_does_not_refresh_when_fresh(self):
        transport = self._use_transport()
        account = self.provider._xpay_account()
        self.assertEqual(transport.calls, [])
        self.assertEqual(account["id"], "acct_test_1")

    def test_account_cache_refreshes_when_older_than_max_age(self):
        transport = self._use_transport()
        self._age_the_cache()
        refreshed = dict(self.xpay_account, defaultCurrency="USD")
        transport.queue(200, refreshed)

        account = self.provider._xpay_account()

        self.assertEqual(len(transport.calls), 1)
        self.assertIn("/account", transport.last_call["url"])
        self.assertEqual(account["defaultCurrency"], "USD")
        fetched_at = self.provider._xpay_snapshot_datetime("fetched_at")
        self.assertTrue(fetched_at and Datetime.now() - fetched_at < timedelta(minutes=1))

    def test_account_cache_refresh_failure_keeps_the_stale_copy(self):
        transport = self._use_transport()
        self._age_the_cache()
        transport.queue(500, {"error": {"code": "internal_error", "message": "boom"}})

        account = self.provider._xpay_account()

        self.assertEqual(account["id"], "acct_test_1")

    def test_action_refresh_account_forces_the_refresh_and_updates_payment_methods(self):
        transport = self._use_transport()
        # Fresh already, but the admin action forces a refresh regardless.
        refreshed = dict(
            self.xpay_account,
            supportedCurrencies=[{"code": "EGP", "decimals": 2, "paymentMethodTypes": ["card"]}],
        )
        transport.queue(200, refreshed)

        self.provider.action_xpay_refresh_account()

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(set(self.provider.payment_method_ids.mapped("code")), {"card"})
        self.assertEqual(set(self.provider.available_currency_ids.mapped("name")), {"EGP"})

    def test_action_refresh_account_activates_archived_methods_the_account_newly_lists(self):
        """XPay's own `instapay` and `valu`, and Odoo's own `bank_transfer`,
        all ship archived. The default active filter on `payment.method`
        must not hide them from the search that links them, and Odoo only
        un-archives on a disabled -> enabled/test `write()` transition
        (`_activate_default_pms`) — Refresh account does not go through
        that, so it must call the same mechanism directly for a method the
        account now lists to become usable."""
        bank_transfer = self.env.ref("payment.payment_method_bank_transfer")
        instapay = self.env.ref("xpay_payment.payment_method_instapay")
        valu = self.env.ref("xpay_payment.payment_method_valu")
        # Force the precondition instead of asserting it: these three ship
        # archived, but a shared dev database (a prior test run against the
        # same container) may have already activated one.
        to_archive = bank_transfer + instapay + valu
        to_archive.active = False
        self.provider.payment_method_ids -= to_archive

        transport = self._use_transport()
        refreshed = dict(
            self.xpay_account,
            supportedCurrencies=[
                {
                    "code": "EGP",
                    "decimals": 2,
                    "paymentMethodTypes": [
                        "card",
                        "valu",
                        "fawry",
                        "instapay",
                        "bank_transfer",
                    ],
                }
            ],
        )
        transport.queue(200, refreshed)

        self.provider.action_xpay_refresh_account()

        self.assertEqual(
            set(self.provider.payment_method_ids.mapped("code")),
            {"card", "valu", "fawry", "instapay", "bank_transfer"},
        )
        to_archive.invalidate_recordset()
        self.assertTrue(bank_transfer.active)
        self.assertTrue(instapay.active)
        self.assertTrue(valu.active)

    def test_action_refresh_account_leaves_an_unlisted_archived_method_archived(self):
        tabby = self.env.ref("xpay_payment.payment_method_tabby")
        tabby.active = False
        self.provider.payment_method_ids -= tabby

        transport = self._use_transport()
        refreshed = dict(
            self.xpay_account,
            supportedCurrencies=[{"code": "EGP", "decimals": 2, "paymentMethodTypes": ["card"]}],
        )
        transport.queue(200, refreshed)

        self.provider.action_xpay_refresh_account()

        tabby.invalidate_recordset()
        self.assertFalse(tabby.active)
        self.assertNotIn("tabby", self.provider.payment_method_ids.mapped("code"))

    def test_currency_gate_is_not_touched_by_the_passive_cache_refresh(self):
        # A merchant may narrow the list by hand; the two-hourly refresh a
        # shopper triggers must not overwrite it. Only Connect and the
        # Refresh account button re-derive it.
        self.provider.available_currency_ids = [(6, 0, self.currency_egp.ids)]
        transport = self._use_transport()
        self._age_the_cache()
        transport.queue(200, self.xpay_account)  # EGP and KWD

        self.provider._xpay_account()

        self.assertEqual(set(self.provider.available_currency_ids.mapped("name")), {"EGP"})


@tagged("post_install", "-at_install")
class TestProviderSdkAssetsGuard(XPayCommon, PaymentHttpCommon):
    """payment_templates.xml adds the XPay SDK script to every payment form
    render; it must appear only when XPay is actually one of the providers
    the form offers."""

    def test_sdk_script_absent_when_xpay_is_not_offered(self):
        demo_module = self.env["ir.module.module"].sudo().search([("name", "=", "payment_demo")])
        if demo_module.state != "installed":
            self.skipTest("payment_demo is not installed")

        # `is_published` published, so the only thing keeping XPay off this
        # form is `state`, the same variable the other half of this pair
        # flips the other way -- an anonymous portal visitor is otherwise
        # filtered out of an unpublished provider regardless of state, which
        # would make this prove nothing about the form's own guard.
        self.provider.write({"is_published": True, "state": "disabled"})

        response = self._portal_pay(**self._prepare_pay_values())

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(hosts.SDK_URL, response.text)

    def test_sdk_script_present_when_xpay_is_offered(self):
        self.provider.is_published = True

        response = self._portal_pay(**self._prepare_pay_values())

        self.assertEqual(response.status_code, 200)
        self.assertIn(hosts.SDK_URL, response.text)
