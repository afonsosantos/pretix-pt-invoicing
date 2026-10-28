import json
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses
from django.core import mail as django_mail
from django_scopes import scopes_disabled
from pretix.base.models import InvoiceAddress, Order

from pretix_ptinvoicing.models import IssuedInvoice
from pretix_ptinvoicing.providers.moloni import MoloniProvider
from pretix_ptinvoicing.providers.moloni.client import MoloniAPIError, MoloniClient
from pretix_ptinvoicing.signals import ptinvoicing_keepalive
from pretix_ptinvoicing.tasks import issue_credit_note, issue_invoice

GRANT = "https://api.moloni.pt/v1/grant/"
API = "https://api.moloni.pt/v1/{}/"

# What build_customer sends for a buyer with no address: a customer stored with it needs
# no alternate address.
STORED = {"address": "-", "city": "-", "zip_code": "1000-001", "country_id": 1}


def mock_grant(access="tok-1", refresh="ref-1", expires_in=3600):
    responses.add(
        responses.GET,
        GRANT,
        json={
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": expires_in,
            "token_type": "bearer",
        },
        status=200,
    )
    mock_no_existing_document()


def mock_no_existing_document():
    # Every Moloni attempt first asks whether the document already exists
    # (find_document, by our_reference): nothing there, go ahead and issue.
    for endpoint in ("invoiceReceipts/getAll", "creditNotes/getAll"):
        responses.add(responses.POST, API.format(endpoint), json=[], status=200)


def mock_existing_product(product_id=12):
    # Every pretix item already has its catalog product: getByReference echoes it back.
    def found(request):
        reference = json.loads(request.body)["reference"]
        return 200, {}, json.dumps([{"product_id": product_id, "reference": reference}])

    responses.add_callback(
        responses.POST, API.format("products/getByReference"), callback=found
    )


def mock_no_customer():
    # Every issuance looks the buyer up first — by NIF, or 999999990 without one.
    responses.add(responses.POST, API.format("customers/getByVat"), json=[], status=200)


def mock_taxes(rate, tax_id=11):
    responses.add(
        responses.POST,
        API.format("taxes/getAll"),
        json=[{"tax_id": tax_id, "name": f"IVA {rate}%", "value": rate}],
        status=200,
    )


def sent_to(endpoint):
    """The JSON body of the last call to `endpoint`."""
    calls = [c for c in responses.calls if f"/{endpoint}/" in c.request.url]
    return json.loads(calls[-1].request.body)


@pytest.fixture
def moloni_event(event, order):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "moloni"
        event.settings.moloni_client_id = "cid"
        event.settings.moloni_client_secret = "secret"
        event.settings.moloni_username = "user"
        event.settings.moloni_password = "pass"
        event.settings.moloni_company_id = 7
        event.settings.moloni_document_set_id = 3
        event.settings.moloni_credit_note_document_set_id = 4
        event.settings.moloni_tax_id = 11
        event.settings.moloni_exemption_reason = "M07"
        event.settings.moloni_payment_method_id = 5
        event.settings.moloni_maturity_date_id = 9
        event.settings.moloni_unit_id = 1
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    return event


@pytest.mark.django_db
@responses.activate
def test_issues_through_the_generic_core(moloni_event, order, position):
    mock_grant()
    mock_no_customer()
    mock_existing_product()
    responses.add(
        responses.POST,
        API.format("customers/getNextNumber"),
        json={"number": "100"},
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("customers/insert"),
        json={"customer_id": 42},
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("invoiceReceipts/insert"),
        json={"document_id": 900, "public_link": "https://moloni.pt/d/900"},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.provider == "moloni"
    assert invoice.status == IssuedInvoice.STATUS_SUCCESS
    assert invoice.document_id == "900"
    assert invoice.permanent_url == "https://moloni.pt/d/900"

    customer_sent = sent_to("customers/insert")
    assert customer_sent["number"] == "100"
    # Required by customers/insert, and rejected by a real account when absent.
    assert customer_sent["maturity_date_id"] == 9
    assert customer_sent["payment_method_id"] == 5
    for field in (
        "salesman_id",
        "payment_day",
        "discount",
        "credit_limit",
        "delivery_method_id",
    ):
        assert customer_sent[field] == 0

    sent = sent_to("invoiceReceipts/insert")
    assert sent["company_id"] == 7
    assert sent["customer_id"] == 42
    assert sent["document_set_id"] == 3
    assert sent["our_reference"] == "pretix-dummy-FOOBAR"
    assert sent["products"][0]["price"] == 23.0
    # Required: Moloni only invoices catalog products (rejected by a real account).
    assert sent["products"][0]["product_id"] == 12
    assert sent_to("products/getByReference")["reference"] == (
        f"pretix-item-{position.item_id}"
    )
    # A 0% line (the fixture's position): no tax — Moloni rejects a 0 rate — but the
    # exemption reason instead.
    assert "taxes" not in sent["products"][0]
    assert sent["products"][0]["exemption_reason"] == "M07"
    assert sent["payments"][0]["payment_method_id"] == 5


@pytest.mark.django_db
@responses.activate
def test_existing_customer_is_reused_by_vat(moloni_event, order, position):
    with scopes_disabled():
        InvoiceAddress.objects.create(order=order, vat_id="PT237892294", country="PT")
    order.refresh_from_db()

    mock_grant()
    mock_existing_product()
    responses.add(
        responses.POST,
        API.format("customers/getByVat"),
        json=[{"customer_id": 88, "vat": "237892294", **STORED}],
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("invoiceReceipts/insert"),
        json={"document_id": 901},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    called = [c.request.url for c in responses.calls]
    assert not any("customers/insert" in url for url in called)
    document = sent_to("invoiceReceipts/insert")
    assert document["customer_id"] == 88
    assert "alternate_address_id" not in document


def _issue_to_moved_customer(event, order, alternates):
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order,
            vat_id="PT237892294",
            name_parts={"_scheme": "full", "full_name": "Ana Silva"},
            country="PT",
            street="Rua Nova 1",
            zipcode="4000-001",
            city="Porto",
        )
    order.refresh_from_db()
    mock_grant()
    mock_existing_product()
    responses.add(
        responses.POST,
        API.format("customers/getByVat"),
        json=[{"customer_id": 88, "vat": "237892294", **STORED}],
    )
    responses.add(
        responses.POST,
        API.format("customerAlternateAddresses/getAll"),
        json=alternates,
    )
    responses.add(
        responses.POST,
        API.format("customerAlternateAddresses/insert"),
        json={"valid": 1, "address_id": 5},
    )
    responses.add(
        responses.POST, API.format("invoiceReceipts/insert"), json={"document_id": 1}
    )
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})


@pytest.mark.django_db
@responses.activate
def test_new_address_goes_in_as_alternate_address(moloni_event, order, position):
    # The customer is shared by every order with that NIF: never overwritten.
    _issue_to_moved_customer(moloni_event, order, [])

    assert not _inserted("customers/update")
    alternate = sent_to("customerAlternateAddresses/insert")
    assert alternate["customer_id"] == 88
    assert alternate["address"] == "Rua Nova 1"
    assert alternate["zip_code"] == "4000-001"
    # Printed by Moloni as the name line of the address: a real FR M/8 showed the
    # street twice when the street was sent here.
    assert alternate["designation"] == "Ana Silva"
    assert alternate["code"].startswith("pretix-")
    assert sent_to("invoiceReceipts/insert")["alternate_address_id"] == 5


@pytest.mark.django_db
@responses.activate
def test_known_alternate_address_is_reused(moloni_event, order, position):
    _issue_to_moved_customer(moloni_event, order, [])
    code = sent_to("customerAlternateAddresses/insert")["code"]
    responses.reset()

    with scopes_disabled():
        IssuedInvoice.objects.all().delete()
        order.invoice_address.delete()
    _issue_to_moved_customer(moloni_event, order, [{"address_id": 9, "code": code}])

    assert not _inserted("customerAlternateAddresses/insert")
    assert sent_to("invoiceReceipts/insert")["alternate_address_id"] == 9


@pytest.mark.django_db
@responses.activate
def test_api_rejection_is_terminal(moloni_event, order, position):
    mock_grant()
    mock_no_customer()
    responses.add(
        responses.POST,
        API.format("customers/insert"),
        json={"error": "invalid_vat", "errors": {"vat": "Invalid"}},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert invoice.error_detail == {"vat": "Invalid"}


@pytest.mark.django_db
@responses.activate
def test_token_is_cached_in_event_settings(moloni_event, order, position):
    mock_grant(access="tok-cached")
    mock_no_customer()
    mock_existing_product()
    responses.add(
        responses.POST,
        API.format("customers/insert"),
        json={"customer_id": 1},
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("invoiceReceipts/insert"),
        json={"document_id": 902},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    with scopes_disabled():
        moloni_event.settings.flush()
        assert moloni_event.settings.get("moloni_access_token") == "tok-cached"
        assert moloni_event.settings.get("moloni_refresh_token") == "ref-1"
        assert float(moloni_event.settings.get("moloni_token_expires")) > time.time()


@responses.activate
def test_client_refreshes_an_expired_token_before_falling_back():
    # An access token that's already expired must be renewed with the refresh token, not
    # by burning another password grant.
    grants = []

    def record(request):
        grants.append(request.url)
        return (
            200,
            {},
            json.dumps(
                {"access_token": "new", "expires_in": 3600, "refresh_token": "ref-2"}
            ),
        )

    responses.add_callback(responses.GET, GRANT, callback=record)

    client = MoloniClient(
        client_id="cid",
        client_secret="secret",
        username="user",
        password="pass",
        access_token="old",
        refresh_token="ref-1",
        expires_at=time.time() - 10,
    )
    assert client.token() == "new"
    assert "grant_type=refresh_token" in grants[0]
    assert len(grants) == 1


@responses.activate
def test_client_reports_bad_credentials_as_a_provider_error():
    responses.add(
        responses.GET,
        GRANT,
        json={"error": "invalid_client", "error_description": "Bad credentials"},
        status=200,
    )
    client = MoloniClient(
        client_id="cid", client_secret="bad", username="u", password="p"
    )
    with pytest.raises(MoloniAPIError) as excinfo:
        client.token()
    assert "Bad credentials" in str(excinfo.value)


@responses.activate
def test_client_rejects_non_object_grant_response():
    responses.add(responses.GET, GRANT, json=["unexpected"], status=200)
    client = MoloniClient(
        client_id="cid", client_secret="secret", username="u", password="p"
    )

    with pytest.raises(MoloniAPIError, match="Invalid response"):
        client.token()


@pytest.mark.django_db
@responses.activate
def test_credit_note_fetches_the_original_then_inserts_a_credit_note(
    moloni_event, order, position
):
    with scopes_disabled():
        IssuedInvoice.objects.create(
            order=order,
            provider="moloni",
            identifier_id="pretix-dummy-FOOBAR",
            kind=IssuedInvoice.KIND_INVOICE,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="900",
        )

    mock_grant()
    responses.add(
        responses.POST,
        API.format("documents/getOne"),
        json={
            "document_id": 900,
            "customer_id": 42,
            "gross_value": 23.0,
            "your_reference": "FOOBAR",
        },
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("documents/getUnrelatedProducts"),
        json=[
            {
                "product_id": 3,
                "document_product_id": 5,
                "name": "Ticket",
                "qty": 1,
                "price": 23.0,
                "discount": 0,
                "taxes": [{"tax_id": 11, "value": 23}],
            }
        ],
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("creditNotes/insert"),
        json={"document_id": 950},
        status=200,
    )

    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    sent = sent_to("creditNotes/insert")
    assert sent["customer_id"] == 42
    assert sent["document_set_id"] == 4
    # Invoice-receipts are a self-paid Moloni document type — the credited value is 0,
    # not the document's total.
    assert sent["associated_documents"] == [{"associated_id": 900, "value": 0}]
    # related_id maps to the *document's* line id (document_product_id), not product_id.
    assert sent["products"][0]["product_id"] == 3
    assert sent["products"][0]["related_id"] == 5
    # The rate is required next to tax_id, as on invoices.
    assert sent["products"][0]["taxes"] == [
        {"tax_id": 11, "value": 23, "order": 0, "cumulative": 0}
    ]

    with scopes_disabled():
        credit_note = IssuedInvoice.objects.get(kind=IssuedInvoice.KIND_CREDIT_NOTE)
    assert credit_note.status == IssuedInvoice.STATUS_SUCCESS
    assert credit_note.document_id == "950"


@pytest.mark.django_db
def test_moloni_declares_it_cannot_deduplicate(event):
    # The whole reason deduplicates_issuance exists: documents/insert has no such field.
    assert MoloniProvider(event).deduplicates_issuance is False


@pytest.mark.django_db
@responses.activate
def test_net_price_is_sent(moloni_event, order, item):
    from pretix.base.models import OrderPosition

    with scopes_disabled():
        OrderPosition.objects.create(
            order=order,
            item=item,
            price=Decimal("18.45"),
            tax_rate=Decimal("23.00"),
            tax_value=Decimal("3.45"),
        )

    mock_grant()
    mock_no_customer()
    mock_existing_product()
    mock_taxes(rate=23)
    responses.add(
        responses.POST,
        API.format("customers/insert"),
        json={"customer_id": 1},
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("invoiceReceipts/insert"),
        json={"document_id": 903},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    sent = sent_to("invoiceReceipts/insert")
    assert sent["products"][0]["price"] == 15.0  # 18.45 / 1.23
    # A taxed line carries the rate: Moloni rejected line taxes without `value`.
    assert sent["products"][0]["taxes"] == [
        {"tax_id": 11, "value": 23.0, "order": 0, "cumulative": 0}
    ]
    assert "exemption_reason" not in sent["products"][0]


@pytest.mark.django_db
@responses.activate
def test_tax_rate_mismatch_is_refused_before_touching_moloni(moloni_event, order, item):
    from pretix.base.models import OrderPosition

    with scopes_disabled():
        OrderPosition.objects.create(
            order=order,
            item=item,
            price=Decimal("18.45"),
            tax_rate=Decimal("23.00"),
            tax_value=Decimal("3.45"),
        )
    mock_grant()
    mock_taxes(rate=6)

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert "VAT mismatch" in invoice.error_message
    # Nothing was created in Moloni: no customer, no product, no document.
    assert not [c for c in responses.calls if "/insert/" in c.request.url]


@pytest.mark.django_db
@responses.activate
def test_zero_vat_line_without_exemption_reason_is_refused(
    moloni_event, order, position
):
    with scopes_disabled():
        moloni_event.settings.delete("moloni_exemption_reason")
    mock_grant()

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert "exemption reason" in invoice.error_message


@pytest.mark.django_db
@responses.activate
def test_missing_catalog_product_is_created_once_per_item(
    moloni_event, order, item, position
):
    from pretix.base.models import OrderPosition

    # A second ticket of the same item: one catalog product, not two.
    with scopes_disabled():
        OrderPosition.objects.create(
            order=order, item=item, price=Decimal("23.00"), tax_value=Decimal(0)
        )

    mock_grant()
    mock_no_customer()
    responses.add(
        responses.POST, API.format("products/getByReference"), json=[], status=200
    )
    responses.add(
        responses.POST,
        API.format("taxes/getAll"),
        json=[{"tax_id": 11, "value": 23}],
        status=200,
    )
    # No "Pretix" category yet: both levels of the tree get created.
    mock_categories({})
    responses.add(
        responses.POST,
        API.format("products/insert"),
        json={"valid": 1, "product_id": 77},
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("customers/insert"),
        json={"customer_id": 1},
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("invoiceReceipts/insert"),
        json={"document_id": 904},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    inserts = [c for c in responses.calls if "/products/insert/" in c.request.url]
    assert len(inserts) == 1
    created = json.loads(inserts[0].request.body)
    assert created["reference"] == f"pretix-item-{item.pk}"
    assert created["unit_id"] == 1
    assert created["type"] == 2  # service, the default
    assert created["exemption_reason"] == "M07"  # 0% lines, like the item's sales
    assert "taxes" not in created
    # Filed under Pretix (top level, created first) > the event's name.
    categories = [
        json.loads(c.request.body)
        for c in responses.calls
        if "/productCategories/insert/" in c.request.url
    ]
    assert [(c["parent_id"], c["name"]) for c in categories] == [
        (0, "Pretix"),
        (CATEGORY_IDS["Pretix"], "Dummy"),
    ]
    assert created["category_id"] == CATEGORY_IDS["Dummy"]
    lines = sent_to("invoiceReceipts/insert")["products"]
    # Two identical tickets: one line, qty 2.
    assert [(line["product_id"], line["qty"]) for line in lines] == [(77, 2)]


# What mock_categories hands out for each created category, by name.
CATEGORY_IDS = {"Pretix": 500, "Dummy": 501}


def mock_categories(existing):
    """
    productCategories/getAll and /insert against a company whose categories are
    `existing`: {parent_id: [{"category_id", "name"}, ...]}.
    """

    def get_all(request):
        body = json.loads(request.body)
        return 200, {}, json.dumps(existing.get(body["parent_id"], []))

    def insert(request):
        body = json.loads(request.body)
        return (
            200,
            {},
            json.dumps({"valid": 1, "category_id": CATEGORY_IDS[body["name"]]}),
        )

    responses.add_callback(
        responses.POST, API.format("productCategories/getAll"), callback=get_all
    )
    responses.add_callback(
        responses.POST, API.format("productCategories/insert"), callback=insert
    )


def _issue_creating_product(order, event):
    mock_grant()
    mock_no_customer()
    responses.add(
        responses.POST, API.format("products/getByReference"), json=[], status=200
    )
    responses.add(
        responses.POST,
        API.format("products/insert"),
        json={"valid": 1, "product_id": 77},
        status=200,
    )
    responses.add(
        responses.POST, API.format("customers/insert"), json={"customer_id": 1}
    )
    responses.add(
        responses.POST, API.format("invoiceReceipts/insert"), json={"document_id": 1}
    )
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})


@pytest.mark.django_db
@responses.activate
def test_existing_category_tree_is_reused(moloni_event, order, item, position):
    # Pretix > Dummy already exist (an earlier sale, or made by hand): found by name,
    # nothing inserted — a second "Dummy" branch would scatter the event's products.
    mock_categories(
        {
            0: [
                {"category_id": 3, "name": "Bilhetes"},
                {"category_id": 40, "name": "Pretix"},
            ],
            40: [{"category_id": 41, "name": "Dummy"}],
        }
    )
    _issue_creating_product(order, moloni_event)

    assert sent_to("products/insert")["category_id"] == 41
    assert not [
        c for c in responses.calls if "/productCategories/insert/" in c.request.url
    ]
    looked_up = [
        json.loads(c.request.body)
        for c in responses.calls
        if "/productCategories/getAll/" in c.request.url
    ]
    assert [c["parent_id"] for c in looked_up] == [0, 40]


@pytest.mark.django_db
@responses.activate
def test_product_page_reference_and_type_are_used(moloni_event, order, item, position):
    from pretix_ptinvoicing.models import ItemSettings

    with scopes_disabled():
        ItemSettings.objects.create(
            item=item, reference="bilhete-geral", data={"moloni_type": 1}
        )
        moloni_event.settings.ptinvoicing_reference_prefix = "ev26-"
    mock_categories({})
    _issue_creating_product(order, moloni_event)

    assert sent_to("products/getByReference")["reference"] == "ev26-bilhete-geral"
    created = sent_to("products/insert")
    assert created["reference"] == "ev26-bilhete-geral"
    assert created["type"] == 1  # the product's own, not the event's default (2)


@pytest.mark.django_db
@responses.activate
def test_catalog_product_is_named_after_the_item_not_the_line(
    moloni_event, order, item, position
):
    # An itemized line is "Regular ticket — Ana"; the catalog product it creates must
    # not be, or Moloni prints "— Ana" under every later buyer's line (real FR M/11).
    with scopes_disabled():
        position.attendee_name_parts = {"_legacy": "Ana Silva"}
        position.save()
        order.meta_info = '{"contact_form_data": {"ptinvoicing_itemize": true}}'
        order.save()
    mock_categories({})
    _issue_creating_product(order, moloni_event)

    assert sent_to("products/insert")["name"] == "Ticket"
    assert sent_to("invoiceReceipts/insert")["products"][0]["name"] == (
        "Ticket — Ana Silva"
    )


@pytest.mark.django_db
@responses.activate
def test_an_empty_prefix_is_respected(moloni_event, order, item, position):
    with scopes_disabled():
        moloni_event.settings.ptinvoicing_reference_prefix = ""
    mock_categories({})
    _issue_creating_product(order, moloni_event)
    assert sent_to("products/insert")["reference"] == f"item-{item.pk}"


@pytest.mark.django_db
@responses.activate
def test_over_long_reference_is_refused_before_touching_the_catalog(
    moloni_event, order, item, position
):
    from pretix_ptinvoicing.models import ItemSettings

    # Set within the limit, then the limit was lowered (or the prefix lengthened).
    with scopes_disabled():
        ItemSettings.objects.create(item=item, reference="bilhete-geral-dia-1")
        moloni_event.settings.moloni_reference_limit = 20
    mock_grant()
    mock_no_customer()
    responses.add(
        responses.POST, API.format("customers/insert"), json={"customer_id": 1}
    )
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert "pretix-bilhete-geral-dia-1" in invoice.error_message
    assert "20" in invoice.error_message
    assert not [c for c in responses.calls if "/products/" in c.request.url]
    assert not [c for c in responses.calls if "/productCategories/" in c.request.url]


@pytest.mark.django_db
def test_reference_limit_never_exceeds_moloni_s_thirty(event):
    provider = MoloniProvider(event)
    assert provider.reference_limit == 30
    event.settings.moloni_reference_limit = 20
    assert provider.reference_limit == 20
    event.settings.moloni_reference_limit = 45  # an old or hand-edited value
    assert provider.reference_limit == 30


@pytest.mark.django_db
def test_settings_refuse_a_prefix_that_leaves_no_room(logged_in_client, moloni_event):
    url = f"/control/event/{moloni_event.organizer.slug}/{moloni_event.slug}/invoicing/settings/"
    data = {
        "ptinvoicing_provider": "moloni",
        "ptinvoicing_reference_prefix": "festival-de-verao-2026-",
        "moloni-moloni_client_id": "cid",
        "moloni-moloni_client_secret": "secret",
        "moloni-moloni_username": "user",
        "moloni-moloni_password": "pass",
        "moloni-moloni_company_id": "7",
        "moloni-moloni_document_set_id": "3",
        "moloni-moloni_credit_note_document_set_id": "4",
        "moloni-moloni_payment_method_id": "5",
        "moloni-moloni_maturity_date_id": "9",
        "moloni-moloni_product_type": "2",
        "moloni-moloni_unit_id": "1",
        "moloni-moloni_reference_limit": "30",
    }
    response = logged_in_client.post(url, data)
    assert response.status_code == 200
    assert "leaves fewer than" in response.content.decode()
    with scopes_disabled():
        moloni_event.settings.flush()
        assert moloni_event.settings.get("ptinvoicing_reference_prefix") is None

    # Nothing above 30, however the account is set.
    data["ptinvoicing_reference_prefix"] = "pretix-"
    data["moloni-moloni_reference_limit"] = "40"
    assert logged_in_client.post(url, data).status_code == 200

    data["moloni-moloni_reference_limit"] = "25"
    assert logged_in_client.post(url, data).status_code == 302
    with scopes_disabled():
        moloni_event.settings.flush()
        assert moloni_event.settings.get("moloni_reference_limit", as_type=int) == 25
        assert moloni_event.settings.get("ptinvoicing_reference_prefix") == "pretix-"


def _issue_with_customers(order, event, found, address=None):
    """Issue against a Moloni that already holds `found` customers for the lookup."""
    if address:
        with scopes_disabled():
            InvoiceAddress.objects.create(order=order, **address)
        order.refresh_from_db()
    mock_grant()
    mock_existing_product()
    responses.add(
        responses.POST, API.format("customers/getByVat"), json=found, status=200
    )
    responses.add(
        responses.POST,
        API.format("countries/getAll"),
        json=[
            {"country_id": 1, "iso_3166_1": "pt"},
            {"country_id": 4, "iso_3166_1": "de"},
        ],
        status=200,
    )
    responses.add(
        responses.POST, API.format("customers/insert"), json={"customer_id": 50}
    )
    responses.add(
        responses.POST, API.format("invoiceReceipts/insert"), json={"document_id": 1}
    )
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})


def _inserted(endpoint):
    return [c for c in responses.calls if f"/{endpoint}/" in c.request.url]


@pytest.mark.django_db
@responses.activate
def test_blank_billing_data_is_just_consumidor_final(moloni_event, order, position):
    # L0YCM on a real account: nothing filled in, issued to Moloni's own "Consumidor
    # Final", stored with its "Desconhecido" address — no alternate address of ours.
    _issue_with_customers(
        order,
        moloni_event,
        [
            {
                "customer_id": 161091328,
                "vat": "999999990",
                "name": "Consumidor Final",
                "address": "Desconhecido",
                "city": "Desconhecido",
                "zip_code": "0000-000",
                "country_id": 1,
            }
        ],
    )

    assert not _inserted("customers/insert")
    assert not _inserted("customerAlternateAddresses/getAll")
    document = sent_to("invoiceReceipts/insert")
    assert document["customer_id"] == 161091328
    assert "alternate_address_id" not in document


@pytest.mark.django_db
@responses.activate
def test_buyers_without_nif_reuse_the_final_consumer_with_their_name(
    moloni_event, order, position
):
    # One customer per sale is what once bricked a Fact.pt account.
    _issue_with_customers(
        order,
        moloni_event,
        [
            {
                "customer_id": 31,
                "vat": "999999990",
                "name": "Consumidor Final",
                **STORED,
            },
            {
                "customer_id": 30,
                "vat": "999999990",
                "name": "Consumidor Final",
                **STORED,
            },
            {"customer_id": 29, "vat": "999999990", "name": "Someone else", **STORED},
        ],
    )

    assert sent_to("customers/getByVat")["vat"] == "999999990"
    assert not _inserted("customers/insert")
    assert sent_to("invoiceReceipts/insert")["customer_id"] == 30


@pytest.mark.django_db
@responses.activate
def test_several_customers_with_one_nif_stop_issuance(moloni_event, order, position):
    _issue_with_customers(
        order,
        moloni_event,
        [
            {"customer_id": 1, "vat": "237892294"},
            {"customer_id": 2, "vat": "237892294"},
        ],
        address={"vat_id": "PT237892294", "country": "PT"},
    )

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert "Merge them" in invoice.error_message
    assert not _inserted("customers/insert")
    assert not _inserted("invoiceReceipts/insert")


@pytest.mark.django_db
@responses.activate
def test_a_foreign_buyer_gets_their_own_country(moloni_event, order, position):
    _issue_with_customers(
        order,
        moloni_event,
        [],
        address={"company": "GmbH", "vat_id": "DE123456789", "country": "DE"},
    )

    customer = sent_to("customers/insert")
    assert customer["country_id"] == 4
    assert customer["vat"] == "DE123456789"


@pytest.mark.django_db
@responses.activate
def test_a_failed_customer_lookup_does_not_create_another(
    moloni_event, order, position
):
    mock_grant()
    responses.add(
        responses.POST,
        API.format("customers/getByVat"),
        json={"error": "boom"},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    assert not _inserted("customers/insert")
    with scopes_disabled():
        assert (
            IssuedInvoice.objects.get(order=order).status == IssuedInvoice.STATUS_ERROR
        )


@pytest.mark.django_db
@responses.activate
def test_fees_are_invoiced_as_their_own_catalog_product(moloni_event, order, position):
    from pretix.base.models import OrderFee

    with scopes_disabled():
        OrderFee.objects.create(
            order=order,
            fee_type=OrderFee.FEE_TYPE_SERVICE,
            value=Decimal("2.00"),
            tax_rate=Decimal("0.00"),
            tax_value=Decimal("0.00"),
        )
        order.total = Decimal("25.00")
        order.save(update_fields=["total"])
    _issue_with_customers(order, moloni_event, [])

    sent = sent_to("invoiceReceipts/insert")
    assert [p["price"] for p in sent["products"]] == [23.0, 2.0]
    references = [
        json.loads(c.request.body)["reference"]
        for c in _inserted("products/getByReference")
    ]
    assert references == [f"pretix-item-{position.item_id}", "pretix-fee-service"]
    # The payment and the lines now add up to the same total.
    assert sent["payments"][0]["value"] == 25.0


@responses.activate
def test_a_refresh_token_rotated_by_another_worker_is_adopted():
    # Two workers refresh at once: Moloni rotates the token, so the loser's has just
    # been spent. It must pick up the pair the winner stored, not declare the
    # connection dead.
    responses.add(responses.GET, GRANT, json={"error": "invalid_grant"}, status=400)
    client = MoloniClient(
        "cid",
        "secret",
        refresh_token="ref-spent",
        reload_tokens=lambda: ("tok-winner", "ref-winner", time.time() + 3000),
    )

    assert client.token() == "tok-winner"
    assert client.refresh_token == "ref-winner"


CONNECT_URL = "/control/event/{}/{}/invoicing/settings/moloni/connect/"
DISCONNECT_URL = "/control/event/{}/{}/invoicing/settings/moloni/disconnect/"
CALLBACK_URL = "/control/ptinvoicing/moloni/callback/"


@pytest.fixture
def oauth_event(moloni_event):
    # Connected through "Connect to Moloni": no username/password stored.
    with scopes_disabled():
        moloni_event.settings.delete("moloni_username")
        moloni_event.settings.delete("moloni_password")
        # The keepalive only visits events with the plugin enabled.
        moloni_event.plugins = "pretix_ptinvoicing"
        moloni_event.save(update_fields=["plugins"])
    return moloni_event


def _start_connect(client, event):
    response = client.post(
        CONNECT_URL.format(event.organizer.slug, event.slug),
        {"moloni-moloni_client_id": "typed-cid"},
    )
    assert response.status_code == 302
    location = response["Location"]
    assert location.startswith("https://www.moloni.pt/ac/root/oauth/?")
    query = parse_qs(urlparse(location).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["typed-cid"]
    assert query["redirect_uri"][0].endswith(CALLBACK_URL)
    return query["state"][0]


@pytest.mark.django_db
@responses.activate
def test_connect_flow_stores_tokens_and_configures_without_password(
    logged_in_client, oauth_event
):
    assert not MoloniProvider(oauth_event).is_configured
    state = _start_connect(logged_in_client, oauth_event)
    mock_grant(access="tok-code", refresh="ref-code")

    response = logged_in_client.get(CALLBACK_URL, {"code": "abc", "state": state})

    assert response.status_code == 302
    # Back on the settings page with the Moloni card selected, not the saved provider.
    assert response["Location"].endswith("/invoicing/settings/?provider=moloni")
    grant = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert grant["grant_type"] == ["authorization_code"]
    assert grant["code"] == ["abc"]
    assert grant["client_id"] == ["typed-cid"]
    assert grant["redirect_uri"][0].endswith(CALLBACK_URL)
    with scopes_disabled():
        oauth_event.settings.flush()
        assert oauth_event.settings.get("moloni_refresh_token") == "ref-code"
        provider = MoloniProvider(oauth_event)
        assert provider.connection_state == "connected"
        assert provider.is_configured


@pytest.mark.django_db
@responses.activate
def test_callback_rejects_a_wrong_state_or_no_pending_flow(
    logged_in_client, oauth_event
):
    _start_connect(logged_in_client, oauth_event)
    assert (
        logged_in_client.get(CALLBACK_URL, {"code": "x", "state": "forged"}).status_code
        == 404
    )
    # The pending flow was consumed: replaying with no state is refused too.
    assert logged_in_client.get(CALLBACK_URL, {"code": "x"}).status_code == 404
    assert not responses.calls


@pytest.mark.django_db
@responses.activate
def test_callback_without_state_is_accepted_from_the_session(
    logged_in_client, oauth_event
):
    # Moloni's docs don't mention echoing `state`; the session alone must suffice.
    _start_connect(logged_in_client, oauth_event)
    mock_grant()
    assert logged_in_client.get(CALLBACK_URL, {"code": "abc"}).status_code == 302
    with scopes_disabled():
        oauth_event.settings.flush()
        assert MoloniProvider(oauth_event).connection_state == "connected"


@pytest.mark.django_db
def test_disconnect_clears_tokens(logged_in_client, oauth_event):
    with scopes_disabled():
        oauth_event.settings.moloni_refresh_token = "ref"
        oauth_event.settings.moloni_access_token = "tok"
    url = DISCONNECT_URL.format(oauth_event.organizer.slug, oauth_event.slug)
    # Asked first: it stops issuance.
    confirm = logged_in_client.get(url)
    assert confirm.status_code == 200
    assert b"until you connect again" in confirm.content
    with scopes_disabled():
        oauth_event.settings.flush()
        assert MoloniProvider(oauth_event).connection_state == "connected"

    logged_in_client.post(url)
    with scopes_disabled():
        oauth_event.settings.flush()
        assert MoloniProvider(oauth_event).connection_state is None
        assert not oauth_event.settings.get("moloni_access_token")


@pytest.mark.django_db
def test_connect_keeps_everything_typed_but_not_the_provider_choice(
    logged_in_client, oauth_event
):
    # The OAuth round-trip leaves the page: anything typed but unsaved used to be lost.
    with scopes_disabled():
        oauth_event.settings.ptinvoicing_provider = "factpt"
    logged_in_client.post(
        CONNECT_URL.format(oauth_event.organizer.slug, oauth_event.slug),
        {
            "ptinvoicing_provider": "moloni",
            "ptinvoicing_email_invoice": "on",
            "moloni-moloni_client_id": "typed-cid",
            "moloni-moloni_client_secret": "typed-secret",
            "moloni-moloni_exemption_reason": "M05",
        },
    )
    with scopes_disabled():
        oauth_event.settings.flush()
        s = oauth_event.settings
        assert s.get("moloni_client_secret") == "typed-secret"
        assert s.get("moloni_exemption_reason") == "M05"
        assert s.get("ptinvoicing_email_invoice", as_type=bool) is True
        # Connecting a provider isn't choosing it.
        assert s.get("ptinvoicing_provider") == "factpt"


@pytest.mark.django_db
@responses.activate
def test_keepalive_rotates_the_refresh_token(oauth_event):
    with scopes_disabled():
        oauth_event.settings.moloni_refresh_token = "ref-old"
        oauth_event.settings.moloni_access_token = "tok-old"
        oauth_event.settings.moloni_token_expires = str(time.time() + 3000)
    mock_grant(access="tok-new", refresh="ref-new")

    ptinvoicing_keepalive.__wrapped__(sender=None)

    grant = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert grant["grant_type"] == ["refresh_token"]
    assert grant["refresh_token"] == ["ref-old"]
    with scopes_disabled():
        oauth_event.settings.flush()
        assert oauth_event.settings.get("moloni_refresh_token") == "ref-new"


@pytest.mark.django_db
@responses.activate
def test_keepalive_marks_a_dead_connection_and_emails_the_organizer(oauth_event):
    with scopes_disabled():
        oauth_event.settings.moloni_refresh_token = "ref-dead"
        oauth_event.settings.contact_mail = "organizer@example.org"
    responses.add(
        responses.GET,
        GRANT,
        json={"error": "invalid_grant", "error_description": "expired"},
        status=400,
    )
    django_mail.outbox = []

    ptinvoicing_keepalive.__wrapped__(sender=None)

    with scopes_disabled():
        oauth_event.settings.flush()
        provider = MoloniProvider(oauth_event)
        assert provider.connection_state == "expired"
        assert not provider.is_configured
    assert [m.to for m in django_mail.outbox] == [["organizer@example.org"]]
    assert "/invoicing/settings/" in django_mail.outbox[0].body


@pytest.mark.django_db
@responses.activate
def test_keepalive_network_failure_does_not_mark_expired(oauth_event):
    with scopes_disabled():
        oauth_event.settings.moloni_refresh_token = "ref"
    responses.add(responses.GET, GRANT, body=requests.ConnectionError("down"))

    ptinvoicing_keepalive.__wrapped__(sender=None)

    with scopes_disabled():
        oauth_event.settings.flush()
        assert MoloniProvider(oauth_event).connection_state == "connected"


@pytest.mark.django_db
def test_settings_page_shows_connection_state(logged_in_client, oauth_event):
    url = f"/control/event/{oauth_event.organizer.slug}/{oauth_event.slug}/invoicing/settings/"
    assert b"Connect to Moloni" in logged_in_client.get(url).content
    with scopes_disabled():
        oauth_event.settings.moloni_refresh_token = "ref"
        oauth_event.settings.moloni_connection_expired = True
    content = logged_in_client.get(url).content
    assert b"has expired" in content
    assert b"Reconnect to Moloni" in content


@pytest.mark.django_db
def test_settings_page_asks_for_a_save_until_moloni_is_in_use(
    logged_in_client, oauth_event
):
    url = f"/control/event/{oauth_event.organizer.slug}/{oauth_event.slug}/invoicing/settings/"
    with scopes_disabled():
        oauth_event.settings.moloni_refresh_token = "ref"
        oauth_event.settings.ptinvoicing_provider = "factpt"
    content = logged_in_client.get(url).content.decode()
    assert "fill in the Moloni settings" in content
    # The redirect URI comes with a copy button.
    assert 'class="btn btn-default btn-xs ptinvoicing-copy"' in content
    assert "/control/ptinvoicing/moloni/callback/" in content

    with scopes_disabled():
        oauth_event.settings.ptinvoicing_provider = "moloni"
    content = logged_in_client.get(url).content.decode()
    assert "fill in the Moloni settings" not in content
    # Every provider's fieldset is on the page; the one not chosen still says so.
    assert "fill in the Fact.pt settings" in content


@pytest.mark.django_db
@responses.activate
def test_lookups_list_companies_exemptions_and_per_company_fields(oauth_event):
    # Connected through OAuth, nothing typed: the stored token is used, no grant.
    with scopes_disabled():
        oauth_event.settings.moloni_access_token = "tok"
        oauth_event.settings.moloni_refresh_token = "ref"
        oauth_event.settings.moloni_token_expires = str(time.time() + 3000)
    rows = {
        "companies/getAll": [
            {"company_id": 1, "name": "Alpha"},
            {"company_id": 2, "name": "Beta"},
        ],
        "taxExemptions/getAll": [{"code": "M07", "name": "Artigo 9.º do CIVA"}],
        "documentSets/getAll": [{"document_set_id": 30, "name": "FR2026"}],
        "taxes/getAll": [{"tax_id": 11, "name": "IVA 23%"}],
        "paymentMethods/getAll": [{"payment_method_id": 5, "name": "MB Way"}],
        "maturityDates/getAll": [{"maturity_date_id": 9, "name": "Pronto pagamento"}],
        "productCategories/getAll": [{"category_id": 20, "name": "Bilhetes"}],
        "measurementUnits/getAll": [{"unit_id": 1, "name": "Unidade"}],
    }
    for endpoint, body in rows.items():
        responses.add(responses.POST, API.format(endpoint), json=body, status=200)

    provider = MoloniProvider(oauth_event)
    without_company = provider.lookups({})
    # Multi-company: a blank first option forces an explicit choice.
    assert [c["id"] for c in without_company["moloni_company_id"]] == ["", 1, 2]
    assert without_company["moloni_exemption_reason"][1] == {
        "id": "M07",
        "label": "M07 — Artigo 9.º do CIVA",
    }
    assert "moloni_document_set_id" not in without_company

    with_company = provider.lookups({"moloni_company_id": "2"})
    assert with_company["moloni_document_set_id"] == [{"id": 30, "label": "FR2026"}]
    assert with_company["moloni_payment_method_id"] == [{"id": 5, "label": "MB Way"}]
    # The payment-method table: a row per active pretix method even though nothing
    # was posted for it (settings.js skips empty inputs), blank = the default first.
    assert with_company["moloni_payment_method_id_giftcard"] == [
        {"id": "", "label": "— default —"},
        {"id": 5, "label": "MB Way"},
    ]
    assert len([c for c in responses.calls if "paymentMethods" in c.request.url]) == 1
    assert with_company["moloni_maturity_date_id"] == [
        {"id": 9, "label": "Pronto pagamento"}
    ]
    # Categories aren't picked any more: products go under Pretix > <event>.
    assert "moloni_product_category_id" not in with_company
    assert not [c for c in responses.calls if "productCategories" in c.request.url]
    assert with_company["moloni_unit_id"] == [{"id": 1, "label": "Unidade"}]
    sent = [
        json.loads(c.request.body)
        for c in responses.calls
        if "documentSets" in c.request.url
    ]
    assert sent[-1] == {"company_id": 2}
    assert not [c for c in responses.calls if "/grant/" in c.request.url]


@pytest.mark.django_db
def test_password_fields_hidden_while_connected_and_kept_on_save(
    logged_in_client, oauth_event
):
    url = f"/control/event/{oauth_event.organizer.slug}/{oauth_event.slug}/invoicing/settings/"
    content = logged_in_client.get(url).content.decode()
    assert "moloni-moloni_password" in content
    # The connection block sits at the top of the Moloni fieldset.
    assert content.index("Moloni connection") < content.index("moloni-moloni_client_id")

    with scopes_disabled():
        oauth_event.settings.moloni_refresh_token = "ref"
        oauth_event.settings.moloni_password = "kept"
    content = logged_in_client.get(url).content.decode()
    assert "moloni-moloni_username" not in content
    assert "moloni-moloni_password" not in content

    response = logged_in_client.post(
        url,
        {
            "ptinvoicing_provider": "moloni",
            "moloni-moloni_client_id": "cid",
            "moloni-moloni_client_secret": "secret",
            "moloni-moloni_company_id": "7",
            "moloni-moloni_document_set_id": "3",
            "moloni-moloni_credit_note_document_set_id": "4",
            "moloni-moloni_payment_method_id": "5",
            "moloni-moloni_maturity_date_id": "9",
            "moloni-moloni_product_type": "2",
            "moloni-moloni_unit_id": "1",
        },
    )
    assert response.status_code == 302
    with scopes_disabled():
        oauth_event.settings.flush()
        assert oauth_event.settings.get("moloni_password") == "kept"


@pytest.mark.django_db
@responses.activate
@pytest.mark.parametrize(
    "body, expected",
    [
        (
            [{"code": "1 name", "description": "O campo name é obrigatório"}],
            {"name": "O campo name é obrigatório"},
        ),
        (
            ["8 vat", "2 language_id 1 0"],
            {"vat": "8 vat", "language_id": "2 language_id 1 0"},
        ),
        # From a write, any list is an error, even in shapes neither form above
        # matches (a real invoiceReceipts/insert rejection didn't).
        (
            [{"code": "1 name", "description": "obrigatório", "field": "name"}],
            {"name": "obrigatório"},
        ),
        # A real invoiceReceipts/insert rejection: per-line errors, nested.
        (
            [
                [[{"code": "2 value 0 0", "description": "must be float"}]],
                [[{"code": "2 value 0 0", "description": "must be float"}]],
            ],
            {"#1 value": "must be float", "#2 value": "must be float"},
        ),
        ([42], {"error 0": "42"}),
    ],
)
def test_validation_error_list_is_a_terminal_provider_error(
    moloni_event, order, position, body, expected
):
    # Moloni returns validation errors as a list with HTTP 200; this used to crash with
    # "'list' object has no attribute 'get'" instead of recording the rejection.
    mock_grant()
    mock_no_customer()
    responses.add(
        responses.POST,
        API.format("customers/getNextNumber"),
        json={"number": "1"},
        status=200,
    )
    responses.add(responses.POST, API.format("customers/insert"), json=body, status=200)

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert invoice.error_detail == expected
    assert "human_errors=true" in responses.calls[-1].request.url


@pytest.mark.django_db
@responses.activate
@pytest.mark.parametrize(
    "body, ok",
    [(b"%PDF-1.7 fake", True), (b"<!DOCTYPE html><html>download page", False)],
)
def test_download_fetches_the_file_not_the_download_page(moloni_event, body, ok):
    mock_grant()
    responses.add(
        responses.POST,
        API.format("documents/getPDFLink"),
        json={"url": "https://www.moloni.pt/downloads/?h=abc&d=1"},
        status=200,
    )
    # getPDFLink's own URL is Moloni's HTML page; the file is the getDownload action.
    responses.add(
        responses.GET,
        "https://www.moloni.pt/downloads/index.php",
        body=body,
        status=200,
    )

    provider = MoloniProvider(moloni_event)
    if ok:
        assert provider.download("900") == body
        url = responses.calls[-1].request.url
        assert "action=getDownload" in url
        assert "h=abc" in url
    else:
        with pytest.raises(MoloniAPIError):
            provider.download("900")


@pytest.mark.django_db
@responses.activate
@pytest.mark.parametrize(
    "saft_code, number, expected",
    [("FR", 20, "FR M2026/20"), ("NC", 6, "NC M2026/6")],
)
def test_document_number_reads_series_and_number(
    moloni_event, saft_code, number, expected
):
    # documents/getOne's shape, verified on a real account.
    mock_grant()
    responses.add(
        responses.POST,
        API.format("documents/getOne"),
        json={
            "number": number,
            "document_set_name": "M2026",
            "document_type": {"document_type_id": 27, "saft_code": saft_code},
        },
        status=200,
    )
    assert MoloniProvider(moloni_event).document_number("1028494033") == expected
    assert sent_to("documents/getOne") == {"company_id": 7, "document_id": 1028494033}


@pytest.mark.django_db
def test_payments_follow_the_payment_method_table(moloni_event, order):
    from pretix.base.models import OrderPayment

    from pretix_ptinvoicing.providers.moloni.payload import build_payments

    with scopes_disabled():
        moloni_event.settings.moloni_payment_method_id = 1
        moloni_event.settings.moloni_payment_method_id_stripe = 2
        order.total = Decimal("30.00")
        order.save()
        for provider, amount in (("stripe", "20.00"), ("giftcard", "10.00")):
            OrderPayment.objects.create(
                order=order,
                provider=provider,
                amount=Decimal(amount),
                state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            )
        # Split by method: stripe is mapped, the gift card falls back to the default.
        assert build_payments(order, moloni_event.settings, "2026-10-08") == [
            {"payment_method_id": 2, "date": "2026-10-08", "value": 20.0},
            {"payment_method_id": 1, "date": "2026-10-08", "value": 10.0},
        ]

        # Not adding up to the total: all of it under the main method.
        order.total = Decimal("25.00")
        order.save()
        assert build_payments(order, moloni_event.settings, "2026-10-08") == [
            {"payment_method_id": 2, "date": "2026-10-08", "value": 25.0},
        ]


@pytest.mark.django_db
def test_settings_page_has_a_table_row_per_active_payment_method(
    logged_in_client, moloni_event
):
    # Gift cards are on by default in pretix core; "free" is never a real payment.
    with scopes_disabled():
        moloni_event.settings.payment_giftcard__enabled = True
        moloni_event.settings.payment_banktransfer__enabled = False
    response = logged_in_client.get(
        f"/control/event/{moloni_event.organizer.slug}/{moloni_event.slug}"
        "/invoicing/settings/"
    )
    html = response.content.decode()
    table = html[html.index("<table") : html.index("</table>")]
    assert 'name="moloni-moloni_payment_method_id_giftcard"' in table
    assert "moloni_payment_method_id_banktransfer" not in html
    assert "moloni_payment_method_id_free" not in html
    # In the table, not repeated as a plain form row.
    assert html.count('name="moloni-moloni_payment_method_id_giftcard"') == 1


@pytest.mark.django_db
def test_payment_marked_paid_by_hand_counts_as_the_buyers_method(moloni_event, order):
    # U0RYS: Multibanco reference pending, the organizer marked the order paid ("manual").
    from pretix.base.models import OrderPayment

    from pretix_ptinvoicing.providers.moloni.payload import build_payments

    with scopes_disabled():
        moloni_event.settings.moloni_payment_method_id = 1
        moloni_event.settings.moloni_payment_method_id_eupago_multibanco = 7
        order.total = Decimal("163.00")
        order.save()
        for provider, state in (
            ("eupago_multibanco", OrderPayment.PAYMENT_STATE_PENDING),
            ("manual", OrderPayment.PAYMENT_STATE_CONFIRMED),
        ):
            OrderPayment.objects.create(
                order=order, provider=provider, amount=order.total, state=state
            )
        assert build_payments(order, moloni_event.settings, "2026-10-08") == [
            {"payment_method_id": 7, "date": "2026-10-08", "value": 163.0}
        ]


@pytest.mark.django_db
@responses.activate
def test_an_existing_document_is_adopted_instead_of_issued_again(
    moloni_event, order, position
):
    # invoiceReceipts/insert has no idempotency field, so before every attempt the
    # document is looked up by our_reference (exact match on a real account). A
    # timed-out insert, a stale claim or a manual retry then adopts what exists: no
    # customer, no product, no insert — and never two official invoices.
    responses.add(
        responses.GET,
        GRANT,
        json={"access_token": "tok", "refresh_token": "ref", "expires_in": 3600},
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("invoiceReceipts/getAll"),
        json=[
            # A draft with our reference doesn't count; the closed one does.
            {"document_id": 880, "our_reference": "pretix-dummy-FOOBAR", "status": 0},
            {"document_id": 881, "our_reference": "pretix-dummy-FOOBAR", "status": 1},
            {"document_id": 882, "our_reference": "pretix-dummy-OTHER", "status": 1},
        ],
        status=200,
    )
    responses.add(
        responses.POST,
        API.format("documents/getOne"),
        json={
            "number": 16,
            "document_set_name": "M",
            "document_type": {"saft_code": "FR"},
        },
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": moloni_event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_SUCCESS
    assert invoice.document_id == "881"
    assert invoice.document_number == "FR M/16"
    assert sent_to("invoiceReceipts/getAll") == {
        "company_id": 7,
        "our_reference": "pretix-dummy-FOOBAR",
    }
    assert not [c for c in responses.calls if "insert" in c.request.url]


@pytest.mark.django_db
def test_document_lines_carry_the_discount_moloni_applies(moloni_event):
    # A draft on the real account: price 28.45528 at discount 42.8571429 (7 decimals
    # kept) gave incidence 16.26016 — the net of a 35.00 ticket sold for 20.00. The
    # listed price is what's sent, the voucher the percentage; a 100% voucher sends the
    # listed price 100% off.
    from pretix_ptinvoicing.orderdata import Line, set_nets
    from pretix_ptinvoicing.providers.moloni.payload import build_products

    lines = set_nets(
        [
            Line(
                "Regular (voucher TWENTY)",
                Decimal("20.00"),
                Decimal(23),
                "item-1",
                "r1",
                listed_gross=Decimal("35.00"),
            ),
            Line(
                "Reduced (voucher FREE)",
                Decimal("0.00"),
                Decimal(23),
                "item-2",
                "r2",
                listed_gross=Decimal("29.00"),
            ),
            Line("Regular", Decimal("35.00"), Decimal(23), "item-1", "r3"),
        ],
        discount_places=MoloniProvider.discount_places,
    )
    products = build_products(
        lines, moloni_event.settings, {"item-1": 1, "item-2": 2}, Decimal(23)
    )
    assert [(p["price"], p["discount"]) for p in products] == [
        (28.45528457, 42.8571429),
        (23.57723577, 100.0),
        (28.45528455, 0.0),
    ]
