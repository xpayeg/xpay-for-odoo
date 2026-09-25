"""Puts the same note in front of a human on every document a transaction touches."""


def escalate(tx, summary, message):
    """Post `message` on every sale order and invoice linked to `tx`, and
    schedule a `mail.mail_activity_data_todo` on each order for its own
    salesperson, or the admin user when it has none, carrying `summary`
    and `note=message`."""
    orders = tx.sale_order_ids if "sale_order_ids" in tx._fields else tx.browse()
    invoices = tx.invoice_ids if "invoice_ids" in tx._fields else tx.browse()

    for order in orders:
        order.message_post(body=message)
        user = order.user_id or tx.env.ref("base.user_admin", raise_if_not_found=False)
        if user:
            order.activity_schedule(
                "mail.mail_activity_data_todo",
                summary=summary,
                note=message,
                user_id=user.id,
            )
    for invoice in invoices:
        invoice.message_post(body=message)
