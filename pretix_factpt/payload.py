from django.utils import timezone


def build_identifier_id(event, order):
    # Fact.pt rejects a second document with the same identifierId — guards against duplicate issuance.
    return f"pretix-{event.slug}-{order.code}"[:50]


def bare_tin(order):
    # Bare NIF, no country prefix (e.g. "PT123456789" -> "123456789"), or None if the
    # order has no VAT id at all.
    ia = getattr(order, "invoice_address", None)
    vat_id = ia and getattr(ia, "vat_id", None)
    if not vat_id:
        return None
    return vat_id[2:] if vat_id.upper().startswith("PT") else vat_id


def build_client_block(order, client_id=None):
    # Referencing an existing client by id sidesteps forceTin entirely — including the
    # case forceTin can't resolve on its own: Fact.pt refusing to upsert a tin that
    # already matches more than one client record ("Specify an ID").
    if client_id is not None:
        return {"id": client_id}

    ia = getattr(order, "invoice_address", None)
    has_tin = bool(ia and getattr(ia, "vat_id", None))

    client = {
        "name": (ia.name if ia and ia.name else order.email) or "Consumidor Final",
        "address": (ia.street if ia and ia.street else "-"),
        "city": (ia.city if ia and ia.city else "-"),
        "zip": (ia.zipcode if ia and ia.zipcode else "-"),
        "country": (ia.country.code if ia and ia.country else "PT"),
        # Per Fact.pt's docs, this covers two collisions: a NIF that already has a client
        # record on file, or (for Final Consumer, below) a name+country combo that already
        # does. Either way it turns client creation into an upsert instead of a strict
        # create — without it, a repeat buyer or a retry after a prior attempt already
        # registered the client fails with "clientBlock: Force update is not true."
        "forceTin": True,
        # Both required by Fact.pt on every client block (error otherwise:
        # "ric: The RIC is required.; retention: The retention is required."). Event
        # ticket sales never carry IRS/IRC withholding, so both are always false.
        # ponytail: hardcoded no-withholding; make per-event settings if that ever changes.
        "retention": False,
        "ric": False,
    }

    if has_tin:
        client["tin"] = bare_tin(order)
        client["finalConsumer"] = False
    else:
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
                "price": str(position.price),
                "reference": f"pretix-{position.pk}"[:20],
                "retention": False,
                "type": default_type,
                "unitId": default_unit_id,
                "taxId": default_tax_id,
                "quantity": 1,
            }
        )
    return items


def build_payload(order, event_settings, client_id=None):
    identifier_id = build_identifier_id(order.event, order)

    payload = {
        "client": build_client_block(order, client_id=client_id),
        "document": {
            "date": timezone.now().date().isoformat(),
            "markPaid": True,
            "reference": order.code,
            "identifierId": identifier_id,
        },
        "items": build_items_block(order, event_settings),
    }
    return payload, identifier_id
