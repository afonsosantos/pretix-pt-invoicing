from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm

from ...orderdata import bare_tin, client_name
from ..base import InvoiceProvider, IssuedDocument, ProviderError
from .client import FactptAPIError, FactptClient
from .payload import build_payload

__all__ = ["FactptAPIError", "FactptClient", "FactptProvider", "FactptSettingsForm"]


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
    # Fact.pt's product units are a fixed, documented list (not an API lookup) — same for
    # every account, unlike VAT rates.
    factpt_default_unit_id = forms.TypedChoiceField(
        label=_("Unit (Fact.pt)"),
        coerce=int,
        choices=[
            (1, _("Units")),
            (2, _("Meters")),
            (3, _("Boxes")),
            (4, _("Kilograms")),
            (5, _("Liters")),
        ],
        initial=1,
    )
    factpt_default_type = forms.ChoiceField(
        label=_("Item type"),
        choices=[("service", _("Service")), ("product", _("Product"))],
        initial="service",
    )
    factpt_send_client_email = forms.BooleanField(
        label=_("Send the buyer's e-mail address to Fact.pt"),
        required=False,
        help_text=_(
            "Stores the e-mail on the Fact.pt client record, which lets Fact.pt send the "
            "document to the buyer. Off by default: pretix already e-mails the buyer."
        ),
    )


def _describe_tax(item):
    description = item.get("description")
    name = item.get("name")  # e.g. "23%"
    if description and name:
        return f"{description} ({name})"
    return description or name or str(item.get("id"))


class FactptProvider(InvoiceProvider):
    identifier = "factpt"
    verbose_name = "Fact.pt"
    settings_form_class = FactptSettingsForm

    @property
    def is_configured(self):
        return bool(self.settings.get("factpt_token"))

    def _client(self):
        return FactptClient(
            token=self.settings.get("factpt_token"),
            sandbox=self.settings.get("factpt_sandbox", as_type=bool, default=False),
        )

    def _resolve_client_id(self, client, order):
        # Reference an existing client by id instead of sending an inline block, so Fact.pt
        # never has to resolve one itself. GET /clients?search= matches on both tin and
        # name (verified against a real account).
        tin = bare_tin(
            order,
            custom_field_is_nif=self.settings.get(
                "ptinvoicing_nif_custom_field", as_type=bool, default=False
            ),
        )
        name = client_name(order)
        try:
            matches = client.search_clients(tin or name)
        except FactptAPIError:
            # A failed search is not fatal — fall back to inline client creation.
            return None

        if tin:
            # A real NIF identifies one entity, but if the account holds duplicates for it,
            # don't guess which to attach an official invoice to — fall through and let
            # Fact.pt reject it ("Multiple clients with same tin. Specify an ID.") until a
            # human resolves them.
            exact = [c for c in matches if str(c.get("tin")) == tin]
            return exact[0].get("id") if len(exact) == 1 else None

        # No NIF: Fact.pt files every such buyer under the final-consumer NIF 999999990, so
        # the name is all there is to match on. Reuse an existing record deterministically
        # rather than creating another — creating one per issuance is what filled the
        # account with duplicates until Fact.pt could resolve neither by tin nor by details
        # ("Multiple clients with same tin/details. Specify an ID."). Duplicates here are
        # the same fiscal entity under the same name, so the lowest id is as good as any.
        finals = [
            c
            for c in matches
            if c.get("isFinalConsumer") and c.get("name") == name and c.get("id")
        ]
        ids = sorted(int(c["id"]) for c in finals if str(c["id"]).isdigit())
        return ids[0] if ids else None

    def _check_tax_rate_matches(self, client, order):
        # Fact.pt applies taxId's VAT on top of the net price we send, so the document's
        # total only equals what the buyer paid if the configured Fact.pt rate is the same
        # rate pretix charged. They can silently diverge (one fixed taxId per event vs.
        # per-item tax rules), and the result is an invoice for the wrong amount — so stop
        # instead, with an error the admin can act on.
        tax_id = self.settings.get("factpt_default_tax_id", as_type=int)
        rate = next(
            (t for t in client.list_taxes() if str(t.get("id")) == str(tax_id)), None
        )
        if rate is None:
            raise ProviderError(
                _("VAT rate %(id)s does not exist in this Fact.pt account.")
                % {"id": tax_id}
            )

        configured = Decimal(str(rate.get("value")))
        for position in order.positions.all():
            if position.tax_rate != configured:
                raise ProviderError(
                    _(
                        "VAT mismatch: pretix charged %(pretix)s%% on this order but the "
                        "configured Fact.pt rate is %(factpt)s%% (%(label)s). The invoice "
                        "would be issued for the wrong amount. Fix the event's tax rule "
                        "or the Fact.pt VAT rate setting."
                    )
                    % {
                        "pretix": position.tax_rate,
                        "factpt": configured,
                        "label": _describe_tax(rate),
                    }
                )

    def issue(self, order, identifier_id):
        client = self._client()
        self._check_tax_rate_matches(client, order)
        payload = build_payload(
            order,
            self.settings,
            identifier_id,
            client_id=self._resolve_client_id(client, order),
        )
        result = client.create_invoice_receipt(payload)
        data = result.get("data") or {}
        return IssuedDocument(
            document_id=data.get("id"),
            link=result.get("link"),
            permanent_url=result.get("permanentUrl"),
        )

    def download(self, document_id):
        return self._client().download_document(document_id)

    def lookups(self, data):
        # Uses the posted token rather than the saved one, so options show up before Save.
        # Units aren't looked up — Fact.pt's unit list is fixed (see the form above).
        token = (data.get("factpt_token") or "").strip()
        if not token:
            raise ProviderError(_("No API token provided."))
        client = FactptClient(token=token, sandbox=data.get("factpt_sandbox") == "true")
        return {
            "factpt_default_tax_id": [
                {"id": t.get("id"), "label": _describe_tax(t)}
                for t in client.list_taxes()
                if t.get("isActive", True)
            ]
        }
