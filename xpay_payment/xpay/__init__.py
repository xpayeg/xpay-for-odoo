"""Odoo-free XPay core: money, signatures, redaction, the API client, and the
pure payment-lifecycle rules the Odoo models delegate to.

Relative imports only — this package must never import `odoo`. Importable
two ways: inside Odoo as `odoo.addons.xpay_payment.xpay`, and in pytest as
`xpay` with `addons/19.0/xpay_payment` on `sys.path` (see `tests/conftest.py`).
"""

from . import (
    api_client,
    connect,
    errors,
    events,
    hosts,
    idempotency,
    lifecycle,
    methods,
    money,
    redactor,
    signature,
)

__all__ = [
    "api_client",
    "connect",
    "errors",
    "events",
    "hosts",
    "idempotency",
    "lifecycle",
    "methods",
    "money",
    "redactor",
    "signature",
]
