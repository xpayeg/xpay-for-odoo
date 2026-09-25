from odoo import fields, models


class ResPartner(models.Model):
    """The XPay customer a registered shopper's checkouts run under, one id
    per plane: a test customer is a different platform object from a live
    one.

    Only partners someone can log in as get one. A guest partner is created
    per checkout and never comes back, and XPay's guest records are
    checkout-only anyway: passing a guest's `cus_*` as `customerId` is
    refused.
    """

    _inherit = "res.partner"

    xpay_customer_ids = fields.Json(groups="base.group_system", copy=False)

    def _xpay_is_registered_shopper(self):
        self.ensure_one()
        return any(not user._is_public() for user in self.sudo().user_ids)

    def _xpay_customer_id(self, plane):
        self.ensure_one()
        ids = self.sudo().xpay_customer_ids
        value = ids.get(plane) if isinstance(ids, dict) else None
        return value if isinstance(value, str) and value.startswith("cus_") else None

    def _xpay_remember_customer(self, plane, customer_id):
        self.ensure_one()
        ids = self.sudo().xpay_customer_ids
        ids = dict(ids) if isinstance(ids, dict) else {}
        ids[plane] = customer_id
        self.sudo().write({"xpay_customer_ids": ids})

    def _xpay_forget_customer(self, plane):
        self.ensure_one()
        ids = self.sudo().xpay_customer_ids
        ids = dict(ids) if isinstance(ids, dict) else {}
        ids.pop(plane, None)
        self.sudo().write({"xpay_customer_ids": ids or False})
