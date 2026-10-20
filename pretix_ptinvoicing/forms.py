from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm

from .models import ItemSettings
from .orderdata import (
    DEFAULT_REFERENCE_PREFIX,
    RESERVED_REFERENCE,
    item_settings,
    reference_prefix,
)
from .providers import get_provider, provider_choices


def prefix_note(provider, prefix):
    """
    The sentence under the prefix field about the provider's limit, if it has one —
    the server-rendered version of what settings.js keeps updating as the admin types.
    """
    limit = provider and provider.reference_limit
    if not limit:
        return ""
    return _(
        "With %(provider)s, prefix and product reference together may be at most "
        "%(limit)s characters: %(left)s left for the reference."
    ) % {
        "provider": provider.verbose_name,
        "limit": limit,
        "left": max(0, limit - len(prefix)),
    }


class ProviderSelectForm(SettingsForm):
    ptinvoicing_provider = forms.ChoiceField(
        label=_("Invoicing provider"),
        required=False,
        choices=[("", _("No invoicing"))] + provider_choices(),
        help_text=_(
            "Only the selected provider's settings below are saved and used for issuance."
        ),
    )
    # Rendered by hand in settings.html (its note is updated live by settings.js). The
    # product's own part is set on the product page; see ItemSettingsForm.
    ptinvoicing_reference_prefix = forms.CharField(
        label=_("Product reference prefix"),
        required=False,
        initial=DEFAULT_REFERENCE_PREFIX,
        max_length=50,
        strip=True,
        # Rendered by hand, not by bootstrap_form, so styled by hand.
        widget=forms.TextInput(attrs={"class": "form-control"}),
        help_text=_(
            "Put in front of every product's reference in the provider's catalog, e.g. "
            "“pretix-” makes the reference “pretix-item-12”. Products already created "
            "there keep the reference they were created with."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prefix_note = prefix_note(
            get_provider(self.obj), reference_prefix(self.obj)
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
            "numbers that fail their check digit are ignored, not sent. It then is the "
            "one tax number field at checkout for Portuguese addresses: pretix's own "
            "VAT ID field is hidden there, businesses included."
        ),
    )
    ptinvoicing_itemize_option = forms.BooleanField(
        label=_("Let buyers itemize the invoice by attendee"),
        required=False,
        help_text=_(
            "Adds a checkbox at checkout. When ticked, each invoice line carries the "
            "attendee's name next to the product."
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


class ItemSettingsForm(forms.Form):
    """
    The "Invoicing (PT)" box on the product page (pretix's item_forms signal): the
    product's catalog reference, plus whatever the event's provider adds
    (InvoiceProvider.item_fields). Stored in ItemSettings, not in the item itself.
    """

    title = _("Invoicing (PT)")

    def __init__(self, *args, item, provider, **kwargs):
        self.item = item
        self.provider = provider
        self.instance = item_settings(item) or ItemSettings(item=item)
        kwargs.setdefault(
            "initial", {"reference": self.instance.reference, **self.instance.data}
        )
        super().__init__(*args, **kwargs)

        prefix = reference_prefix(item.event)
        limit = provider.reference_limit
        # Sized to what the provider accepts after the prefix (the whole limit when a
        # provider keeps no catalog, so the row still stores something sensible).
        room = max(1, min(50, (limit - len(prefix)) if limit else 50))
        help_text = _(
            "The product's reference in %(provider)s's catalog, after the prefix "
            "“%(prefix)s” set in the invoicing settings. Leave empty to use the pretix "
            "id: %(prefix)s%(fallback)s. Only matters before the product first exists "
            "there; later changes create a new catalog product."
        ) % {
            "provider": provider.verbose_name,
            "prefix": prefix,
            "fallback": f"item-{item.pk}",
        }
        if limit:
            help_text = (
                str(help_text)
                + " "
                + _(
                    "At most %(room)s characters: %(provider)s allows %(limit)s in total "
                    "and the prefix takes %(used)s."
                )
                % {
                    "room": room,
                    "provider": provider.verbose_name,
                    "limit": limit,
                    "used": len(prefix),
                }
            )
        self.fields["reference"] = forms.CharField(
            label=_("Product reference"),
            required=False,
            max_length=room,
            strip=True,
            help_text=help_text,
        )
        self.fields.update(provider.item_fields())

    def clean_reference(self):
        value = self.cleaned_data["reference"]
        if not value:
            return ""
        if RESERVED_REFERENCE.match(value):
            raise forms.ValidationError(
                _("References of the form “item-<number>” or “fee-…” are reserved.")
            )
        # Two products with one reference would share one catalog product: the second
        # would be invoiced under the first's name. Within the event is all that can be
        # checked here; across events sharing a provider account it's the admin's call.
        clash = (
            ItemSettings.objects.filter(item__event=self.item.event, reference=value)
            .exclude(item=self.item)
            .select_related("item")
            .first()
        )
        if clash:
            raise forms.ValidationError(
                _("“%(reference)s” is already the reference of “%(item)s”.")
                % {"reference": value, "item": clash.item.name}
            )
        return value

    def save(self):
        data = {
            name: value
            for name, value in self.cleaned_data.items()
            if name != "reference" and value not in (None, "")
        }
        reference = self.cleaned_data.get("reference", "")
        if not reference and not data:
            # Nothing set: no row (and none left behind once everything is cleared).
            if self.instance.pk:
                self.instance.delete()
            return
        self.instance.reference = reference
        self.instance.data = data
        self.instance.save()
