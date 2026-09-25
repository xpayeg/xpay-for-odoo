# Fixtures

`openapi.json` is XPay's public merchant OpenAPI contract: the documented
request/response schemas for checkout sessions, charges, refunds, accounts,
and webhook endpoints that merchants integrate against.

- Source: https://docs.xpay.app/openapi.json
- Contract version (`info.version`): 1.0

Refresh with `../../bin/refresh-openapi.sh`, which re-downloads the file and
prints the new version; commit the result.

`addons/19.0/xpay_payment/tests/common.py`'s `schema()`/`build()` load this
file and build every session, charge, refund, account, and webhook-endpoint
fixture from it, so a fixture can never carry a field, or omit a required
one, that the real contract disagrees with. `test_fixture_conformance.py`
checks that promise for every fixture the module's tests use.
