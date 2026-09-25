import base64

from odoo import api, models
from odoo.tools import file_open

# Brand artwork for payment methods Odoo ships with its own, older picture,
# and for this module's own methods, so their creation-time image survives
# a later change to the shipped file.
_BRAND_IMAGES = {
    "payment.payment_method_valu": "xpay_payment/static/src/img/valu.png",
    "xpay_payment.payment_method_fawry": "xpay_payment/static/src/img/fawry.png",
}


class PaymentMethod(models.Model):
    _inherit = "payment.method"

    @api.model
    def _xpay_apply_brand_images(self):
        """Called from data/payment_method_images.xml on install and on
        every upgrade of this module. A `<record>` cannot do this: Odoo
        never rewrites another module's noupdate record from a data file
        during an upgrade, and Odoo's own payment methods are noupdate.
        Odoo's record never brings its picture back either, so the write
        sticks until the next upgrade re-applies it."""
        for xmlid, path in _BRAND_IMAGES.items():
            method = self.env.ref(xmlid, raise_if_not_found=False)
            if not method:
                continue
            with file_open(path, "rb") as handle:
                method.write({"image": base64.b64encode(handle.read())})
