from decimal import Decimal

import pytest
from django_scopes import scopes_disabled
from pretix.base.models import (
    Discount,
    InvoiceAddress,
    Item,
    OrderFee,
    OrderPosition,
    Voucher,
)

from pretix_ptinvoicing.orderdata import Line, invoice_lines, set_nets
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
    assert items[0]["price"] == "23.00000000"


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

    assert items[0]["price"] == "15.00000000"


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


HALF_UP = {"rounding": "ROUND_HALF_UP"}


FIVE = Decimal("0.00001")


def _stored_price(line):
    # Moloni keeps the price to 5 decimals (a draft sent 28.45528455, got 28.45528).
    return line.price_net.quantize(FIVE, **HALF_UP)


def _stored(line, places=7):
    # The unit net Moloni works from: the stored price less the line discount, which it
    # keeps to 7 decimals and applies at 5 (28.45528 at 42.8571429% → 16.26016, draft).
    # Fact.pt (2 decimals) is simulated as exact on an 8-decimal series.
    discount = line.discount.quantize(Decimal(1).scaleb(-places), **HALF_UP)
    return (_stored_price(line) * (100 - discount) / 100).quantize(FIVE, **HALF_UP)


def _pvp(line):
    # Moloni's "PVP Unit." column: the stored price plus VAT, to 5 decimals — the
    # listed price, before any discount.
    return (_stored_price(line) * (100 + line.tax_rate) / 100).quantize(FIVE)


def _check_document(lines, rate, places=7):
    """
    What a provider prints, computed the way Moloni did on real documents: incidence
    from each line's net rounded, total from the unrounded nets — as Moloni stores
    them. Both must match what the buyer paid. `lines` is [(gross, qty)] or
    [(gross, qty, listed gross)] for a line with a voucher/discount.
    """
    lines = set_nets(
        [
            Line("x", g, rate, "k", "r", qty=q, listed_gross=(l or (None,))[0])
            for g, q, *l in lines
        ],
        discount_places=places,
    )
    for line in lines:
        # The provider's own arithmetic lands on the net that was paid.
        sent = line.price_net * (100 - line.discount) / 100
        assert abs(sent - line.net) < Decimal("0.0000001"), line
        assert 0 <= line.discount <= 100
        assert line.discount == line.discount.quantize(Decimal(1).scaleb(-places))
    incidence = sum(
        (_stored(line, places) * line.qty).quantize(Decimal("0.01"), **HALF_UP)
        for line in lines
    )
    exact = sum(_stored(line, places) * line.qty for line in lines)
    paid = sum(line.gross * line.qty for line in lines)
    assert (exact * (100 + rate) / 100).quantize(Decimal("0.01"), **HALF_UP) == paid
    assert incidence == (paid * 100 / (100 + rate)).quantize(
        Decimal("0.01"), **HALF_UP
    ), lines
    assert all(line.net == line.net.quantize(Decimal("1e-8")) for line in lines)
    return lines


def test_tax_inclusive_prices_print_a_consistent_document():
    # L0YCM on a real Moloni account: 28.4553 + 23.5772 printed incidence 52.04 and VAT
    # 11.96 — 52.04 + 23% isn't 64.00. Now 28.45 + 23.58 = 52.03, VAT 11.97.
    lines = _check_document([(Decimal("35.00"), 1), (Decimal("29.00"), 1)], Decimal(23))
    assert [(line.net * line.qty).quantize(Decimal("0.01")) for line in lines] == [
        Decimal("28.45"),
        Decimal("23.58"),
    ]


def test_identical_tickets_print_their_unit_price_as_close_as_moloni_can():
    # U0RYS: 3 × 35.00 + 2 × 29.00 at 23% printed "PVP Unit." 35,001 / 34,99953 /
    # 28,99996 with 4-decimal nets, one line per ticket. One line per kind now — and
    # 35.00 / 1.23 repeats forever, so no 5-decimal net prints 35,00000: 28.45528 →
    # 34,99999 and 28.45529 → 35,00001 (a real document printed the first).
    lines = _check_document([(Decimal("35.00"), 3), (Decimal("29.00"), 2)], Decimal(23))
    assert [_pvp(line) for line in lines] == [
        Decimal("34.99999"),
        Decimal("29.00001"),
    ]


def test_tax_exclusive_prices_stay_whole_cents():
    lines = _check_document([(Decimal("43.05"), 1), (Decimal("35.67"), 1)], Decimal(23))
    assert [line.net for line in lines] == [Decimal(35), Decimal(29)]


def test_nets_add_up_for_every_cent_price_and_random_documents():
    import random

    rates = (Decimal(23), Decimal(13), Decimal(6), Decimal(0))
    for cents in range(1, 20001):
        for rate in rates:
            for qty in (1, 3):
                lines = _check_document([(Decimal(cents) / 100, qty)], rate)
                # A single kind of ticket prints its price to within Moloni's last
                # decimal — exactly, whenever a 5-decimal net can.
                assert abs(_pvp(lines[0]) - lines[0].gross) <= Decimal("0.00001")
    rng = random.Random(0)
    for _ in range(5000):
        lines = [
            (Decimal(rng.randint(1, 20000)) / 100, rng.randint(1, 5))
            for _ in range(rng.randint(2, 12))
        ]
        _check_document(lines, rng.choice(rates))


def test_discounted_lines_add_up_at_either_providers_precision():
    # A fixed-amount voucher (35.00 → 20.00 is 42.857142…%) can't be exact as a
    # 2-decimal percentage, so the net is derived back from the discount instead; a
    # 100% voucher sends the listed price with 100% off. Moloni keeps 7 decimals,
    # Fact.pt 2 — the totals must be what was paid either way.
    import random

    lines = [
        (Decimal("20.00"), 1, Decimal("35.00")),
        (Decimal("0.00"), 2, Decimal("35.00")),
        (Decimal("31.50"), 1, Decimal("35.00")),  # 10% voucher
        (Decimal("29.00"), 1),
    ]
    for places in (7, 2):
        got = _check_document(lines, Decimal(23), places)
        assert [line.discount for line in got] == [
            Decimal("42.8571429") if places == 7 else Decimal("42.86"),
            Decimal(100),
            Decimal("10.0000000") if places == 7 else Decimal("10.00"),
            Decimal(0),
        ]
        # The free ticket is the listed price, 100% off: prints 35.00 and adds nothing.
        assert _pvp(got[1]) == Decimal("34.99999") and got[1].net == 0
        # Free of the discount's rounding, a percentage voucher prints the listed price.
        assert _pvp(got[2]) == Decimal("34.99999")
    # The 2-decimal one is a fraction of a cent off the listed price, the 7-decimal
    # one is not: 28.45528455 × (1 − 0.428571429) = 16.26016.
    assert _pvp(_check_document(lines, Decimal(23), 7)[0]) == Decimal("34.99999")
    assert _pvp(_check_document(lines, Decimal(23), 2)[0]) == Decimal("35.00175")

    rng = random.Random(1)
    rates = (Decimal(23), Decimal(13), Decimal(6), Decimal(0))
    for _ in range(3000):
        lines = []
        for _ in range(rng.randint(1, 8)):
            listed = Decimal(rng.randint(1, 20000)) / 100
            kind = rng.random()
            if kind < 0.3:
                lines.append((listed, rng.randint(1, 5)))
            elif kind < 0.4:
                lines.append((Decimal(0), rng.randint(1, 5), listed))
            else:
                gross = Decimal(rng.randint(0, int(listed * 100))) / 100
                lines.append((gross, rng.randint(1, 5), listed))
        for places in (7, 2):
            _check_document(lines, rng.choice(rates), places)


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

    assert [i["price"] for i in items] == ["23.00000000", "2.00000000"]
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


@pytest.mark.django_db
def test_free_positions_and_fees_are_left_off_the_document(event, order, position):
    # A free product in a paid order (a bundled add-on, a 100% voucher on one ticket)
    # says nothing about the money received, and Fact.pt rejects a price that isn't
    # positive ("Número positivo"). Only what was paid for is invoiced.
    with scopes_disabled():
        free_item = Item.objects.create(
            event=event, name="Free add-on", default_price=Decimal("0.00")
        )
        OrderPosition.objects.create(order=order, item=free_item, price=Decimal("0.00"))
        OrderFee.objects.create(
            order=order,
            fee_type=OrderFee.FEE_TYPE_PAYMENT,
            value=Decimal("0.00"),
            tax_rate=Decimal("0.00"),
            tax_value=Decimal("0.00"),
        )
        lines = invoice_lines(order)
        items = build_items_block(order, event.settings)

    assert [(line.name, line.gross) for line in lines] == [("Ticket", Decimal("23.00"))]
    assert [i["price"] for i in items] == ["23.00000000"]
    # Never exponent notation: str() of a zero Decimal at 8 places is "0E-8".
    assert all("E" not in i["price"] for i in items)


@pytest.mark.django_db
def test_vouchers_and_discounts_show_as_line_discounts(event, order, item):
    # pretix stores the *final* price on the position plus what the voucher took off
    # (voucher_budget_use); an automatic discount only its percentage. The line shows
    # the listed price with the discount and names what took it off; the totals stay
    # what was paid. A ticket made free by a voucher is on the document, 100% off.
    with scopes_disabled():
        twenty = Voucher.objects.create(
            event=event, code="TWENTY", price_mode="set", value=Decimal("20.00")
        )
        free = Voucher.objects.create(
            event=event, code="FREE", price_mode="set", value=Decimal("0.00")
        )
        early = Discount.objects.create(
            event=event,
            internal_name="Early bird",
            benefit_discount_matching_percent=Decimal("10.00"),
        )
        OrderPosition.objects.create(
            order=order,
            item=item,
            price=Decimal("20.00"),
            voucher=twenty,
            voucher_budget_use=Decimal("3.00"),
        )
        OrderPosition.objects.create(
            order=order,
            item=item,
            price=Decimal("0.00"),
            voucher=free,
            voucher_budget_use=Decimal("23.00"),
        )
        OrderPosition.objects.create(
            order=order, item=item, price=Decimal("20.70"), discount=early
        )
        OrderPosition.objects.create(order=order, item=item, price=Decimal("23.00"))
        lines = invoice_lines(order)
        items = build_items_block(order, event.settings)

    assert [
        (line.name, line.gross, line.listed_gross, line.discount) for line in lines
    ] == [
        (
            "Ticket (voucher TWENTY)",
            Decimal("20.00"),
            Decimal("23.00"),
            Decimal("13.04"),
        ),
        ("Ticket (voucher FREE)", Decimal("0.00"), Decimal("23.00"), Decimal(100)),
        ("Ticket (Early bird)", Decimal("20.70"), Decimal("23.00"), Decimal("10.00")),
        ("Ticket", Decimal("23.00"), None, Decimal(0)),
    ]
    # Fact.pt gets price × (1 − discount) = the net paid (0% VAT in this fixture). The
    # 13.04% line's price is derived back from the rounded percentage, so it is a
    # fraction of a cent under the listed 23.00 — the alternative, 23.00 at 13.04%,
    # would be 20.0008 and could land on the wrong cent with a quantity.
    assert [(i["price"], i.get("discount")) for i in items] == [
        ("22.99908004", "13.04"),
        ("23.00000000", "100"),
        ("23.00000000", "10.00"),
        ("23.00000000", None),
    ]
    assert (Decimal("22.99908004") * Decimal("0.8696")).quantize(
        Decimal("0.01")
    ) == Decimal("20.00")
