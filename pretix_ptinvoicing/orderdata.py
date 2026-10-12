"""
Reading buyer data off a pretix order, provider-independent.

These are pretix-side and Portugal-side concerns — how pretix stores a NIF, what counts as
a valid one, which field is the invoice's recipient — so they belong here rather than in
whichever provider happened to need them first. Field *limits* stay with each provider,
since those are its API's rules.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)

FINAL_CONSUMER_NIF = "999999990"
FINAL_CONSUMER_NAME = "Consumidor Final"


@dataclass
class Line:
    """One line of the document: an order position or a fee, as the providers see it."""

    name: str
    gross: Decimal
    tax_rate: Decimal
    # Stable per pretix product/fee kind — Moloni keys its catalog products on it.
    catalog_key: str
    # Unique per line — the line's own reference on the document.
    reference: str
    qty: int = 1
    # Net unit price, 8 decimals — set by set_nets(), which explains the choice.
    net: Decimal = None

    @property
    def exact_net(self):
        """Net of the whole line (unit × qty), unrounded."""
        return self.gross * self.qty * 100 / (100 + self.tax_rate)


CENT = Decimal("0.01")
# Fact.pt's 8-decimal series is the most either provider takes.
NET_PLACES = Decimal("0.00000001")
# How far a line's net may sit from the cent it must round to, either side: short of
# the half cent, so it rounds there whichever rounding mode the provider uses.
SLACK = Decimal("0.0049")


def set_nets(lines):
    """
    Each line's net unit price: 8 decimals, chosen per VAT rate across the document.

    Providers add their VAT on top of the net, so pretix's gross prices have to come
    back out of it. Not gross - tax_value: pretix rounds tax_value to cents, and a
    2-decimal net can't always get back to the gross at all (no net gives 15.00 at 23%:
    12.19 → 14.99, 12.20 → 15.01). Seen on real Moloni documents, with prices including
    VAT:
    - Moloni takes the total from the unrounded nets, the incidence from each line's net
      *rounded*, and the VAT as the difference: 35.00 + 29.00 at 23% sent as 28.4553 +
      23.5772 printed incidence 52.04 with VAT 11.96 — 52.04 + 23% isn't 64.00.
    - It stores the price to 5 decimals and prints each line's unit price with VAT
      ("PVP Unit.") from it: 4-decimal nets printed 35.00 as 35,001 or 34,99953. With
      more decimals it prints the price exactly whenever a 5-decimal net can — not
      always: 35.00 / 1.23 repeats forever, so 28.45528 prints 34,99999 (28.45529
      would be 35,00001). Only prices whose net is whole cents (pretix's "price
      includes tax" off) avoid that.
    Prices entered without VAT were clean only because their nets are whole cents.

    So, per rate: the cents each line's net rounds to are the exact total rounded
    (52.03), shared out by largest remainder (28.45 + 23.58); each line keeps its exact
    net unless that doesn't round to its cent, in which case it's held within SLACK of
    it — the only lines whose unit price then doesn't print clean. Whatever that moved
    is handed back to lines with room, so the nets still add up to the exact total and
    the document's total is what the buyer paid. Identical tickets share one line
    (invoice_lines), so this rarely has to move anything.
    """
    for rate in {line.tax_rate for line in lines}:
        group = [line for line in lines if line.tax_rate == rate]
        exact = [line.exact_net for line in group]
        cents = [e.quantize(CENT, rounding="ROUND_FLOOR") for e in exact]
        total = sum(exact).quantize(CENT, rounding="ROUND_HALF_UP")
        short = int((total - sum(cents)) / CENT)
        by_remainder = sorted(
            range(len(group)), key=lambda i: exact[i] - cents[i], reverse=True
        )
        for i in by_remainder[:short]:
            cents[i] += CENT

        nets = [
            e
            if e.quantize(CENT, rounding="ROUND_HALF_UP") == c
            else min(max(e, c - SLACK), c + SLACK)
            for e, c in zip(exact, cents)
        ]
        # Hand back what that moved only if the document's total would otherwise miss
        # what was paid — each line moved is one more unit price not printing clean.
        paid = sum(line.gross * line.qty for line in group)
        if (sum(nets) * (100 + rate) / 100).quantize(CENT, "ROUND_HALF_UP") != paid:
            residual = sum(exact) - sum(nets)
            for i, c in enumerate(cents):
                moved = min(max(nets[i] + residual, c - SLACK), c + SLACK)
                residual -= moved - nets[i]
                nets[i] = moved
        for line, net in zip(group, nets):
            line.net = (net / line.qty).quantize(NET_PLACES)
    return lines


def invoice_lines(order):
    """
    Everything the buyer paid for: positions *and* fees (payment, service, shipping…).
    Leaving fees out would issue an invoice below the order total.
    """
    # The buyer ticked "itemize by attendee" at checkout (signals.contact_form_fields).
    itemize = (
        (order.meta_info_data or {})
        .get("contact_form_data", {})
        .get("ptinvoicing_itemize")
    )
    # Identical tickets share one line with a quantity — one line per attendee only when
    # the buyer asked for it. Also what keeps unit prices clean (set_nets).
    lines = {}
    for p in order.positions.select_related("item").order_by("positionid"):
        name = (
            f"{p.item.name} — {p.attendee_name}"
            if itemize and p.attendee_name
            else str(p.item.name)
        )
        key = (p.pk,) if itemize else (name, p.item_id, p.price, p.tax_rate)
        if key in lines:
            lines[key].qty += 1
            continue
        lines[key] = Line(
            name=name,
            gross=p.price,
            tax_rate=p.tax_rate,
            catalog_key=f"item-{p.item_id}",
            reference=f"pretix-{p.pk}",
        )
    lines = list(lines.values())
    lines += [
        Line(
            name=f.description or str(f.get_fee_type_display()),
            gross=f.value,
            tax_rate=f.tax_rate,
            catalog_key=f"fee-{f.fee_type}",
            reference=f"pretix-fee-{f.pk}",
        )
        for f in order.fees.all()
        if f.value
    ]
    return set_nets(lines)


def one_line(value, limit):
    """Collapse any whitespace run (newlines included) into spaces, then truncate."""
    return " ".join(str(value).split())[:limit] if value else ""


def is_valid_pt_nif(nif):
    # Portuguese NIF check digit (mod 11).
    if len(nif) != 9 or not nif.isdigit():
        return False
    total = sum(int(d) * (9 - i) for i, d in enumerate(nif[:8]))
    check = 11 - (total % 11)
    return (0 if check >= 10 else check) == int(nif[8])


def bare_tin(order, custom_field_is_nif=False):
    """
    The buyer's NIF, bare (no "PT" prefix), or None.

    pretix only shows its own VAT ID field to buyers who tick "Business customer"
    (`data-display-dependency` on `is_business`, hardcoded in pretix's invoice address
    form), so an individual who wants their NIF on the invoice has nowhere to put it. The
    usual way out is pretix's custom invoice-address field, which is shown to everyone —
    `custom_field_is_nif` says the organizer has repurposed it for exactly that.
    """
    ia = getattr(order, "invoice_address", None)
    if not ia:
        return None

    vat_id = (getattr(ia, "vat_id", None) or "").strip()
    from_custom_field = False
    if not vat_id and custom_field_is_nif:
        vat_id = (getattr(ia, "custom_field", None) or "").strip()
        from_custom_field = True
    if not vat_id:
        return None

    # Buyers type "PT 123 456 789" or "123.456.789" as often as the bare number.
    vat_id = re.sub(r"[\s.\-]", "", vat_id)
    looks_portuguese = vat_id.upper().startswith("PT") or (
        ia.country and str(ia.country) == "PT"
    )
    tin = vat_id[2:].strip() if vat_id.upper().startswith("PT") else vat_id

    if looks_portuguese and not is_valid_pt_nif(tin):
        # Don't hand a provider a number that will bounce; issue to the final consumer
        # instead, which is what would have happened without a NIF at all.
        logger.warning(
            "ptinvoicing: ignoring invalid NIF %r on order %s (from %s)",
            tin,
            order.code,
            "custom field" if from_custom_field else "VAT ID field",
        )
        return None
    return tin


def billing_data(order):
    """
    The buyer data an invoice is made out with — stored on each issued document, so a
    later change by the buyer can be spotted (signals.ptinvoicing_order_modified).
    """
    ia = getattr(order, "invoice_address", None)
    return {
        "name": client_name(order),
        "nif": bare_tin(
            order,
            custom_field_is_nif=order.event.settings.get(
                "ptinvoicing_nif_custom_field", as_type=bool, default=False
            ),
        ),
        "street": one_line(ia and ia.street, 1000),
        "zipcode": one_line(ia and ia.zipcode, 100),
        "city": one_line(ia and ia.city, 1000),
        "country": str(ia.country) if ia and ia.country else "",
    }


def billing_changed(order, document):
    # Rows issued before billing_data was stored have nothing to compare against.
    return bool(document.billing_data) and document.billing_data != billing_data(order)


def client_name(order, limit=100):
    """
    Who the invoice is made out to.

    Neither provider has a separate company field, so a business buyer's company has to be
    the name — that is the entity being invoiced. Falls back to the person, then to
    "Consumidor Final" — never the email, which isn't a name to print on an invoice.
    """
    ia = getattr(order, "invoice_address", None)
    company = ia and getattr(ia, "company", None)
    person = ia and getattr(ia, "name", None)
    # A whitespace-only company value is truthy, but is not a usable name. Normalize
    # each candidate before falling back so it cannot hide the person's name.
    return one_line(company, limit) or one_line(person, limit) or FINAL_CONSUMER_NAME
