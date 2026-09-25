"""Decimal-only conversions between Odoo's presentation amounts and XPay's
integer minor units.

All arithmetic goes through `decimal.Decimal` — a float silently corrupts
piasters (0.1 + 0.2 territory), and a hardcoded 2-decimal assumption breaks
the 3-decimal currencies (KWD, JOD, OMR, BHD, LYD, IQD, TND) and the
0-decimal ones (JPY, KRW, ...) alike.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

# Minor-unit exponents for currencies that are not 2 decimals. Everything
# else defaults to 2 (see `decimals`).
DECIMALS: dict[str, int] = {
    # 0-decimal ISO 4217 currencies.
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "MGA": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    # 3-decimal ISO 4217 currencies.
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
}

_PLAIN_DECIMAL = re.compile(r"^-?\d*(\.\d+)?$")
_PLAIN_RATE = re.compile(r"^\d*(\.\d+)?$")


def decimals(currency: str) -> int:
    """The minor-unit exponent for `currency`, defaulting to 2."""
    return DECIMALS.get(currency.upper(), 2)


_WHOLE_NUMBER = re.compile(r"^\s*[+-]?\d+(?:\.0+)?\s*$")


def parse_minor(value: object) -> int | None:
    """An integer minor amount out of what the platform sends: an int, or
    the same number serialised as text (`POST /refunds` answers with the
    amount as a string while `GET /refunds/{id}` answers with a number).
    Never a bool, a fraction, or anything else: `None` then, and the
    caller treats it as "no usable amount"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and _WHOLE_NUMBER.match(value):
        return int(Decimal(value.strip()))
    return None


def to_minor(amount: str | Decimal | int, currency: str) -> int:
    """Convert a presentment amount to integer minor units.

    Exact quantization: `ROUND_HALF_UP` on the currency's own exponent — the
    canonical trap is `'1.005'` EGP, which must become `101`, not `100`.
    Floats are refused outright (a float has already lost precision before
    it reaches here); a string that is not a plain decimal number (thousands
    separators, scientific notation, ...) is refused the same way, since a
    malformed total must never silently reach the API as a real amount.
    """
    if isinstance(amount, (bool, float)):
        raise TypeError("Amount must be a str, Decimal, or int, never a float")

    if isinstance(amount, str):
        text = amount.strip()
        if text in ("", "-") or not _PLAIN_DECIMAL.match(text):
            raise TypeError(f"Amount is not a plain decimal number: {amount!r}")
        value = Decimal(text)
    elif isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, int):
        value = Decimal(amount)
    else:
        raise TypeError(f"Amount must be a str, Decimal, or int, got {type(amount).__name__}")

    exponent = decimals(currency)
    try:
        quantized = value.quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise TypeError(f"Amount is not a valid decimal number: {amount!r}") from exc
    return int(quantized.scaleb(exponent))


def from_minor(minor: int, currency: str) -> Decimal:
    """Integer minor units back to a `Decimal` amount."""
    exponent = decimals(currency)
    return (Decimal(minor) / (Decimal(10) ** exponent)).quantize(Decimal(1).scaleb(-exponent))


def to_odoo_amount(minor: int, currency: str) -> float:
    """Integer minor units to the float Odoo's Monetary fields take. The one
    place the integration turns money into a float, and it is the last step
    before the value is handed to Odoo."""
    return float(from_minor(minor, currency))


def session_charge(session: dict) -> tuple[int, str]:
    """What a checkout session says it is charging: `(amount_minor, currency)`.

    Presentment-first: when the merchant prices in a currency other than the
    settlement currency, `presentmentDetails` carries the customer-facing
    mirror and is read in preference to the processing-currency figure.
    Reads `amountSubtotal`, never `amountTotal` — `amountTotal` adds the
    platform fee and collected VAT on fee/VAT-passthrough accounts, so it is
    not the figure that corresponds to what the shopper was shown.

    Raises `ValueError` when neither source states both fields — an absent
    amount must never silently pass as zero.
    """
    presentment = session.get("presentmentDetails")
    presentment = presentment if isinstance(presentment, dict) else {}

    for source in (presentment, session):
        amount = source.get("amountSubtotal")
        currency = source.get("currency")
        if (
            isinstance(amount, int)
            and not isinstance(amount, bool)
            and isinstance(currency, str)
            and currency
        ):
            return amount, currency.upper()

    raise ValueError("Session carries no usable amountSubtotal/currency pair")


def presentment_to_processing(
    amount_minor: int, presentment_currency: str, processing_currency: str, rate: str
) -> int:
    """A presentment-currency minor amount converted to the charge's
    processing currency at a locked `rate`:
    `presentment_major * rate = processing_major`, truncated — never
    rounded — to the processing currency's own exponent. `rate` is
    "processing major per presentment major" and is kept a `Decimal`
    end-to-end; a float here would silently corrupt it.

    Same currency on both sides is not a conversion and returns
    `amount_minor` unchanged without even looking at `rate`.
    """
    presentment_currency = presentment_currency.upper()
    processing_currency = processing_currency.upper()
    if presentment_currency == processing_currency:
        return amount_minor

    rate_text = rate.strip() if isinstance(rate, str) else ""
    if not rate_text or not _PLAIN_RATE.match(rate_text):
        raise TypeError(f"Rate is not a plain positive decimal number: {rate!r}")
    rate_value = Decimal(rate_text)
    if rate_value <= 0:
        raise TypeError(f"Rate must be positive: {rate!r}")

    presentment_major = Decimal(amount_minor).scaleb(-decimals(presentment_currency))
    processing_major = presentment_major * rate_value
    exponent = decimals(processing_currency)
    truncated = processing_major.quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_DOWN)
    return int(truncated.scaleb(exponent))
