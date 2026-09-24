"""Create / persist / dedupe / decommission this store's webhook endpoint.

The new secret is always persisted before any duplicate endpoint is
deleted, so a failure between the two steps never leaves the store
holding no working endpoint and no usable secret at the same time.

One endpoint per store, for whichever plane the stored key belongs to;
its id and the operation id live in the provider's snapshot, the secret
in `xpay_webhook_secret`."""

import uuid

from ..xpay import events, idempotency
from ..xpay.errors import XPayApiError
from .logging import get_logger, log

_logger = get_logger(__name__)


def ensure_endpoint(provider, client=None):
    """Create the webhook endpoint if needed and persist its secret.
    `client` lets a caller (connect_service) reuse a freshly exchanged
    key without re-reading it off the provider.

    An op id names ONE intended creation. It is reused only to retry a
    creation that has not yet succeeded (no endpoint id persisted yet):
    the platform binds the idempotency key to this call's body, and that
    body (this store's URL and `SUBSCRIBED`) is the same every time, so
    replaying the op id after a creation already succeeded would return
    the platform's CACHED response for the endpoint a caller is about to
    swap out — `action_xpay_reconfigure_webhook` and
    `connect_service._provision` both call this while the previous
    endpoint id is still the one in the snapshot, precisely so this check
    sees it and mints a fresh id instead."""
    provider.ensure_one()
    client = client or provider._xpay_client()

    snapshot = provider._xpay_snapshot()
    op_id = snapshot.get("webhook_op_id")
    if not op_id or snapshot.get("webhook_endpoint_id"):
        op_id = str(uuid.uuid4())
        provider._xpay_snapshot_update({"webhook_op_id": op_id})

    url = _endpoint_url(provider)
    body = {"url": url, "enabledEvents": list(events.SUBSCRIBED)}
    key = idempotency.bind_to_body(idempotency.webhook_key(op_id), body)

    try:
        endpoint = client.create_webhook_endpoint(body, idempotency_key=key)
    except XPayApiError as exc:
        if not _is_endpoint_cap_error(exc):
            log(_logger, "error", "webhook.create_failed", plane=client.plane, code=exc.code)
            raise
        _free_a_slot(client, url)
        # The rejected create's key is cached by the platform as a definitive
        # answer for a day; replaying it here would return that same
        # rejection instead of trying again against the now-freed slot.
        op_id = str(uuid.uuid4())
        provider._xpay_snapshot_update({"webhook_op_id": op_id})
        key = idempotency.bind_to_body(idempotency.webhook_key(op_id), body)
        endpoint = client.create_webhook_endpoint(body, idempotency_key=key)

    endpoint_id = endpoint.get("id")
    secret = endpoint.get("secret")
    id_valid = isinstance(endpoint_id, str) and endpoint_id
    secret_valid = isinstance(secret, str) and secret
    if not id_valid or not secret_valid:
        # Fail closed rather than persist a falsy secret and then delete
        # every other endpoint aimed at this URL with keep_id=None (which
        # matches nothing): that would leave the store with no working
        # endpoint AND no usable secret, and webhook verification would fail
        # for every later delivery.
        log(_logger, "error", "webhook.create_response_invalid", plane=client.plane)
        raise XPayApiError(
            "webhook_create_response_invalid",
            "XPay's webhook endpoint response was missing an id or secret",
            http_status=0,
        )

    provider.write({"xpay_webhook_secret": secret})
    provider._xpay_snapshot_update({"webhook_endpoint_id": endpoint_id})

    _dedupe_endpoints(client, keep_id=endpoint_id, url=url)

    log(_logger, "info", "webhook.configured", plane=client.plane, endpoint_id=endpoint_id)
    return endpoint


def decommission(provider, endpoint_id, client):
    """Best-effort delete: an orphaned endpoint at XPay is recoverable, a
    blocked settings/connect flow is not."""
    if not endpoint_id:
        return
    try:
        client.delete_webhook_endpoint(endpoint_id)
        log(_logger, "info", "webhook.decommissioned", plane=client.plane, endpoint_id=endpoint_id)
    except XPayApiError as exc:
        log(
            _logger,
            "error",
            "webhook.decommission_failed",
            plane=client.plane,
            endpoint_id=endpoint_id,
            code=exc.code,
        )


def reconcile_enabled_events(provider, client=None):
    """Compare the recorded endpoint's `enabledEvents` (sorted) against
    `SUBSCRIBED` and PATCH only on a difference — e.g. after an
    integration version bump added a new event type. A missing endpoint
    id is a no-op: nothing to reconcile."""
    provider.ensure_one()
    snapshot = provider._xpay_snapshot()
    endpoint_id = snapshot.get("webhook_endpoint_id")
    if not endpoint_id:
        return

    client = client or provider._xpay_client()
    endpoint = _find_own_endpoint(client, endpoint_id)
    if endpoint is None:
        return

    current = sorted(e for e in (endpoint.get("enabledEvents") or []) if isinstance(e, str))
    desired = sorted(events.SUBSCRIBED)
    if current == desired:
        return

    body = {"enabledEvents": list(events.SUBSCRIBED)}
    key = idempotency.bind_to_body(
        idempotency.webhook_key(f"{snapshot.get('webhook_op_id')}_reconcile"), body
    )
    try:
        client.update_webhook_endpoint(endpoint_id, body, idempotency_key=key)
        log(_logger, "info", "webhook.reconciled", plane=client.plane, endpoint_id=endpoint_id)
    except XPayApiError as exc:
        log(
            _logger,
            "error",
            "webhook.reconcile_failed",
            plane=client.plane,
            endpoint_id=endpoint_id,
            code=exc.code,
        )


def _find_own_endpoint(client, endpoint_id):
    try:
        listing = client.list_webhook_endpoints()
    except XPayApiError as exc:
        log(_logger, "error", "webhook.list_failed", plane=client.plane, code=exc.code)
        return None
    endpoints = listing.get("data") if isinstance(listing, dict) else None
    endpoints = endpoints if isinstance(endpoints, list) else []
    return next((e for e in endpoints if isinstance(e, dict) and e.get("id") == endpoint_id), None)


def _endpoint_url(provider):
    return f"{provider._xpay_base_url()}/payment/xpay/webhook/{provider.id}"


def _is_endpoint_cap_error(exc):
    """Best-effort: the platform's error-code catalogue does not name a
    dedicated code for the endpoint cap, so this reads the message the
    way a human would rather than assume a code."""
    message = (exc.message or "").lower()
    return exc.http_status == 400 and any(
        word in message for word in ("cap", "limit", "maximum", "too many")
    )


def _free_a_slot(client, url):
    """List this store's own endpoints and delete the oldest duplicate, so
    a create rejected for the endpoint cap can be retried once."""
    endpoints = _list_own_endpoints(client, url)
    if not endpoints:
        return
    endpoints.sort(key=lambda e: e.get("createdAt") or "")
    oldest_id = endpoints[0].get("id")
    try:
        client.delete_webhook_endpoint(oldest_id)
        log(_logger, "info", "webhook.duplicate_deleted", plane=client.plane, endpoint_id=oldest_id)
    except XPayApiError as exc:
        log(_logger, "error", "webhook.duplicate_delete_failed", plane=client.plane, code=exc.code)


def _dedupe_endpoints(client, keep_id, url):
    """Delete every other endpoint aimed at this store's URL. Best-effort:
    a reinstall must not leave the platform delivering to one store twice,
    but a failed cleanup must never block a save."""
    for endpoint in _list_own_endpoints(client, url):
        endpoint_id = endpoint.get("id")
        if endpoint_id == keep_id:
            continue
        try:
            client.delete_webhook_endpoint(endpoint_id)
            log(
                _logger,
                "info",
                "webhook.duplicate_deleted",
                plane=client.plane,
                endpoint_id=endpoint_id,
            )
        except XPayApiError as exc:
            log(
                _logger,
                "error",
                "webhook.duplicate_delete_failed",
                plane=client.plane,
                code=exc.code,
            )


def _list_own_endpoints(client, url):
    try:
        listing = client.list_webhook_endpoints()
    except XPayApiError as exc:
        log(_logger, "error", "webhook.list_failed", plane=client.plane, code=exc.code)
        return []
    endpoints = listing.get("data") if isinstance(listing, dict) else None
    endpoints = endpoints if isinstance(endpoints, list) else []
    return [
        endpoint
        for endpoint in endpoints
        if isinstance(endpoint, dict) and _urls_match(endpoint.get("url"), url)
    ]


def _urls_match(a, b):
    """Scheme-blind URL comparison: an http->https migration must still
    recognize its own endpoint."""

    def strip(value):
        value = (value or "").strip().lower()
        for scheme in ("https://", "http://"):
            if value.startswith(scheme):
                value = value[len(scheme) :]
                break
        return value.rstrip("/")

    return strip(a) == strip(b)
