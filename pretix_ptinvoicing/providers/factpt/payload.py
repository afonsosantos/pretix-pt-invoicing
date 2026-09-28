import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# Fact.pt's documented limits for the client block. Every one of these is a single line:
# pretix's street is a TextField, so a buyer pressing Enter would otherwise send a newline
# and get the whole document rejected.
MAX_NAME = 100
MAX_ADDRESS = 100
MAX_CITY = 50
MAX_EMAIL = 100


def _one_line(value, limit):
    # Collapse any whitespace run (newlines included) into single spaces, then truncate.
    return " ".join(str(value).split())[:limit] if value else ""


def is_valid_pt_nif(nif):
    # Portuguese NIF check digit (mod 11). Worth having because the custom-field route
    # below is free text typed by the buyer: an invalid number is rejected by the provider,
    # which is a terminal failure for the whole document.
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

    looks_portuguese = vat_id.upper().startswith("PT") or (
        ia.country and str(ia.country) == "PT"
    )
    tin = vat_id[2:].strip() if vat_id.upper().startswith("PT") else vat_id

    if looks_portuguese and not is_valid_pt_nif(tin):
        # Don't hand the provider a number that will bounce; issue to the final consumer
        # instead, which is what would have happened without a NIF at all.
        logger.warning(
            "ptinvoicing: ignoring invalid NIF %r on order %s (from %s)",
            tin,
            order.code,
            "custom field" if from_custom_field else "VAT ID field",
        )
        return None
    return tin


def client_name(order):
    # Fact.pt has a single `name` field, no separate company field, so a business buyer's
    # invoice has to carry the company there — that's the entity the invoice is made out
    # to. Falls back to the person, then the email.
    ia = getattr(order, "invoice_address", None)
    company = ia and getattr(ia, "company", None)
    person = ia and getattr(ia, "name", None)
    return _one_line(company or person or order.email or "Consumidor Final", MAX_NAME)


def build_client_block(
    order, client_id=None, send_email=False, custom_field_is_nif=False
):
    # Referencing an existing client by id sidesteps forceTin entirely — including the
    # case forceTin can't resolve on its own: Fact.pt refusing to upsert a tin that
    # already matches more than one client record ("Specify an ID").
    if client_id is not None:
        return {"id": client_id}

    ia = getattr(order, "invoice_address", None)
    tin = bare_tin(order, custom_field_is_nif=custom_field_is_nif)

    client = {
        "name": client_name(order),
        "address": _one_line(ia and ia.street, MAX_ADDRESS) or "-",
        "city": _one_line(ia and ia.city, MAX_CITY) or "-",
        "zip": _one_line(ia and ia.zipcode, 30) or "-",
        "country": (ia.country.code if ia and ia.country else "PT"),
    }

    if send_email and order.email:
        client["email"] = _one_line(order.email, MAX_EMAIL)

    if tin:
        client["tin"] = tin
        client["finalConsumer"] = False
        # Turns creation into an upsert for a NIF that already has a client on file, so a
        # repeat buyer (or a retry after a prior attempt registered them) doesn't fail with
        # "clientBlock: Force update is not true." Safe here because a real NIF identifies
        # one entity — unlike the final-consumer branch below, where every buyer shares
        # 999999990 and this flag means "duplicate", not "reuse".
        client["forceTin"] = True
        # Required on this branch only. Both hardcoded false because event ticket sales
        # never carry IRS/IRC withholding.
        # ponytail: hardcoded no-withholding; make per-event settings if that ever changes.
        client["retention"] = False
        client["ric"] = False
    else:
        # No NIF given (pretix asks for it, but optionally). Fact.pt's docs are explicit
        # that tin/ric/retention must *not* be declared here, and it then registers the
        # client under the final-consumer NIF 999999990 by itself — verified against the
        # sandbox, which returned {"tin": "999999990", "isFinalConsumer": true}.
        client["finalConsumer"] = True

    return client


def build_items_block(order, event_settings):
    # taxId/unitId come from the event's plugin settings, not from each item's own tax_rate.
    default_tax_id = event_settings.get("factpt_default_tax_id", as_type=int)
    default_unit_id = event_settings.get(
        "factpt_default_unit_id", as_type=int, default=1
    )
    default_type = event_settings.get("factpt_default_type", default="service")

    items = []
    for position in order.positions.all():
        items.append(
            {
                "description": str(position.item.name)[:150],
                # Fact.pt's `price` is the NET unit price: it adds taxId's VAT on top
                # (verified against the sandbox — 15.00 at 23% came back as gross 18.45).
                # pretix's position.price is gross, so the tax has to come back out, or
                # every invoice would be issued above what the buyer actually paid.
                "price": str(position.price - position.tax_value),
                "reference": f"pretix-{position.pk}"[:20],
                "retention": False,
                "type": default_type,
                "unitId": default_unit_id,
                "taxId": default_tax_id,
                "quantity": 1,
            }
        )
    return items


def build_payload(order, event_settings, identifier_id, client_id=None):
    return {
        "client": build_client_block(
            order,
            client_id=client_id,
            send_email=event_settings.get(
                "factpt_send_client_email", as_type=bool, default=False
            ),
            custom_field_is_nif=event_settings.get(
                "ptinvoicing_nif_custom_field", as_type=bool, default=False
            ),
        ),
        "document": {
            "date": timezone.now().date().isoformat(),
            "markPaid": True,
            "reference": order.code,
            "identifierId": identifier_id,
        },
        "items": build_items_block(order, event_settings),
    }
