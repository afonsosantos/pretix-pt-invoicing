from decimal import Decimal
from unittest import mock

import pytest
import responses
from django.db import transaction
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment, OrderRefund

from pretix_ptinvoicing.models import IssuedInvoice
from pretix_ptinvoicing.signals import ptinvoicing_order_paid

CREDIT_URL = "https://api.fact.pt/documents/12345/credit"


@pytest.fixture
def invoiced_order(order, event):
    with scopes_disabled():
        event.plugins = "pretix_ptinvoicing"
        event.save(update_fields=["plugins"])
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        order.status = Order.STATUS_PAID
        order.total = Decimal("23.00")
        order.save(update_fields=["status", "total"])
        OrderPayment.objects.create(
            order=order,
            amount=Decimal("23.00"),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            provider="manual",
        )
        IssuedInvoice.objects.create(
            order=order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            kind=IssuedInvoice.KIND_INVOICE,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="12345",
        )
    return order


@pytest.fixture
def complete(django_capture_on_commit_callbacks):
    """Mark a refund done, then run what was deferred to commit — as in production."""

    def run(refund):
        with scopes_disabled(), django_capture_on_commit_callbacks(execute=True):
            refund.done()

    return run


def _make_refund(order, amount, state=OrderRefund.REFUND_STATE_CREATED):
    with scopes_disabled():
        return OrderRefund.objects.create(
            order=order,
            amount=amount,
            state=state,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            provider="manual",
        )


def _credit_notes():
    with scopes_disabled():
        return IssuedInvoice.objects.filter(kind=IssuedInvoice.KIND_CREDIT_NOTE)


@pytest.mark.django_db
@responses.activate
def test_full_refund_auto_issues_a_credit_note(invoiced_order, complete):
    responses.add(
        responses.POST,
        CREDIT_URL,
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "999"}}},
        status=200,
    )
    complete(_make_refund(invoiced_order, Decimal("23.00")))

    with scopes_disabled():
        credit_note = _credit_notes().get()
    assert credit_note.status == IssuedInvoice.STATUS_SUCCESS
    assert credit_note.document_id == "999"


@pytest.mark.django_db
def test_partial_refund_does_not_auto_issue_a_credit_note(invoiced_order, complete):
    # No responses mock registered: a credit() call would blow up the test — the whole
    # point is that a partial refund must not reach the provider at all, since no provider
    # here can credit less than a document's full value.
    complete(_make_refund(invoiced_order, Decimal("10.00")))

    assert not _credit_notes().exists()


@pytest.mark.django_db
@responses.activate
def test_second_partial_refund_completing_the_total_triggers_the_credit_note(
    invoiced_order, complete
):
    # Two partial refunds that together cover the full amount must still end up crediting
    # the whole document — the order's total refunded, not any single refund's own
    # amount, is what the receiver checks.
    responses.add(
        responses.POST,
        CREDIT_URL,
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "999"}}},
        status=200,
    )
    complete(_make_refund(invoiced_order, Decimal("10.00")))
    assert not _credit_notes().exists()

    complete(_make_refund(invoiced_order, Decimal("13.00")))

    assert _credit_notes().filter(status=IssuedInvoice.STATUS_SUCCESS).exists()


@pytest.mark.django_db
def test_refunds_not_yet_done_do_not_count_as_refunded(invoiced_order, complete):
    # 13.00 merely created (it may still fail) plus 10.00 done is not a full refund,
    # even though pretix's payment_refund_sum already reads zero.
    _make_refund(
        invoiced_order, Decimal("13.00"), state=OrderRefund.REFUND_STATE_CREATED
    )
    complete(_make_refund(invoiced_order, Decimal("10.00")))

    assert not _credit_notes().exists()


@pytest.mark.django_db
def test_refund_signal_ignores_events_without_the_plugin_enabled(
    order, event, complete
):
    # The receiver must bail before it ever reaches the (unmocked) provider.
    with scopes_disabled():
        event.plugins = ""
        event.save(update_fields=["plugins"])
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        order.status = Order.STATUS_PAID
        order.total = Decimal("23.00")
        order.save(update_fields=["status", "total"])
        OrderPayment.objects.create(
            order=order,
            amount=Decimal("23.00"),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            provider="manual",
        )
        IssuedInvoice.objects.create(
            order=order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="12345",
        )
    complete(_make_refund(order, Decimal("23.00")))

    assert not _credit_notes().exists()


@pytest.mark.django_db
def test_refund_signal_ignores_non_done_states(invoiced_order):
    # A plain save() with state left as "created" must not trigger anything — only the
    # transition to REFUND_STATE_DONE does.
    _make_refund(
        invoiced_order, Decimal("23.00"), state=OrderRefund.REFUND_STATE_CREATED
    )

    assert not _credit_notes().exists()


@pytest.mark.django_db
def test_order_paid_enqueues_only_once_the_payment_commits(
    order, event, django_capture_on_commit_callbacks
):
    # pretix sends order_paid inside the payment's transaction. Enqueued straight away,
    # a worker could load the order before the commit, see it unpaid, and skip it.
    with mock.patch("pretix_ptinvoicing.signals.issue_invoice") as task:
        with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
            ptinvoicing_order_paid(sender=event, order=order)
            task.apply_async.assert_not_called()
        task.apply_async.assert_called_once()


@pytest.mark.django_db
def test_manual_only_issues_nothing_on_payment_or_refund(
    order, invoiced_order, event, complete, django_capture_on_commit_callbacks
):
    # Off: documents come only from the order page's buttons.
    with scopes_disabled():
        event.settings.ptinvoicing_auto_issue = False

    with (
        mock.patch("pretix_ptinvoicing.signals.issue_invoice") as task,
        django_capture_on_commit_callbacks(execute=True),
    ):
        ptinvoicing_order_paid(sender=event, order=order)
    task.apply_async.assert_not_called()

    # No HTTP mock registered: a credit() call would blow up the test.
    complete(_make_refund(invoiced_order, Decimal("23.00")))
    assert not _credit_notes().exists()


@pytest.mark.django_db
def test_billing_change_after_invoicing_alerts_and_reissues_nothing(invoiced_order):
    from django.core import mail as django_mail
    from pretix.base.models import InvoiceAddress

    from pretix_ptinvoicing.orderdata import billing_data
    from pretix_ptinvoicing.signals import ptinvoicing_order_modified

    order, event = invoiced_order, invoiced_order.event
    with scopes_disabled():
        event.settings.contact_mail = "org@example.org"
        # Even with pretix's own auto-reissue on: never without staff approval.
        event.settings.invoice_reissue_after_modify = True
        ia = InvoiceAddress.objects.create(order=order, street="Rua A")
        order.refresh_from_db()
        IssuedInvoice.objects.update(billing_data=billing_data(order))

        ptinvoicing_order_modified(event, order=order)  # nothing changed
        assert not django_mail.outbox

        ia.street = "Rua B"
        ia.save()
        order.refresh_from_db()
        with mock.patch("pretix_ptinvoicing.signals.transaction.on_commit") as commit:
            ptinvoicing_order_modified(event, order=order)

    assert not commit.called
    assert len(django_mail.outbox) == 1
    assert django_mail.outbox[0].to == ["org@example.org"]
    assert order.code in django_mail.outbox[0].subject
    assert IssuedInvoice.objects.count() == 1


@pytest.mark.django_db
def test_checkout_assets_load_only_with_the_custom_nif_field(event, rf):
    from pretix_ptinvoicing.signals import ptinvoicing_html_head

    request = rf.get("/")
    assert ptinvoicing_html_head(event, request=request) == ""
    event.settings.ptinvoicing_nif_custom_field = True
    head = ptinvoicing_html_head(event, request=request)
    assert "checkout.js" in head and "checkout.css" in head
    request.pci_dss_payment_page = True
    assert ptinvoicing_html_head(event, request=request) == ""
