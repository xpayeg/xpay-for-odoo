"""`/payment/xpay/connect/callback`: the OAuth 2.1 + PKCE handshake's
return leg. The start leg is the provider form's own button
(`action_xpay_connect` / `action_xpay_go_live`), so a failure to start
surfaces in Odoo's error dialog rather than as a silent redirect."""

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from ..services import connect_errors, connect_service
from ..services.logging import get_logger, log
from ..xpay import connect

_logger = get_logger(__name__)


class XPayConnectController(http.Controller):
    @http.route("/payment/xpay/connect/callback", type="http", auth="user", methods=["GET"])
    def xpay_connect_callback(self, **kwargs):
        if not request.env.user.has_group("base.group_system"):
            return request.make_response("Forbidden", status=403)

        flow = request.session.pop(connect_service.SESSION_KEY, None)
        if not isinstance(flow, dict) or not flow.get("client_id"):
            # Every flow this module starts now carries a client id
            # alongside state and verifier; one that lacks it is not a
            # flow this module produced, so it is refused the same way as
            # a missing one.
            return request.make_response(
                "This connection link is stale or was already used. Start again from the"
                " Connect button.",
                status=403,
            )

        provider = request.env["payment.provider"].browse(flow.get("provider_id")).exists()
        plane = flow.get("plane")
        if not provider:
            return request.make_response("Not found", status=404)

        # Record the refusal on the provider itself, not a URL query flag:
        # the redirect below drops any query string on the next navigation,
        # so nothing there survives to be read back.
        error = kwargs.get("error")
        if error:
            log(_logger, "error", "connect.refused", plane=plane, error=error)
            message = connect_errors.describe_authorization_error(
                error, kwargs.get("error_description")
            )
            connect_errors.record_error(provider, error, message)
            return request.redirect(_provider_form_url(provider.id))

        state = kwargs.get("state") or ""
        stored_state = flow.get("state") or ""
        if not connect.states_match(state, stored_state):
            log(_logger, "error", "connect.state_mismatch", plane=plane)
            connect_errors.record_error(
                provider,
                "state_mismatch",
                connect_errors.describe_authorization_error("state_mismatch"),
            )
            return request.redirect(_provider_form_url(provider.id))

        try:
            connect_service.complete(
                provider, plane, kwargs.get("code"), flow.get("verifier"), flow.get("client_id")
            )
        except UserError as exc:
            log(_logger, "error", "connect.complete_failed", plane=plane, error=str(exc))
            # A same-plane failure raises XPaySetupIncomplete: the form
            # already shows the incomplete-setup banner and a Complete setup
            # button for that case, so a separate connect_error alert would
            # be redundant. Every other failure still gets its own visible
            # error, even on a provider an earlier, unrelated attempt left
            # flagged incomplete.
            if not isinstance(exc, connect_errors.XPaySetupIncomplete):
                connect_errors.record_error(provider, "connect_failed", str(exc))
            return request.redirect(_provider_form_url(provider.id))

        return request.redirect(_provider_form_url(provider.id))


def _provider_form_url(provider_id):
    return f"/odoo/action-payment.action_payment_provider/{provider_id}"
