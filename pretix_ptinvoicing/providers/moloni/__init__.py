import hashlib
import logging
import random
from decimal import Decimal

import requests
from django import forms
from django.utils import timezone
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SettingsForm

from ...orderdata import (
    FINAL_CONSUMER_NIF,
    MIN_REFERENCE_ROOM,
    bare_tin,
    invoice_lines,
    one_line,
    reference_prefix,
)
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

# Moloni's product references take at most this many characters; an account may be set
# lower (moloni_reference_limit), never higher.
MAX_REFERENCE_LENGTH = 30
# The top-level catalog category every event's products go under: Pretix > <event name>
# > products. Created on the first product, found by name after that.
ROOT_CATEGORY = "Pretix"
# Moloni's own category name limit isn't documented; this is safely short.
MAX_CATEGORY_NAME = 100

# products/insert's `type` (docs: 1 product, 2 service, 3 other, 4 taxes/fees).
PRODUCT_TYPES = [
    (1, _("Product")),
    (2, _("Service")),
    (3, _("Other")),
    (4, _("Taxes/fees")),
]


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
    # Its category is not a setting: products go under Pretix > <event name>, created
    # as needed (MoloniProvider._category_id).
    moloni_product_type = forms.TypedChoiceField(
        label=_("Product type (Moloni)"),
        coerce=int,
        initial=2,
        choices=[(2, _("Service")), (1, _("Product")), (3, _("Other"))],
        help_text=_(
            "How each pretix product is created in Moloni's catalog on its first sale, "
            "unless the product's own page says otherwise."
        ),
    )
    moloni_unit_id = forms.IntegerField(label=_("Unit (Moloni)"))
    moloni_reference_limit = forms.IntegerField(
        label=_("Reference limit (Moloni)"),
        required=False,
        initial=MAX_REFERENCE_LENGTH,
        min_value=MIN_REFERENCE_ROOM + 1,
        max_value=MAX_REFERENCE_LENGTH,
        # format_lazy, not %: at class-definition time no language is active.
        help_text=format_lazy(
            _(
                "Moloni product references take at most {max} characters, the prefix "
                "above included. Lower it if your account allows fewer; it can never be "
                "raised above {max}."
            ),
            max=MAX_REFERENCE_LENGTH,
        ),
    )

    def clean(self):
        cleaned = super().clean()
        limit = cleaned.get("moloni_reference_limit") or MAX_REFERENCE_LENGTH
        # The prefix is on the same page but in the generic form (unprefixed name): the
        # posted value if there is one, else what's saved.
        prefix = self.data.get("ptinvoicing_reference_prefix")
        prefix = reference_prefix(self.obj) if prefix is None else prefix.strip()
        if len(prefix) > limit - MIN_REFERENCE_ROOM:
            self.add_error(
                "moloni_reference_limit",
                _(
                    "The prefix “%(prefix)s” leaves fewer than %(room)s of the %(limit)s "
                    "characters for the product reference. Shorten the prefix."
                )
                % {"prefix": prefix, "room": MIN_REFERENCE_ROOM, "limit": limit},
            )
        return cleaned

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The payment-method table: one optional Moloni method per payment method the
        # event takes, rendered as a table (payment_methods.html), not as form rows.
        # Same dropdown options as the default (lookups), so settings.js needs nothing
        # new.
        for identifier, pp in mapped_payment_providers(self.obj):
            self.fields[payment_method_key(identifier)] = forms.IntegerField(
                label=pp.verbose_name,
                required=False,
                # Rendered by hand, not by bootstrap_form, so styled by hand.
                widget=forms.NumberInput(attrs={"class": "form-control"}),
            )

    @property
    def payment_method_rows(self):
        return [self[name] for name in self.fields if name.startswith(MAPPING_PREFIX)]

    @property
    def table_fields(self):
        # What settings.html leaves out of the plain form rows: it's in the table.
        return ",".join(f.name for f in self.payment_method_rows)

    moloni_maturity_date_id = forms.IntegerField(
        label=_("Maturity date (Moloni)"),
        help_text=_(
            "Moloni requires one on every new customer it creates for a buyer, e.g. "
            '"Pronto pagamento".'
        ),
    )


MAPPING_PREFIX = "moloni_payment_method_id_"


def mapped_payment_providers(event):
    """The payment-method table's rows: the event's active pretix payment methods —
    not "free" (nothing is paid) nor meta providers (settings holders only)."""
    return [
        (identifier, pp)
        for identifier, pp in event.get_payment_providers().items()
        if pp.is_enabled and not pp.is_meta and identifier != "free"
    ]


def payment_method_key(pretix_provider):
    """Settings key of the Moloni method a pretix payment method is recorded under."""
    return MAPPING_PREFIX + pretix_provider


class MoloniProvider(InvoiceProvider):
    identifier = "moloni"
    verbose_name = "Moloni"
    settings_form_class = MoloniSettingsForm

    # Moloni's documents/insert has no idempotency field — our_reference and your_reference
    # are free text it doesn't dedupe on. A retry after a timeout could therefore issue a
    # second official invoice, so the core must not retry on its own.
    deduplicates_issuance = False
    # What makes up for that: the document is sent with our_reference = identifier_id,
    # and invoiceReceipts/getAll (creditNotes/getAll) filters on our_reference exactly
    # (verified on a real account: "pretix-test-LHLQ9" found FR M/16, a prefix of it
    # found nothing), so find_document can tell whether it already exists.
    recovers_documents = True

    settings_template = "pretix_ptinvoicing/control/moloni_connection.html"
    logo = "pretix_ptinvoicing/logos/moloni.svg"
    lookup_triggers = ("moloni_company_id",)
    reference_limit_field = "moloni_reference_limit"
    # A draft sent discount 42.857142857 and read back 42.8571429 (7 decimals), with
    # the incidence computed from it on the 5-decimal stored price.
    discount_places = 7

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
                "moloni_unit_id",
            )
        )

    @property
    def reference_limit(self):
        limit = self.settings.get("moloni_reference_limit", as_type=int)
        return min(limit, MAX_REFERENCE_LENGTH) if limit else MAX_REFERENCE_LENGTH

    def item_fields(self):
        return {
            "moloni_type": forms.TypedChoiceField(
                label=_("Type (Moloni)"),
                coerce=int,
                required=False,
                empty_value=None,
                choices=[("", _("Event default")), *PRODUCT_TYPES],
                help_text=_(
                    "How this product is created in Moloni's catalog on its first "
                    "sale; the invoicing settings hold the event's default."
                ),
            )
        }

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

        Returns (customer_id, alternate_address_id or None): see _alternate_address_id.
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
                return self._existing(client, company_id, matches[0], customer)
        else:
            # Every buyer without a NIF is filed under 999999990, so the name is all
            # there is to match on. Reuse the lowest id rather than creating one per
            # sale — the duplication that once bricked a Fact.pt account. Same fiscal
            # entity, same name: which duplicate is picked doesn't matter.
            same_name = sorted(
                (c for c in matches if c.get("name") == customer["name"]),
                key=lambda c: int(c["customer_id"]),
            )
            if same_name:
                return self._existing(client, company_id, same_name[0], customer)

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
        return customer_id, None

    def _existing(self, client, company_id, found, customer):
        return int(found["customer_id"]), self._alternate_address_id(
            client, company_id, found, customer
        )

    def _alternate_address_id(self, client, company_id, found, customer):
        """
        The document's alternate_address_id when this order's address isn't the one on
        the reused customer, else None.

        A customer is shared — by every order with that NIF, or every final consumer of
        that name — so its own record is never overwritten with one buyer's address (and
        an invoice already issued against it keeps its own). Instead the address goes in
        as one of Moloni's customer alternate addresses, keyed by a hash of the name and
        address in `code`, so the same one is found again rather than added twice.

        Moloni prints the alternate address in the document's header *with its
        `designation` as the name line* (seen on a real FR: sending the street there
        printed the street twice and no name), so the designation is the buyer's name —
        and part of the key, since two buyers can share a NIF's customer record.
        """
        fields = ("address", "city", "zip_code", "country_id")
        if customer["address"] == "-" and customer["city"] == "-":
            # The buyer left the address blank (build_customer's placeholders): the
            # customer's own record stands — for a blank order, Moloni's "Consumidor
            # Final", printed with its "Desconhecido" address, nothing of ours added.
            return None
        if all(str(found.get(k) or "") == str(customer[k]) for k in fields):
            return None
        code = (
            "pretix-"
            + hashlib.sha1(
                "|".join(str(customer[k]) for k in ("name", *fields)).encode()
            ).hexdigest()[:10]
        )
        payload = {"company_id": company_id, "customer_id": int(found["customer_id"])}
        # Not swallowed, like every lookup here: a failure must not add a duplicate.
        for alternate in (
            client.call("customerAlternateAddresses/getAll", payload) or []
        ):
            if alternate.get("code") == code:
                return alternate["address_id"]
        created = client.call(
            "customerAlternateAddresses/insert",
            {
                **payload,
                "designation": customer["name"][:50],
                "code": code,
                **{k: customer[k] for k in fields},
            },
        )
        address_id = (created or {}).get("address_id")
        if not address_id:
            raise MoloniAPIError(_("Moloni did not return an alternate address id."))
        return address_id

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
        fee — gets one, keyed by reference: the event's prefix plus the product's
        reference from its page, else its pretix id (`pretix-item-<pk>`, fees
        `pretix-fee-<type>`). Found if it already exists, created otherwise under
        Pretix > <event name> (the official WooCommerce plugin resolves by reference the
        same way). Item pks are unique across the whole pretix install, so events sharing
        a Moloni company don't collide.
        """
        prefix = reference_prefix(self.event)
        limit = self.reference_limit
        references = {}
        for line in lines:
            reference = references.setdefault(
                line.catalog_key, prefix + line.catalog_key
            )
            # Refused up front, before any product or category is created: Moloni would
            # reject (or worse, cut) it, and the fix is on our side.
            if len(reference) > limit:
                raise MoloniAPIError(
                    _(
                        "The reference “%(reference)s” of “%(name)s” has %(length)s "
                        "characters; Moloni allows %(limit)s. Shorten the product's "
                        "reference on its page, or the prefix in the invoicing settings."
                    )
                    % {
                        "reference": reference,
                        "name": line.name,
                        "length": len(reference),
                        "limit": limit,
                    }
                )

        product_ids = {}
        category_id = None
        for line in lines:
            if line.catalog_key in product_ids:
                continue
            reference = references[line.catalog_key]
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

            if category_id is None:
                # Only when something is actually created — most orders never get here.
                category_id = self._category_id(client, company_id)
            created = client.call(
                "products/insert",
                build_catalog_product(
                    line,
                    reference,
                    category_id,
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

    def _category_id(self, client, company_id):
        """
        The category new products are filed under: Pretix > <event name>, both found by
        name or created. One tree per Moloni company, one branch per event — so the
        catalog reads by event rather than as one flat list, and nothing is configured.
        """
        root = self._find_or_create_category(client, company_id, 0, ROOT_CATEGORY)
        return self._find_or_create_category(
            client, company_id, root, one_line(self.event.name, MAX_CATEGORY_NAME)
        )

    def _find_or_create_category(self, client, company_id, parent_id, name):
        # getAll pages by 50 (its maximum): a company's "Pretix" branch can outgrow one
        # page of events. A failed lookup raises, like every lookup here — it must not
        # fall through to creating the category again.
        offset = 0
        while True:
            page = (
                client.call(
                    "productCategories/getAll",
                    {
                        "company_id": company_id,
                        "parent_id": parent_id,
                        "qty": 50,
                        "offset": offset,
                    },
                )
                or []
            )
            for category in page:
                if category.get("name") == name and category.get("category_id"):
                    return int(category["category_id"])
            if len(page) < 50:
                break
            offset += 50

        created = client.call(
            "productCategories/insert",
            {"company_id": company_id, "parent_id": parent_id, "name": name},
        )
        category_id = (created or {}).get("category_id")
        if not category_id:
            raise MoloniAPIError(_("Moloni did not return a category id."))
        return int(category_id)

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

        lines = invoice_lines(order, discount_places=self.discount_places)
        tax_rate = self._check_taxes(client, lines, company_id)
        customer_id, alternate_address_id = self._resolve_customer_id(
            client, order, company_id
        )
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
        if alternate_address_id:
            document["alternate_address_id"] = alternate_address_id
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

    def find_document(self, identifier_id, kind, since):
        """
        The closed invoice-receipt (or credit note) carrying our_reference =
        identifier_id, or None. Asked by the core before every attempt, since
        invoiceReceipts/insert has nothing it dedupes on: this is what keeps a
        timed-out insert, a stale claim or a manual retry from issuing twice.
        """
        endpoint = (
            "creditNotes/getAll" if kind == "credit_note" else "invoiceReceipts/getAll"
        )
        company_id = self.settings.get("moloni_company_id", as_type=int)
        rows = self._client().call(
            endpoint, {"company_id": company_id, "our_reference": identifier_id}
        )
        found = sorted(
            (
                row
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict)
                and row.get("our_reference") == identifier_id
                # 1 = closed/issued; a draft (0) is not a document anyone received.
                and row.get("status") == 1
                and row.get("document_id")
            ),
            key=lambda row: row["document_id"],
        )
        if not found:
            return None
        if len(found) > 1:
            logger.warning(
                "moloni: %s documents share our_reference %s; adopting the first",
                len(found),
                identifier_id,
            )
        document_id = found[0]["document_id"]
        return IssuedDocument(
            document_id=str(document_id),
            link=None,
            permanent_url=None,
            number=self.document_number(document_id),
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
        # The payment-method table (MoloniSettingsForm.__init__) shares the default's
        # options. Its rows come from the event, not from `data`: settings.js only
        # posts fields that have a value, so empty rows would never get a dropdown.
        mapping = [
            payment_method_key(identifier)
            for identifier, _pp in mapped_payment_providers(self.event)
        ]
        fetched = {}
        for field, endpoint, id_key, label_key in (
            *(
                (k, "paymentMethods/getAll", "payment_method_id", "name")
                for k in mapping
            ),
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
            ("moloni_unit_id", "measurementUnits/getAll", "unit_id", "name"),
        ):
            # Several fields share an endpoint (both document sets, the payment
            # methods): one call each.
            if endpoint not in fetched:
                try:
                    fetched[endpoint] = client.call(endpoint, payload) or []
                except MoloniAPIError:
                    fetched[endpoint] = None
            rows = fetched[endpoint]
            if rows is None:
                continue
            fields[field] = [
                {"id": r.get(id_key), "label": r.get(label_key) or r.get(id_key)}
                for r in rows
            ]
        for field in mapping:
            # Empty = the default method. Without this first option the dropdown would
            # preselect a real method, and the next Save would store it unasked.
            if field in fields:
                fields[field].insert(0, {"id": "", "label": str(_("— default —"))})
        return fields
