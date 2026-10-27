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


@pytest.mark.django_db
def test_client_name_falls_back_from_blank_company_to_person(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order,
            company="   ",
            name_parts={"_scheme": "full", "full_name": "Jane Doe"},
        )
    order.refresh_from_db()
    assert client_name(order) == "Jane Doe"


@pytest.mark.django_db
def test_client_name_without_a_name_is_the_final_consumer_not_the_email(order):
    # An empty invoice address, as pretix saves one when the buyer skips it.
    with scopes_disabled():
        InvoiceAddress.objects.create(order=order)
    order.refresh_from_db()
    assert client_name(order) == "Consumidor Final"


def test_one_line_flattens_newlines():
    # pretix's street is a TextField; both providers' APIs want a single line.
    assert one_line("Rua do Alecrim 11\n3.º Esq.", 100) == "Rua do Alecrim 11 3.º Esq."
    assert one_line("", 100) == ""
    assert one_line(None, 100) == ""


@pytest.mark.django_db
def test_document_date_is_the_events_local_day(order, monkeypatch):
    from datetime import UTC, datetime

    from pretix_ptinvoicing import orderdata

    # 00:30 in Lisbon summer time (UTC+1) is still the previous day in UTC.
    monkeypatch.setattr(
        orderdata.timezone,
        "now",
        lambda: datetime(2026, 7, 14, 23, 30, tzinfo=UTC),
    )
    order.event.settings.timezone = "Europe/Lisbon"
    assert orderdata.document_date(order) == "2026-07-15"


@pytest.mark.django_db
def test_lines_carry_attendee_names_only_when_the_buyer_asked(order, position):
    from pretix_ptinvoicing.orderdata import invoice_lines

    with scopes_disabled():
        position.attendee_name_parts = {"_legacy": "Ana Silva"}
        position.save()
        assert invoice_lines(order)[0].name == str(position.item.name)

        order.meta_info = '{"contact_form_data": {"ptinvoicing_itemize": true}}'
        order.save()
        order = type(order).objects.get(pk=order.pk)  # meta_info_data is cached
        assert invoice_lines(order)[0].name == f"{position.item.name} — Ana Silva"


@pytest.mark.django_db
def test_billing_changed_compares_with_what_was_issued(order):
    from pretix_ptinvoicing.models import IssuedInvoice
    from pretix_ptinvoicing.orderdata import billing_changed, billing_data

    with scopes_disabled():
        ia = InvoiceAddress.objects.create(order=order, street="Rua A", country="PT")
        order.refresh_from_db()
        doc = IssuedInvoice(billing_data=None)
        assert not billing_changed(order, doc)  # issued before this was stored

        doc.billing_data = billing_data(order)
        assert not billing_changed(order, doc)

        ia.street = "Rua B"
        ia.save()
        order.refresh_from_db()
        assert billing_changed(order, doc)
