import pytest
from django_scopes import scopes_disabled

from pretix_ptinvoicing.models import IssuedInvoice


@pytest.mark.django_db
def test_build_identifier_id_format(event, order):
    assert IssuedInvoice.build_identifier_id(event, order) == "pretix-dummy-FOOBAR"


@pytest.mark.django_db
def test_build_identifier_id_truncates_to_50_chars(order):
    with scopes_disabled():
        order.event.slug = "a" * 60
    assert len(IssuedInvoice.build_identifier_id(order.event, order)) == 50


@pytest.mark.django_db
def test_later_cycles_get_their_own_identifier_even_when_truncated(event, order):
    # Paid again after a credited refund: a new key, cycle 0 unchanged.
    assert (
        IssuedInvoice.build_identifier_id(event, order, cycle=1)
        == "pretix-dummy-FOOBAR-r1"
    )
    with scopes_disabled():
        order.event.slug = "a" * 60
    long_id = IssuedInvoice.build_identifier_id(order.event, order, cycle=2)
    assert len(long_id) == 50
    assert long_id.endswith("-r2")


@pytest.mark.django_db
def test_build_credit_identifier_id_is_distinct_from_the_invoice_one(event, order):
    invoice_id = IssuedInvoice.build_identifier_id(event, order)
    assert (
        IssuedInvoice.build_credit_identifier_id(invoice_id)
        == "pretix-dummy-FOOBAR-credit"
    )
    assert IssuedInvoice.build_credit_identifier_id(invoice_id) != invoice_id


@pytest.mark.django_db
def test_build_credit_identifier_id_keeps_its_suffix_when_truncated(order):
    with scopes_disabled():
        order.event.slug = "a" * 60
    invoice_id = IssuedInvoice.build_identifier_id(order.event, order)
    credit_id = IssuedInvoice.build_credit_identifier_id(invoice_id)
    assert len(credit_id) == 50
    assert credit_id.endswith("-credit")
    assert credit_id != invoice_id


@pytest.mark.django_db
def test_long_slugs_keep_keys_unique_across_orders_and_cycles(event, order):
    # Slugs can be 50 characters: plain truncation cut the order code off, so two
    # orders shared one key (IntegrityError), and a second credit note shared the
    # first's (silently skipped).
    other = type(order)(code="ZZZZZ", event=event)
    with scopes_disabled():
        event.slug = "e" * 50
    keys = {
        IssuedInvoice.build_identifier_id(event, o, cycle)
        for o in (order, other)
        for cycle in (0, 1, 2)
    }
    credits = {IssuedInvoice.build_credit_identifier_id(k) for k in keys}
    assert len(keys) == 6
    assert len(credits) == 6
    assert not keys & credits
    assert all(len(k) <= 50 for k in keys | credits)


@pytest.mark.django_db
def test_provider_label_falls_back_to_raw_identifier(order):
    with scopes_disabled():
        invoice = IssuedInvoice(order=order, provider="factpt")
        assert invoice.provider_label == "Fact.pt"
        assert IssuedInvoice(order=order, provider="gone").provider_label == "gone"
