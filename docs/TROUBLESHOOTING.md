# Troubleshooting XPay for Odoo

## Where to look first

1. Open **XPay** under **Website > Configuration > eCommerce > Payment Providers** (or
   **Invoicing > Configuration > Online Payments > Payment Providers**). The **Connection** line
   says which XPay account is connected and whether it is a test or a live account. The
   **Webhook** line says whether XPay can reach your store: *Healthy*, *Waiting for the first
   event*, *Failing since …*, or *Not set up*.
2. Open the order or invoice. The module posts a note in its chatter every time XPay confirms,
   refunds, or declines a payment.
3. Open the payment under **Payment Transactions** in the same menu. Its status (*Draft*,
   *Pending*, *Confirmed*, *Canceled*, *Error*) is what XPay last reported. The **Provider
   Reference** is the XPay payment id you can search for in your XPay dashboard.
4. Odoo's server log. Every line the module writes starts with an event name, for example
   `webhook.signature_rejected`, followed by details such as the transaction reference. Keys and
   secrets are removed and card data and personal data are masked before the line is written;
   only the last four characters of a masked value remain, so support can match it to a record.
   On Odoo.sh use the **Logs** tab; on your own server read the log file Odoo writes to.

## XPay does not appear at checkout

Check that:

- The provider is **Enabled** or in **Test Mode**, not **Disabled**.
- In **Test Mode** the provider is shown to administrators only. Set it to **Published** at the
  top of the provider form to show it to everyone, or place the test order while logged in as
  an administrator.
- The order currency is in the provider's **Currencies** list on the **Configuration** tab. The
  list is filled from your XPay account when you connect. Fawry and ValU take EGP only.
- The payment method is active on the provider. Methods are also filled from your account; use
  **Refresh account** after XPay enables a new one for you.

## The payment fields do not load

The card form is XPay's own, loaded from `https://checkout.xpay.app`.

- Your site must be served over HTTPS.
- Allow scripts and frames from `https://checkout.xpay.app` in any Content Security Policy,
  script optimizer, or consent tool on the site.
- Test once in a private browser window with extensions disabled.

## The shopper paid but the order is not confirmed

XPay confirms payments by webhook. On the provider form:

- **Webhook: Not set up** or **Failing since …**: click **Reconfigure webhook**. It can run once
  a minute.
- The webhook address is built from your site's public address: the website's **Domain**
  setting, or the `web.base.url` system parameter. It must be the HTTPS address XPay can reach
  from the internet, not `localhost` or an internal name.
- A firewall or proxy must let XPay POST to `/payment/xpay/webhook/<id>` on your site.
- In XPay's dashboard, a delivery answered with `401` means the signing secret XPay holds no
  longer matches the one stored in Odoo. **Reconfigure webhook** fixes it.
- A `404` can be a short race while Odoo creates the transaction. XPay retries it on its own.

When the shopper reaches the confirmation page, the module also asks XPay for the payment's
status directly, so most orders confirm even while the webhook is down.

## The payment shows In Process in Invoicing

This is normal. Odoo creates the accounting payment in the *In Process* state and moves it to
*Paid* when you reconcile it with your bank statement. Whether XPay was paid is on the Payment
Transaction, not on the accounting payment.

## A Fawry order stays pending

A Fawry payment gives the shopper a reference to pay at a Fawry outlet. The transaction stays
*Pending* and the order stays unconfirmed until XPay reports the reference paid. Nothing to do
on your side.

## Connect or Go live is refused

The error names the cause. The common ones:

- *XPay requires the site's base URL to be https*: set the website **Domain** or `web.base.url`
  to your HTTPS address.
- *Your XPay API key is missing the following permission(s)*: grant them in your XPay dashboard
  and connect again. The module needs to create checkout sessions, refunds, and webhook
  endpoints.
- *Your XPay account is not activated for live payments yet*: complete activation in your XPay
  dashboard, then click **Go live** again. Nothing changes until then.
- The button does nothing or shows a permission error: only Odoo administrators (Settings
  access) can connect.

## A refund fails

Check the note on the order and the transaction's state.

- ValU payments cannot be refunded through XPay. The **Refund** button is not offered on them.
- The connected XPay account must still hold the refunds permission.
- *XPay is still processing a previous refund on this payment*: wait a moment and retry.

If needed, refund in the XPay dashboard. Dashboard refunds are copied back to Odoo by webhook
as refund transactions on the same order.

## What to send to support

Send:

- The order or invoice number and the transaction reference.
- What the shopper did and what they saw.
- The approximate time and timezone.
- The lines from Odoo's log for that transaction reference.
- A screenshot of the provider form's Connection and Webhook lines, and of failed webhook
  deliveries in the XPay dashboard when an order is stuck.

Never send API keys, webhook secrets, or card details.
