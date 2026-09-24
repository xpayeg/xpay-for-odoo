"""Create-or-replay the XPay checkout session for a payment transaction.

One `payment.transaction` is one XPay checkout session: Odoo creates a
fresh transaction for every payment attempt, so there is no reprice-in-place
here — a new attempt simply gets a new session, and the previous attempt's
session is expired best-effort.
"""

from urllib.parse import quote

from odoo import _
from odoo.exceptions import UserError, ValidationError

from ..xpay import hosts, idempotency, methods, money
from ..xpay.errors import Codes, XPayApiError
from .logging import get_logger, log

_logger = get_logger(__name__)

# The platform's `unitAmount` is a 4-byte signed integer column; a bigger
# amount is refused locally rather than sent and rejected.
_MAX_UNIT_AMOUNT = 2147483647


def session_for(tx):
    """Return `{client_secret, session_id, return_url}` for `tx`, creating
    a session only when there is no usable one already."""
    tx.ensure_one()
    provider = tx.provider_id
    # Money boundary: a same-plane reconnect whose setup never finished
    # must not start a session on the new, half-provisioned key.
    provider._xpay_require_setup_complete()

    _expire_sibling_sessions(tx)

    old_session_id = None
    if tx.xpay_session_id:
        session = _fetch_stored_session(tx, provider)
        if session is not None and session.get("status") == "open" and not session.get("isExpired"):
            client_secret = session.get("clientSecret")
            if client_secret:
                return {
                    "client_secret": client_secret,
                    "session_id": tx.xpay_session_id,
                    "return_url": _return_url(provider, tx, tx.xpay_session_id),
                }
        tx.xpay_session_attempt += 1
        old_session_id = tx.xpay_session_id
    else:
        tx.xpay_session_attempt = 1

    session = _create_session(tx, provider)
    if old_session_id:
        # The SAME transaction is minting a replacement for its own
        # expired/missing session: the old id moves into the superseded
        # ledger BEFORE it is overwritten, so a late payment on it is still
        # recognizable as this transaction's money (parked, not dropped as
        # foreign) rather than judged foreign.
        superseded = list(tx.xpay_superseded_session_ids or [])
        superseded.append(old_session_id)
        tx.xpay_superseded_session_ids = superseded[-10:]
    tx.xpay_session_id = session["id"]

    url = session.get("url")
    if isinstance(url, str) and url and not hosts.is_allowed_xpay_url(url):
        raise UserError(_("XPay returned a session URL that failed the host allowlist check."))

    return {
        "client_secret": session["clientSecret"],
        "session_id": session["id"],
        "return_url": _return_url(provider, tx, session["id"]),
    }


def _fetch_stored_session(tx, provider):
    """The stored session, re-read server-side, or None when it is
    genuinely gone. Only a 404/resource_missing falls through to minting a
    fresh session — every other failure surfaces."""
    client = provider._xpay_client()
    try:
        return client.get_checkout_session(tx.xpay_session_id, shopper_facing=True)
    except XPayApiError as exc:
        if exc.code == Codes.RESOURCE_MISSING or exc.http_status == 404:
            log(
                _logger,
                "info",
                "checkout.stored_session_missing",
                tx_reference=tx.reference,
                session_id=tx.xpay_session_id,
            )
            return None
        log(
            _logger,
            "error",
            "checkout.session_fetch_failed",
            tx_reference=tx.reference,
            code=exc.code,
        )
        raise UserError(
            _(
                "Could not reach XPay to check the payment session (%(code)s). Please try again.",
                code=exc.code,
            )
        ) from exc


def _create_session(tx, provider):
    client = provider._xpay_client()
    plane = client.plane
    currency = tx.currency_id
    currency_code = currency.name.upper()
    minor_amount = money.to_minor(
        f"{currency.round(tx.amount):.{currency.decimal_places}f}", currency_code
    )
    if minor_amount > _MAX_UNIT_AMOUNT:
        raise UserError(_("This amount is too large for XPay to process."))

    enabled_types = provider._xpay_enabled_wire_types(currency)
    wire_types = [
        t for t in methods.wire_types_for(tx.payment_method_id.code) if t in enabled_types
    ]
    if not wire_types:
        # Naming a type the account does not enable for this currency would
        # be refused by the platform anyway; failing before any call keeps
        # that refusal local and the reason legible to the merchant.
        raise ValidationError(
            _(
                "XPay's connected account does not accept %(method)s for %(currency)s.",
                method=tx.payment_method_id.name,
                currency=currency_code,
            )
        )

    body = {
        "uiMode": "custom",
        "currency": currency_code,
        "lineItems": [
            {
                "quantity": 1,
                "priceData": {
                    "currency": currency_code,
                    "unitAmount": minor_amount,
                    "productData": {"name": tx.reference},
                },
            }
        ],
        "afterCompletion": {
            "type": "redirect",
            "redirect": {"url": _return_url(provider, tx)},
        },
        "locale": _locale_for(tx),
        "metadata": {
            "integration": "odoo",
            "tx_reference": tx.reference,
            "db_uuid": tx.env["ir.config_parameter"].sudo().get_param("database.uuid") or "",
        },
        "paymentMethodTypes": list(wire_types),
    }
    body.update(_customer_fields(tx, plane))

    key = idempotency.bind_to_body(
        idempotency.session_key(tx.reference, tx.xpay_session_attempt), body
    )
    try:
        session = _post_session(client, body, key, tx)
    except XPayApiError as exc:
        if "customerId" not in body or exc.code != Codes.RESOURCE_MISSING:
            raise _shopper_safe(exc) from exc
        # A stored customer link the platform no longer knows about: clear
        # it so the next checkout re-creates, and retry this
        # attempt ONCE without it, under the retry key — the same key with
        # a different body would be refused as a fingerprint mismatch.
        log(
            _logger,
            "info",
            "customer.stale_link_cleared",
            tx_reference=tx.reference,
            partner_id=tx.partner_id.id,
        )
        tx.partner_id._xpay_forget_customer(plane)
        # A new dict, not an edit of the first: the transport (and any log
        # of it) keeps a reference to the body it sent.
        body = {k: v for k, v in body.items() if k != "customerId"}
        body.update(_customer_fields(tx, plane, link=False))
        key = idempotency.bind_to_body(
            idempotency.session_key(tx.reference, tx.xpay_session_attempt, retry=True), body
        )
        try:
            session = _post_session(client, body, key, tx)
        except XPayApiError as retry_exc:
            raise _shopper_safe(retry_exc) from retry_exc

    if not isinstance(session.get("id"), str) or not session["id"]:
        raise UserError(_("XPay's session response was missing its id."))
    if not isinstance(session.get("clientSecret"), str) or not session["clientSecret"]:
        raise UserError(_("XPay's session response was missing its client secret."))

    return session


def _post_session(client, body, key, tx):
    try:
        return client.create_checkout_session(body, idempotency_key=key)
    except XPayApiError as exc:
        log(
            _logger,
            "error",
            "checkout.session_create_failed",
            tx_reference=tx.reference,
            code=exc.code,
        )
        raise


def _shopper_safe(exc):
    """exc.message is text echoed from XPay's own API response — never a
    shopper-facing surface. The code and full message are already in the
    log line written by `_post_session`."""
    return UserError(
        _("XPay could not start the payment (%(code)s). Please try again.", code=exc.code)
    )


def _customer_fields(tx, plane, link=True):
    """Who is paying, in the three shapes XPay's "Customers" guide
    describes. A registered shopper with a stored `cus_*`
    for this plane → `customerId` only (the two are mutually exclusive on
    the platform). A registered shopper without one → `customerDetails`
    plus `customerCreation: always`, so the id comes back with the paid
    session and order_sync stores it for next time. A guest →
    `customerDetails` only; the platform's default `if_required` with
    guest dedupe by email, phone and card handles the rest."""
    partner = tx.partner_id
    registered = bool(partner) and partner._xpay_is_registered_shopper()
    if registered and link:
        customer_id = partner._xpay_customer_id(plane)
        if customer_id:
            log(
                _logger,
                "info",
                "customer.linked",
                tx_reference=tx.reference,
                partner_id=partner.id,
                customer_id=customer_id,
            )
            return {"customerId": customer_id}
    fields = {"customerDetails": _customer_details(tx)}
    if registered:
        fields["customerCreation"] = "always"
    return fields


def _customer_details(tx):
    """The prefill block: the transaction's own snapshot of the billing
    partner (Odoo copies name, email, phone and address onto the
    transaction at creation), plus the sale order's delivery address when
    there is one. Empty values are dropped, never sent blank."""
    details = _drop_empty(
        {"name": tx.partner_name, "email": tx.partner_email, "phone": tx.partner_phone}
    )
    billing = _address_fields(
        tx.partner_address,
        tx.partner_city,
        tx.partner_state_id.name,
        tx.partner_zip,
        tx.partner_country_id.code,
    )
    if billing:
        details["billingDetails"] = {"address": billing}

    orders = tx.sale_order_ids if "sale_order_ids" in tx._fields else None
    delivery = orders[:1].partner_shipping_id if orders else None
    if delivery:
        shipping = _drop_empty({"name": delivery.name, "phone": delivery.phone})
        address = _address_fields(
            " ".join(part for part in (delivery.street, delivery.street2) if part),
            delivery.city,
            delivery.state_id.name,
            delivery.zip,
            delivery.country_id.code,
        )
        if address:
            shipping["address"] = address
        if shipping:
            details["shipping"] = shipping
    return details


def _address_fields(line1, city, state, postal_code, country):
    return _drop_empty(
        {
            "line1": line1,
            "city": city,
            "state": state,
            "postalCode": postal_code,
            "country": country,
        }
    )


def _drop_empty(values):
    return {key: value for key, value in values.items() if value}


def _locale_for(tx):
    lang = tx.partner_lang or tx.env.user.lang or ""
    return "ar" if lang.startswith("ar") else "en"


def _return_url(provider, tx, session_id=None):
    """The return route with the session id as a support aid; the route
    never reads it back. Without `session_id` the slot is
    the platform's `{CHECKOUT_SESSION_ID}` template, for the URL sent to
    XPay, which substitutes it before saving. With one, the real id: in
    custom UI mode the shopper's browser navigates on its own with the URL
    this module hands it, so the module substitutes for itself."""
    base = provider._xpay_base_url()
    reference = quote(tx.reference, safe="")
    slot = quote(session_id, safe="") if session_id else "{CHECKOUT_SESSION_ID}"
    return f"{base}/payment/xpay/return?reference={reference}&session_id={slot}"


def _expire_sibling_sessions(tx):
    """Expire, best-effort, the open sessions of sibling draft transactions
    on the same sale orders / invoices, moving their ids into their own
    superseded ledger. A no-op unless `sale`/`account_payment` are
    installed (`sale_order_ids`/`invoice_ids` do not exist otherwise)."""
    if "sale_order_ids" not in tx._fields:
        return

    orders = tx.sale_order_ids
    invoices = tx.invoice_ids if "invoice_ids" in tx._fields else tx.env["payment.transaction"]
    if not orders and not invoices:
        return

    domain = [
        ("id", "!=", tx.id),
        ("provider_code", "=", "xpay"),
        ("state", "=", "draft"),
        ("xpay_session_id", "!=", False),
    ]
    if orders and invoices:
        domain += ["|", ("sale_order_ids", "in", orders.ids), ("invoice_ids", "in", invoices.ids)]
    elif orders:
        domain.append(("sale_order_ids", "in", orders.ids))
    else:
        domain.append(("invoice_ids", "in", invoices.ids))

    for sibling in tx.search(domain):
        try:
            sibling.provider_id._xpay_client().expire_checkout_session(
                sibling.xpay_session_id,
                idempotency_key=f"odoo_expire_{sibling.xpay_session_id}",
            )
        except XPayApiError as exc:
            log(
                _logger,
                "error",
                "checkout.sibling_expire_failed",
                tx_reference=sibling.reference,
                code=exc.code,
            )
        # The id moves from CURRENT to the SUPERSEDED ledger: a late payment
        # on it is then parked for a human, never applied as if it were
        # the attempt the shopper is looking at.
        superseded = list(sibling.xpay_superseded_session_ids or [])
        superseded.append(sibling.xpay_session_id)
        sibling.write({"xpay_session_id": False, "xpay_superseded_session_ids": superseded[-10:]})
