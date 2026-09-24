import odoo.addons.payment as payment  # prevent circular import error with payment

from . import controllers, models, services


def post_init_hook(env):
    payment.setup_provider(env, "xpay")


def uninstall_hook(env):
    payment.reset_payment_provider(env, "xpay")
    env["ir.config_parameter"].sudo().search(
        [
            (
                "key",
                "in",
                [
                    "xpay_payment.connect_client_id",
                    "xpay_payment.connect_client_registered_at",
                    "xpay_payment.connect_client_redirect_uri",
                    "xpay_payment.connect_client_completed_at",
                ],
            )
        ]
    ).unlink()
