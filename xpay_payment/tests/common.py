import json
import time
from pathlib import Path
from unittest.mock import patch

try:
    # Real Odoo test runtime: `..xpay` resolves through the addon package
    # (`odoo.addons.xpay_payment.xpay`), and `payment`'s test helpers ship
    # with Odoo itself.
    from odoo.addons.payment.tests.common import PaymentCommon
    from odoo.fields import Datetime

    from ..xpay.api_client import Response
    from ..xpay.signature import compute as sign_body
except ImportError:
    # The Odoo-free fixture-conformance test loads this file directly, by
    # path, without Odoo installed. `xpay_payment` is already on `sys.path`
    # (plugins/odoo/tests/conftest.py), so `xpay` resolves the same package
    # by its absolute name instead. Only schema()/build() run in that case.
    from xpay.api_client import Response
    from xpay.signature import compute as sign_body

    PaymentCommon = object
    Datetime = None


# -- Schema-driven fixtures --------------------------------------------------
#
# Every payload below is built from XPay's committed public OpenAPI contract
# (tests/fixtures/openapi.json beside this file, refreshed by
# bin/refresh-openapi.sh), not
# typed by hand, so a fixture cannot silently drift from what the platform
# actually sends: every field the contract requires is present, and no field
# the contract does not define can sneak in unnoticed.


def _openapi_path():
    """The committed public contract, kept beside the tests that consume it
    so the same path resolves in a checkout, in the dev container, and in
    CI."""
    return Path(__file__).resolve().parent / "fixtures" / "openapi.json"


_spec = None
_resolved_schemas = {}


def _spec_data():
    global _spec
    if _spec is None:
        _spec = json.loads(_openapi_path().read_text())
    return _spec


def _resolve(node):
    """A schema node with every `$ref`/`allOf`/`anyOf`/`oneOf` followed down
    to a plain object description, or a leaf `type`/`enum`."""
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _resolve(_spec_data()["components"]["schemas"][name])
    if "allOf" in node:
        properties, required = {}, []
        for part in node["allOf"]:
            resolved = _resolve(part)
            properties.update(resolved.get("properties", {}))
            for key in resolved.get("required", []):
                if key not in required:
                    required.append(key)
        return {"type": "object", "properties": properties, "required": required}
    if "anyOf" in node:
        return _resolve(node["anyOf"][0])
    if "oneOf" in node:
        return _resolve(node["oneOf"][0])
    return node


def schema(name):
    """The named OpenAPI component schema, `$ref`/`allOf`/`anyOf`/`oneOf`
    resolved to a flat `{properties, required}` object. Loads the committed
    contract once per process."""
    if name not in _resolved_schemas:
        _resolved_schemas[name] = _resolve(_spec_data()["components"]["schemas"][name])
    return _resolved_schemas[name]


def _placeholder(prop_schema, label):
    resolved = _resolve(prop_schema)
    if "enum" in resolved:
        return resolved["enum"][0]
    prop_type = resolved.get("type")
    if prop_type == "string":
        return f"{label}-placeholder"
    if prop_type == "integer":
        return 0
    if prop_type == "number":
        return 0.0
    if prop_type == "boolean":
        return False
    if prop_type == "array":
        return []
    if prop_type == "object" or "properties" in resolved:
        return _build_required(resolved, label)
    raise TypeError(f"{label}: no placeholder rule for schema {resolved!r}")


def _build_required(resolved, label):
    properties = resolved.get("properties", {})
    return {
        key: _placeholder(properties[key], f"{label}.{key}")
        for key in resolved.get("required", [])
        if key in properties
    }


def build(name, **overrides):
    """A fixture payload for the OpenAPI component `name`: every property
    the schema requires gets a type-appropriate placeholder, then
    `overrides` replace or add fields. An override key the schema does not
    define raises, so a fixture cannot silently drift from the contract."""
    resolved = schema(name)
    properties = resolved.get("properties", {})
    result = _build_required(resolved, name)
    for key, value in overrides.items():
        if key not in properties:
            raise KeyError(f"{name} has no property {key!r} in the committed OpenAPI contract")
        result[key] = value
    return result


def account_fixture(**overrides):
    """The default XPay test-merchant `AccountResponse`: a connected test
    account with the three permissions the module's services need."""
    values = dict(
        id="acct_test_1",
        displayName="XPay Test Merchant",
        defaultCurrency="EGP",
        defaultLocale="en",
        supportedCurrencies=[
            build(
                "AccountSupportedCurrency",
                code="EGP",
                decimals=2,
                paymentMethodTypes=["card", "valu", "fawry"],
            ),
            build(
                "AccountSupportedCurrency",
                code="KWD",
                decimals=3,
                paymentMethodTypes=["card"],
            ),
        ],
        livemode=False,
        livePaymentsEnabled=True,
        apiKey=build(
            "AccountApiKey",
            type="RESTRICTED",
            mode="test",
            permissions=[
                "CHECKOUT_SESSIONS_WRITE",
                "REFUNDS_WRITE",
                "WEBHOOK_ENDPOINTS_WRITE",
            ],
        ),
    )
    values.update(overrides)
    return build("AccountResponse", **values)


class FakeTransport:
    """Implements the core `xpay.api_client.Transport` protocol: records
    every request and returns queued responses in order."""

    def __init__(self):
        self.calls = []
        self._responses = []

    def queue(self, status, body):
        """Queue the next response this transport will return."""
        payload = b"" if body is None else json.dumps(body).encode()
        self._responses.append(Response(status=status, headers={}, body=payload))

    def fail(self, exc):
        """Make the next request raise `exc` (a network failure)."""
        self._responses.append(exc)

    def request(self, method, url, *, headers, json_body=None, form=None, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json_body": json_body,
                "form": form,
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError(f"FakeTransport got an unscripted request: {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def last_call(self):
        return self.calls[-1]


class XPayCommon(PaymentCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # The module refuses to build a webhook/redirect URI on a non-https
        # base (the platform rejects http outright); the dev/test base URL
        # is plain http, so every test overrides it here.
        cls.env["ir.config_parameter"].sudo().set_param("web.base.url", "https://xpay-test.example")

        cls.currency_egp = cls._enable_currency("EGP")
        cls.currency_kwd = cls._enable_currency("KWD")
        cls.currency_usd = cls._enable_currency("USD")

        cls.xpay_account = account_fixture()

        now = Datetime.to_string(Datetime.now())
        cls.provider = cls._prepare_provider(
            "xpay",
            update_values={
                # A connected test account: one key set, and `state` (set
                # to 'test' by _prepare_provider in the same write) agrees
                # with the key's plane.
                "xpay_restricted_key": "rk_test_abcdef123456",
                "xpay_publishable_key": "pk_test_abcdef123456",
                "xpay_webhook_secret": "whsec_test_1",
                "xpay_account_json": {
                    "merchant_id": "acct_test_1",
                    "account": cls.xpay_account,
                    # Fresh, so ordinary tests never trigger a background
                    # account-cache refresh network call just by reading
                    # the cached account.
                    "fetched_at": now,
                    "connected_at": now,
                },
            },
        )

        cls.payment_method_card = cls.env.ref("payment.payment_method_card")
        cls.payment_method_fawry = cls.env.ref("xpay_payment.payment_method_fawry")
        cls.provider.payment_method_ids = [
            (4, cls.payment_method_card.id),
            (4, cls.payment_method_fawry.id),
        ]
        cls.payment_method_card.active = True
        cls.payment_method_fawry.active = True

        cls.payment_method_id = cls.payment_method_card.id
        cls.payment_method = cls.payment_method_card
        cls.payment_methods = cls.provider.payment_method_ids
        cls.currency = cls.currency_egp
        cls.amount = 750.00

    def _use_transport(self):
        transport = FakeTransport()
        patcher = patch(
            "odoo.addons.xpay_payment.models.payment_provider.PaymentProvider._xpay_transport",
            lambda _self: transport,
        )
        self.startPatcher(patcher)
        return transport

    def _snapshot(self):
        self.provider.invalidate_recordset()
        return self.provider._xpay_snapshot()

    # -- Fixture payloads -----------------------------------------------

    @staticmethod
    def make_session(
        *,
        session_id="cs_test_1",
        status="open",
        payment_status="unpaid",
        is_expired=False,
        amount_subtotal=75000,
        currency="EGP",
        client_secret=None,
        payment_intent=None,
        url=None,
        presentment=None,
    ):
        session = build(
            "CheckoutSessionResponse",
            id=session_id,
            status=status,
            paymentStatus=payment_status,
            isExpired=is_expired,
            amountSubtotal=amount_subtotal,
            currency=currency,
            clientSecret=client_secret or f"{session_id}_secret",
            paymentIntent=payment_intent,
        )
        if url is not None:
            session["url"] = url
        if presentment is not None:
            session["presentmentDetails"] = presentment
        return session

    @staticmethod
    def make_presentment_details(
        *, amount=11000, currency="USD", exchange_rate=51.01, exchange_rate_id="fxrate_test_1"
    ):
        return build(
            "SessionPresentmentDetails",
            amount=amount,
            currency=currency,
            exchangeRate=exchange_rate,
            exchangeRateId=exchange_rate_id,
            amountSubtotal=amount,
        )

    @staticmethod
    def make_payment_intent(*, intent_id="pi_test_1", charge_id="ch_test_1"):
        # Not built from the schema: embedded only inside `make_session`'s
        # `paymentIntent`, and the full `NestedPaymentIntentResponse` (its
        # own `latestCharge` is a whole `ChargeResponse`) carries nothing
        # any test reads beyond these two ids.
        return {"id": intent_id, "latestCharge": {"id": charge_id} if charge_id else None}

    @staticmethod
    def make_event(event_type, obj, *, event_id="evt_test_1", livemode=False):
        # The webhook envelope itself is XPay's, not the merchant contract's
        # (openapi.json documents the API merchants call, not the events it
        # delivers to them), so it stays hand-written; `obj` is one of the
        # schema-built fixtures above.
        return {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "livemode": livemode,
            "data": {"object": obj},
        }

    @staticmethod
    def make_refund(
        *,
        refund_id="re_test_1",
        status="SUCCEEDED",
        amount=75000,
        currency="EGP",
        charge_id="ch_1",
        payment_intent_id="pi_1",
        presentment=None,
    ):
        refund = build(
            "RefundResponse",
            id=refund_id,
            status=status,
            amount=amount,
            currency=currency,
            chargeId=charge_id,
            paymentIntentId=payment_intent_id,
        )
        if presentment is not None:
            refund["presentmentDetails"] = presentment
        return refund

    @staticmethod
    def make_refund_presentment_details(
        *, amount=11000, currency="USD", exchange_rate=51.01, exchange_rate_id="fxrate_test_1"
    ):
        return build(
            "RefundPresentmentDetails",
            amount=amount,
            currency=currency,
            exchangeRate=exchange_rate,
            exchangeRateId=exchange_rate_id,
        )

    @staticmethod
    def make_charge(*, charge_id="ch_test_1", refunds=None):
        return build(
            "ChargeResponse",
            id=charge_id,
            refunds=refunds if refunds is not None else [],
        )

    @staticmethod
    def make_webhook_endpoint(*, endpoint_id="we_test_1", secret="whsec_new_1", url="https://x/"):
        return build(
            "WebhookEndpointWithSecretResponse",
            id=endpoint_id,
            secret=secret,
            url=url,
            enabledEvents=[],
        )

    @staticmethod
    def make_token_response(*, plane="test", merchant_id="acct_test_1"):
        # Not built from the schema: the OAuth2 token exchange is part of
        # Connect, not the merchant API openapi.json documents, so no
        # component describes this shape.
        return {
            "xpay_mode": plane,
            "xpay_merchant_id": merchant_id,
            "xpay_restricted_key": f"rk_{plane}_newkey",
            "xpay_publishable_key": f"pk_{plane}_newkey",
            "access_token": "should-be-ignored",
        }

    @staticmethod
    def sign_header(secret, raw_body, timestamp=None):
        ts = timestamp if timestamp is not None else int(time.time())
        return f"t={ts},v1={sign_body(secret, ts, raw_body)}"
