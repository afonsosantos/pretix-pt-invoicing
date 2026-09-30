import random

import requests
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm

from ...orderdata import bare_tin
from ..base import InvoiceProvider, IssuedDocument, ProviderError
from .client import MoloniAPIError, MoloniClient
from .payload import build_credit_note, build_customer, build_document

__all__ = ["MoloniAPIError", "MoloniClient", "MoloniProvider", "MoloniSettingsForm"]


class MoloniSettingsForm(SettingsForm):
    moloni_client_id = forms.CharField(label=_("Developer ID (client_id)"))
    moloni_client_secret = forms.CharField(
        label=_("Client secret"), widget=forms.PasswordInput(render_value=True)
    )
    moloni_username = forms.CharField(label=_("Moloni username"))
    moloni_password = forms.CharField(
        label=_("Moloni password"), widget=forms.PasswordInput(render_value=True)
    )
    moloni_company_id = forms.IntegerField(
        label=_("Company (Moloni)"),
        help_text=_("Populated automatically once the credentials above are valid."),
    )
    moloni_document_set_id = forms.IntegerField(label=_("Document set (Moloni)"))
    # Moloni series are scoped to one document type each — the invoice's own document set
    # cannot number a credit note, so this needs its own, separately configured series.
    moloni_credit_note_document_set_id = forms.IntegerField(
        label=_("Credit note document set (Moloni)"),
        help_text=_(
            "Must be a series created for the Credit Note document type in Moloni — it "
            "cannot be the same series as the invoice-receipt's."
        ),
    )
    moloni_tax_id = forms.IntegerField(label=_("VAT rate (Moloni)"), required=False)
    moloni_exemption_reason = forms.CharField(
        label=_("Exemption reason"),
        required=False,
        help_text=_('Required on 0% lines, e.g. "M07" for Artigo 9.º do CIVA.'),
    )
    moloni_payment_method_id = forms.IntegerField(
        label=_("Payment method (Moloni)"),
        help_text=_(
            "An invoice-receipt is one of Moloni's self-paid document types and always "
            "carries a payment; this is the method it's recorded under."
        ),
    )


class MoloniProvider(InvoiceProvider):
    identifier = "moloni"
    verbose_name = "Moloni"
    settings_form_class = MoloniSettingsForm

    # Moloni's documents/insert has no idempotency field — our_reference and your_reference
    # are free text it doesn't dedupe on. A retry after a timeout could therefore issue a
    # second official invoice, so the core must not retry on its own.
    deduplicates_issuance = False

    @property
    def is_configured(self):
        return all(
            self.settings.get(k)
            for k in (
                "moloni_client_id",
                "moloni_client_secret",
                "moloni_username",
                "moloni_password",
                "moloni_company_id",
                "moloni_document_set_id",
                "moloni_credit_note_document_set_id",
                "moloni_payment_method_id",
            )
        )

    def _store_token(self, access_token, refresh_token, expires_at):
        # self.settings is writable, which is what makes OAuth workable here: without
        # caching, every issuance would burn a fresh password grant.
        self.settings.set("moloni_access_token", access_token)
        self.settings.set("moloni_refresh_token", refresh_token or "")
        self.settings.set("moloni_token_expires", str(expires_at))

    def _client(self):
        return MoloniClient(
            client_id=self.settings.get("moloni_client_id"),
            client_secret=self.settings.get("moloni_client_secret"),
            username=self.settings.get("moloni_username"),
            password=self.settings.get("moloni_password"),
            access_token=self.settings.get("moloni_access_token") or None,
            refresh_token=self.settings.get("moloni_refresh_token") or None,
            expires_at=float(self.settings.get("moloni_token_expires") or 0),
            on_token=self._store_token,
        )

    def _next_customer_number(self, client, company_id):
        # customers/insert requires a 'number' Moloni has no auto-increment for; the
        # official Moloni plugins all fetch one from getNextNumber first (confirmed in
        # moloni-pt/woocommerce's OrderCustomer::getCustomerNextNumber), falling back to a
        # random one if the call fails rather than let that break the whole issuance.
        try:
            result = client.call("customers/getNextNumber", {"company_id": company_id})
        except MoloniAPIError:
            result = None
        return (result or {}).get("number") or str(random.randint(10**10, 10**11 - 1))

    def _resolve_customer_id(self, client, order, company_id):
        """
        Moloni takes only a customer_id — there is no inline client block on the document,
        so the customer has to exist first.
        """
        tin = bare_tin(
            order,
            custom_field_is_nif=self.settings.get(
                "ptinvoicing_nif_custom_field", as_type=bool, default=False
            ),
        )
        customer = build_customer(
            order,
            custom_field_is_nif=self.settings.get(
                "ptinvoicing_nif_custom_field", as_type=bool, default=False
            ),
        )

        if tin:
            # Only reuse an unambiguous match: guessing which duplicate an official
            # invoice belongs to isn't something to do quietly.
            try:
                found = client.call(
                    "customers/getByVat", {"company_id": company_id, "vat": tin}
                )
            except MoloniAPIError:
                found = []
            matches = [c for c in (found or []) if str(c.get("vat")) == tin]
            if len(matches) == 1:
                return matches[0]["customer_id"]

        created = client.call(
            "customers/insert",
            {
                "company_id": company_id,
                "number": self._next_customer_number(client, company_id),
                **customer,
            },
        )
        customer_id = (created or {}).get("customer_id")
        if not customer_id:
            raise MoloniAPIError(_("Moloni did not return a customer id."))
        return customer_id

    def issue(self, order, identifier_id):
        client = self._client()
        company_id = self.settings.get("moloni_company_id", as_type=int)
        date = timezone.now().date().isoformat()

        customer_id = self._resolve_customer_id(client, order, company_id)
        document = build_document(
            order, self.settings, customer_id, identifier_id, date
        )
        result = client.call("invoiceReceipts/insert", document)

        document_id = (result or {}).get("document_id")
        if not document_id:
            raise MoloniAPIError(
                _("Moloni did not return a document id."),
                detail=result if isinstance(result, dict) else None,
            )
        return IssuedDocument(
            document_id=str(document_id),
            link=None,
            permanent_url=(result or {}).get("public_link"),
        )

    def credit(self, order, document_id, identifier_id):
        client = self._client()
        company_id = self.settings.get("moloni_company_id", as_type=int)
        date = timezone.now().date().isoformat()

        # Generic documents/getOne (not invoiceReceipts/getOne), and paired with
        # getUnrelatedProducts for the line items — matching how the official Moloni
        # WooCommerce plugin's CreateCreditNote service does it, since getOne's own
        # products don't carry the document_product_id a credit note needs (see
        # build_credit_note's docstring).
        original = client.call(
            "documents/getOne",
            {"company_id": company_id, "document_id": int(document_id)},
        )
        if not original:
            raise MoloniAPIError(
                _("Moloni could not find the original document %(id)s to credit.")
                % {"id": document_id}
            )
        unrelated_products = client.call(
            "documents/getUnrelatedProducts",
            {"company_id": company_id, "document_id": int(document_id)},
        )

        payload = build_credit_note(
            int(document_id),
            original,
            unrelated_products,
            self.settings,
            identifier_id,
            date,
        )
        result = client.call("creditNotes/insert", payload)

        credit_id = (result or {}).get("document_id")
        if not credit_id:
            raise MoloniAPIError(
                _("Moloni did not return a credit note id."),
                detail=result if isinstance(result, dict) else None,
            )
        return IssuedDocument(document_id=str(credit_id), link=None, permanent_url=None)

    def download(self, document_id):
        client = self._client()
        link = client.call(
            "documents/getPDFLink",
            {
                "company_id": self.settings.get("moloni_company_id", as_type=int),
                "document_id": int(document_id),
            },
        )
        url = (link or {}).get("url") if isinstance(link, dict) else None
        if not url:
            raise MoloniAPIError(_("Moloni did not return a PDF link."))

        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
        except requests.RequestException as e:
            raise MoloniAPIError(
                _("Could not download the document from Moloni: %(error)s")
                % {"error": e}
            )
        return response.content

    def lookups(self, data):
        """
        Five dropdowns at once (company, the two document sets, tax, payment method),
        against credentials the admin hasn't saved yet, using the same
        {field: [{id, label}]} contract as any other provider's lookups().
        """
        missing = [
            k
            for k in (
                "moloni_client_id",
                "moloni_client_secret",
                "moloni_username",
                "moloni_password",
            )
            if not (data.get(k) or "").strip()
        ]
        if missing:
            raise ProviderError(_("Fill in the Moloni credentials first."))

        client = MoloniClient(
            client_id=data["moloni_client_id"].strip(),
            client_secret=data["moloni_client_secret"].strip(),
            username=data["moloni_username"].strip(),
            password=data["moloni_password"].strip(),
        )

        companies = client.call("companies/getAll") or []
        fields = {
            "moloni_company_id": [
                {
                    "id": c.get("company_id"),
                    "label": c.get("name") or c.get("company_id"),
                }
                for c in companies
            ]
        }

        company_id = (data.get("moloni_company_id") or "").strip()
        if not company_id:
            return fields

        payload = {"company_id": int(company_id)}
        for field, endpoint, id_key, label_key in (
            (
                "moloni_document_set_id",
                "documentSets/getAll",
                "document_set_id",
                "name",
            ),
            # Same list as above — Moloni has no per-type filter on documentSets/getAll,
            # so both dropdowns are populated from it and the admin picks distinct series.
            (
                "moloni_credit_note_document_set_id",
                "documentSets/getAll",
                "document_set_id",
                "name",
            ),
            ("moloni_tax_id", "taxes/getAll", "tax_id", "name"),
            (
                "moloni_payment_method_id",
                "paymentMethods/getAll",
                "payment_method_id",
                "name",
            ),
        ):
            try:
                rows = client.call(endpoint, payload) or []
            except MoloniAPIError:
                continue
            fields[field] = [
                {"id": r.get(id_key), "label": r.get(label_key) or r.get(id_key)}
                for r in rows
            ]
        return fields
