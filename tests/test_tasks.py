import json
from decimal import Decimal
from typing import ClassVar
from urllib.parse import quote

import pytest
import responses
from django.core import mail as django_mail
from django_scopes import scopes_disabled
from pretix.base.models import InvoiceAddress, Order, OrderPosition

from pretix_ptinvoicing.models import IssuedInvoice
from pretix_ptinvoicing.providers import PROVIDERS, ProviderError
from pretix_ptinvoicing.providers.base import InvoiceProvider, IssuedDocument
from pretix_ptinvoicing.tasks import issue_credit_note, issue_invoice
from tests.conftest import mock_factpt_taxes


def last_post():
    # The issuing request; issuance ends with a GET for the document's number.
    return [c for c in responses.calls if c.request.method == "POST"][-1]


@pytest.mark.django_db
def test_no_provider_configured_skips_silently(order, event):
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})
    assert IssuedInvoice.objects.count() == 0


@pytest.mark.django_db
@responses.activate
def test_successful_issuance_creates_invoice(order, event, position):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
        event.settings.factpt_default_tax_id = 5

    mock_factpt_taxes()
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
    # Shape verified on a real account: number is "<series>/<n>", type a word.
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345",
        json={
            "AppStatusCode": 200,
            "AppResponse": {
                "data": {"id": 12345, "number": "2025QG/9", "type": "invoicereceipt"}
            },
        },
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_SUCCESS
    assert invoice.document_id == "12345"
    # As Fact.pt returns it — no type mapping.
    assert invoice.document_number == "2025QG/9"
    assert invoice.display_number == "2025QG/9"
    assert invoice.document_link == "https://fact.pt/doc/12345"
    assert invoice.attempts == 1


@pytest.mark.django_db
@responses.activate
def test_failed_number_lookup_keeps_the_issuance_successful(order, event, position):
    # The document already exists by then: a failed lookup must not turn it into an
    # error (or a retry). The id is shown instead.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345",
        json={"AppStatusCode": 500, "AppResponse": {}},
        status=500,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_SUCCESS
    assert invoice.document_number is None
    assert invoice.display_number == "12345"
    assert invoice.attempts == 1


@pytest.mark.django_db
@responses.activate
def test_api_error_marks_invoice_as_error(order, event, position):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
        event.settings.factpt_default_tax_id = 5

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={
            "AppStatusCode": 400,
            "AppResponse": {"message": "Invalid VAT", "errors": {"tin": "Invalid"}},
        },
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert invoice.error_message == "tin: Invalid"
    assert invoice.error_detail == {"tin": "Invalid"}


@pytest.mark.django_db
@responses.activate
def test_uses_existing_client_id_when_search_finds_one_match(order, event, position):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
        event.settings.factpt_default_tax_id = 5
        InvoiceAddress.objects.create(order=order, vat_id="PT123456789")

    mock_factpt_taxes()
    responses.add(
        responses.GET,
        "https://api.fact.pt/clients?search=123456789",
        json={
            "AppStatusCode": 200,
            "AppResponse": {"data": [{"id": "77", "tin": "123456789"}]},
        },
        status=200,
    )
    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    sent = json.loads(last_post().request.body)
    assert sent["client"] == {"id": "77"}


@pytest.mark.django_db
@responses.activate
def test_falls_back_to_inline_client_when_search_finds_multiple_matches(
    order, event, position
):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
        event.settings.factpt_default_tax_id = 5
        InvoiceAddress.objects.create(order=order, vat_id="PT123456789")

    mock_factpt_taxes()
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
    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    sent = json.loads(last_post().request.body)
    assert sent["client"]["tin"] == "123456789"
    assert "id" not in sent["client"]


@pytest.mark.django_db
@responses.activate
def test_credit_note_through_factpt_hits_the_documents_credit_endpoint(
    order, event, position
):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/12345/credit",
        json={
            "AppStatusCode": 200,
            "AppResponse": {
                "data": {"id": "999"},
                "permanentUrl": "https://fact.pt/permanent/999",
            },
        },
        status=200,
    )
    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert last_post().request.url == "https://api.fact.pt/documents/12345/credit"
    sent = json.loads(last_post().request.body)
    assert sent["document"]["identifierId"] == "pretix-dummy-FOOBAR-credit"

    with scopes_disabled():
        credit_note = IssuedInvoice.objects.get(kind=IssuedInvoice.KIND_CREDIT_NOTE)
    assert credit_note.status == IssuedInvoice.STATUS_SUCCESS
    assert credit_note.document_id == "999"
    assert credit_note.permanent_url == "https://fact.pt/permanent/999"


@pytest.mark.django_db
@responses.activate
def test_emails_the_credit_note_to_the_buyer_when_enabled(order, event, position):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        event.settings.ptinvoicing_email_credit_note = True
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/12345/credit",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "999"}}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/999/download",
        body=b"%PDF-1.4 fake",
        status=200,
        content_type="application/pdf",
    )

    django_mail.outbox = []
    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert len(django_mail.outbox) == 1
    sent = django_mail.outbox[0]
    assert order.email in sent.to
    assert [a[0] for a in sent.attachments] == [f"{order.code}-credit.pdf"]
    assert sent.subject == f"Your credit note for order {order.code}"
    assert f"a credit note for order {order.code}" in sent.body


@pytest.mark.django_db
@responses.activate
def test_credit_note_email_is_translated_to_the_buyers_locale(order, event, position):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        event.settings.ptinvoicing_email_credit_note = True
        order.status = Order.STATUS_PAID
        order.locale = "pt-pt"
        order.save(update_fields=["status", "locale"])

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/12345/credit",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "999"}}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/999/download",
        body=b"%PDF-1.4 fake",
        status=200,
        content_type="application/pdf",
    )

    django_mail.outbox = []
    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert len(django_mail.outbox) == 1
    sent = django_mail.outbox[0]
    assert sent.subject == f"A sua nota de crédito da encomenda {order.code}"
    assert f"segue em anexo a nota de crédito da encomenda {order.code}" in sent.body


@pytest.mark.django_db
@responses.activate
def test_does_not_email_the_credit_note_by_default(order, event, position):
    # ptinvoicing_email_invoice being on must not also email the credit note — the two
    # settings are independent.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        event.settings.ptinvoicing_email_invoice = True
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345/download",
        body=b"%PDF-1.4 fake",
        status=200,
        content_type="application/pdf",
    )
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/12345/credit",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "999"}}},
        status=200,
    )
    # No download mock for the credit note's own PDF: fetching it would blow up the test.

    django_mail.outbox = []
    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert django_mail.outbox == []


@pytest.mark.django_db
def test_already_successful_invoice_is_not_reprocessed(order, event):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
        invoice = IssuedInvoice.objects.create(
            order=order,
            provider="factpt",
            identifier_id=f"pretix-{event.slug}-{order.code}",
            status=IssuedInvoice.STATUS_SUCCESS,
            attempts=1,
        )

    # No responses.activate/mock registered: any attempt to actually call the
    # Fact.pt API here would raise, proving the early-return idempotency guard held.
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    invoice.refresh_from_db()
    assert invoice.attempts == 1


@pytest.mark.django_db
def test_provider_selected_but_unconfigured_skips_silently(order, event):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        assert IssuedInvoice.objects.count() == 0


@pytest.mark.django_db
def test_unpaid_order_is_never_issued(order, event):
    # The manual "issue now" button in the admin can target any order, so the paid check
    # lives here rather than relying on order_paid being the only caller.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        assert order.status == Order.STATUS_PENDING

    # No responses mock registered: any HTTP call would raise.
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        assert IssuedInvoice.objects.count() == 0


@pytest.mark.django_db
@responses.activate
def test_vat_mismatch_refuses_to_issue(order, event, item):
    # Fact.pt adds its own rate on top of the net price we send. If that rate isn't the
    # one pretix charged, the document total wouldn't match what the buyer paid — proved
    # against the sandbox, where 15.00 at Fact.pt's 23% came back as gross 18.45.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
        OrderPosition.objects.create(
            order=order,
            item=item,
            price=Decimal("15.00"),
            tax_rate=Decimal("0.00"),
            tax_value=Decimal("0.00"),
        )

    mock_factpt_taxes(tax_id=5, value="23.00")  # configured 23%, order charged 0%
    # No invoicereceipt mock: reaching the API at all would blow up the test.

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert "VAT mismatch" in invoice.error_message


@pytest.mark.django_db
@responses.activate
def test_final_consumer_reuses_an_existing_client_instead_of_duplicating(
    order, event, position
):
    # Every no-NIF buyer is filed under Fact.pt's 999999990, so creating a client per
    # issuance piles up duplicates until Fact.pt refuses to resolve any of them. Reuse the
    # existing record by id instead.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    mock_factpt_taxes()
    responses.add(
        responses.GET,
        f"https://api.fact.pt/clients?search={quote(order.email)}",
        json={
            "AppStatusCode": 200,
            "AppResponse": {
                "data": [
                    {
                        "id": "9987",
                        "name": order.email,
                        "tin": "999999990",
                        "isFinalConsumer": True,
                    },
                    {
                        "id": "9985",
                        "name": order.email,
                        "tin": "999999990",
                        "isFinalConsumer": True,
                    },
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

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    sent = json.loads(last_post().request.body)
    # Lowest id, so repeated issuance is deterministic rather than picking a new duplicate.
    assert sent["client"] == {"id": 9985}


@pytest.mark.django_db
@responses.activate
def test_emails_the_invoice_to_the_buyer_when_enabled(order, event, position):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        event.settings.ptinvoicing_email_invoice = True
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345/download",
        body=b"%PDF-1.4 fake",
        status=200,
        content_type="application/pdf",
    )

    django_mail.outbox = []
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert len(django_mail.outbox) == 1
    sent = django_mail.outbox[0]
    assert order.email in sent.to
    assert [a[0] for a in sent.attachments] == [f"{order.code}.pdf"]
    assert sent.subject == f"Your invoice for order {order.code}"
    assert f"your invoice for order {order.code}" in sent.body


@pytest.mark.django_db
@responses.activate
def test_invoice_email_is_translated_to_the_buyers_locale(order, event, position):
    # subject/text must be passed to mail() still lazy (LazyI18nString), not pre-formatted
    # with `%`: mail() only translates whatever is still lazy by the time it enters its own
    # `with language(order.locale)` block. A %-formatted plain str would already be frozen
    # in whatever language happened to be active in the Celery worker, never order.locale.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        event.settings.ptinvoicing_email_invoice = True
        order.status = Order.STATUS_PAID
        order.locale = "pt-pt"
        order.save(update_fields=["status", "locale"])

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345/download",
        body=b"%PDF-1.4 fake",
        status=200,
        content_type="application/pdf",
    )

    django_mail.outbox = []
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert len(django_mail.outbox) == 1
    sent = django_mail.outbox[0]
    assert sent.subject == f"A sua fatura da encomenda {order.code}"
    assert f"segue em anexo a sua fatura da encomenda {order.code}" in sent.body


@pytest.mark.django_db
@responses.activate
def test_does_not_email_the_invoice_by_default(order, event, position):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    # No download mock: the PDF must not even be fetched when the setting is off.

    django_mail.outbox = []
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        assert (
            IssuedInvoice.objects.get(order=order).status
            == IssuedInvoice.STATUS_SUCCESS
        )
    assert django_mail.outbox == []


@pytest.mark.django_db
@responses.activate
def test_a_failing_email_does_not_undo_a_successful_issuance(order, event, position):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        event.settings.ptinvoicing_email_invoice = True
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345/download",
        json={"AppStatusCode": 500, "AppResponse": {}},
        status=500,
    )

    django_mail.outbox = []
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    # The document exists at the provider; a mail problem must not mark it failed.
    with scopes_disabled():
        assert (
            IssuedInvoice.objects.get(order=order).status
            == IssuedInvoice.STATUS_SUCCESS
        )
    assert django_mail.outbox == []


class _DummyProvider(InvoiceProvider):
    """A second provider that shares nothing with Fact.pt, to keep the core honest."""

    identifier = "dummy"
    verbose_name = "Dummy"
    settings_form_class = None
    deduplicates_issuance = True

    issued: ClassVar = []
    credited: ClassVar = []
    fail_with = None

    @property
    def is_configured(self):
        return bool(self.settings.get("dummy_key"))

    def issue(self, order, identifier_id):
        if self.fail_with:
            raise self.fail_with
        self.issued.append((order.code, identifier_id))
        return IssuedDocument(
            document_id="DUMMY-1", link="https://example.org/d/1", permanent_url=None
        )

    def credit(self, order, document_id, identifier_id):
        if self.fail_with:
            raise self.fail_with
        self.credited.append((order.code, document_id, identifier_id))
        return IssuedDocument(
            document_id="DUMMY-CREDIT-1",
            link="https://example.org/d/credit-1",
            permanent_url=None,
        )

    def download(self, document_id):
        return b"%PDF-1.4 dummy"


@pytest.fixture
def dummy_provider(monkeypatch):
    _DummyProvider.issued = []
    _DummyProvider.credited = []
    _DummyProvider.fail_with = None
    monkeypatch.setitem(PROVIDERS, "dummy", _DummyProvider)
    return _DummyProvider


@pytest.mark.django_db
def test_core_issues_through_any_provider(order, event, position, dummy_provider):
    # No Fact.pt anywhere: proves tasks.py/models carry no provider-specific assumptions.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.provider == "dummy"
    assert invoice.status == IssuedInvoice.STATUS_SUCCESS
    assert invoice.document_id == "DUMMY-1"
    assert invoice.document_link == "https://example.org/d/1"
    assert dummy_provider.issued == [(order.code, "pretix-dummy-FOOBAR")]


@pytest.mark.django_db
def test_paid_again_after_a_credited_refund_issues_a_new_invoice(
    order, event, position, dummy_provider
):
    # Paid → invoiced → refunded → credited → paid again: the order needs a second
    # invoice under a new key, and a later refund credits that one, not the first.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})
    # A duplicate order_paid within the same cycle still issues nothing new.
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})
    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})
    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})
    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert dummy_provider.issued == [
        (order.code, "pretix-dummy-FOOBAR"),
        (order.code, "pretix-dummy-FOOBAR-r1"),
    ]
    assert [c[2] for c in dummy_provider.credited] == [
        "pretix-dummy-FOOBAR-credit",
        "pretix-dummy-FOOBAR-r1-credit",
    ]
    with scopes_disabled():
        second = IssuedInvoice.objects.get(identifier_id="pretix-dummy-FOOBAR-r1")
        credits = IssuedInvoice.objects.get(
            identifier_id="pretix-dummy-FOOBAR-r1-credit"
        )
    assert credits.credits == second


@pytest.mark.django_db
def test_provider_error_is_terminal_for_any_provider(order, event, dummy_provider):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    dummy_provider.fail_with = ProviderError("nope", detail={"field": "bad"})

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_ERROR
    assert invoice.error_detail == {"field": "bad"}
    assert invoice.attempts == 1


@pytest.mark.django_db
def test_no_auto_retry_when_the_provider_cannot_deduplicate(
    order, event, dummy_provider, monkeypatch
):
    # Moloni's documents/insert has no idempotency field, so a timed-out call may already
    # have created the document. Retrying it would issue a second official invoice.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    dummy_provider.fail_with = OSError("connection reset")
    monkeypatch.setattr(dummy_provider, "deduplicates_issuance", False)

    retries = []
    monkeypatch.setattr(
        issue_invoice, "retry", lambda **kw: retries.append(kw) or Exception("retry")
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert retries == []
    with scopes_disabled():
        assert (
            IssuedInvoice.objects.get(order=order).status == IssuedInvoice.STATUS_ERROR
        )


@pytest.mark.django_db
def test_auto_retry_when_the_provider_does_deduplicate(
    order, event, dummy_provider, monkeypatch
):
    # The counterpart to the test above: without this one, that one could pass simply
    # because the failure path was never reached.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    dummy_provider.fail_with = OSError("connection reset")
    assert dummy_provider.deduplicates_issuance is True

    retries = []
    monkeypatch.setattr(
        issue_invoice, "retry", lambda **kw: retries.append(kw) or Exception("retry")
    )

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert len(retries) == 1


@pytest.mark.django_db
def test_credit_note_skips_when_nothing_was_ever_invoiced(order, event, dummy_provider):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"

    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        assert IssuedInvoice.objects.count() == 0
    assert dummy_provider.credited == []


@pytest.mark.django_db
def test_credit_note_issues_against_the_original_invoices_document_id(
    order, event, dummy_provider
):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        original = IssuedInvoice.objects.create(
            order=order,
            provider="dummy",
            identifier_id="pretix-dummy-FOOBAR",
            kind=IssuedInvoice.KIND_INVOICE,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="DUMMY-1",
        )

    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert dummy_provider.credited == [
        (order.code, "DUMMY-1", "pretix-dummy-FOOBAR-credit")
    ]
    with scopes_disabled():
        credit_note = IssuedInvoice.objects.get(
            order=order, kind=IssuedInvoice.KIND_CREDIT_NOTE
        )
    assert credit_note.status == IssuedInvoice.STATUS_SUCCESS
    assert credit_note.document_id == "DUMMY-CREDIT-1"
    assert credit_note.credits_id == original.pk
    # The order needn't be paid any more — a credit note is by definition issued after a
    # refund/cancellation, unlike the invoice it reverses.
    with scopes_disabled():
        assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_credit_note_is_not_reissued_once_successful(order, event, dummy_provider):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        original = IssuedInvoice.objects.create(
            order=order,
            provider="dummy",
            identifier_id="pretix-dummy-FOOBAR",
            kind=IssuedInvoice.KIND_INVOICE,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="DUMMY-1",
        )
        IssuedInvoice.objects.create(
            order=order,
            provider="dummy",
            identifier_id="pretix-dummy-FOOBAR-credit",
            kind=IssuedInvoice.KIND_CREDIT_NOTE,
            credits=original,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="DUMMY-CREDIT-1",
            attempts=1,
        )

    # No credit() call should happen at all: dummy_provider.credited stays empty.
    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert dummy_provider.credited == []


@pytest.mark.django_db
def test_credit_note_provider_error_is_terminal(order, event, dummy_provider):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        IssuedInvoice.objects.create(
            order=order,
            provider="dummy",
            identifier_id="pretix-dummy-FOOBAR",
            kind=IssuedInvoice.KIND_INVOICE,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="DUMMY-1",
        )
    dummy_provider.fail_with = ProviderError("already credited")

    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        credit_note = IssuedInvoice.objects.get(kind=IssuedInvoice.KIND_CREDIT_NOTE)
    assert credit_note.status == IssuedInvoice.STATUS_ERROR
    assert credit_note.error_message == "already credited"


@pytest.mark.django_db
def test_credit_note_no_auto_retry_when_the_provider_cannot_deduplicate(
    order, event, dummy_provider, monkeypatch
):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "dummy"
        event.settings.dummy_key = "x"
        IssuedInvoice.objects.create(
            order=order,
            provider="dummy",
            identifier_id="pretix-dummy-FOOBAR",
            kind=IssuedInvoice.KIND_INVOICE,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="DUMMY-1",
        )
    dummy_provider.fail_with = OSError("connection reset")
    monkeypatch.setattr(dummy_provider, "deduplicates_issuance", False)

    retries = []
    monkeypatch.setattr(
        issue_credit_note,
        "retry",
        lambda **kw: retries.append(kw) or Exception("retry"),
    )

    issue_credit_note.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    assert retries == []
