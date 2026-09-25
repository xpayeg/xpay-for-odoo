import base64

from odoo import api, models
from odoo.tools import file_open

# Brand artwork applied by a non-noupdate function call rather than left to
# the noupdate <record> that creates the method, so an upgrade of this
# module always re-applies it (see data/payment_method_images.xml).
_BRAND_IMAGES = {
    "xpay_payment.payment_method_valu": "xpay_payment/static/src/img/valu.png",
    "xpay_payment.payment_method_fawry": "xpay_payment/static/src/img/fawry.png",
}


class PaymentMethod(models.Model):
    _inherit = "payment.method"

    @api.model
    def _xpay_apply_brand_images(self):
        """Called from data/payment_method_images.xml on install and on
        every upgrade of this module. Not folded into the <record> that
        creates the method: a noupdate record's fields are only ever
        written once, at creation, so a later change to the shipped
        image file would never reach an already-installed database
        without this being called again."""
        for xmlid, path in _BRAND_IMAGES.items():
            method = self.env.ref(xmlid, raise_if_not_found=False)
            if not method:
                continue
            with file_open(path, "rb") as handle:
                method.write({"image": base64.b64encode(handle.read())})
