"""OAuth 2.1 + PKCE handshake orchestration and provisioning for XPay
Connect.
"""

import psycopg2
from odoo import _, fields
from odoo.exceptions import UserError

from ..xpay import connect, hosts
from ..xpay.api_client import XPayApiClient
from ..xpay.errors import XPayApiError, XPayTransportError
from . import connect_errors, webhook_configurator
from .logging import get_logger, log

_logger = get_logger(__name__)

_TIMEOUT = 30.0

# The OAuth client registration is per site (it names the site's redirect
# URI), not per provider, so it lives beside `web.base.url`.
PARAM_CLIENT_ID = "xpay_payment.connect_client_id"
# Where the browser-facing handshake parks state + verifier between the
# button that starts it and the callback that finishes it.
SESSION_KEY = "xpay_connect_flow"
PARAM_CLIENT_REGISTERED_AT = "xpay_payment.connect_client_registered_at"
# The redirect URI the client was registered with, so a moved host is
# detected instead of authorizing against a callback XPay would refuse.
PARAM_CLIENT_REDIRECT_URI = "xpay_payment.connect_client_redirect_uri"
# When the stored client last completed a consent; unset for a client
# that was registered but never finished authorizing.
PARAM_CLIENT_COMPLETED_AT = "xpay_payment.connect_client_completed_at"

# The three permissions this module cannot operate without: no proof, no
# gateway. `apiKey.permissions` is the key's OWN granted set (WRITE implies
# READ) — the platform names nothing as missing, so this module diffs
# against its required set and names each gap to the merchant.
_REQUIRED_PERMISSIONS = ("CHECKOUT_SESSIONS_WRITE", "REFUNDS_WRITE", "WEBHOOK_ENDPOINTS_WRITE")


def start(provider, plane, user=None):
    """Register the public client if needed, mint state + a PKCE verifier,
    and return `{url, state, verifier}` for the controller to keep in
    `request.session`."""
    provider.ensure_one()
    client_id = _ensure_client(provider)

    state = connect.generate_state()
    verifier = connect.generate_verifier()
    url = connect.authorize_url(
        hosts.oauth_base(provider._xpay_api_base()),
        client_id=client_id,
        redirect_uri=_redirect_uri(provider),
        scope=connect.scope_for_plane(plane),
        state=state,
        code_challenge=connect.challenge(verifier),
    )

    log(_logger, "info", "connect.begin", plane=plane, user_id=user.id if user else None)
    return {"url": url, "state": state, "verifier": verifier, "client_id": client_id}


def complete(provider, plane, code, verifier, client_id):
    """Exchange the authorization code and provision the connection.
    Raises `UserError` and changes nothing on any failure.

    `client_id` is the one the flow was started with (carried by the
    caller's session flow, alongside `state` and `verifier`), never read
    back from `ir.config_parameter`: a click that started under one
    client id must exchange under that same one, even when a later
    registration has since replaced the stored client id."""
    provider.ensure_one()
    transport = provider._xpay_transport()
    form = connect.token_request_form(
        code=code,
        redirect_uri=_redirect_uri(provider),
        client_id=client_id,
        verifier=verifier,
    )
    url = f"{hosts.oauth_base(provider._xpay_api_base())}/oauth2/token"
    try:
        response = transport.request(
            "POST",
            url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            form=form,
            timeout=_TIMEOUT,
        )
    except XPayTransportError as exc:
        # A timeout is ambiguous: the platform may have issued the key even
        # though the answer never arrived, so the message is the same as for
        # an unclear status, not "try again".
        log(_logger, "critical", "connect.exchange_unreachable", plane=plane)
        raise _ambiguous_exchange_error() from exc

    if not (200 <= response.status < 300):
        classification = connect.classify_exchange_failure(response.status)
        log(
            _logger,
            "critical",
            "connect.exchange_failed",
            plane=plane,
            status=response.status,
            classification=classification,
        )
        if classification == "no_commit":
            raise UserError(_("XPay could not complete the connection. Please try again."))
        raise _ambiguous_exchange_error()

    try:
        result = connect.parse_token_response(response.json(), plane)
    except ValueError as exc:
        log(_logger, "error", "connect.response_invalid", plane=plane)
        raise UserError(
            _("XPay's connection response could not be validated. Nothing was changed.")
        ) from exc

    # The consent completed here, whatever provisioning below does with
    # the delivered keys next — recorded before provisioning runs so a
    # later provisioning failure still leaves the client marked completed.
    # Only the client id that actually completed is marked: a flow that
    # outlived a later registration completed under the OLD client id,
    # and the new one has not proved itself yet.
    params = _params(provider)
    if params.get_param(PARAM_CLIENT_ID) == client_id:
        params.set_param(
            PARAM_CLIENT_COMPLETED_AT, fields.Datetime.to_string(fields.Datetime.now())
        )
    _provision(provider, plane, result)
    return result


def _ambiguous_exchange_error():
    return UserError(
        _(
            "XPay's response to the connection request was unclear, so nothing was changed."
            " If this store does not show as connected, revoke the connection in your XPay"
            " dashboard and connect again."
        )
    )


def disconnect(provider):
    """Delete the webhook endpoint at XPay, clear every credential, and
    switch the provider off. One write, so the state constraint sees the
    cleared key and the disabled state together."""
    provider.ensure_one()
    endpoint_id = provider._xpay_snapshot().get("webhook_endpoint_id")
    if endpoint_id and provider.xpay_restricted_key:
        webhook_configurator.decommission(provider, endpoint_id, provider._xpay_client())

    provider.write(
        {
            "xpay_publishable_key": False,
            "xpay_restricted_key": False,
            "xpay_webhook_secret": False,
            "xpay_account_json": False,
            "state": "disabled",
            "is_published": False,
        }
    )
    log(_logger, "info", "connect.disconnected")


def _lock_provider(provider):
    """Take a blocking-free row lock on `provider` for the whole
    provisioning sequence, so two overlapping completions (e.g. a stale
    tab replaying the same callback) cannot interleave their writes. Same
    shape as `order_sync.lock`, scoped to `payment_provider`; released when
    the request's own transaction ends, same as that lock."""
    provider.ensure_one()
    try:
        with provider.env.cr.savepoint():
            provider.env.cr.execute(
                "SELECT id FROM payment_provider WHERE id = %s FOR UPDATE NOWAIT", [provider.id]
            )
    except psycopg2.errors.LockNotAvailable as exc:
        raise UserError(
            _(
                "A connection is already in progress for this provider. Please wait for it to"
                " finish, then try again."
            )
        ) from exc
    provider.invalidate_recordset()


def _provision(provider, plane, result):
    """Validate the account, provision the new webhook endpoint with the
    new key, then decommission the old endpoint with the old key — with a
    same-plane/cross-plane split for the platform's own key lifecycle:

    A token exchange retires every OTHER active restricted key for the
    same (merchant, plane, client) atomically. On a SAME-plane exchange
    (reconnecting the plane already connected) the previous key is
    therefore already dead before this function runs — there is nothing
    left to "restore" on a later failure, so both new keys are persisted
    immediately and a later failure instead flags the connection as
    needing Complete setup. On a CROSS-plane exchange (Go live, switch to
    test) the other plane's key is untouched by this exchange and stays
    valid, so a later failure restores it exactly as before.

    Once past that split the order is the same either way: validate the
    account, provision the NEW webhook endpoint with the NEW key, then
    decommission the OLD endpoint with the OLD key."""
    _lock_provider(provider)
    new_client = XPayApiClient(
        result.restricted_key, provider._xpay_transport(), api_base=provider._xpay_api_base()
    )

    previous_plane = provider._xpay_plane()
    previous = {
        "xpay_restricted_key": provider.xpay_restricted_key,
        "xpay_publishable_key": provider.xpay_publishable_key,
        "state": provider.state,
    }
    previous_endpoint_id = provider._xpay_snapshot().get("webhook_endpoint_id")
    target_state = "enabled" if plane == "live" else "test"
    same_plane = previous_plane is not None and previous_plane == plane
    new_keys = {
        "xpay_restricted_key": result.restricted_key,
        "xpay_publishable_key": result.publishable_key,
        "state": target_state,
    }

    if same_plane:
        provider.write(new_keys)

    try:
        account = new_client.get_account()
    except XPayApiError as exc:
        log(_logger, "error", "connect.account_fetch_failed", plane=plane, code=exc.code)
        if same_plane:
            connect_errors.flag_setup_incomplete(provider, connect_errors.STEP_ACCOUNT)
            raise connect_errors.XPaySetupIncomplete(
                _(
                    "XPay reconnected, but your account could not be read, so setup is"
                    " incomplete. Use Complete setup on the payment provider to finish."
                )
            ) from exc
        raise UserError(
            _("Connected, but could not read your XPay account. Please try connecting again.")
        ) from exc

    try:
        _require_permissions(account, plane)
        _require_live_activation(account, plane)
    except UserError as exc:
        if same_plane:
            connect_errors.flag_setup_incomplete(provider, connect_errors.STEP_PERMISSIONS)
            raise connect_errors.XPaySetupIncomplete(str(exc)) from exc
        raise

    # The key and the state move together: the key is the plane authority
    # and the constraint refuses a state that disagrees with it. A
    # same-plane exchange already wrote this above; a cross-plane one
    # writes it only now that the account has been validated, so a
    # cross-plane failure above changed nothing.
    if not same_plane:
        provider.write(new_keys)

    try:
        webhook_configurator.ensure_endpoint(provider, client=new_client)
    except XPayApiError as exc:
        log(_logger, "error", "connect.webhook_failed", plane=plane, code=exc.code)
        if same_plane:
            connect_errors.flag_setup_incomplete(provider, connect_errors.STEP_WEBHOOK)
            raise connect_errors.XPaySetupIncomplete(
                _(
                    "XPay reconnected, but the webhook could not be set up, so setup is"
                    " incomplete. Use Complete setup on the payment provider to finish."
                )
            ) from exc
        provider.write(previous)
        raise UserError(
            _("Connected, but could not set up the webhook. Nothing was changed; please try again.")
        ) from exc

    previous_key = previous["xpay_restricted_key"]
    if previous_endpoint_id and previous_key:
        # A same-plane exchange already retired `previous_key` at the
        # platform atomically with issuing the new one, so a client built
        # from it cannot authenticate the delete; the new key reaches the
        # same account and endpoint. A cross-plane exchange leaves the
        # other plane's key valid, so decommissioning still uses it.
        decommission_client = (
            new_client
            if same_plane
            else XPayApiClient(
                previous_key, provider._xpay_transport(), api_base=provider._xpay_api_base()
            )
        )
        webhook_configurator.decommission(provider, previous_endpoint_id, decommission_client)

    _finish_provisioning(provider, plane, account, merchant_id=result.merchant_id)
    log(_logger, "info", "connect.completed", plane=plane)


def complete_setup(provider):
    """Re-run the provisioning steps a same-plane reconnect deferred after
    persisting its key (`_provision`'s `setup_incomplete_step`): account
    validation, permissions, live-activation when applicable, and the
    webhook. A no-op when nothing is flagged. A failure re-flags the step
    it stopped at and raises; success clears the flag."""
    provider.ensure_one()
    plane = provider._xpay_plane()
    if not plane:
        raise UserError(_("XPay is not connected."))
    if not provider._xpay_setup_incomplete_step():
        return
    _lock_provider(provider)

    client = provider._xpay_client()
    try:
        account = client.get_account()
    except XPayApiError as exc:
        log(_logger, "error", "connect.setup_account_failed", plane=plane, code=exc.code)
        connect_errors.flag_setup_incomplete(provider, connect_errors.STEP_ACCOUNT)
        raise connect_errors.XPaySetupIncomplete(
            _("Could not read your XPay account. Please try Complete setup again.")
        ) from exc

    try:
        _require_permissions(account, plane)
        _require_live_activation(account, plane)
    except UserError as exc:
        connect_errors.flag_setup_incomplete(provider, connect_errors.STEP_PERMISSIONS)
        raise connect_errors.XPaySetupIncomplete(str(exc)) from exc

    try:
        webhook_configurator.ensure_endpoint(provider, client=client)
    except XPayApiError as exc:
        log(_logger, "error", "connect.setup_webhook_failed", plane=plane, code=exc.code)
        connect_errors.flag_setup_incomplete(provider, connect_errors.STEP_WEBHOOK)
        raise connect_errors.XPaySetupIncomplete(
            _("Could not set up the webhook. Please try Complete setup again.")
        ) from exc

    _finish_provisioning(provider, plane, account)
    log(_logger, "info", "connect.setup_completed", plane=plane)


def _finish_provisioning(provider, plane, account, merchant_id=None):
    """The shared tail of a successful provisioning pass, whether it
    finished in the original exchange or via Complete setup: publish when
    live, snapshot the account, re-derive payment methods/currencies, and
    clear any Connect failure or incomplete-setup flag."""
    now = fields.Datetime.to_string(fields.Datetime.now())
    provider.write(
        {
            # Odoo's own rule: a live provider is published, a test one is
            # visible to administrators only until published by hand.
            "is_published": plane == "live",
        }
    )
    values = {
        "account": account,
        "fetched_at": now,
        "connected_at": now,
        "webhook_last_success_at": None,
        "webhook_last_failure_at": None,
        "webhook_last_failure_reason": None,
        "setup_incomplete_step": None,
        "connect_error_code": None,
        "connect_error_message": None,
        "connect_error_at": None,
    }
    if merchant_id is not None:
        values["merchant_id"] = merchant_id
    provider._xpay_snapshot_update(values)
    # Payment methods and currencies, from the account just read.
    provider._xpay_apply_account_capabilities()


def _require_permissions(account, plane):
    """Raise, changing nothing, when the connected key's own granted
    permissions (`apiKey.permissions`) are missing one this module cannot
    operate without."""
    permissions = set((account.get("apiKey") or {}).get("permissions") or [])
    missing = [name for name in _REQUIRED_PERMISSIONS if name not in permissions]
    if not missing:
        return
    log(_logger, "error", "connect.permission_missing", plane=plane, missing=missing)
    raise UserError(
        _(
            "Your XPay API key is missing the following permission(s): %(permissions)s. Grant"
            " them in your XPay dashboard, then connect again.",
            permissions=", ".join(missing),
        )
    )


def _require_live_activation(account, plane):
    """Raise, changing nothing, when a live Connect's account is not yet
    activated for live payments."""
    if plane == "live" and not account.get("livePaymentsEnabled"):
        log(_logger, "error", "connect.live_not_activated")
        raise UserError(
            _(
                "Your XPay account is not activated for live payments yet. Complete the"
                " activation in your XPay dashboard, then use Go live again. Nothing was changed."
            )
        )


def _params(provider):
    return provider.env["ir.config_parameter"].sudo()


def _ensure_client(provider):
    params = _params(provider)
    client_id = params.get_param(PARAM_CLIENT_ID)
    stored_uri = params.get_param(PARAM_CLIENT_REDIRECT_URI)
    registered_at = fields.Datetime.to_datetime(params.get_param(PARAM_CLIENT_REGISTERED_AT))
    completed_at = fields.Datetime.to_datetime(params.get_param(PARAM_CLIENT_COMPLETED_AT))
    current_uri = _redirect_uri(provider)
    if not connect.client_needs_registration(
        client_id, stored_uri, current_uri, registered_at, fields.Datetime.now(), completed_at
    ):
        return client_id
    return _register_client(provider)


def _register_client(provider):
    transport = provider._xpay_transport()
    redirect_uri = _redirect_uri(provider)
    body = connect.registration_body(
        provider.company_id.name or "Odoo",
        provider._xpay_base_url(),
        redirect_uri,
    )
    url = f"{hosts.oauth_base(provider._xpay_api_base())}/oauth2/register"
    try:
        response = transport.request(
            "POST",
            url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json_body=body,
            timeout=_TIMEOUT,
        )
    except XPayTransportError as exc:
        log(_logger, "error", "connect.registration_unreachable")
        raise UserError(
            _("XPay could not be reached to register this store for Connect. Please try again.")
        ) from exc
    payload = response.json() or {}
    if not (200 <= response.status < 300) or not isinstance(payload.get("client_id"), str):
        log(_logger, "error", "connect.registration_failed", status=response.status)
        raise UserError(_("XPay could not register this store for Connect. Please try again."))

    params = _params(provider)
    params.set_param(PARAM_CLIENT_ID, payload["client_id"])
    params.set_param(PARAM_CLIENT_REDIRECT_URI, redirect_uri)
    params.set_param(PARAM_CLIENT_REGISTERED_AT, fields.Datetime.to_string(fields.Datetime.now()))
    # A brand new client has not completed a consent yet, whatever the
    # client id it replaces had done.
    params.set_param(PARAM_CLIENT_COMPLETED_AT, False)
    return payload["client_id"]


def _redirect_uri(provider):
    return f"{provider._xpay_base_url()}/payment/xpay/connect/callback"
