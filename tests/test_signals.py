from decimal import Decimal

import pytest
import responses
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment, OrderRefund

from pretix_ptinvoicing.models import IssuedInvoice

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


def _make_refund(order, amount, state=OrderRefund.REFUND_STATE_CREATED):
    with scopes_disabled():
        return OrderRefund.objects.create(
            order=order,
            amount=amount,
            state=state,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            provider="manual",
        )


@pytest.mark.django_db
@responses.activate
def test_full_refund_auto_issues_a_credit_note(invoiced_order):
    responses.add(
        responses.POST,
        CREDIT_URL,
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "999"}}},
        status=200,
    )
    refund = _make_refund(invoiced_order, Decimal("23.00"))

    with scopes_disabled():
        refund.done()

    with scopes_disabled():
        credit_note = IssuedInvoice.objects.get(kind=IssuedInvoice.KIND_CREDIT_NOTE)
    assert credit_note.status == IssuedInvoice.STATUS_SUCCESS
    assert credit_note.document_id == "999"


@pytest.mark.django_db
def test_partial_refund_does_not_auto_issue_a_credit_note(invoiced_order):
    # No responses mock registered: a credit() call would blow up the test — the whole
    # point is that a partial refund must not reach the provider at all, since no provider
    # here can credit less than a document's full value.
    refund = _make_refund(invoiced_order, Decimal("10.00"))

    with scopes_disabled():
        refund.done()

    with scopes_disabled():
        assert not IssuedInvoice.objects.filter(
            kind=IssuedInvoice.KIND_CREDIT_NOTE
        ).exists()


@pytest.mark.django_db
@responses.activate
def test_second_partial_refund_completing_the_total_triggers_the_credit_note(
    invoiced_order,
):
    # Two partial refunds that together cover the full amount must still end up crediting
    # the whole document — payment_refund_sum, not any single refund's own amount, is what
    # the receiver checks.
    responses.add(
        responses.POST,
        CREDIT_URL,
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "999"}}},
        status=200,
    )
    first = _make_refund(invoiced_order, Decimal("10.00"))
    with scopes_disabled():
        first.done()
    with scopes_disabled():
        assert not IssuedInvoice.objects.filter(
            kind=IssuedInvoice.KIND_CREDIT_NOTE
        ).exists()

    second = _make_refund(invoiced_order, Decimal("13.00"))
    with scopes_disabled():
        second.done()

    with scopes_disabled():
        assert IssuedInvoice.objects.filter(
            kind=IssuedInvoice.KIND_CREDIT_NOTE, status=IssuedInvoice.STATUS_SUCCESS
        ).exists()


@pytest.mark.django_db
def test_refund_signal_ignores_events_without_the_plugin_enabled(order, event):
    # Not the invoiced_order fixture: this event never turns the plugin on, so the
    # receiver must bail before it ever reaches the (unmocked) provider.
    with scopes_disabled():
        order.status = Order.STATUS_PAID
        order.total = Decimal("23.00")
        order.save(update_fields=["status", "total"])
        OrderPayment.objects.create(
            order=order,
            amount=Decimal("23.00"),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            provider="manual",
        )
    refund = _make_refund(order, Decimal("23.00"))

    with scopes_disabled():
        refund.done()

    with scopes_disabled():
        assert IssuedInvoice.objects.count() == 0


@pytest.mark.django_db
def test_refund_signal_ignores_non_done_states(invoiced_order):
    # A plain save() with state left as "created" must not trigger anything — only the
    # transition to REFUND_STATE_DONE does.
    _make_refund(
        invoiced_order, Decimal("23.00"), state=OrderRefund.REFUND_STATE_CREATED
    )

    with scopes_disabled():
        assert not IssuedInvoice.objects.filter(
            kind=IssuedInvoice.KIND_CREDIT_NOTE
        ).exists()
