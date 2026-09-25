"""The only HTTP path to the XPay API: auth, idempotency, timeouts, and
error mapping in one place.

Paths carry no `/v1/` prefix. The key's own prefix is the plane authority
server-side, but `liveMode=true|false` is sent as a query parameter on
every request anyway: the platform rejects a mismatch with the key, which
is the point — defense in depth against a client built with the wrong
plane's key.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from .errors import XPayApiError, XPayTransportError
from .hosts import API_BASE

_PLANE_TEST = "test"
_PLANE_LIVE = "live"


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        """Decoded JSON body, or `None` when it is empty or not valid JSON."""
        if not self.body:
            return None
        try:
            return _json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return None


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict | None = None,
        form: dict | None = None,
        timeout: float,
    ) -> Response:
        """Perform one HTTP request. `json_body` and `form` are mutually
        exclusive — the OAuth token endpoint is form-encoded, everything
        else on the merchant API is JSON. Raise `XPayTransportError` on any
        network failure (timeout, DNS, TLS, connection reset) — never let a
        transport-specific exception escape."""
        ...


class RequestsTransport:
    """Transport backed by `requests`.

    The import is deferred to the call site so the pure modules and the
    pytest suite never need `requests` installed.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict | None = None,
        form: dict | None = None,
        timeout: float,
    ) -> Response:
        import requests

        try:
            response = requests.request(
                method, url, headers=headers, json=json_body, data=form, timeout=timeout
            )
        except requests.RequestException as exc:
            raise XPayTransportError(f"Could not reach the XPay API: {exc}") from exc

        return Response(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )


def _validate_and_derive_plane(api_key: str) -> str:
    if api_key.startswith(("rk_live_", "sk_live_")):
        return _PLANE_LIVE
    if api_key.startswith(("rk_test_", "sk_test_")):
        return _PLANE_TEST
    raise ValueError("API key must be a restricted or secret key (rk_/sk_), test or live plane")


class XPayApiClient:
    """The module's only HTTP path to the XPay API.

    Sends `Authorization: Bearer <key>` and never `liveMode`. Writes carry
    an `Idempotency-Key` when the caller supplies one; reads and deletes
    never do.
    """

    def __init__(
        self,
        api_key: str,
        transport: Transport,
        *,
        api_base: str = API_BASE,
        write_timeout: float = 30.0,
        read_timeout: float = 5.0,
        user_agent: str = "xpay-odoo",
    ) -> None:
        self.plane = _validate_and_derive_plane(api_key)
        self._api_key = api_key
        self._transport = transport
        self._api_base = api_base.rstrip("/")
        self._write_timeout = write_timeout
        self._read_timeout = read_timeout
        self._user_agent = user_agent

    def _headers(self, *, idempotency_key: str | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ):
        live_mode = "true" if self.plane == _PLANE_LIVE else "false"
        url = f"{self._api_base}{path}?liveMode={live_mode}"
        response = self._transport.request(
            method,
            url,
            headers=self._headers(idempotency_key=idempotency_key),
            json_body=body,
            timeout=timeout if timeout is not None else self._write_timeout,
        )

        if 200 <= response.status < 300:
            return response.json()

        raise XPayApiError.from_response(response.status, response.json())

    # -- Checkout sessions ------------------------------------------------

    def create_checkout_session(self, body: dict, *, idempotency_key: str) -> dict:
        return self._request(
            "POST", "/checkout/sessions", body=body, idempotency_key=idempotency_key
        )

    def get_checkout_session(self, session_id: str, *, shopper_facing: bool = False) -> dict:
        timeout = self._read_timeout if shopper_facing else self._write_timeout
        return self._request(
            "GET", f"/checkout/sessions/{quote(session_id, safe='')}", timeout=timeout
        )

    def update_checkout_session(self, session_id: str, body: dict, *, idempotency_key: str) -> dict:
        return self._request(
            "PATCH",
            f"/checkout/sessions/{quote(session_id, safe='')}",
            body=body,
            idempotency_key=idempotency_key,
        )

    def expire_checkout_session(self, session_id: str, *, idempotency_key: str) -> dict:
        return self._request(
            "POST",
            f"/checkout/sessions/{quote(session_id, safe='')}/expire",
            idempotency_key=idempotency_key,
        )

    # -- Refunds -----------------------------------------------------------

    def create_refund(self, body: dict, *, idempotency_key: str) -> dict:
        return self._request("POST", "/refunds", body=body, idempotency_key=idempotency_key)

    def get_refund(self, refund_id: str) -> dict:
        return self._request("GET", f"/refunds/{quote(refund_id, safe='')}")

    # -- Account -------------------------------------------------------------

    def get_account(self, *, shopper_facing: bool = False) -> dict:
        timeout = self._read_timeout if shopper_facing else self._write_timeout
        return self._request("GET", "/account", timeout=timeout)

    # -- Webhook endpoints ---------------------------------------------------

    def create_webhook_endpoint(self, body: dict, *, idempotency_key: str) -> dict:
        return self._request(
            "POST", "/webhook-endpoints", body=body, idempotency_key=idempotency_key
        )

    def update_webhook_endpoint(
        self, endpoint_id: str, body: dict, *, idempotency_key: str
    ) -> dict:
        return self._request(
            "PATCH",
            f"/webhook-endpoints/{quote(endpoint_id, safe='')}",
            body=body,
            idempotency_key=idempotency_key,
        )

    def list_webhook_endpoints(self) -> dict:
        return self._request("GET", "/webhook-endpoints")

    def delete_webhook_endpoint(self, endpoint_id: str) -> None:
        self._request("DELETE", f"/webhook-endpoints/{quote(endpoint_id, safe='')}")
