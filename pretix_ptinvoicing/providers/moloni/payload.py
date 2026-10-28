from decimal import Decimal

from pretix.base.models import OrderPayment

from ...orderdata import (
    FINAL_CONSUMER_NAME,
    FINAL_CONSUMER_NIF,
    bare_tin,
    client_name,
    one_line,
)

# Moloni's own field limits — a provider never reaches into a sibling.
MAX_NAME = 100
MAX_ADDRESS = 100
MAX_CITY = 50

# Moloni's country ids are its own list; 1 is Portugal.
COUNTRY_PT = 1


def build_customer(
    order,
    payment_method_id,
    maturity_date_id,
    custom_field_is_nif=False,
    country_id=COUNTRY_PT,
):
    """
    The customer to create in Moloni.

    Moloni takes no inline client block on the document — the customer is a separate object
    and the document carries only its `customer_id`. This is what `customers/insert` gets
    when no existing customer matches.
    """
    ia = getattr(order, "invoice_address", None)
    tin = bare_tin(order, custom_field_is_nif=custom_field_is_nif)
    name = client_name(order, MAX_NAME)

    return {
        "vat": tin or FINAL_CONSUMER_NIF,
        "name": name,
        "address": one_line(ia and ia.street, MAX_ADDRESS) or "-",
        "city": one_line(ia and ia.city, MAX_CITY) or "-",
        "zip_code": one_line(ia and ia.zipcode, 30) or "1000-001",
        "country_id": country_id,
        "language_id": 1,
        # The anonymous "Consumidor Final" customer is shared by every buyer without a
        # name (MoloniProvider._resolve_customer_id), so no one buyer's email goes on it.
        # pretix sends the invoice itself, not Moloni.
        "email": "" if name == FINAL_CONSUMER_NAME else one_line(order.email, MAX_NAME),
        # The customer's defaults. maturity_date_id and payment_method_id are required
        # real ids (docs); the rest are documented as optional but a real account
        # rejected their absence, so they get neutral values within the ranges its
        # errors state.
        "maturity_date_id": maturity_date_id,
        "payment_method_id": payment_method_id,
        "salesman_id": 0,
        "payment_day": 0,
        "discount": 0,
        "credit_limit": 0,
        "delivery_method_id": 0,
    }


def line_tax(line, event_settings, tax_rate):
    """
    The tax part of a line, decided by the rate pretix actually charged on it.

    Taxed: the configured Moloni tax, with its rate as `value` — Moloni rejects a line
    tax without one (verified against a real account), and the rate must match pretix's
    (MoloniProvider._check_taxes). 0%: no tax at all — Moloni rejects a 0 `value` —
    and an exemption reason (e.g. "M07") instead.
    """
    if line.tax_rate:
        return {
            "taxes": [
                {
                    "tax_id": event_settings.get("moloni_tax_id", as_type=int),
                    "value": float(tax_rate),
                    "order": 0,
                    "cumulative": 0,
                }
            ]
        }
    return {
        "exemption_reason": event_settings.get("moloni_exemption_reason", default="")
    }


def build_catalog_product(
    line, reference, category_id, event_settings, company_id, tax_rate
):
    """
    products/insert for the pretix item (or fee kind) behind `line`, created on its
    first sale, in `category_id` (Pretix > <event>, MoloniProvider._category_id). Type
    comes from the product's own page when set there (ItemSettings' moloni_type), else
    the event's default; unit and tax from the event's settings, like Fact.pt's item
    defaults.
    """
    return {
        "company_id": company_id,
        "category_id": category_id,
        "type": line.item_settings.get("moloni_type")
        or event_settings.get("moloni_product_type", as_type=int, default=2),
        # The item's name, not the line's: Moloni prints the catalog name under any
        # line whose name differs, so an attendee's name here would follow the product
        # onto every later document.
        "name": (line.catalog_name or line.name)[:100],
        "reference": reference,
        # Only the catalog default; every document line sends its own price.
        "price": float(line.price_net),
        "unit_id": event_settings.get("moloni_unit_id", as_type=int),
        "has_stock": 0,
        "stock": 0,
        **line_tax(line, event_settings, tax_rate),
    }


def build_products(lines, event_settings, product_ids, tax_rate):
    return [
        {
            # Required: Moloni only invoices catalog products. One per pretix item or
            # fee kind, see MoloniProvider._resolve_product_ids.
            "product_id": product_ids[line.catalog_key],
            "name": line.name[:100],
            # No summary: Moloni prints it under the name, and "pretix-33" means
            # nothing to the buyer.
            "qty": line.qty,
            # Net, 8 decimals (see set_nets): Moloni applies `taxes` on top of `price` —
            # confirmed on real documents — after the line's percentage `discount`
            # (a voucher or automatic discount in pretix; 0 otherwise). The draft that
            # settled it: 28.45528 at 42.8571429% gave incidence 16.26016.
            "price": float(line.price_net),
            "order": index,
            "discount": float(line.discount),
            **line_tax(line, event_settings, tax_rate),
        }
        for index, line in enumerate(lines)
    ]


def build_payments(order, event_settings, date):
    """
    An invoice-receipt is by definition settled, so its payments add up to the total.

    Each pretix payment method is recorded under the Moloni method it's mapped to
    (MoloniSettingsForm's table), else the default. Split by method only when the
    confirmed payments add up to the total exactly; anything else (overpaid, partly
    refunded, paid by hand with no payment) goes in whole under the main method.

    A payment the organizer confirmed by hand ("manual" — marking the order paid in
    the backend) counts as the method the buyer chose, i.e. the order's latest other
    payment attempt: the Multibanco reference paid but never confirmed automatically
    is still a Multibanco payment.
    """
    default = event_settings.get("moloni_payment_method_id", as_type=int)
    chosen = (
        order.payments.exclude(provider="manual")
        .order_by("-created")
        .values_list("provider", flat=True)
        .first()
    )

    def method(pretix_provider):
        if pretix_provider == "manual" and chosen:
            pretix_provider = chosen
        return (
            event_settings.get(
                f"moloni_payment_method_id_{pretix_provider}", as_type=int
            )
            or default
        )

    amounts = {}
    for payment in order.payments.filter(state=OrderPayment.PAYMENT_STATE_CONFIRMED):
        key = method(payment.provider)
        amounts[key] = amounts.get(key, Decimal(0)) + payment.amount
    if not amounts or sum(amounts.values()) != order.total:
        main = max(amounts, key=amounts.get) if amounts else default
        amounts = {main: order.total}
    return [
        {"payment_method_id": key, "date": date, "value": float(value)}
        for key, value in amounts.items()
        if key
    ]


def build_document(
    order,
    lines,
    event_settings,
    customer_id,
    identifier_id,
    date,
    product_ids,
    tax_rate,
):
    return {
        "company_id": event_settings.get("moloni_company_id", as_type=int),
        "customer_id": customer_id,
        "document_set_id": event_settings.get("moloni_document_set_id", as_type=int),
        "date": date,
        "expiration_date": date,
        "our_reference": identifier_id,
        "your_reference": order.code,
        "status": 1,  # 1 = closed/issued, as opposed to a draft
        "products": build_products(lines, event_settings, product_ids, tax_rate),
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
                # `value` (the rate) is required on line taxes, same as on invoices.
                {
                    "tax_id": t.get("tax_id"),
                    "value": t.get("value"),
                    "order": 0,
                    "cumulative": 0,
                }
                for t in taxes
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
