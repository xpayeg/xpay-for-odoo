"""Connect failure bookkeeping.

Failures are written to the provider's own snapshot fields and read back
for the provider form's alerts, instead of round-tripping through a URL
query string that nothing reads back once the browser navigates away.
"""

from odoo import _, fields
from odoo.exceptions import UserError

# `setup_incomplete_step` values a same-plane reconnect can stall on;
# Complete setup re-runs from whichever one it stopped at.
STEP_ACCOUNT = "account"
STEP_PERMISSIONS = "permissions"
STEP_WEBHOOK = "webhook"


class XPaySetupIncomplete(UserError):
    """Raised by a same-plane reconnect's own provisioning failure, after the
    new key already replaced the old one at the platform: the incomplete-
    setup banner and its Complete setup button already tell the merchant
    what to do, so the callback must not also raise a separate connect
    error for this one. Any other failure — including one on a provider an
    earlier, unrelated attempt already left flagged incomplete — is still a
    plain `UserError` and still gets its own visible error."""


def flag_setup_incomplete(provider, step):
    """Record which provisioning step a same-plane reconnect stopped at."""
    provider._xpay_snapshot_update({"setup_incomplete_step": step})


def record_error(provider, code, message):
    """Persist a Connect failure to the snapshot instead of the URL, so
    the provider form's alert survives a page reload; cleared by the next
    successful Connect."""
    provider._xpay_snapshot_update(
        {
            "connect_error_code": code,
            "connect_error_message": message,
            "connect_error_at": fields.Datetime.to_string(fields.Datetime.now()),
        }
    )


def describe_incomplete_step(step):
    """A short merchant-facing phrase for the provisioning step a same-plane
    reconnect stopped at, for the incomplete-setup banner."""
    return {
        STEP_ACCOUNT: _("reading your account"),
        STEP_PERMISSIONS: _("checking permissions"),
        STEP_WEBHOOK: _("setting up the webhook"),
    }[step]


def describe_authorization_error(code, description=None):
    """Safe merchant sentence for the callback's `?error=` gate and this
    module's own state-mismatch check: only `access_denied` carries a
    real reason (XPay's own `error_description`, shown verbatim when
    present); every other wire code (`invalid_scope`, `select_business`,
    ...) and a `state_mismatch` describe a protocol state no merchant
    caused, so they share one generic sentence and keep their detail in
    the log."""
    if code == "access_denied":
        if description:
            return _(
                "XPay: the connection was not completed. %(description)s",
                description=description,
            )
        return _(
            "XPay: the connection was canceled before it finished. Nothing changed. To try"
            " again, click Connect."
        )
    return _("XPay: the connection could not be completed. Nothing changed. Try again in a moment.")
