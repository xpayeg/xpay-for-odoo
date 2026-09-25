/** @odoo-module **/
/* global XPay */

import { browser } from '@web/core/browser/browser';
import { _t } from '@web/core/l10n/translation';

import paymentForm from '@payment/js/payment_form';

/**
 * The colorMode the Payment Element should render in, taken from the page
 * itself rather than the shopper's OS: a light Odoo theme must not turn its
 * card fields dark because the shopper's laptop is. Walks up from the mount
 * point to the first opaque background and reads its brightness; anything
 * unreadable (fully transparent all the way up) answers light.
 *
 * Takes the same approach Stripe's own plugin does.
 *
 * @param {Element} node - The mount point, where the walk starts.
 * @return {string} "light" or "dark".
 */
function xpayResolveColorMode(node) {
    try {
        let el = node;
        while (el) {
            const rgb = xpayOpaqueColor(window.getComputedStyle(el).backgroundColor);
            if (rgb) {
                return xpayIsDark(rgb) ? 'dark' : 'light';
            }
            el = el.parentElement;
        }
    } catch {
        return 'light';
    }
    return 'light';
}

/**
 * An [r, g, b] triple when the CSS color is opaque enough to judge, null when
 * it is transparent (keep walking) or unparseable.
 *
 * @param {string} css - A computed `background-color`.
 * @return {?number[]} [r, g, b] or null.
 */
function xpayOpaqueColor(css) {
    const match = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([0-9.]+)\s*)?\)$/.exec(
        String(css || '')
    );
    if (!match) {
        return null;
    }
    if (match[4] !== undefined && parseFloat(match[4]) < 0.5) {
        return null; // Mostly see-through; whatever is behind it decides.
    }
    return [parseInt(match[1], 10), parseInt(match[2], 10), parseInt(match[3], 10)];
}

/**
 * Perceived-brightness test: below 128 of 255 reads as dark.
 *
 * @param {number[]} rgb - [r, g, b].
 * @return {boolean} Whether the color reads as dark.
 */
function xpayIsDark(rgb) {
    return (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 1000 < 128;
}

paymentForm.include({
    init() {
        this._super(...arguments);
        this.xpayElements = {}; // { xpay, elements, element } of the mounted payment option, if any.
        this.xpayInstances = {}; // XPay(publishableKey) instances, cached per key.
        this.xpayActiveOptionId = null; // The one payment option currently allowed a live iframe.
    },


    // #=== DOM MANIPULATION ===#

    /**
     * Prepare the inline form of XPay for direct payment.
     *
     * @override method from @payment/js/payment_form
     * @private
     * @param {number} providerId - The id of the selected payment option's provider.
     * @param {string} providerCode - The code of the selected payment option's provider.
     * @param {number} paymentOptionId - The id of the selected payment option.
     * @param {string} paymentMethodCode - The code of the selected payment method, if any.
     * @param {string} flow - The online payment flow of the selected payment option.
     * @return {void}
     */
    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== 'xpay') {
            this._xpayTeardownActive(); // Switching away from XPay; no listener should linger.
            await this._super(...arguments);
            return;
        }

        if (flow === 'token') {
            this._xpayTeardownActive(); // Saved tokens are charged server-side; no element to mount.
            return;
        }

        // Overwrite the flow of the selected payment option, even if no re-instantiation
        // happens below.
        this._setPaymentFlow('direct');

        if (this.xpayActiveOptionId !== paymentOptionId) {
            // Only one XPay iframe and bridge may be alive at a time: leaving the old one
            // running would leave a second bridge listening for messages meant for the new
            // element.
            this._xpayTeardownActive();
        }
        this.xpayActiveOptionId = paymentOptionId;

        if (this.xpayElements[paymentOptionId]) {
            return; // Don't re-instantiate if already done for this payment option.
        }

        // Extract and deserialize the inline form values.
        const radio = document.querySelector('input[name="o_payment_radio"]:checked');
        const inlineForm = this._getInlineForm(radio);
        const xpayContainer = inlineForm.querySelector('[name="o_xpay_element_container"]');
        const values = JSON.parse(xpayContainer.dataset['xpayInlineFormValues']);

        if (!window.XPay) {
            // The SDK script tag is injected by the inline-form template; its absence here
            // means it failed to load (network, ad blocker, CSP), not a form input problem.
            // No _disableButton() here: the host re-enables the button right after this
            // method returns, whatever it did, so the call would read as a guarantee it
            // is not.
            this._displayErrorDialog(_t("Cannot display the payment form"), "");
            return;
        }

        // One XPay(publishableKey) instance per key. The global factory is synchronous and
        // safe to call again.
        this.xpayInstances[values['publishable_key']] ??= window.XPay(values['publishable_key']);
        const xpay = this.xpayInstances[values['publishable_key']];

        // Session-less ("deferred") Elements: an amount and a currency, no session yet.
        // paymentMethodTypes narrows the fields to the one method this radio row
        // represents; layout: 'tabs' with a single entry collapses XPay's own chooser
        // chrome since the radio row already drew the method's logo and label.
        const elements = xpay.elements({
            mode: values['mode'],
            amount: values['minor_amount'],
            currency: String(values['currency'] || '').toUpperCase(),
            paymentMethodTypes: values['payment_method_types'],
            locale: values['locale'],
            appearance: { colorMode: xpayResolveColorMode(xpayContainer) },
        });
        const element = elements.create('payment', { layout: 'tabs' });
        element.on('loaderror', (event) => {
            this._displayErrorDialog(_t("Cannot display the payment form"), event?.message);
            this._disableButton(); // Keep the shopper from paying against a form that never rendered.
        });
        element.mount(xpayContainer);

        this.xpayElements[paymentOptionId] = { xpay, elements, element };
    },

    /**
     * Tear down the currently active XPay element, if any.
     *
     * destroy(), not unmount(): the whole Elements instance goes, iframe and bridge
     * included, so a stale listener never answers a message meant for the next element.
     *
     * @private
     * @return {void}
     */
    _xpayTeardownActive() {
        const optionId = this.xpayActiveOptionId;
        this.xpayActiveOptionId = null;
        const entry = optionId !== null ? this.xpayElements[optionId] : undefined;
        if (!entry) {
            return;
        }
        delete this.xpayElements[optionId];
        try {
            entry.elements.destroy();
        } catch {
            // Already gone.
        }
    },

    // #=== PAYMENT FLOW ===#

    /**
     * Trigger client-side validation before creating the transaction.
     *
     * @override method from @payment/js/payment_form
     * @private
     * @param {string} providerCode - The code of the selected payment option's provider.
     * @param {number} paymentOptionId - The id of the selected payment option.
     * @param {string} paymentMethodCode - The code of the selected payment method, if any.
     * @param {string} flow - The payment flow of the selected payment option.
     * @return {void}
     */
    async _initiatePaymentFlow(providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== 'xpay' || flow !== 'direct') {
            await this._super(...arguments);
            return;
        }

        const entry = this.xpayElements[paymentOptionId];
        if (!entry) {
            // No element was ever mounted for this option (it failed to load, or the
            // shopper never opened the inline form): delegating to the host here would
            // create a transaction, and through it an XPay session, for a form the
            // shopper cannot fill.
            this._displayErrorDialog(_t("Cannot display the payment form"), "");
            this._enableButton();
            return;
        }

        // this._super is only valid for the remainder of this synchronous turn (the widget
        // mixin restores it right after this method starts running), so it is captured now,
        // before the await below, and called through the capture afterward.
        const _super = this._super.bind(this);

        // Validated here even though a prior 'change' event may already have looked
        // complete: the element does not fire 'change' for a method it auto-selects on
        // its own when only one method exists.
        let outcome;
        try {
            outcome = await entry.elements.submit();
        } catch (error) {
            this._displayErrorDialog(_t("Incorrect payment details"), error?.message);
            this._enableButton();
            return;
        }
        if (outcome?.error) {
            this._displayErrorDialog(_t("Incorrect payment details"), outcome.error.message);
            this._enableButton();
            return;
        }

        await _super(...arguments);
    },

    /**
     * Process XPay's implementation of the direct payment flow.
     *
     * @override method from @payment/js/payment_form
     * @private
     * @param {string} providerCode - The code of the selected payment option's provider.
     * @param {number} paymentOptionId - The id of the selected payment option.
     * @param {string} paymentMethodCode - The code of the selected payment method, if any.
     * @param {object} processingValues - The processing values of the transaction.
     * @return {void}
     */
    async _processDirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        if (providerCode !== 'xpay') {
            await this._super(...arguments);
            return;
        }

        const entry = this.xpayElements[paymentOptionId];
        if (!entry) {
            this._displayErrorDialog(_t("Payment processing failed"), "");
            this._enableButton();
            return;
        }

        // redirect: 'always' lets the SDK itself carry the shopper to the session's own
        // return URL after any 3-DS overlay; the promise below only resolves in the case
        // the SDK does not navigate on its own (no redirectUrl on the session).
        let result;
        try {
            result = await entry.xpay.confirmPayment({
                elements: entry.elements,
                clientSecret: processingValues['client_secret'],
                redirect: 'always',
            });
        } catch (error) {
            // A rejection is a transport or SDK failure, not a decline: nothing was
            // confirmed, so the shopper may simply try again.
            this._displayErrorDialog(_t("Payment processing failed"), error?.message);
            this._enableButton();
            return;
        }

        // Never trust this result as payment truth either way: the server's return
        // route and the webhook decide. This call only ever moves the browser.
        if (result.type === 'success') {
            // The SDK didn't navigate on its own; the server-issued return_url is the
            // only URL this code is allowed to send the browser to.
            browser.location.assign(processingValues['return_url']);
            return;
        }

        const error = result.error;
        if (error?.code === 'amount_reconfirmation_required') {
            // The cart changed since the element was mounted. Retrying would charge a
            // total nobody approved, so reload instead of resubmitting the same intent.
            this._displayErrorDialog(
                _t("Your order changed"),
                _t("Your order total has changed. Reloading the page.")
            );
            browser.location.reload();
            return;
        }
        this._displayErrorDialog(_t("Payment processing failed"), error?.message);
        this._enableButton();
    },

});
