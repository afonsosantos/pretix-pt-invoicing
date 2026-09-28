from ...orderdata import FINAL_CONSUMER_NIF, bare_tin, client_name, one_line

# Moloni's own field limits, not Fact.pt's — a provider never reaches into a sibling.
MAX_NAME = 100
MAX_ADDRESS = 100
MAX_CITY = 50

# Moloni's country ids are its own list; 1 is Portugal.
COUNTRY_PT = 1


def build_customer(order, custom_field_is_nif=False):
    """
    The customer to create in Moloni.

    Unlike Fact.pt, Moloni takes no inline client block on the document — the customer is a
    separate object and the document carries only its `customer_id`. This is what
    `customers/insert` gets when no existing customer matches.
    """
    ia = getattr(order, "invoice_address", None)
    tin = bare_tin(order, custom_field_is_nif=custom_field_is_nif)

    return {
        "vat": tin or FINAL_CONSUMER_NIF,
        "name": client_name(order, MAX_NAME),
        "address": one_line(ia and ia.street, MAX_ADDRESS) or "-",
        "city": one_line(ia and ia.city, MAX_CITY) or "-",
        "zip_code": one_line(ia and ia.zipcode, 30) or "1000-001",
        "country_id": COUNTRY_PT,
        "language_id": 1,
        "email": one_line(order.email, MAX_NAME),
    }


def build_products(order, event_settings):
    tax_id = event_settings.get("moloni_tax_id", as_type=int)
    exemption_reason = event_settings.get("moloni_exemption_reason", default="")

    products = []
    for index, position in enumerate(order.positions.all()):
        product = {
            "name": str(position.item.name)[:100],
            "summary": f"pretix-{position.pk}",
            "qty": 1,
            # ponytail: sent net, mirroring Fact.pt, because Moloni applies `taxes` on top.
            # Moloni's docs don't state net vs gross — settle it with one document against a
            # real account before trusting this in production, the way Fact.pt's was settled.
            "price": float(position.price - position.tax_value),
            "order": index,
            "discount": 0,
        }
        if tax_id:
            product["taxes"] = [{"tax_id": tax_id, "order": 0, "cumulative": 0}]
        if exemption_reason:
            # Moloni requires an exemption reason (e.g. "M07") on a 0% line.
            product["exemption_reason"] = exemption_reason
        products.append(product)
    return products


def build_payments(order, event_settings, date):
    payment_method_id = event_settings.get("moloni_payment_method_id", as_type=int)
    if not payment_method_id:
        return []
    # An invoice-receipt is by definition settled, so the payment carries the full total.
    return [
        {
            "payment_method_id": payment_method_id,
            "date": date,
            "value": float(order.total),
        }
    ]


def build_document(order, event_settings, customer_id, identifier_id, date):
    return {
        "company_id": event_settings.get("moloni_company_id", as_type=int),
        "customer_id": customer_id,
        "document_set_id": event_settings.get("moloni_document_set_id", as_type=int),
        "date": date,
        "expiration_date": date,
        "our_reference": identifier_id,
        "your_reference": order.code,
        "status": 1,  # 1 = closed/issued, as opposed to a draft
        "products": build_products(order, event_settings),
        "payments": build_payments(order, event_settings, date),
    }
