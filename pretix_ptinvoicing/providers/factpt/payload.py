from django.utils import timezone

from ...orderdata import bare_tin, client_name, invoice_lines, one_line

# Fact.pt's documented limits for the client block. Every one of these is a single line:
# pretix's street is a TextField, so a buyer pressing Enter would otherwise send a newline
# and get the whole document rejected.
MAX_NAME = 100
MAX_ADDRESS = 100
MAX_CITY = 50
MAX_EMAIL = 100


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
        "name": client_name(order, MAX_NAME),
        "address": one_line(ia and ia.street, MAX_ADDRESS) or "-",
        "city": one_line(ia and ia.city, MAX_CITY) or "-",
        "zip": one_line(ia and ia.zipcode, 30) or "-",
        "country": (ia.country.code if ia and ia.country else "PT"),
    }

    if send_email and order.email:
        client["email"] = one_line(order.email, MAX_EMAIL)

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

    return [
        {
            "description": line.name[:150],
            # Fact.pt's `price` is the NET unit price: it adds taxId's VAT on top
            # (verified against the sandbox — 15.00 at 23% came back as gross 18.45).
            # pretix's prices are gross, so the tax has to come back out — see Line.net
            # for why that's done from the rate rather than with pretix's rounded
            # tax_value.
            # ponytail: 4-decimal net is unverified against Fact.pt; confirm with one
            # sandbox document at 15.00/23% that it comes back as gross 15.00.
            "price": str(line.net),
            "reference": line.reference[:20],
            "retention": False,
            "type": default_type,
            "unitId": default_unit_id,
            "taxId": default_tax_id,
            "quantity": 1,
        }
        for line in invoice_lines(order)
    ]


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


def build_credit_payload(order, identifier_id):
    # POST /documents/{id}/credit only accepts date/comments/reference/identifierId/
    # download/language — no client or items block, and the API only ever credits the
    # referenced document's full value (one credit note per document, "de valor igual ao
    # total do documento a creditar").
    return {
        "document": {
            "date": timezone.now().date().isoformat(),
            "reference": order.code,
            "identifierId": identifier_id,
        }
    }
