import json

import pytest
import responses
from django_scopes import scopes_disabled
from pretix.base.models import InvoiceAddress

from pretix_factpt.models import FactptInvoice
from pretix_factpt.tasks import generate_factpt_invoice


@pytest.mark.django_db
def test_no_token_configured_skips_silently(order, event):
    generate_factpt_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})
    assert FactptInvoice.objects.count() == 0


@pytest.mark.django_db
@responses.activate
def test_successful_issuance_creates_invoice(order, event, position):
    with scopes_disabled():
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5

    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={
            "AppStatusCode": 200,
            "AppResponse": {
                "data": {"id": "12345"},
                "link": "https://fact.pt/doc/12345",
                "permanentUrl": "https://fact.pt/permanent/12345",
            },
        },
        status=200,
    )

    generate_factpt_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = FactptInvoice.objects.get(order=order)
    assert invoice.status == FactptInvoice.STATUS_SUCCESS
    assert invoice.factpt_document_id == "12345"
    assert invoice.factpt_link == "https://fact.pt/doc/12345"
    assert invoice.attempts == 1


@pytest.mark.django_db
@responses.activate
def test_api_error_marks_invoice_as_error(order, event, position):
    with scopes_disabled():
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5

    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={
            "AppStatusCode": 400,
            "AppResponse": {"message": "Invalid VAT", "errors": {"tin": "Invalid"}},
        },
        status=200,
    )

    generate_factpt_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = FactptInvoice.objects.get(order=order)
    assert invoice.status == FactptInvoice.STATUS_ERROR
    assert invoice.error_message == "tin: Invalid"
    assert invoice.error_detail == {"tin": "Invalid"}


@pytest.mark.django_db
@responses.activate
def test_uses_existing_client_id_when_search_finds_one_match(order, event, position):
    with scopes_disabled():
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        InvoiceAddress.objects.create(order=order, vat_id="PT123456789")

    responses.add(
        responses.GET,
        "https://api.fact.pt/clients?search=123456789",
        json={
            "AppStatusCode": 200,
            "AppResponse": {"data": [{"id": "77", "tin": "123456789"}]},
        },
        status=200,
    )
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )

    generate_factpt_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["client"] == {"id": "77"}


@pytest.mark.django_db
@responses.activate
def test_falls_back_to_inline_client_when_search_finds_multiple_matches(
    order, event, position
):
    with scopes_disabled():
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        InvoiceAddress.objects.create(order=order, vat_id="PT123456789")

    responses.add(
        responses.GET,
        "https://api.fact.pt/clients?search=123456789",
        json={
            "AppStatusCode": 200,
            "AppResponse": {
                "data": [
                    {"id": "77", "tin": "123456789"},
                    {"id": "78", "tin": "123456789"},
                ]
            },
        },
        status=200,
    )
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )

    generate_factpt_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["client"]["tin"] == "123456789"
    assert "id" not in sent["client"]


@pytest.mark.django_db
def test_already_successful_invoice_is_not_reprocessed(order, event):
    with scopes_disabled():
        event.settings.factpt_token = "test-token"
        invoice = FactptInvoice.objects.create(
            order=order,
            identifier_id=f"pretix-{event.slug}-{order.code}",
            status=FactptInvoice.STATUS_SUCCESS,
            attempts=1,
        )

    # No responses.activate/mock registered: any attempt to actually call the
    # Fact.pt API here would raise, proving the early-return idempotency guard held.
    generate_factpt_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    invoice.refresh_from_db()
    assert invoice.attempts == 1
