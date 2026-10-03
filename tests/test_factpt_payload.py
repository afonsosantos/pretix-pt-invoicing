from decimal import Decimal

import pytest
from django_scopes import scopes_disabled
from pretix.base.models import InvoiceAddress, OrderFee, OrderPosition

from pretix_ptinvoicing.orderdata import Line
from pretix_ptinvoicing.providers.factpt.payload import (
    bare_tin,
    build_client_block,
    build_credit_payload,
    build_items_block,
    build_payload,
)


@pytest.mark.django_db
def test_build_payload_passes_identifier_id_through(event, order, position):
    with scopes_disabled():
        event.settings.factpt_default_tax_id = 5
        payload = build_payload(order, event.settings, "pretix-dummy-FOOBAR")

    assert payload["document"]["identifierId"] == "pretix-dummy-FOOBAR"
    assert payload["document"]["reference"] == "FOOBAR"
    assert len(payload["items"]) == 1


@pytest.mark.django_db
def test_build_credit_payload_has_no_client_or_items_block(order):
    # POST /documents/{id}/credit only accepts date/comments/reference/identifierId/
    # download/language — there's nothing to describe the client or line items with.
    payload = build_credit_payload(order, "pretix-dummy-FOOBAR-credit")
    assert payload["document"]["identifierId"] == "pretix-dummy-FOOBAR-credit"
    assert payload["document"]["reference"] == "FOOBAR"
    assert "client" not in payload
    assert "items" not in payload


@pytest.mark.django_db
def test_build_client_block_without_invoice_address_is_final_consumer(order):
    client = build_client_block(order)
    assert client["finalConsumer"] is True
    assert "tin" not in client
    assert client["name"] == "Consumidor Final"
    # Fact.pt rejects "-" as a PT zip.
    assert client["zip"] == "0000-000"
    # forceTin means "duplicate a final consumer whose name+country already exists", so
    # sending it here created a new client on every issuance until Fact.pt could no longer
    # resolve one ("Multiple clients with same tin. Specify an ID."). Reuse is handled by
    # FactptProvider._resolve_client_id instead.
    assert "forceTin" not in client
    # ric/retention are asserted by
    # test_build_client_block_without_nif_omits_tin_ric_and_retention: Fact.pt's docs
    # require them absent on this branch, and a sandbox document confirmed it works.


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
    assert items[0]["price"] == "23.0000"


@pytest.mark.django_db
def test_build_client_block_without_nif_omits_tin_ric_and_retention(order):
    # Fact.pt's docs: with finalConsumer true, tin/ric/retention must not be declared —
    # it then registers the client under the final-consumer NIF 999999990 itself.
    client = build_client_block(order)
    assert client["finalConsumer"] is True
    assert "tin" not in client
    assert "ric" not in client
    assert "retention" not in client


@pytest.mark.django_db
def test_build_client_block_with_nif_declares_ric_and_retention(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(order=order, vat_id="PT123456789")
    order.refresh_from_db()
    client = build_client_block(order)
    assert client["finalConsumer"] is False
    assert client["tin"] == "123456789"
    assert client["ric"] is False
    assert client["retention"] is False


@pytest.mark.django_db
def test_build_client_block_prefers_the_company_over_the_person(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order,
            company="Acme Lda",
            name_parts={"_scheme": "full", "full_name": "Jane Doe"},
            vat_id="PT123456789",
        )
    order.refresh_from_db()
    # Fact.pt has one name field; the invoice is made out to the company.
    assert build_client_block(order)["name"] == "Acme Lda"


@pytest.mark.django_db
def test_build_client_block_flattens_and_truncates_to_factpt_limits(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order,
            company="C" * 150,
            street="Rua do Alecrim 11\n3.º Esq.\n" + "x" * 150,
            city="C" * 80,
        )
    order.refresh_from_db()
    client = build_client_block(order)

    # A multi-line street is the realistic failure: pretix's field is a textarea and
    # Fact.pt rejects anything but one line, which would fail the whole document.
    assert "\n" not in client["address"]
    assert client["address"].startswith("Rua do Alecrim 11 3.º Esq.")
    assert len(client["address"]) == 100
    assert len(client["name"]) == 100
    assert len(client["city"]) == 50


@pytest.mark.django_db
def test_build_client_block_never_sends_the_email(order):
    # pretix e-mails the document itself (the e-mail settings on the main page).
    assert "email" not in build_client_block(order)


@pytest.mark.django_db
def test_build_items_block_sends_the_net_price(event, order, item):
    # Fact.pt adds taxId's VAT on top of `price`, while pretix's position.price is gross.
    with scopes_disabled():
        OrderPosition.objects.create(
            order=order,
            item=item,
            price=Decimal("18.45"),
            tax_rate=Decimal("23.00"),
            tax_value=Decimal("3.45"),
        )
        event.settings.factpt_default_tax_id = 5
        items = build_items_block(order, event.settings)

    assert items[0]["price"] == "15.0000"


@pytest.mark.django_db
def test_build_items_block_net_price_survives_the_round_trip(event, order, item):
    # 15.00 at 23%: pretix's cent-rounded tax_value gives a net of 12.20, and Fact.pt's
    # 12.20 + 23% is 15.006 → 15.01, a cent above what the buyer paid.
    with scopes_disabled():
        OrderPosition.objects.create(
            order=order,
            item=item,
            price=Decimal("15.00"),
            tax_rate=Decimal("23.00"),
            tax_value=Decimal("2.80"),
        )
        items = build_items_block(order, event.settings)

    net = Decimal(items[0]["price"])
    assert (net * Decimal("1.23")).quantize(Decimal("0.01")) == Decimal("15.00")


def test_net_price_round_trips_for_every_cent_price():
    rates = (Decimal(23), Decimal(13), Decimal(6), Decimal(0))
    for cents in range(1, 20001):
        gross = Decimal(cents) / 100
        for rate in rates:
            line = Line("x", gross, rate, "k", "r")
            back = (line.net * (100 + rate) / 100).quantize(Decimal("0.01"))
            assert back == gross, (gross, rate)


@pytest.mark.django_db
def test_build_items_block_includes_fees(event, order, position):
    # A payment fee is part of what the buyer paid, so it's part of the invoice.
    with scopes_disabled():
        OrderFee.objects.create(
            order=order,
            fee_type=OrderFee.FEE_TYPE_PAYMENT,
            value=Decimal("2.00"),
            tax_rate=Decimal("0.00"),
            tax_value=Decimal("0.00"),
        )
        items = build_items_block(order, event.settings)

    assert [i["price"] for i in items] == ["23.0000", "2.0000"]
    assert items[1]["description"] == "Payment fee"


@pytest.mark.django_db
def test_bare_tin_accepts_spaces_and_dots(order):
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order, vat_id="PT 237.892.294", country="PT"
        )
    order.refresh_from_db()
    assert bare_tin(order) == "237892294"


@pytest.mark.django_db
def test_bare_tin_ignores_an_invalid_portuguese_nif(order):
    # Free text typed by the buyer: a bad check digit would be rejected by the provider,
    # which is a terminal failure for the document. Issue to the final consumer instead.
    # 237892294 is valid, so 237892295 differs only in the check digit. (Watch out with
    # made-up numbers: 123456789 happens to be a *valid* NIF.)
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=order, custom_field="237892295", country="PT"
        )
    order.refresh_from_db()

    assert bare_tin(order, custom_field_is_nif=True) is None
    assert build_client_block(order, custom_field_is_nif=True)["finalConsumer"] is True
