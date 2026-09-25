import json
from datetime import timedelta
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request
from odoo.tools.misc import format_datetime

from ..services import connect_errors, connect_service, webhook_configurator
from ..services.logging import get_logger, log
from ..xpay import hosts, methods, money
from ..xpay.api_client import RequestsTransport, XPayApiClient
from ..xpay.errors import XPayApiError

_logger = get_logger(__name__)

_PLANES = ("test", "live")
_ACCOUNT_CACHE_MAX_AGE = timedelta(hours=2)
_WEBHOOK_RECONFIGURE_COOLDOWN = timedelta(seconds=60)


class PaymentProvider(models.Model):
    """XPay keeps the shape of Odoo's own providers: one set of credentials,
    and `state` says which XPay plane they belong to (Test Mode for a test
    account, Enabled for a live one). Connect fills the credentials; the
    key prefix (`rk_test_` / `rk_live_`) is the plane authority, and a
    constraint refuses a `state` that disagrees with it, the way
    payment_stripe refuses Test Mode on a connected account.

    Four stored fields, like Stripe's three plus one: the two keys, the
    webhook secret, and `xpay_account_json`, a flat snapshot of everything
    Connect learned (merchant, the `GET /account` body, the webhook
    endpoint and its health). Everything the form shows is computed from
    it and never stored. The OAuth client registration is per site, not
    per provider, and lives in `ir.config_parameter`.
    """

    _inherit = "payment.provider"

    code = fields.Selection(selection_add=[("xpay", "XPay")], ondelete={"xpay": "set default"})

    xpay_publishable_key = fields.Char(groups="base.group_system", copy=False)
    xpay_restricted_key = fields.Char(groups="base.group_system", copy=False)
    xpay_webhook_secret = fields.Char(groups="base.group_system", copy=False)
    xpay_account_json = fields.Json(groups="base.group_system", copy=False)

    xpay_is_connected = fields.Boolean(compute="_compute_xpay_status")
    xpay_plane = fields.Selection(
        [("test", "Test"), ("live", "Live")], compute="_compute_xpay_status"
    )
    xpay_connection_status = fields.Char(compute="_compute_xpay_status")
    xpay_webhook_status = fields.Char(compute="_compute_xpay_status")
    # A same-plane reconnect that persisted its new key but stopped short
    # of a finished setup.
    xpay_setup_incomplete = fields.Boolean(compute="_compute_xpay_status")
    # Merchant-facing phrase for the step it stopped at, for the banner.
    xpay_setup_incomplete_step_label = fields.Char(compute="_compute_xpay_status")
    # The last Connect failure's merchant-safe sentence, stored on the
    # provider so it survives a page reload; cleared by the next
    # successful Connect.
    xpay_connect_error = fields.Char(compute="_compute_xpay_status")

    # === Snapshot ==========================================================
    #
    # Flat keys, merged one level deep, so two writers never clobber each
    # other: merchant_id, connected_at, account, fetched_at,
    # webhook_endpoint_id, webhook_op_id, webhook_last_success_at,
    # webhook_last_failure_at, webhook_last_failure_reason,
    # webhook_reconfigured_at. Datetimes are stored as Odoo's string form.

    def _xpay_snapshot(self):
        self.ensure_one()
        data = self.xpay_account_json
        return data if isinstance(data, dict) else {}

    def _xpay_snapshot_update(self, values):
        """Merge `values` into the snapshot atomically (jsonb `||`), so a
        webhook delivery stamping its health and an account refresh
        writing the new body cannot overwrite each other's keys. Flushes
        the whole record first, not just the snapshot field: the caller
        may have written other fields (the restricted key, the state)
        moments earlier, and the `invalidate_recordset()` below would
        otherwise discard that write before it ever reached the database."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "UPDATE payment_provider"
            " SET xpay_account_json = COALESCE(xpay_account_json, '{}'::jsonb) || %s::jsonb"
            " WHERE id = %s",
            (json.dumps(values), self.id),
        )
        # Raw SQL bypasses the ORM's dependency tracking: drop every cached
        # value of this record, the computed status fields included.
        self.invalidate_recordset()

    def _xpay_snapshot_datetime(self, key):
        value = self._xpay_snapshot().get(key)
        return fields.Datetime.to_datetime(value) if isinstance(value, str) and value else None

    # === Plane and credentials ===========================================

    def _xpay_plane(self):
        """'test' or 'live' from the stored key's prefix, `None` when not
        connected. The key itself is the plane authority."""
        self.ensure_one()
        key = self.xpay_restricted_key or ""
        for plane in _PLANES:
            if key.startswith((f"rk_{plane}_", f"sk_{plane}_")):
                return plane
        return None

    @api.depends("xpay_restricted_key", "xpay_account_json")
    def _compute_xpay_status(self):
        for provider in self:
            plane = provider._xpay_plane() if provider.code == "xpay" else None
            provider.xpay_is_connected = bool(plane)
            provider.xpay_plane = plane or False
            # Computed for every record, connected or not: a first failed
            # Connect has no plane yet, and the alert must still render.
            is_xpay = provider.code == "xpay"
            snapshot = provider._xpay_snapshot() if is_xpay else {}
            incomplete_step = snapshot.get("setup_incomplete_step")
            provider.xpay_setup_incomplete = bool(incomplete_step)
            provider.xpay_setup_incomplete_step_label = (
                connect_errors.describe_incomplete_step(incomplete_step)
                if incomplete_step
                else False
            )
            provider.xpay_connect_error = snapshot.get("connect_error_message") or False
            if not plane:
                provider.xpay_connection_status = _("Not connected")
                provider.xpay_webhook_status = False
                continue
            provider.xpay_connection_status = provider._xpay_connection_status_text(plane)
            provider.xpay_webhook_status = provider._xpay_webhook_status_text()

    def _xpay_connection_status_text(self, plane):
        snapshot = self._xpay_snapshot()
        account = snapshot.get("account") or {}
        merchant = (
            account.get("displayName") or snapshot.get("merchant_id") or _("your XPay account")
        )
        plane_label = _("test account") if plane == "test" else _("live account")
        connected_at = self._xpay_snapshot_datetime("connected_at")
        if connected_at:
            return _(
                "Connected to %(merchant)s (%(plane)s) since %(date)s",
                merchant=merchant,
                plane=plane_label,
                date=format_datetime(self.env, connected_at, dt_format="medium"),
            )
        return _("Connected to %(merchant)s (%(plane)s)", merchant=merchant, plane=plane_label)

    def _xpay_webhook_status_text(self):
        snapshot = self._xpay_snapshot()
        if not snapshot.get("webhook_endpoint_id"):
            return _("Not set up. Use Reconfigure webhook.")
        success = self._xpay_snapshot_datetime("webhook_last_success_at")
        failure = self._xpay_snapshot_datetime("webhook_last_failure_at")
        if failure and (not success or failure > success):
            return _(
                "Failing since %(when)s: %(reason)s",
                when=format_datetime(self.env, failure, dt_format="medium"),
                reason=snapshot.get("webhook_last_failure_reason") or _("unknown"),
            )
        if success:
            return _(
                "Healthy. Last event received %(when)s",
                when=format_datetime(self.env, success, dt_format="medium"),
            )
        return _("Set up. Waiting for the first event.")

    @api.constrains("state", "xpay_restricted_key")
    def _check_xpay_state_matches_connection(self):
        for provider in self.filtered(lambda p: p.code == "xpay" and p.state != "disabled"):
            plane = provider._xpay_plane()
            if not plane:
                raise ValidationError(_("Connect an XPay account before switching XPay on."))
            if provider.state == "enabled" and plane != "live":
                raise ValidationError(
                    _(
                        "XPay is connected to a test account. Use Go live to connect your live"
                        " account before enabling it."
                    )
                )
            if provider.state == "test" and plane != "test":
                raise ValidationError(
                    _(
                        "XPay is connected to a live account. Set it to Enabled, or switch to a"
                        " test account."
                    )
                )

    def _xpay_transport(self):
        """The single seam tests patch to fake the XPay API."""
        return RequestsTransport()

    def _xpay_api_base(self):
        """The merchant API's base URL: an `ir.config_parameter` override
        (server-side only) when set, else the platform default."""
        return (
            self.env["ir.config_parameter"].sudo().get_param("xpay_payment.api_base")
            or hosts.API_BASE
        )

    def _xpay_client(self):
        self.ensure_one()
        key = self.xpay_restricted_key
        if not key:
            raise UserError(
                _(
                    "XPay is not connected. Connect your XPay account first."
                    " (code: gateway_not_configured)"
                )
            )
        return XPayApiClient(
            key,
            self._xpay_transport(),
            api_base=self._xpay_api_base(),
            user_agent=f"xpay-odoo/{self._xpay_module_version()} ({self.get_base_url()})",
        )

    def _xpay_setup_incomplete_step(self):
        """The provisioning step a same-plane reconnect stopped at, or
        `None` when setup is finished (or nothing is connected)."""
        self.ensure_one()
        return self._xpay_snapshot().get("setup_incomplete_step") or None

    def _xpay_require_setup_complete(self):
        """Refuse a money-moving call while a same-plane reconnect's setup
        is unfinished. Checked only at each caller's own money boundary
        (session creation, the return route, refund submission) — never
        inside `_xpay_client()` itself, which the passive account refresh
        and Disconnect must keep using freely against the new key."""
        self.ensure_one()
        if self._xpay_setup_incomplete_step():
            raise ValidationError(
                _(
                    "XPay setup is incomplete. Complete it from the payment provider before"
                    " taking payments."
                )
            )

    def _xpay_module_version(self):
        """The installed `xpay_payment` version, falling back to the
        manifest when the module row has none yet."""
        module = (
            self.env["ir.module.module"].sudo().search([("name", "=", "xpay_payment")], limit=1)
        )
        if module and module.installed_version:
            return module.installed_version
        from odoo.modules.module import get_manifest

        return get_manifest("xpay_payment").get("version") or "0.0.0"

    def _xpay_account(self, max_age=_ACCOUNT_CACHE_MAX_AGE):
        """The cached `GET /account` body, refreshed best-effort first when
        the cache is older than `max_age` — including when the cache
        currently hides everything, which is the exact state a refresh
        must be able to heal out of."""
        self.ensure_one()
        if self.xpay_restricted_key:
            fetched_at = self._xpay_snapshot_datetime("fetched_at")
            if not fetched_at or fields.Datetime.now() - fetched_at > max_age:
                self._xpay_refresh_account()
        account = self._xpay_snapshot().get("account")
        return account if isinstance(account, dict) else {}

    def _xpay_refresh_account(self):
        """Force a `GET /account` refresh (a short, shopper-facing-style
        timeout). The check time is stamped BEFORE the call so a down API
        costs one bounded-timeout request per cache window, never one per
        shopper-facing render. On failure the stale
        cached copy is kept and the failure is only logged — a passive
        refresh must never block a shopper or a page render."""
        self.ensure_one()
        self._xpay_snapshot_update({"fetched_at": fields.Datetime.to_string(fields.Datetime.now())})
        try:
            account = self._xpay_client().get_account(shopper_facing=True)
        except XPayApiError as exc:
            log(_logger, "error", "account.refresh_failed", plane=self._xpay_plane(), code=exc.code)
            return
        self._xpay_snapshot_update({"account": account})

    def _get_supported_currencies(self):
        self.ensure_one()
        if self.code != "xpay":
            return super()._get_supported_currencies()
        account = self._xpay_account()
        codes = {
            entry["code"].upper()
            for entry in account.get("supportedCurrencies", []) or []
            if isinstance(entry, dict) and isinstance(entry.get("code"), str)
        }
        if not codes:
            return self.env["res.currency"]
        return (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("name", "in", sorted(codes))])
        )

    def _get_default_payment_method_codes(self):
        self.ensure_one()
        if self.code != "xpay":
            return super()._get_default_payment_method_codes()
        return self._xpay_method_codes_from_account(self._xpay_account())

    @staticmethod
    def _xpay_method_codes_from_account(account):
        wire_types = set()
        for entry in account.get("supportedCurrencies", []) or []:
            if isinstance(entry, dict):
                wire_types.update(
                    t for t in entry.get("paymentMethodTypes", []) or [] if isinstance(t, str)
                )
        return set(methods.odoo_codes_for(wire_types))

    def _compute_feature_support_fields(self):
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == "xpay").update({"support_refund": "partial"})

    def _xpay_enabled_wire_types(self, currency):
        """The wire payment-method strings the connected account enables
        for `currency`, read from the cached `GET /account` the same way
        `_get_default_payment_method_codes` does, restricted to one
        currency. What a merchant can actually charge with is decided
        here, never assumed from the wire vocabulary table alone."""
        self.ensure_one()
        currency_code = currency.name.upper()
        for entry in self._xpay_account().get("supportedCurrencies", []) or []:
            if isinstance(entry, dict) and entry.get("code") == currency_code:
                return [t for t in entry.get("paymentMethodTypes", []) or [] if isinstance(t, str)]
        return []

    def _xpay_inline_form_values(self, amount, currency, payment_method_code, locale):
        """JSON string the JS reads from the inline form container's data
        attribute. Odoo's float `amount` is converted exactly once here,
        through `currency.round` then the money module's strict parser."""
        self.ensure_one()

        rounded = currency.round(amount)
        minor_amount = money.to_minor(f"{rounded:.{currency.decimal_places}f}", currency.name)

        currency_code = currency.name.upper()
        enabled_types = self._xpay_enabled_wire_types(currency)
        wire_types = [t for t in methods.wire_types_for(payment_method_code) if t in enabled_types]

        lang = locale or ""
        return json.dumps(
            {
                "publishable_key": self.xpay_publishable_key or "",
                "mode": "payment",
                "minor_amount": minor_amount,
                "currency": currency_code,
                "payment_method_types": wire_types,
                "locale": "ar" if lang.startswith("ar") else "en",
            }
        )

    def _xpay_base_url(self):
        """The site's configured base URL, refused when it is not https —
        the platform rejects http webhook and redirect URIs outright.

        Deliberately not `get_base_url()`: with website_payment installed
        that prefers the current request's own root, so the webhook URL
        and the OAuth redirect URI registered at XPay would depend on
        which hostname (or scheme) the administrator happened to use to
        reach the admin. The website's domain when the provider is bound
        to one, else `web.base.url`, is the address the store is known by."""
        self.ensure_one()
        website = self.website_id if "website_id" in self._fields else None
        base = (
            website.get_base_url()
            if website
            else self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        if urlsplit(base).scheme != "https":
            raise UserError(
                _(
                    "XPay requires the site's base URL to be https to build webhook and"
                    " redirect links."
                )
            )
        return base

    # === Connect actions ==================================================

    def _xpay_connect_action(self, plane):
        """Start the OAuth handshake from the button itself, the way
        payment_stripe's onboarding does: register the client, mint state
        and PKCE verifier, park them in the session, and send the browser
        to XPay. A failure raises, so Odoo shows it in its error dialog
        instead of bouncing silently back to the form."""
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators can connect XPay."))
        if not request:
            raise UserError(_("Connect XPay from the provider form in your browser."))
        result = connect_service.start(self, plane, self.env.user)
        request.session[connect_service.SESSION_KEY] = {
            "provider_id": self.id,
            "plane": plane,
            "state": result["state"],
            "verifier": result["verifier"],
            "client_id": result["client_id"],
        }
        return {"type": "ir.actions.act_url", "url": result["url"], "target": "self"}

    def action_xpay_connect(self):
        """Connect a test account: the first connection, or the way back
        from a live one."""
        return self._xpay_connect_action("test")

    def action_xpay_go_live(self):
        """Connect the live account. On success it replaces the test keys
        and the provider becomes Enabled."""
        return self._xpay_connect_action("live")

    def action_xpay_disconnect(self):
        self.ensure_one()
        connect_service.disconnect(self)
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def action_xpay_complete_setup(self):
        """Finish a same-plane reconnect that persisted its new key but
        stopped short of a finished setup (account, permissions, or the
        webhook)."""
        self.ensure_one()
        connect_service.complete_setup(self)
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    # === Admin maintenance actions ========================================

    def action_xpay_refresh_account(self):
        """Force the account cache refresh and re-derive the payment
        methods and the currencies from the freshly read account."""
        self.ensure_one()
        self._xpay_refresh_account()
        self._xpay_apply_account_capabilities()
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def _xpay_apply_account_capabilities(self):
        """Re-derive what Odoo gates on from the cached account: the
        provider's payment methods, and its currency list.

        Odoo computes `available_currency_ids` (stored, merchant-editable)
        only when `code` changes, i.e. at install, before Connect has read
        the account: `_get_supported_currencies()` is empty then and Odoo
        stores "no restriction", which would offer XPay for an order in
        any currency. So the compute is re-run here, on Connect and on the
        merchant's explicit Refresh account, never on the passive cache
        refresh, so a narrower list the merchant typed stays theirs."""
        self.ensure_one()
        codes = sorted(self._xpay_method_codes_from_account(self._xpay_account()))
        # `active_test=False`: the account can list a method this module or
        # Odoo ships archived (XPay's own `instapay`, Odoo's own
        # `bank_transfer`), and the default active filter would otherwise
        # neither find it here nor let `_activate_default_pms` below see it
        # as linked.
        method_ids = (
            self.env["payment.method"]
            .with_context(active_test=False)
            .search([("code", "in", codes)])
        )
        self.payment_method_ids = [(6, 0, method_ids.ids)]
        # Odoo itself un-archives a provider's methods only inside `write()`,
        # on a disabled -> enabled/test transition (`_activate_default_pms`).
        # Refresh account and Connect change the method list without going
        # through that transition, so a listed-but-archived method would
        # otherwise stay archived and unusable; call the same native
        # mechanism directly, scoped to what the account actually lists.
        self._activate_default_pms()
        self._compute_available_currency_ids()

    def action_xpay_reconfigure_webhook(self):
        """Create the replacement endpoint before decommissioning the
        current one (mirrors `connect_service._provision`'s order), so a
        failed create leaves the live endpoint in place instead of a dead
        endpoint id and secret being persisted over it. `ensure_endpoint`
        sees the still-live `webhook_endpoint_id` and mints a fresh op id,
        so the create is never a replay of the endpoint about to be
        decommissioned. Rate-limited to once per 60 s: the cooldown stamp
        is set BEFORE any work runs, so a second click inside the window
        is refused whatever the first attempt's outcome. An overlapping run
        is refused by the provider's own row lock."""
        self.ensure_one()
        connect_service._lock_provider(self)
        last = self._xpay_snapshot_datetime("webhook_reconfigured_at")
        now = fields.Datetime.now()
        if last and now - last < _WEBHOOK_RECONFIGURE_COOLDOWN:
            retry_at = last + _WEBHOOK_RECONFIGURE_COOLDOWN
            raise UserError(
                _(
                    "Webhook reconfiguration already ran less than a minute ago. Try again"
                    " after %(retry_at)s.",
                    retry_at=retry_at,
                )
            )
        self._xpay_snapshot_update({"webhook_reconfigured_at": fields.Datetime.to_string(now)})

        client = self._xpay_client()
        old_endpoint_id = self._xpay_snapshot().get("webhook_endpoint_id")
        webhook_configurator.ensure_endpoint(self, client=client)
        if old_endpoint_id:
            webhook_configurator.decommission(self, old_endpoint_id, client)
        webhook_configurator.reconcile_enabled_events(self, client=client)
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    # === Uninstall =========================================================

    def _get_removal_values(self):
        values = super()._get_removal_values()
        values.update(
            {
                "xpay_publishable_key": False,
                "xpay_restricted_key": False,
                "xpay_webhook_secret": False,
                "xpay_account_json": False,
            }
        )
        return values
