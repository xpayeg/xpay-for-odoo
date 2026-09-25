import base64
import inspect

from odoo.tests import TransactionCase, tagged
from odoo.tools import file_open
from odoo.tools.image import image_process


@tagged("post_install", "-at_install")
class TestInstall(TransactionCase):
    def test_module_installed(self):
        module = self.env["ir.module.module"].search([("name", "=", "xpay_payment")])
        self.assertEqual(module.state, "installed")

    def test_brand_images_are_reapplied_for_fawry_and_valu(self):
        # A noupdate <record>'s fields are only ever written once, at
        # creation, so a later change to the shipped image file would never
        # reach an already-installed database without this being called
        # again.
        # A minimal valid 1x1 transparent PNG, base64-encoded already: the
        # image field rejects anything Pillow cannot decode.
        placeholder = (
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            b"YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        )
        fawry = self.env.ref("xpay_payment.payment_method_fawry")
        valu = self.env.ref("payment.payment_method_valu")
        fawry.write({"image": placeholder})
        valu.write({"image": placeholder})

        self.env["payment.method"]._xpay_apply_brand_images()

        # payment.method's image field caps at 64x64 (Odoo core); both
        # shipped files exceed that, so the field resizes and re-encodes on
        # every write, this call's included. Comparing to the raw shipped
        # bytes would never match; the field's own processing of those same
        # bytes is what the write is expected to have produced.
        with file_open("xpay_payment/static/src/img/fawry.png", "rb") as handle:
            expected = base64.b64encode(
                image_process(handle.read(), size=(64, 64), verify_resolution=True)
            )
        self.assertEqual(fawry.image, expected)
        with file_open("xpay_payment/static/src/img/valu.png", "rb") as handle:
            expected = base64.b64encode(
                image_process(handle.read(), size=(64, 64), verify_resolution=True)
            )
        self.assertEqual(valu.image, expected)

    def _base_method(self, model_name, method_name, base_module):
        """The class actually defining `method_name` under `base_module` in
        the registry's MRO for `model_name` -- the framework's own class,
        never this module's override of the same name, even though both
        classes answer to the same name in the merged registry class."""
        registry_cls = type(self.env[model_name])
        for cls in registry_cls.__mro__:
            if cls.__module__ == base_module and method_name in cls.__dict__:
                return cls.__dict__[method_name]
        self.fail(f"{method_name} not found on any {base_module} class in {model_name}'s MRO")

    def test_transaction_hooks_match_the_base_signature(self):
        """Every payment.transaction hook this module overrides must keep the
        base framework's parameter names. Every other installed payment
        provider shares this same base class; a drifted override signature
        (an extra required argument, a renamed parameter) breaks them the
        moment the framework calls the hook with its own argument names,
        not only this module's own tests."""
        base_module = "odoo.addons.payment.models.payment_transaction"
        expectations = {
            "_process": ["self", "provider_code", "payment_data"],
            "_search_by_reference": ["self", "provider_code", "payment_data"],
            "_extract_amount_data": ["self", "payment_data"],
            "_apply_updates": ["self", "payment_data"],
            "_send_refund_request": ["self"],
            "_get_specific_processing_values": ["self", "processing_values"],
        }
        for method_name, expected_params in expectations.items():
            method = self._base_method("payment.transaction", method_name, base_module)
            params = list(inspect.signature(method).parameters)
            self.assertEqual(
                params,
                expected_params,
                f"payment.transaction.{method_name} base signature drifted: {params}",
            )

        create_child = self._base_method(
            "payment.transaction", "_create_child_transaction", base_module
        )
        sig = inspect.signature(create_child)
        params = sig.parameters
        self.assertEqual(list(params)[:3], ["self", "amount", "is_refund"])
        self.assertEqual(params["is_refund"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        var_keyword = [p for p in params.values() if p.kind == inspect.Parameter.VAR_KEYWORD]
        self.assertTrue(var_keyword, "_create_child_transaction lost its **custom_create_values")
        self.assertEqual(var_keyword[0].name, "custom_create_values")

    def test_provider_hooks_match_the_base_signature(self):
        """Same guarantee as above, for the payment.provider hooks this
        module overrides: every hook exists on the base with no parameters
        beyond self, so an override cannot silently gain a required
        argument the framework never passes."""
        base_module = "odoo.addons.payment.models.payment_provider"
        for method_name in (
            "_compute_feature_support_fields",
            "_get_supported_currencies",
            "_get_default_payment_method_codes",
            "_activate_default_pms",
        ):
            method = self._base_method("payment.provider", method_name, base_module)
            params = list(inspect.signature(method).parameters)
            self.assertEqual(
                params,
                ["self"],
                f"payment.provider.{method_name} base signature drifted: {params}",
            )
