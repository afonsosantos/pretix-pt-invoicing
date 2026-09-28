from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm

from .providers import provider_choices


class ProviderSelectForm(SettingsForm):
    ptinvoicing_provider = forms.ChoiceField(
        label=_("Invoicing provider"),
        required=False,
        choices=[("", _("— none (no invoices are issued) —"))] + provider_choices(),
        help_text=_(
            "Only the selected provider's settings below are saved and used for issuance."
        ),
    )
    ptinvoicing_email_invoice = forms.BooleanField(
        label=_("E-mail the invoice to the buyer"),
        required=False,
        help_text=_(
            "Sends the issued document as a PDF attachment once it is issued. pretix's own "
            "order e-mails don't carry it: the document belongs to the provider, not to "
            "pretix's invoice records."
        ),
    )
    ptinvoicing_nif_custom_field = forms.BooleanField(
        label=_("The custom invoice-address field holds the buyer's tax number"),
        required=False,
        help_text=_(
            "pretix only offers its VAT ID field to business customers, so individuals "
            "have nowhere to enter a tax number. Turn this on after adding a custom "
            'recipient field labelled e.g. "NIF" under Settings \u2192 Invoicing; it is '
            "then used as the tax number when the VAT ID field is empty. Portuguese "
            "numbers that fail their check digit are ignored, not sent."
        ),
    )
    ptinvoicing_show_in_order = forms.BooleanField(
        label=_("Show the invoice on the buyer's order page"),
        required=False,
        initial=True,
        help_text=_("Adds a download link next to pretix's own invoice list."),
    )
