"""XPay wire payment-method strings <-> Odoo `payment.method` codes.

This table is the vocabulary bridge only. What a merchant can actually
charge with comes from `GET /account`'s `supportedCurrencies[].paymentMethodTypes`
at runtime — never hardcode the wire list as a source of availability.
"""

from __future__ import annotations

from collections.abc import Iterable

XPAY_METHODS: tuple[str, ...] = (
    "card",
    "fawry",
    "aman",
    "valu",
    "sympl",
    "tabby",
    "tamara",
    "vodafone_cash",
    "etisalat_cash",
    "orange_cash",
    "we_pay",
    "apple_pay",
    "google_pay",
    "samsung_pay",
    "instapay",
    "bank_transfer",
    "cash_on_delivery",
)

# cash_on_delivery has no XPay-processed wire flow, so it carries no mapping
# here — Odoo's own COD payment provider owns that method.
TO_ODOO: dict[str, str] = {
    "card": "card",
    "fawry": "fawry",
    "aman": "aman",
    "valu": "valu",
    "sympl": "sympl",
    "tabby": "tabby",
    "tamara": "tamara",
    "vodafone_cash": "mobile_wallet_eg",
    "etisalat_cash": "mobile_wallet_eg",
    "orange_cash": "mobile_wallet_eg",
    "we_pay": "mobile_wallet_eg",
    "apple_pay": "apple_pay",
    "google_pay": "google_pay",
    "samsung_pay": "samsung_pay",
    "instapay": "instapay",
    "bank_transfer": "bank_transfer",
}


def _invert(mapping: dict[str, str]) -> dict[str, tuple[str, ...]]:
    inverted: dict[str, list[str]] = {}
    for wire, odoo_code in mapping.items():
        inverted.setdefault(odoo_code, []).append(wire)
    return {odoo_code: tuple(wires) for odoo_code, wires in inverted.items()}


FROM_ODOO: dict[str, tuple[str, ...]] = _invert(TO_ODOO)


def odoo_codes_for(wire_types: Iterable[str]) -> list[str]:
    """Sorted, deduplicated Odoo `payment.method` codes for a set of wire
    strings. A wire string with no mapping (e.g. `cash_on_delivery`) is
    silently skipped."""
    return sorted({TO_ODOO[wire] for wire in wire_types if wire in TO_ODOO})


def wire_types_for(odoo_code: str) -> tuple[str, ...]:
    """XPay wire strings that map to `odoo_code`, or an empty tuple when
    there are none."""
    return FROM_ODOO.get(odoo_code, ())
