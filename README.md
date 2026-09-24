# XPay for Odoo

Accept payments with XPay in Odoo: the eCommerce checkout, payment links, and invoices paid
online.

## Requirements

- Odoo 19 Community or Enterprise, self-hosted or on Odoo.sh. Odoo Online does not allow
  third-party modules.
- An XPay merchant account.
- A public HTTPS address for the site. XPay sends payment confirmations to it, and the module
  refuses to connect over plain HTTP.

## Install

Download the release ZIP for your Odoo series and extract it into your `addons` path, then
restart Odoo and update the apps list. Install **XPay** from **Apps**.

## Enable XPay

1. Go to **Website > Configuration > eCommerce > Payment Providers** (or **Invoicing >
   Configuration > Online Payments > Payment Providers**).
2. Open **XPay** and click **Connect with XPay**. This opens XPay's own sign-in and consent page;
   no API key is ever entered into Odoo. You come back connected to your test account, with the
   provider in **Test Mode** and its webhook set up. In Test Mode, Odoo shows XPay at checkout to
   administrators only, so place a test order while logged in.
3. When you are ready, click **Go live** and approve your live account on XPay. It replaces the
   test connection, sets the provider to **Enabled**, and publishes it for shoppers. Go live is
   refused until live payments are activated on your XPay account.
4. **Switch to a test account** takes you back; **Disconnect** removes the connection and turns
   XPay off.

## Payment methods and currencies

The methods and currencies XPay offers at checkout come from your XPay account: cards in every
currency your account accepts, and in EGP the local methods your account has enabled, such as
Fawry, ValU, InstaPay and mobile wallets. They are read when you connect and when you click
**Refresh account**. You can narrow the currency list on the provider's **Configuration**
tab; **Refresh account** resets it to what your account supports.

## Refunds

Refund from Odoo: open the payment from the order or invoice and click **Refund**, for the full
amount or part of it. XPay confirms each refund and the module records it as a refund
transaction on the order. Refunds made in the XPay dashboard are copied back to Odoo the same
way. ValU payments cannot be refunded through XPay.

## Something wrong?

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Support

Contact XPay support through your merchant dashboard.
