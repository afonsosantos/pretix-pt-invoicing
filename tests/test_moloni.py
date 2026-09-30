import json
import time
from decimal import Decimal

import pytest
import responses
from django_scopes import scopes_disabled
from pretix.base.models import InvoiceAddress, Order

from pretix_ptinvoicing.models import IssuedInvoice
from pretix_ptinvoicing.providers.moloni import MoloniProvider
from pretix_ptinvoicing.providers.moloni.client import MoloniAPIError, MoloniClient
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
        event.settings.moloni_payment_method_id = 5
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    return event


@pytest.mark.django_db
@responses.activate
def test_issues_through_the_generic_core(moloni_event, order, position):
    mock_grant()
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

    customer_sent = json.loads(responses.calls[-2].request.body)
    assert customer_sent["number"] == "100"

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["company_id"] == 7
    assert sent["customer_id"] == 42
    assert sent["document_set_id"] == 3
    assert sent["our_reference"] == "pretix-dummy-FOOBAR"
    assert sent["products"][0]["price"] == 23.0
    assert sent["products"][0]["taxes"] == [{"tax_id": 11, "order": 0, "cumulative": 0}]
    assert sent["payments"][0]["payment_method_id"] == 5


@pytest.mark.django_db
@responses.activate
def test_existing_customer_is_reused_by_vat(moloni_event, order, position):
    with scopes_disabled():
        InvoiceAddress.objects.create(order=order, vat_id="PT237892294", country="PT")
    order.refresh_from_db()

    mock_grant()
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
    assert json.loads(responses.calls[-1].request.body)["customer_id"] == 88


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
                "taxes": [{"tax_id": 11}],
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

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["customer_id"] == 42
    assert sent["document_set_id"] == 4
    # Invoice-receipts are a self-paid Moloni document type — the credited value is 0,
    # not the document's total.
    assert sent["associated_documents"] == [{"associated_id": 900, "value": 0}]
    # related_id maps to the *document's* line id (document_product_id), not product_id.
    assert sent["products"][0]["product_id"] == 3
    assert sent["products"][0]["related_id"] == 5
    assert sent["products"][0]["taxes"] == [{"tax_id": 11, "order": 0, "cumulative": 0}]

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

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["products"][0]["price"] == 15.0
