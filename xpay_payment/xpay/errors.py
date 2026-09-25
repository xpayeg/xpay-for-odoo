"""Error codes and typed exceptions for the XPay API and this module's own checks.

One registry: codes this module mints, plus the XPay API catalogue copied
verbatim (the API's `doc_url` deep links are keyed on the exact code string).
Never string-compare a raw code literal at a call site.
"""

from __future__ import annotations

_RETRYABLE_STATUSES = frozenset({408, 429})


class Codes:
    """String constants for every error code this module compares against."""

    # Plugin-minted: webhook receiver
    WEBHOOK_SIGNATURE_MISSING = "webhook_signature_missing"
    WEBHOOK_SIGNATURE_INVALID = "webhook_signature_invalid"
    WEBHOOK_TIMESTAMP_OUT_OF_TOLERANCE = "webhook_timestamp_out_of_tolerance"
    WEBHOOK_PAYLOAD_MALFORMED = "webhook_payload_malformed"
    WEBHOOK_NOT_CONFIGURED = "webhook_not_configured"
    WEBHOOK_TRANSACTION_NOT_FOUND = "webhook_transaction_not_found"

    # Plugin-minted: gateway/config
    GATEWAY_NOT_CONFIGURED = "gateway_not_configured"
    SESSION_URL_UNTRUSTED = "session_url_untrusted"
    ORDER_LOCK_BUSY = "order_lock_busy"
    SESSION_OWNERSHIP_MISMATCH = "session_ownership_mismatch"
    PAYMENT_METHODS_UNAVAILABLE = "payment_methods_unavailable"

    # Plugin-minted: Connect (OAuth)
    CONNECT_REGISTRATION_FAILED = "connect_registration_failed"
    CONNECT_EXCHANGE_FAILED = "connect_exchange_failed"
    CONNECT_EXCHANGE_AMBIGUOUS = "connect_exchange_ambiguous"
    CONNECT_STATE_MISMATCH = "connect_state_mismatch"

    # Plugin-minted: API client fallbacks
    TRANSPORT_ERROR = "transport_error"  # No HTTP response at all (timeout, DNS, TLS).
    API_ERROR = "api_error"  # API error response carried no usable code.

    # XPay API catalogue, copied verbatim from the platform's error-code enum.
    INVALID_REQUEST = "invalid_request"
    PARAMETER_OUT_OF_RANGE = "parameter_out_of_range"
    PARAMETER_INVALID = "parameter_invalid"
    PARAMETER_MISSING = "parameter_missing"
    PARAMETER_UNKNOWN = "parameter_unknown"
    PARAMETERS_EXCLUSIVE = "parameters_exclusive"
    RESOURCE_MISSING = "resource_missing"
    RESOURCE_INVALID_STATE = "resource_invalid_state"
    AUTHENTICATION_REQUIRED = "authentication_required"
    INVALID_API_KEY = "invalid_api_key"
    API_KEY_INACTIVE = "api_key_inactive"
    INVALID_SIGNATURE = "invalid_signature"
    MERCHANT_NOT_ACTIVATED = "merchant_not_activated"
    PERMISSION_DENIED = "permission_denied"
    CHECKOUT_SESSION_EXPIRED = "checkout_session_expired"
    INVALID_CLIENT_SECRET = "invalid_client_secret"
    AMOUNT_RECONFIRMATION_REQUIRED = "amount_reconfirmation_required"
    PAYMENT_STILL_CONFIRMING = "payment_still_confirming"
    PAYMENT_ALREADY_COMPLETED = "payment_already_completed"
    AMOUNT_INVALID = "amount_invalid"
    CURRENCY_INVALID = "currency_invalid"
    CHARGE_NOT_CAPTURED = "charge_not_captured"
    MERCHANT_NO_BALANCE = "merchant_no_balance"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    RATE_LIMIT = "rate_limit"
    IDEMPOTENCY_KEY_IN_USE = "idempotency_key_in_use"
    INTERNAL_ERROR = "internal_error"
    REQUEST_TIMEOUT = "request_timeout"


def _clean_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class XPayApiError(Exception):
    """An XPay API error: the platform's own envelope, or a transport failure.

    Retryable exactly when a retry has a chance of a different answer: a
    request timeout, a rate limit, any 5xx, or no response at all (the
    transport marker, status 0).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int,
        error_type: str | None = None,
        param: str | None = None,
        doc_url: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.error_type = error_type
        self.param = param
        self.doc_url = doc_url
        self.request_id = request_id

    @classmethod
    def from_response(cls, http_status: int, body: dict | None) -> XPayApiError:
        """Parse the API's `{error: {...}, request_id}` envelope.

        A blank or non-string `code`/`message` falls back exactly like an
        absent one — a blank code would silently defeat every downstream
        comparison.
        """
        error = body.get("error") if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else {}

        code = _clean_str(error.get("code")) or Codes.API_ERROR
        message = _clean_str(error.get("message")) or "XPay API request failed"
        error_type = _clean_str(error.get("type"))
        param = _clean_str(error.get("param"))
        doc_url = _clean_str(error.get("doc_url"))
        request_id = _clean_str(body.get("request_id")) if isinstance(body, dict) else None

        return cls(
            code,
            message,
            http_status=http_status,
            error_type=error_type,
            param=param,
            doc_url=doc_url,
            request_id=request_id,
        )

    @property
    def is_retryable(self) -> bool:
        return (
            self.http_status == 0
            or self.http_status in _RETRYABLE_STATUSES
            or self.http_status >= 500
        )


class XPayTransportError(XPayApiError):
    """No HTTP response at all: timeout, DNS failure, TLS error. Status 0 is
    the marker."""

    def __init__(self, message: str) -> None:
        super().__init__(Codes.TRANSPORT_ERROR, message, http_status=0)


class SignatureError(Exception):
    """Webhook signature verification failed. `code` names which check failed,
    so the caller can answer 500 for a configuration fault and 401 for
    everything else."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
