import pytest
from django_scopes import scopes_disabled
from pretix.base.models import InvoiceAddress

from pretix_ptinvoicing.orderdata import (
    bare_tin,
    client_name,
    is_valid_pt_nif,
    one_line,
)


@pytest.mark.django_db
def test_bare_tin_without_invoice_address_is_none(order):
    assert bare_tin(order) is None


@pytest.mark.django_db
def test_bare_tin_strips_pt_prefix(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(order=order, vat_id="PT123456789")
    order.refresh_from_db()
    assert bare_tin(order) == "123456789"


@pytest.mark.django_db
def test_bare_tin_reads_the_custom_field_when_enabled(order):
    # pretix only shows its VAT ID field to business customers, so an individual's NIF has
    # to come from the custom invoice-address field.
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order, custom_field="237892294", country="PT"
        )
    order.refresh_from_db()

    assert bare_tin(order) is None
    assert bare_tin(order, custom_field_is_nif=True) == "237892294"


@pytest.mark.django_db
def test_bare_tin_keeps_a_non_portuguese_vat_id_unvalidated(order):
    # The PT check digit must not be applied to, say, a Spanish VAT id.
    with scopes_disabled():
        InvoiceAddress.objects.create(order=order, vat_id="ESX1234567", country="ES")
    order.refresh_from_db()

    assert bare_tin(order) == "ESX1234567"


def test_pt_nif_check_digit():
    assert is_valid_pt_nif("999999990")  # Fact.pt's final-consumer NIF
    assert is_valid_pt_nif("237892294")
    assert not is_valid_pt_nif("237892295")
    assert is_valid_pt_nif("123456789")  # yes, really — the check digit works out
    assert not is_valid_pt_nif("12345678")
    assert not is_valid_pt_nif("abcdefghi")


@pytest.mark.django_db
def test_client_name_prefers_the_company_and_truncates(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order,
            company="C" * 150,
            name_parts={"_scheme": "full", "full_name": "Jane Doe"},
        )
    order.refresh_from_db()
    assert len(client_name(order, 100)) == 100
    assert client_name(order, 100).startswith("CCC")


def test_one_line_flattens_newlines():
    # pretix's street is a TextField; both providers' APIs want a single line.
    assert one_line("Rua do Alecrim 11\n3.º Esq.", 100) == "Rua do Alecrim 11 3.º Esq."
    assert one_line("", 100) == ""
    assert one_line(None, 100) == ""
