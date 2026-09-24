{
    "name": "XPay",
    "summary": "Accept payments with XPay",
    "version": "19.0.1.0.0",
    "category": "Accounting/Payment Providers",
    "author": "XPay",
    "website": "https://xpay.app",
    "license": "LGPL-3",
    "images": [
        "static/description/banner.png",
        "static/description/connect.png",
        "static/description/refunds.png",
    ],
    # account_payment, not just payment: it creates the account.payment.method
    # for a provider code when the provider module installs after it, and a
    # provider without one cannot be post-processed (Odoo refuses the
    # account.payment with "Please define a payment method line"). Every
    # store that renders a payment form already has it (sale and
    # website_payment both depend on it); naming it here fixes the install
    # order instead of hoping for it.
    "depends": ["payment", "account_payment"],
    "data": [
        "views/payment_xpay_templates.xml",
        "views/payment_templates.xml",
        "views/payment_provider_views.xml",
        "data/payment_method_data.xml",
        "data/payment_method_images.xml",
        "data/payment_provider_data.xml",
    ],
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "assets": {
        "web.assets_frontend": [
            "xpay_payment/static/src/interactions/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
