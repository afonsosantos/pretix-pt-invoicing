from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm


class FactptSettingsForm(SettingsForm):
    factpt_token = forms.CharField(
        label=_("API token (x-auth-token)"),
        widget=forms.PasswordInput(render_value=True),
        required=True,
    )
    factpt_sandbox = forms.BooleanField(
        label=_("Use sandbox environment"),
        required=False,
        help_text=_("The sandbox and production API tokens are different in Fact.pt."),
    )
    factpt_default_tax_id = forms.IntegerField(
        label=_("VAT rate ID (Fact.pt)"),
        help_text=_(
            "Populated automatically as a dropdown once a valid API token is entered above."
        ),
    )
    factpt_default_unit_id = forms.IntegerField(
        label=_("Unit ID (Fact.pt)"),
        initial=1,
    )
    factpt_default_type = forms.ChoiceField(
        label=_("Item type"),
        choices=[("service", _("Service")), ("product", _("Product"))],
        initial="service",
    )
