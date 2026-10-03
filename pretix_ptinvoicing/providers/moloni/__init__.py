import logging
import random
from decimal import Decimal

import requests
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm

from ...orderdata import FINAL_CONSUMER_NIF, bare_tin, invoice_lines
from ..base import (
    InvoiceProvider,
    IssuedDocument,
    ProviderError,
    ProviderUnreachable,
)
from .client import MoloniAPIError, MoloniClient
from .payload import (
    COUNTRY_PT,
    build_catalog_product,
    build_credit_note,
    build_customer,
    build_document,
)

__all__ = ["MoloniAPIError", "MoloniClient", "MoloniProvider", "MoloniSettingsForm"]

logger = logging.getLogger(__name__)

DOWNLOAD_URL = "https://www.moloni.pt/downloads/index.php"


class MoloniSettingsForm(SettingsForm):
    moloni_client_id = forms.CharField(label=_("Developer ID (client_id)"))
    moloni_client_secret = forms.CharField(
        label=_("Client secret"), widget=forms.PasswordInput(render_value=True)
    )
    # Optional since "Connect to Moloni" (authorization-code grant) needs no password.
    # Kept as an alternative: with them, an expired connection heals itself.
    moloni_username = forms.CharField(
        label=_("Moloni username"),
        required=False,
        help_text=_('Only needed if you don\'t use "Connect to Moloni".'),
    )
    moloni_password = forms.CharField(
        label=_("Moloni password"),
        required=False,
        widget=forms.PasswordInput(render_value=True),
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
        help_text=_(
            "Required on 0% lines. Lists Moloni's exemption codes once connected."
        ),
    )
    moloni_payment_method_id = forms.IntegerField(
        label=_("Payment method (Moloni)"),
        help_text=_(
            "An invoice-receipt is one of Moloni's self-paid document types and always "
            "carries a payment; this is the method it's recorded under."
        ),
    )
    # Moloni only invoices catalog products, so each pretix item gets one, created on
    # its first sale — these are what it's created with (like Fact.pt's item defaults).
    moloni_product_category_id = forms.IntegerField(
        label=_("Product category (Moloni)"),
        help_text=_(
            "Each pretix product is created in Moloni's catalog on its first sale, "
            "in this category."
        ),
    )
    moloni_product_type = forms.TypedChoiceField(
        label=_("Product type (Moloni)"),
        coerce=int,
        initial=2,
        choices=[(2, _("Service")), (1, _("Product"))],
    )
    moloni_unit_id = forms.IntegerField(label=_("Unit (Moloni)"))
    moloni_maturity_date_id = forms.IntegerField(
        label=_("Maturity date (Moloni)"),
        help_text=_(
            "Moloni requires one on every new customer it creates for a buyer, e.g. "
            '"Pronto pagamento".'
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

    settings_template = "pretix_ptinvoicing/control/moloni_connection.html"
    logo = "pretix_ptinvoicing/logos/moloni.svg"
    lookup_triggers = ("moloni_company_id",)

    @property
    def is_configured(self):
        return self._has_login() and all(
            self.settings.get(k)
            for k in (
                "moloni_client_id",
                "moloni_client_secret",
                "moloni_company_id",
                "moloni_document_set_id",
                "moloni_credit_note_document_set_id",
                "moloni_payment_method_id",
                "moloni_maturity_date_id",
                "moloni_product_category_id",
                "moloni_unit_id",
            )
        )

    def _has_password(self):
        return bool(
            self.settings.get("moloni_username")
            and self.settings.get("moloni_password")
        )

    def _has_login(self):
        return self._has_password() or self.connection_state == "connected"

    @property
    def connection_state(self):
        """'connected', 'expired' or None — the "Connect to Moloni" flow's state."""
        if self.settings.get("moloni_connection_expired", as_type=bool, default=False):
            return "expired"
        if self.settings.get("moloni_refresh_token"):
            return "connected"
        return None

    @property
    def redirect_uri(self):
        # Shown on the settings page: it's what the admin registers in Moloni.
        from .views import callback_url

        return callback_url()

    def _store_token(self, access_token, refresh_token, expires_at):
        # self.settings is writable, which is what makes OAuth workable here: without
        # caching, every issuance would burn a fresh grant.
        self.settings.set("moloni_access_token", access_token)
        self.settings.set("moloni_refresh_token", refresh_token or "")
        self.settings.set("moloni_token_expires", str(expires_at))
        self.settings.delete("moloni_connection_expired")

    def hidden_settings_fields(self):
        if self.connection_state == "connected":
            return ("moloni_username", "moloni_password")
        return ()

    def disconnect(self):
        for key in (
            "moloni_access_token",
            "moloni_refresh_token",
            "moloni_token_expires",
            "moloni_connection_expired",
        ):
            self.settings.delete(key)

    def connect(self, code, redirect_uri):
        self._client().exchange_code(code, redirect_uri)

    def keepalive(self):
        """
        Rotate the refresh token so it never reaches its 14-day expiry, however long the
        event goes without a sale. Called daily from the periodic_task receiver.

        Returns True when the connection has just been found dead — Moloni itself refused
        the refresh and there's no password to fall back to. A network failure isn't
        that: tomorrow's run tries again, with days of slack before the token expires.
        """
        if self.connection_state != "connected":
            return False
        client = self._client()
        try:
            client.refresh()
        except ProviderUnreachable:
            logger.warning(
                "moloni: keepalive could not reach Moloni for %s", self.event
            )
            return False
        except MoloniAPIError as e:
            if client.adopt_rotated():
                # An issuance refreshed at the same moment; its new pair is stored.
                return False
            if not e.detail or self._has_password():
                logger.warning("moloni: keepalive refresh failed for %s", self.event)
                return False
            self.settings.set("moloni_connection_expired", True)
            return True
        return False

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
            reload_tokens=self._reload_tokens,
        )

    def _reload_tokens(self):
        # Straight from the database: the settings cache would hand back the very token
        # another worker just spent.
        self.settings.flush()
        return (
            self.settings.get("moloni_access_token") or None,
            self.settings.get("moloni_refresh_token") or None,
            float(self.settings.get("moloni_token_expires") or 0),
        )

    def _next_customer_number(self, client, company_id):
        # customers/insert requires a 'number' Moloni has no auto-increment for; the
        # official Moloni plugins all fetch one from getNextNumber first (confirmed in
        # moloni-pt/woocommerce's OrderCustomer::getCustomerNextNumber), falling back to a
        # random one if the call fails rather than let that break the whole issuance.
        try:
            result = client.call("customers/getNextNumber", {"company_id": company_id})
        except (MoloniAPIError, ProviderUnreachable):
            result = None
        return (result or {}).get("number") or str(random.randint(10**10, 10**11 - 1))

    def _resolve_customer_id(self, client, order, company_id):
        """
        Moloni takes only a customer_id — there is no inline client block on the document,
        so the customer has to exist first.
        """
        custom_field_is_nif = self.settings.get(
            "ptinvoicing_nif_custom_field", as_type=bool, default=False
        )
        tin = bare_tin(order, custom_field_is_nif=custom_field_is_nif)
        customer = build_customer(
            order,
            payment_method_id=self.settings.get(
                "moloni_payment_method_id", as_type=int
            ),
            maturity_date_id=self.settings.get("moloni_maturity_date_id", as_type=int),
            custom_field_is_nif=custom_field_is_nif,
            country_id=self._country_id(client, order),
        )

        # A failed lookup raises rather than falling through to insert: creating the
        # customer again is exactly how duplicates pile up.
        found = client.call(
            "customers/getByVat",
            {"company_id": company_id, "vat": tin or FINAL_CONSUMER_NIF},
        )
        matches = [
            c
            for c in (found if isinstance(found, list) else [])
            if str(c.get("vat")) == (tin or FINAL_CONSUMER_NIF) and c.get("customer_id")
        ]
        if tin:
            # Only reuse an unambiguous match: guessing which duplicate an official
            # invoice belongs to isn't something to do quietly — and inserting yet
            # another would only make it worse.
            if len(matches) > 1:
                raise MoloniAPIError(
                    _(
                        "Moloni has %(count)s customers with tax number %(vat)s. Merge "
                        "them in Moloni, then retry."
                    )
                    % {"count": len(matches), "vat": tin}
                )
            if matches:
                return matches[0]["customer_id"]
        else:
            # Every buyer without a NIF is filed under 999999990, so the name is all
            # there is to match on. Reuse the lowest id rather than creating one per
            # sale — the duplication that once bricked a Fact.pt account. Same fiscal
            # entity, same name: which duplicate is picked doesn't matter.
            same_name = sorted(
                int(c["customer_id"])
                for c in matches
                if c.get("name") == customer["name"]
            )
            if same_name:
                return same_name[0]

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

    def _country_id(self, client, order):
        """
        Moloni's own id for the buyer's country. Its ids are a list of its own, so
        anything but Portugal is looked up by ISO code — never defaulted to Portugal,
        which would put a foreign buyer on an official document as Portuguese.
        """
        ia = getattr(order, "invoice_address", None)
        code = str(ia.country).upper() if ia and ia.country else "PT"
        if code == "PT":
            return COUNTRY_PT
        for country in client.call("countries/getAll") or []:
            if str(country.get("iso_3166_1", "")).upper() == code:
                return country["country_id"]
        raise MoloniAPIError(
            _("Moloni has no country with code %(code)s.") % {"code": code}
        )

    def _resolve_product_ids(self, client, lines, company_id, tax_rate):
        """
        {line.catalog_key: Moloni product_id} for the order's lines.

        Moloni only invoices catalog products, so each pretix item — and each kind of
        fee — gets one, keyed by reference `pretix-item-<pk>` / `pretix-fee-<type>`:
        found if it already exists, created otherwise (the official WooCommerce plugin
        does the same). Item pks are unique across the whole pretix install, so events
        sharing a Moloni company don't collide.
        """
        product_ids = {}
        for line in lines:
            if line.catalog_key in product_ids:
                continue
            reference = f"pretix-{line.catalog_key}"
            # Not swallowed: a failed lookup must not fall through to creating the
            # product a second time.
            found = client.call(
                "products/getByReference",
                {"company_id": company_id, "reference": reference, "exact": 1},
            )
            matches = [
                p
                for p in (found if isinstance(found, list) else [])
                if p.get("reference") == reference
            ]
            if matches:
                product_ids[line.catalog_key] = matches[0]["product_id"]
                continue

            created = client.call(
                "products/insert",
                build_catalog_product(
                    line,
                    reference,
                    self.settings,
                    company_id,
                    tax_rate,
                ),
            )
            product_id = (created or {}).get("product_id")
            if not product_id:
                raise MoloniAPIError(_("Moloni did not return a product id."))
            product_ids[line.catalog_key] = product_id
        return product_ids

    def _check_taxes(self, client, lines, company_id):
        """
        Refuse to issue unless every line's tax can be stated truthfully; return the
        configured tax's rate (None when no line is taxed).

        Lines carry the rate pretix charged (payload.line_tax): 0% lines need an
        exemption reason, taxed lines the configured Moloni tax at the *same* rate —
        `price` is sent net and Moloni adds its rate back, so a mismatch would invoice a
        different amount than the buyer paid. Same guard as Fact.pt's
        _check_tax_rate_matches. Runs before anything is created in Moloni.
        """
        if any(not line.tax_rate for line in lines) and not self.settings.get(
            "moloni_exemption_reason"
        ):
            raise MoloniAPIError(
                _(
                    "This order has 0% VAT lines, which Moloni only accepts with an "
                    "exemption reason. Set one in the invoicing settings."
                )
            )
        taxed = [line for line in lines if line.tax_rate]
        if not taxed:
            return None

        tax_id = self.settings.get("moloni_tax_id", as_type=int)
        rate = None
        for tax in client.call("taxes/getAll", {"company_id": company_id}) or []:
            if tax.get("tax_id") == tax_id:
                rate = Decimal(str(tax.get("value")))
        if rate is None:
            raise MoloniAPIError(
                _("VAT rate %(id)s does not exist in this Moloni company.")
                % {"id": tax_id}
            )
        for line in taxed:
            if line.tax_rate != rate:
                raise MoloniAPIError(
                    _(
                        "VAT mismatch: pretix charged %(pretix)s%% on this order but the "
                        "configured Moloni rate is %(moloni)s%%. The invoice would be "
                        "issued for the wrong amount. Fix the event's tax rule or the "
                        "Moloni VAT rate setting."
                    )
                    % {"pretix": line.tax_rate, "moloni": rate}
                )
        return rate

    def issue(self, order, identifier_id):
        client = self._client()
        company_id = self.settings.get("moloni_company_id", as_type=int)
        date = timezone.now().date().isoformat()

        lines = invoice_lines(order)
        tax_rate = self._check_taxes(client, lines, company_id)
        customer_id = self._resolve_customer_id(client, order, company_id)
        product_ids = self._resolve_product_ids(client, lines, company_id, tax_rate)
        document = build_document(
            order,
            lines,
            self.settings,
            customer_id,
            identifier_id,
            date,
            product_ids,
            tax_rate,
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
            number=self.document_number(document_id),
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
        return IssuedDocument(
            document_id=str(credit_id),
            link=None,
            permanent_url=None,
            number=self.document_number(credit_id),
        )

    def document_number(self, document_id):
        # documents/getOne carries the pieces: document_type.saft_code ("FR"/"NC"),
        # document_set_name ("M2026") and number (20) — verified on a real account.
        try:
            document = self._client().call(
                "documents/getOne",
                {
                    "company_id": self.settings.get("moloni_company_id", as_type=int),
                    "document_id": int(document_id),
                },
            )
        except (MoloniAPIError, ProviderUnreachable):
            logger.warning("moloni: could not read number of document %s", document_id)
            return None
        if not isinstance(document, dict) or not document.get("number"):
            return None
        code = (document.get("document_type") or {}).get("saft_code")
        series = document.get("document_set_name")
        number = document["number"]
        return f"{code} {series}/{number}" if code and series else str(number)

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
        if not url or "?" not in url:
            raise MoloniAPIError(_("Moloni did not return a PDF link."))

        # getPDFLink's URL opens Moloni's HTML download *page*, not the file. The file
        # itself is the same query on the getDownload action — what the official
        # WooCommerce plugin's DownloadDocument.php does.
        query = url.split("?", 1)[1]
        try:
            response = requests.get(
                f"{DOWNLOAD_URL}?action=getDownload&{query}", timeout=20
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise MoloniAPIError(
                _("Could not download the document from Moloni: %(error)s")
                % {"error": e}
            )
        # Never hand anything else to the browser as application/pdf.
        if not response.content.startswith(b"%PDF"):
            raise MoloniAPIError(_("Moloni did not return a PDF."))
        return response.content

    def lookups(self, data):
        """
        Five dropdowns at once (company, the two document sets, tax, payment method),
        against credentials the admin hasn't saved yet, using the same
        {field: [{id, label}]} contract as any other provider's lookups().
        """
        typed = {
            k: (data.get(k) or "").strip()
            for k in (
                "moloni_client_id",
                "moloni_client_secret",
                "moloni_username",
                "moloni_password",
            )
        }
        if all(typed.values()):
            client = MoloniClient(
                client_id=typed["moloni_client_id"],
                client_secret=typed["moloni_client_secret"],
                username=typed["moloni_username"],
                password=typed["moloni_password"],
            )
        elif self.connection_state == "connected":
            # Connected through OAuth: the stored tokens work without a password.
            client = self._client()
        else:
            raise ProviderError(
                _("Connect to Moloni, or fill in the Moloni credentials, first.")
            )

        companies = client.call("companies/getAll") or []
        company_options = [
            {
                "id": c.get("company_id"),
                "label": " — ".join(str(v) for v in (c.get("name"), c.get("vat")) if v)
                or c.get("company_id"),
            }
            for c in companies
        ]
        # A multi-company account must pick one explicitly: defaulting to the first
        # would quietly issue official invoices under the wrong company. The blank
        # option fails the required field on Save until a real one is chosen.
        if len(company_options) > 1:
            company_options.insert(
                0, {"id": "", "label": str(_("— choose a company —"))}
            )
        fields = {"moloni_company_id": company_options}

        # Global data, not per company. The stored value is the code itself ("M07"),
        # so existing settings keep matching an option.
        try:
            exemptions = client.call("taxExemptions/getAll") or []
        except MoloniAPIError:
            exemptions = []
        if exemptions:
            fields["moloni_exemption_reason"] = [{"id": "", "label": "—"}] + [
                {"id": e.get("code"), "label": f"{e.get('code')} — {e.get('name')}"}
                for e in exemptions
            ]

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
            (
                "moloni_maturity_date_id",
                "maturityDates/getAll",
                "maturity_date_id",
                "name",
            ),
            (
                "moloni_product_category_id",
                "productCategories/getAll",
                "category_id",
                "name",
            ),
            ("moloni_unit_id", "measurementUnits/getAll", "unit_id", "name"),
        ):
            # ponytail: top-level categories only (parent_id 0); walk the tree if
            # someone needs to file tickets under a subcategory.
            extra = {"parent_id": 0} if endpoint == "productCategories/getAll" else {}
            try:
                rows = client.call(endpoint, {**payload, **extra}) or []
            except MoloniAPIError:
                continue
            fields[field] = [
                {"id": r.get(id_key), "label": r.get(label_key) or r.get(id_key)}
                for r in rows
            ]
        return fields
