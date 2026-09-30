from ...orderdata import FINAL_CONSUMER_NIF, bare_tin, client_name, one_line

# Moloni's own field limits — a provider never reaches into a sibling.
MAX_NAME = 100
MAX_ADDRESS = 100
MAX_CITY = 50

# Moloni's country ids are its own list; 1 is Portugal.
COUNTRY_PT = 1


def build_customer(order, custom_field_is_nif=False):
    """
    The customer to create in Moloni.

    Moloni takes no inline client block on the document — the customer is a separate object
    and the document carries only its `customer_id`. This is what `customers/insert` gets
    when no existing customer matches.
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
            # ponytail: sent net, because Moloni applies `taxes` on top of `price`. Moloni's
            # docs don't state net vs gross — settle it with one document against a real
            # account before trusting this in production.
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


def build_credit_note(
    original_document_id,
    original_document,
    unrelated_products,
    event_settings,
    identifier_id,
    date,
):
    """
    A full credit note for `original_document` (a `documents/getOne` response) — Moloni's
    creditNotes/insert requires the *product* lines plus an associated_documents entry
    linking back to it.

    `unrelated_products` is `documents/getUnrelatedProducts`'s response for the same
    document: the lines still available to credit, each carrying `document_product_id` —
    confirmed (against the official Moloni WooCommerce plugin's `CreateCreditNote.php`,
    which sends `'related_id' => $matchedDocumentProduct['document_product_id']`) to be
    what `products[].related_id` actually references, not `product_id`.

    `associated_documents[0].value` is 0, not the document's total: invoice-receipts are
    one of Moloni's "self-paid" document types (`DocumentTypes::TYPES_SELF_PAID` in that
    same plugin includes `invoiceReceipts`), and a self-paid document's credit note
    reconciles nothing — the money was already settled when it was issued.

    A separate Moloni document set is required for this
    (`moloni_credit_note_document_set_id`, not the invoice's own `moloni_document_set_id`):
    Moloni series are scoped to one document type each, and a series created for
    Invoice-Receipts cannot be used to number a Credit Note.
    """
    products = []
    for index, item in enumerate(unrelated_products or []):
        product = {
            "product_id": item.get("product_id"),
            "related_id": item.get("document_product_id"),
            "name": item.get("name"),
            "summary": item.get("summary"),
            "qty": item.get("qty"),
            "price": item.get("price"),
            "order": index,
            "discount": item.get("discount") or 0,
        }
        taxes = item.get("taxes") or []
        if taxes:
            product["taxes"] = [
                {"tax_id": t.get("tax_id"), "order": 0, "cumulative": 0} for t in taxes
            ]
        exemption_reason = item.get("exemption_reason")
        if exemption_reason:
            product["exemption_reason"] = exemption_reason
        products.append(product)

    return {
        "company_id": event_settings.get("moloni_company_id", as_type=int),
        "customer_id": original_document.get("customer_id"),
        "document_set_id": event_settings.get(
            "moloni_credit_note_document_set_id", as_type=int
        ),
        "date": date,
        "our_reference": identifier_id,
        "your_reference": original_document.get("your_reference"),
        "status": 1,
        "associated_documents": [{"associated_id": original_document_id, "value": 0}],
        "products": products,
    }
