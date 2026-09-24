"""Structured, redacted logging for the whole module.

Every log line in xpay_payment goes through `log()` so context is redacted
exactly once, at the write path.
"""

import json

from odoo.addons.payment.logging import get_payment_logger

from ..xpay import redactor

# Odoo's own SensitiveDataFilter regex-redacts these key names out of
# record.args before emission; the module's own redactor.redact() (below)
# is the primary choke point, this is defense in depth on top of it.
SENSITIVE_KEYS = redactor.SECRET_KEYS | redactor.PII_KEYS


def get_logger(name):
    return get_payment_logger(name, SENSITIVE_KEYS)


def log(logger, level, message, **context):
    """Write one structured, redacted log line.

    `level` is one of 'info' (diagnostic, gated by Odoo's own log level),
    'error', or 'critical' (money-loss events) — always written regardless
    of the diagnostic level.
    """
    safe_context = redactor.redact(context) if context else {}
    method = getattr(logger, level, None)
    if not callable(method):
        method = logger.info
    method("%s %s", message, json.dumps(safe_context, default=str))
