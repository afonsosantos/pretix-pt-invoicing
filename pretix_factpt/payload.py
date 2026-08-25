from django.utils import timezone


def build_identifier_id(event, order):
    # Fact.pt rejects a second document with the same identifierId — guards against duplicate issuance.
    return f"pretix-{event.slug}-{order.code}"[:50]


def build_client_block(order):
    ia = getattr(order, "invoice_address", None)
    has_tin = bool(ia and getattr(ia, "vat_id", None))

    client = {
        "name": (ia.name if ia and ia.name else order.email) or "Consumidor Final",
        "address": (ia.street if ia and ia.street else "-"),
        "city": (ia.city if ia and ia.city else "-"),
        "zip": (ia.zipcode if ia and ia.zipcode else "-"),
        "country": (ia.country.code if ia and ia.country else "PT"),
    }

    if has_tin:
        # Bare NIF, no country prefix (e.g. "PT123456789" -> "123456789")
        tin = ia.vat_id
        if tin.upper().startswith("PT"):
            tin = tin[2:]
        client["tin"] = tin
        client["finalConsumer"] = False
        # Without this, Fact.pt rejects the request ("clientBlock: Force update is not
        # true.") whenever this NIF already has a client record on file with different
        # details than what we're sending — e.g. a repeat buyer, or a retry after a
        # previous attempt already registered the client. forceTin makes it an upsert.
        client["forceTin"] = True
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


def build_payload(order, event_settings):
    identifier_id = build_identifier_id(order.event, order)

    payload = {
        "client": build_client_block(order),
        "document": {
            "date": timezone.now().date().isoformat(),
            "markPaid": True,
            "reference": order.code,
            "identifierId": identifier_id,
        },
        "items": build_items_block(order, event_settings),
    }
    return payload, identifier_id
