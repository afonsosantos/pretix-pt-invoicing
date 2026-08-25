import pytest
from django_scopes import scopes_disabled
from pretix.base.models import InvoiceAddress

from pretix_factpt.payload import (
    bare_tin,
    build_client_block,
    build_identifier_id,
    build_items_block,
)


@pytest.mark.django_db
def test_build_identifier_id_format(event, order):
    assert build_identifier_id(event, order) == "pretix-dummy-FOOBAR"


@pytest.mark.django_db
def test_build_identifier_id_truncates_to_50_chars(order):
    with scopes_disabled():
        order.event.slug = "a" * 60
    assert len(build_identifier_id(order.event, order)) == 50


@pytest.mark.django_db
def test_build_client_block_without_invoice_address_is_final_consumer(order):
    client = build_client_block(order)
    assert client["finalConsumer"] is True
    assert "tin" not in client
    assert "ric" not in client
    assert "retention" not in client
    assert client["forceTin"] is True
    assert client["name"] == order.email


@pytest.mark.django_db
def test_build_client_block_with_vat_id_strips_pt_prefix(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order,
            name_parts={"_scheme": "full", "full_name": "Jane Doe"},
            vat_id="PT123456789",
        )
    order.refresh_from_db()
    client = build_client_block(order)
    assert client["finalConsumer"] is False
    assert client["tin"] == "123456789"
    assert client["name"] == "Jane Doe"
    assert client["forceTin"] is True


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
def test_build_client_block_with_client_id_ignores_invoice_address(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(order=order, vat_id="PT123456789")
    order.refresh_from_db()
    assert build_client_block(order, client_id="42") == {"id": "42"}


@pytest.mark.django_db
def test_build_items_block_reads_defaults_from_event_settings(event, order, position):
    with scopes_disabled():
        event.settings.factpt_default_tax_id = 5
        event.settings.factpt_default_unit_id = 2
        event.settings.factpt_default_type = "product"

        items = build_items_block(order, event.settings)

    assert len(items) == 1
    assert items[0]["taxId"] == 5
    assert items[0]["unitId"] == 2
    assert items[0]["type"] == "product"
    assert items[0]["price"] == "23.00"
