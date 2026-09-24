from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from odoo import api, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from ..services import checkout_service, connect_errors, connect_service
from ..xpay import connect
from ..xpay.errors import XPayTransportError
from .common import XPayCommon


@tagged("post_install", "-at_install")
class TestConnect(XPayCommon):
    def _params(self):
        return self.env["ir.config_parameter"].sudo()

    def test_start_registers_a_client_and_builds_the_authorize_url_with_s256(self):
        transport = self._use_transport()
        transport.queue(200, {"client_id": "client_1", "client_secret": "xpcs_1"})

        result = connect_service.start(self.provider, "test", self.env.user)

        self.assertEqual(self._params().get_param(connect_service.PARAM_CLIENT_ID), "client_1")
        self.assertTrue(self._params().get_param(connect_service.PARAM_CLIENT_REGISTERED_AT))

        parts = urlsplit(result["url"])
        self.assertEqual(parts.scheme, "https")
        query = parse_qs(parts.query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["client_1"])
        self.assertEqual(query["scope"], ["merchant.connect.test"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [connect.challenge(result["verifier"])])
        self.assertEqual(query["state"], [result["state"]])

        register_call = transport.calls[0]
        self.assertIn("oauth2/register", register_call["url"])
        self.assertEqual(register_call["json_body"]["token_endpoint_auth_method"], "none")

    def test_start_reuses_a_recent_client_registration(self):
        transport = self._use_transport()
        self._params().set_param(connect_service.PARAM_CLIENT_ID, "client_existing")
        self._params().set_param(
            connect_service.PARAM_CLIENT_REDIRECT_URI, connect_service._redirect_uri(self.provider)
        )
        self._params().set_param(
            connect_service.PARAM_CLIENT_REGISTERED_AT,
            fields.Datetime.to_string(fields.Datetime.now()),
        )

        result = connect_service.start(self.provider, "test", self.env.user)

        self.assertEqual(transport.calls, [])
        self.assertIn("client_existing", result["url"])

    def test_start_reuses_within_six_days_with_an_unchanged_redirect_uri(self):
        transport = self._use_transport()
        self._params().set_param(connect_service.PARAM_CLIENT_ID, "client_existing")
        self._params().set_param(
            connect_service.PARAM_CLIENT_REDIRECT_URI, connect_service._redirect_uri(self.provider)
        )
        self._params().set_param(
            connect_service.PARAM_CLIENT_REGISTERED_AT,
            fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=5)),
        )

        connect_service.start(self.provider, "test", self.env.user)

        self.assertEqual(transport.calls, [])

    def test_start_reregisters_when_the_base_url_parameter_changes(self):
        transport = self._use_transport()
        self._params().set_param(connect_service.PARAM_CLIENT_ID, "client_old")
        self._params().set_param(
            connect_service.PARAM_CLIENT_REDIRECT_URI,
            "https://old-host.example/payment/xpay/connect/callback",
        )
        self._params().set_param(
            connect_service.PARAM_CLIENT_REGISTERED_AT,
            fields.Datetime.to_string(fields.Datetime.now()),
        )
        transport.queue(200, {"client_id": "client_new", "client_secret": "xpcs_new"})

        result = connect_service.start(self.provider, "test", self.env.user)

        register_call = transport.calls[0]
        self.assertIn("oauth2/register", register_call["url"])
        self.assertEqual(self._params().get_param(connect_service.PARAM_CLIENT_ID), "client_new")
        self.assertEqual(
            self._params().get_param(connect_service.PARAM_CLIENT_REDIRECT_URI),
            connect_service._redirect_uri(self.provider),
        )
        self.assertIn("client_new", result["url"])

    def test_start_reuses_a_completed_client_past_the_max_age(self):
        transport = self._use_transport()
        self._params().set_param(connect_service.PARAM_CLIENT_ID, "client_existing")
        self._params().set_param(
            connect_service.PARAM_CLIENT_REDIRECT_URI, connect_service._redirect_uri(self.provider)
        )
        self._params().set_param(
            connect_service.PARAM_CLIENT_REGISTERED_AT,
            fields.Datetime.to_string(
                fields.Datetime.now() - timedelta(days=connect.REGISTRATION_MAX_AGE_DAYS + 5)
            ),
        )
        self._params().set_param(
            connect_service.PARAM_CLIENT_COMPLETED_AT,
            fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=1)),
        )

        result = connect_service.start(self.provider, "test", self.env.user)

        self.assertEqual(transport.calls, [])
        self.assertIn("client_existing", result["url"])

    def test_start_registering_a_new_client_clears_completed_at(self):
        transport = self._use_transport()
        self._params().set_param(connect_service.PARAM_CLIENT_ID, "client_old")
        self._params().set_param(
            connect_service.PARAM_CLIENT_REDIRECT_URI,
            "https://old-host.example/payment/xpay/connect/callback",
        )
        self._params().set_param(
            connect_service.PARAM_CLIENT_REGISTERED_AT,
            fields.Datetime.to_string(fields.Datetime.now()),
        )
        self._params().set_param(
            connect_service.PARAM_CLIENT_COMPLETED_AT,
            fields.Datetime.to_string(fields.Datetime.now()),
        )
        transport.queue(200, {"client_id": "client_new", "client_secret": "xpcs_new"})

        connect_service.start(self.provider, "test", self.env.user)

        self.assertFalse(self._params().get_param(connect_service.PARAM_CLIENT_COMPLETED_AT))

    def test_start_reports_an_unreachable_registration_as_a_user_error(self):
        transport = self._use_transport()
        transport.fail(XPayTransportError("connection refused"))

        with self.assertRaises(UserError):
            connect_service.start(self.provider, "test", self.env.user)

        self.assertFalse(self._params().get_param(connect_service.PARAM_CLIENT_ID))

    def test_complete_reports_an_unreachable_token_endpoint_as_ambiguous(self):
        transport = self._use_transport()
        self.provider.write({"xpay_restricted_key": "rk_test_old"})
        transport.fail(XPayTransportError("timed out"))

        # A plain try/except, not `self.assertRaises` as a context manager:
        # see test_complete_same_plane_webhook_failure_* for why the
        # context-manager form would roll back the very "nothing changed"
        # state this test checks.
        try:
            connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")
            self.fail("expected a UserError")
        except UserError as exc:
            # Same wording as an unclear status: the platform may have
            # issued the key, so the merchant is told how to check, not
            # "try again".
            self.assertIn("unclear", str(exc))

        self.provider.invalidate_recordset()
        self.assertEqual(self.provider.xpay_restricted_key, "rk_test_old")

    def test_complete_provisions_in_the_amended_s15_order(self):
        transport = self._use_transport()
        self.provider.write({"xpay_restricted_key": "rk_test_old"})
        self.provider._xpay_snapshot_update({"webhook_endpoint_id": "we_old"})
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})  # dedupe list: nothing else to delete
        transport.queue(200, {})  # decommission of the OLD endpoint

        result = connect_service.complete(
            self.provider, "test", "auth_code_1", "verifier_1", "client_1"
        )

        self.assertIn("oauth2/token", transport.calls[0]["url"])
        self.assertTrue(transport.calls[0]["url"].endswith("oauth2/token"))
        self.assertIn("/account", transport.calls[1]["url"])
        self.assertIn("/webhook-endpoints", transport.calls[2]["url"])
        self.assertEqual(transport.calls[2]["method"], "POST")
        self.assertEqual(transport.calls[3]["method"], "GET")
        self.assertEqual(transport.calls[4]["method"], "DELETE")
        self.assertIn("we_old", transport.calls[4]["url"])

        snapshot = self._snapshot()
        self.assertEqual(self.provider.xpay_restricted_key, result.restricted_key)
        self.assertEqual(self.provider.xpay_publishable_key, result.publishable_key)
        self.assertEqual(snapshot["webhook_endpoint_id"], "we_new")
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_new")
        self.assertEqual(snapshot["merchant_id"], "acct_test_1")
        self.assertTrue(snapshot["connected_at"])
        self.assertEqual(self.provider.state, "test")
        self.assertFalse(self.provider.is_published)
        self.assertEqual(
            set(self.provider.payment_method_ids.mapped("code")), {"card", "valu", "fawry"}
        )
        # The currency gate follows the account: Odoo only computes it at
        # install, when nothing is connected yet.
        self.assertEqual(set(self.provider.available_currency_ids.mapped("name")), {"EGP", "KWD"})

    def test_complete_same_plane_reconnect_decommissions_the_old_endpoint_with_the_new_key(self):
        # A same-plane exchange retires the previous key at the platform
        # atomically with issuing the new one, so a client built from the
        # previous key cannot authenticate the delete of the old endpoint.
        transport = self._use_transport()
        self.provider.write({"xpay_restricted_key": "rk_test_old"})
        self.provider._xpay_snapshot_update({"webhook_endpoint_id": "we_old"})
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})  # dedupe list: nothing else to delete
        transport.queue(200, {})  # decommission of the OLD endpoint

        connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")

        self.assertEqual(transport.calls[4]["method"], "DELETE")
        self.assertIn("we_old", transport.calls[4]["url"])
        self.assertIn("rk_test_newkey", transport.calls[4]["headers"]["Authorization"])

    def test_complete_persists_the_secret_before_dedupe(self):
        """The endpoint create response's secret must be written to the
        provider before the dedupe list/delete pass runs: queuing the
        dedupe response with an empty `data` list and letting the
        call-order assertions in the test above cover the ordering
        directly is redundant here, so this asserts the end state only."""
        transport = self._use_transport()
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})

        connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")

        self.provider.invalidate_recordset()
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_new")

    def test_complete_marks_the_client_registration_completed(self):
        transport = self._use_transport()
        self._params().set_param(connect_service.PARAM_CLIENT_ID, "client_1")
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})

        connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")

        self.assertTrue(self._params().get_param(connect_service.PARAM_CLIENT_COMPLETED_AT))

    def test_complete_uses_the_flow_client_id_even_if_the_parameter_changed(self):
        # A stale tab, or a second administrator registering a new client
        # in between, must not make the exchange use the wrong client id:
        # `complete()` never reads `PARAM_CLIENT_ID` at all.
        transport = self._use_transport()
        self._params().set_param(connect_service.PARAM_CLIENT_ID, "client_changed_since_start")
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})

        connect_service.complete(
            self.provider, "test", "auth_code_1", "verifier_1", "client_from_flow"
        )

        token_call = transport.calls[0]
        self.assertTrue(token_call["url"].endswith("oauth2/token"))
        self.assertEqual(token_call["form"]["client_id"], "client_from_flow")
        # The client that completed is not the one now stored, so the
        # stored one must not be marked completed on its behalf.
        self.assertFalse(self._params().get_param(connect_service.PARAM_CLIENT_COMPLETED_AT))

    def test_complete_same_plane_webhook_failure_keeps_the_new_key_and_flags_incomplete(self):
        """Platform fact: a same-plane token exchange retires the previous
        key atomically, so it is already dead when the webhook step fails
        — restoring it would leave the provider unable to reach XPay at
        all. The new key is kept and setup is flagged incomplete instead."""
        transport = self._use_transport()
        self.provider.write({"xpay_restricted_key": "rk_test_old"})
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)
        transport.queue(400, {"error": {"code": "invalid_request", "message": "nope"}})

        # A plain try/except, not `self.assertRaises` as a context manager:
        # Odoo's own override wraps that context in a savepoint and rolls
        # it back on the expected exception, which would erase the very
        # persisted-despite-the-error write this test exists to check.
        try:
            connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")
            self.fail("expected a UserError")
        except UserError:
            pass

        self.provider.invalidate_recordset()
        self.assertEqual(self.provider.xpay_restricted_key, "rk_test_newkey")
        self.assertEqual(self.provider.xpay_publishable_key, "pk_test_newkey")
        self.assertEqual(self.provider.state, "test")
        self.assertEqual(self._snapshot()["setup_incomplete_step"], connect_errors.STEP_WEBHOOK)
        self.assertTrue(self.provider.xpay_setup_incomplete)

    def test_complete_same_plane_account_fetch_failure_keeps_the_new_key_and_flags_incomplete(self):
        """Same platform fact as the webhook-step test above: a same-plane
        token exchange retires the previous key atomically, so it is
        already dead when the account read fails right after. The new key
        is kept and setup is flagged incomplete at the account step
        instead."""
        transport = self._use_transport()
        self.provider.write({"xpay_restricted_key": "rk_test_old"})
        self._params().set_param(connect_service.PARAM_CLIENT_ID, "client_1")
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(400, {"error": {"code": "invalid_request", "message": "nope"}})

        # A plain try/except, not `self.assertRaises` as a context manager:
        # see test_complete_same_plane_webhook_failure_* above for why the
        # context-manager form would roll back the very persisted-despite-
        # the-error write this test exists to check.
        try:
            connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")
            self.fail("expected a UserError")
        except UserError:
            pass

        self.provider.invalidate_recordset()
        self.assertEqual(self.provider.xpay_restricted_key, "rk_test_newkey")
        self.assertEqual(self.provider.xpay_publishable_key, "pk_test_newkey")
        self.assertEqual(self.provider.state, "test")
        self.assertEqual(self._snapshot()["setup_incomplete_step"], connect_errors.STEP_ACCOUNT)
        self.assertTrue(self.provider.xpay_setup_incomplete)
        # The consent is recorded as completed even though provisioning
        # stopped at the very next step.
        self.assertTrue(self._params().get_param(connect_service.PARAM_CLIENT_COMPLETED_AT))
        # Only the token exchange and the failed account fetch happened.
        self.assertEqual(len(transport.calls), 2)

    def test_complete_cross_plane_webhook_failure_restores_the_previous_key(self):
        """A cross-plane exchange (test -> live, or back) leaves the other
        plane's key valid at the platform, so a later failure can still
        fall back to it exactly as before."""
        transport = self._use_transport()
        connected_at_before = self._snapshot()["connected_at"]
        transport.queue(200, self.make_token_response(plane="live", merchant_id="acct_live_1"))
        transport.queue(200, dict(self.xpay_account, id="acct_live_1", livemode=True))
        transport.queue(400, {"error": {"code": "invalid_request", "message": "nope"}})

        # A plain try/except: see the same-plane test above for why
        # `self.assertRaises` as a context manager cannot be used to
        # observe state a restore-on-failure path is meant to leave behind.
        try:
            connect_service.complete(self.provider, "live", "auth_code_1", "verifier_1", "client_1")
            self.fail("expected a UserError")
        except UserError:
            pass

        self.provider.invalidate_recordset()
        self.assertEqual(self.provider.xpay_restricted_key, "rk_test_abcdef123456")
        self.assertEqual(self.provider.state, "test")
        self.assertEqual(self._snapshot()["connected_at"], connected_at_before)
        self.assertFalse(self._snapshot().get("setup_incomplete_step"))
        self.assertFalse(self.provider.xpay_setup_incomplete)

    def test_complete_setup_finishes_a_same_plane_reconnect_and_clears_the_flag(self):
        transport = self._use_transport()
        self.provider._xpay_snapshot_update({"webhook_endpoint_id": "we_old"})
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)
        transport.queue(400, {"error": {"code": "invalid_request", "message": "nope"}})
        # A plain try/except: see test_complete_same_plane_webhook_failure_*
        # for why `self.assertRaises` as a context manager would roll back
        # the precondition this test needs to carry into its second half.
        try:
            connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")
            self.fail("expected a UserError")
        except UserError:
            pass
        self.provider.invalidate_recordset()
        self.assertTrue(self._snapshot()["setup_incomplete_step"])

        transport.queue(200, self.xpay_account)
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})

        connect_service.complete_setup(self.provider)

        self.provider.invalidate_recordset()
        self.assertFalse(self._snapshot().get("setup_incomplete_step"))
        self.assertFalse(self.provider.xpay_setup_incomplete)
        self.assertEqual(self._snapshot()["webhook_endpoint_id"], "we_new")
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_new")

    def test_complete_setup_is_a_no_op_when_nothing_is_incomplete(self):
        transport = self._use_transport()
        connect_service.complete_setup(self.provider)
        self.assertEqual(transport.calls, [])

    def test_lock_provider_busy_raises(self):
        # Real cross-connection contention needs a row genuinely committed
        # and a genuinely independent connection on each side of the race.
        # `self.provider` will not do: it was written (state, keys) by
        # `_prepare_provider` on `self.env.cr` in setUpClass, and Postgres
        # holds that write's row lock for the rest of `self.env.cr`'s own
        # transaction — a second connection's plain `FOR UPDATE` on that
        # same row would block forever waiting for a transaction that never
        # commits mid-suite, not exercise NOWAIT contention. A fresh row,
        # created and committed through its own connection, carries no such
        # lock. Mirrors order_sync's own test_lock_busy_raises.
        with self.registry.cursor() as cr1:
            env1 = api.Environment(cr1, api.SUPERUSER_ID, {})
            fixture_provider = env1["payment.provider"].create({"name": "Lock test provider"})
            provider_id = fixture_provider.id
        # cr1 committed the fixture on clean `with` exit.

        cr2 = self.registry.cursor()
        try:
            cr2.execute("SELECT id FROM payment_provider WHERE id = %s FOR UPDATE", [provider_id])

            with self.registry.cursor() as cr3:
                env3 = api.Environment(cr3, api.SUPERUSER_ID, {})
                victim = env3["payment.provider"].browse(provider_id)
                with self.assertRaises(UserError) as catcher:
                    connect_service._lock_provider(victim)
                self.assertIn("already in progress", str(catcher.exception))
        finally:
            cr2.rollback()
            cr2.close()
            # The fixture provider was committed through cr1, its own
            # connection, independently of this test's transaction — nothing
            # rolls it back on its own, so it must be deleted explicitly or
            # it leaks into the shared database on every run.
            with self.registry.cursor() as cr4:
                cr4.execute("DELETE FROM payment_provider WHERE id = %s", [provider_id])

    def test_checkout_is_refused_while_setup_is_incomplete(self):
        self.provider._xpay_snapshot_update({"setup_incomplete_step": connect_errors.STEP_WEBHOOK})
        tx = self._create_transaction("direct")

        with self.assertRaises(ValidationError):
            checkout_service.session_for(tx)

    def test_complete_rejects_a_mode_mismatched_token_response(self):
        transport = self._use_transport()
        transport.queue(200, self.make_token_response(plane="live"))  # asked for 'test'

        with self.assertRaises(UserError):
            connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")

    def test_complete_succeeds_when_all_required_permissions_are_granted(self):
        transport = self._use_transport()
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, self.xpay_account)  # apiKey.permissions has all three
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_new", secret="whsec_new"))
        transport.queue(200, {"data": []})

        connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")

        self.assertTrue(self._snapshot()["connected_at"])

    def test_complete_same_plane_permission_missing_keeps_the_new_key_and_flags_incomplete(self):
        transport = self._use_transport()
        self.provider.write({"xpay_restricted_key": "rk_test_old"})
        account = dict(self.xpay_account, apiKey={"permissions": ["CHECKOUT_SESSIONS_WRITE"]})
        transport.queue(200, self.make_token_response(plane="test"))
        transport.queue(200, account)

        # A plain try/except: see test_complete_same_plane_webhook_failure_*
        # for why `self.assertRaises` as a context manager would roll back
        # the new-key write this test asserts survives the failure.
        try:
            connect_service.complete(self.provider, "test", "auth_code_1", "verifier_1", "client_1")
            self.fail("expected a UserError")
        except UserError as exc:
            message = str(exc)

        self.assertIn("REFUNDS_WRITE", message)
        self.assertIn("WEBHOOK_ENDPOINTS_WRITE", message)
        self.provider.invalidate_recordset()
        self.assertEqual(self.provider.xpay_restricted_key, "rk_test_newkey")
        self.assertEqual(self._snapshot()["setup_incomplete_step"], connect_errors.STEP_PERMISSIONS)
        # Nothing beyond the account fetch happened: no webhook endpoint call.
        self.assertEqual(len(transport.calls), 2)

    # -- Go live: one credential set, state follows the key's plane ---------

    def test_go_live_replaces_the_test_connection_and_enables_the_provider(self):
        transport = self._use_transport()
        self.provider._xpay_snapshot_update({"webhook_endpoint_id": "we_test_old"})
        live_account = dict(self.xpay_account, id="acct_live_1", livemode=True)
        transport.queue(200, self.make_token_response(plane="live", merchant_id="acct_live_1"))
        transport.queue(200, live_account)
        transport.queue(200, self.make_webhook_endpoint(endpoint_id="we_live", secret="whsec_live"))
        transport.queue(200, {"data": []})
        transport.queue(200, {})  # the old TEST endpoint is decommissioned with the old key

        connect_service.complete(self.provider, "live", "auth_code_1", "verifier_1", "client_1")

        self.assertEqual(transport.calls[4]["method"], "DELETE")
        self.assertIn("we_test_old", transport.calls[4]["url"])
        # The delete used the previous (test) key, the create the new (live) one.
        self.assertIn("rk_test_abcdef123456", transport.calls[4]["headers"]["Authorization"])
        self.assertIn("rk_live_newkey", transport.calls[2]["headers"]["Authorization"])

        self.provider.invalidate_recordset()
        self.assertEqual(self.provider._xpay_plane(), "live")
        self.assertEqual(self.provider.xpay_restricted_key, "rk_live_newkey")
        self.assertEqual(self.provider.xpay_webhook_secret, "whsec_live")
        self.assertEqual(self.provider.state, "enabled")
        self.assertTrue(self.provider.is_published)
        self.assertEqual(self._snapshot()["merchant_id"], "acct_live_1")
        self.assertEqual(self._snapshot()["webhook_endpoint_id"], "we_live")
        self.assertIn("live account", self.provider.xpay_connection_status)

    def test_go_live_is_refused_until_live_payments_are_activated(self):
        transport = self._use_transport()
        not_activated = dict(self.xpay_account, livemode=True, livePaymentsEnabled=False)
        transport.queue(200, self.make_token_response(plane="live"))
        transport.queue(200, not_activated)

        # A plain try/except, not `self.assertRaises` as a context manager:
        # see test_complete_same_plane_webhook_failure_* for why the
        # context-manager form would roll back the very "nothing changed"
        # state this test checks.
        try:
            connect_service.complete(self.provider, "live", "auth_code_1", "verifier_1", "client_1")
            self.fail("expected a UserError")
        except UserError as exc:
            self.assertIn("not activated", str(exc))

        self.provider.invalidate_recordset()
        self.assertEqual(self.provider.xpay_restricted_key, "rk_test_abcdef123456")
        self.assertEqual(self.provider.state, "test")
        self.assertEqual(len(transport.calls), 2)  # token + account, nothing else

    def test_connect_action_needs_a_browser_request(self):
        # No HTTP request in a unit test: the action must say so instead of
        # crashing on a missing session.
        with self.assertRaises(UserError):
            self.provider.action_xpay_connect()

    def test_base_url_is_the_configured_address_with_no_trailing_slash(self):
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://shop.example/")
        self.assertEqual(self.provider._xpay_base_url(), "https://shop.example")
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "http://shop.example")
        with self.assertRaises(UserError):
            self.provider._xpay_base_url()

    def test_disconnect_removes_the_endpoint_clears_the_credentials_and_disables(self):
        transport = self._use_transport()
        self.provider._xpay_snapshot_update({"webhook_endpoint_id": "we_1"})
        transport.queue(200, {})  # DELETE

        connect_service.disconnect(self.provider)

        self.assertEqual(transport.calls[0]["method"], "DELETE")
        self.assertIn("we_1", transport.calls[0]["url"])
        self.provider.invalidate_recordset()
        self.assertFalse(self.provider.xpay_restricted_key)
        self.assertFalse(self.provider.xpay_publishable_key)
        self.assertFalse(self.provider.xpay_webhook_secret)
        self.assertFalse(self.provider.xpay_account_json)
        self.assertEqual(self.provider.state, "disabled")
        self.assertFalse(self.provider.xpay_is_connected)

    # -- describe_authorization_error -----------------------------------

    def test_describe_authorization_error_uses_the_platform_description_for_access_denied(self):
        message = connect_errors.describe_authorization_error(
            "access_denied", "You declined the request."
        )
        self.assertIn("You declined the request.", message)

    def test_describe_authorization_error_falls_back_when_no_description_is_given(self):
        message = connect_errors.describe_authorization_error("access_denied")
        self.assertIn("canceled", message)

    def test_describe_authorization_error_is_generic_for_every_other_code(self):
        # invalid_scope, select_business, state_mismatch: none of these
        # describe something the merchant caused, so they share one
        # sentence and keep their detail in the log (unlike access_denied).
        for code in ("invalid_scope", "select_business", "state_mismatch"):
            message = connect_errors.describe_authorization_error(code)
            self.assertIn("could not be completed", message)
