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


def mock_existing_product(product_id=12):
    # Every pretix item already has its catalog product: getByReference echoes it back.
    def found(request):
        reference = json.loads(request.body)["reference"]
        return 200, {}, json.dumps([{"product_id": product_id, "reference": reference}])

    responses.add_callback(
        responses.POST, API.format("products/getByReference"), callback=found
    )


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
        event.settings.moloni_product_category_id = 20
        event.settings.moloni_unit_id = 1
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    return event


@pytest.mark.django_db
@responses.activate
def test_issues_through_the_generic_core(moloni_event, order, position):
    mock_grant()
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
        json=[{"customer_id": 88, "vat": "237892294"}],
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
    assert sent_to("invoiceReceipts/insert")["customer_id"] == 88


@pytest.mark.django_db
@responses.activate
def test_api_rejection_is_terminal(moloni_event, order, position):
    mock_grant()
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
    assert sent["products"][0]["price"] == 15.0
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
    responses.add(
        responses.POST, API.format("products/getByReference"), json=[], status=200
    )
    responses.add(
        responses.POST,
        API.format("taxes/getAll"),
        json=[{"tax_id": 11, "value": 23}],
        status=200,
    )
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
    assert created["category_id"] == 20
    assert created["unit_id"] == 1
    assert created["type"] == 2  # service, the default
    assert created["exemption_reason"] == "M07"  # 0% lines, like the item's sales
    assert "taxes" not in created
    lines = sent_to("invoiceReceipts/insert")["products"]
    assert [line["product_id"] for line in lines] == [77, 77]


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
    logged_in_client.post(
        DISCONNECT_URL.format(oauth_event.organizer.slug, oauth_event.slug)
    )
    with scopes_disabled():
        oauth_event.settings.flush()
        assert MoloniProvider(oauth_event).connection_state is None
        assert not oauth_event.settings.get("moloni_access_token")


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
    assert with_company["moloni_maturity_date_id"] == [
        {"id": 9, "label": "Pronto pagamento"}
    ]
    assert with_company["moloni_product_category_id"] == [
        {"id": 20, "label": "Bilhetes"}
    ]
    assert sent_to("productCategories/getAll") == {"company_id": 2, "parent_id": 0}
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
            "moloni-moloni_product_category_id": "20",
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
