from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm

from .providers import provider_choices


class ProviderSelectForm(SettingsForm):
    ptinvoicing_provider = forms.ChoiceField(
        label=_("Invoicing provider"),
        required=False,
        choices=[("", _("No invoicing"))] + provider_choices(),
        help_text=_(
            "Only the selected provider's settings below are saved and used for issuance."
        ),
    )
    ptinvoicing_auto_issue = forms.BooleanField(
        label=_("Issue documents automatically"),
        required=False,
        initial=True,
        help_text=_(
            "Issues the invoice-receipt once an order is paid, and the credit note once "
            "it is fully refunded. When off, nothing is issued unless you use the "
            "buttons on the order's page."
        ),
    )
    ptinvoicing_nif_custom_field = forms.BooleanField(
        label=_("The custom invoice-address field holds the buyer's tax number"),
        required=False,
        help_text=_(
            "pretix only offers its VAT ID field to business customers, so individuals "
            "have nowhere to enter a tax number. Turn this on after adding a custom "
            'recipient field labelled e.g. "NIF" under Settings → Invoicing; it is '
            "then used as the tax number when the VAT ID field is empty. Portuguese "
            "numbers that fail their check digit are ignored, not sent."
        ),
    )
    ptinvoicing_show_in_order = forms.BooleanField(
        label=_("Show the invoice on the buyer's order page"),
        required=False,
        initial=True,
        help_text=_(
            "Adds download buttons for the invoice and any credit note beside the "
            "ticket downloads on the buyer's order page."
        ),
    )


class EmailSettingsForm(SettingsForm):
    # pretix's own order e-mails don't carry either document: they attach order.invoices,
    # pretix's own invoice records, and neither an issued invoice-receipt nor a credit note
    # belongs to those. Both off by default — an organizer may not want the extra e-mail.
    ptinvoicing_email_invoice = forms.BooleanField(
        label=_("E-mail the invoice-receipt to the buyer"),
        required=False,
        help_text=_("Sends the issued document as a PDF attachment once it is issued."),
    )
    ptinvoicing_email_credit_note = forms.BooleanField(
        label=_("E-mail the credit note to the buyer"),
        required=False,
        help_text=_("Sends the credit note as a PDF attachment once it is issued."),
    )
