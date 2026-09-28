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
def test_provider_label_falls_back_to_raw_identifier(order):
    with scopes_disabled():
        invoice = IssuedInvoice(order=order, provider="factpt")
        assert invoice.provider_label == "Fact.pt"
        assert IssuedInvoice(order=order, provider="gone").provider_label == "gone"
