"""Structured, redacted logging for the whole module.

Every log line in xpay_payment goes through `log()` so context is redacted
exactly once, at the write path.
"""

import json
import logging

from ..xpay import redactor


def get_logger(name):
    return logging.getLogger(name)


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
