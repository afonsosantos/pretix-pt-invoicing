"""
Reading buyer data off a pretix order, provider-independent.

These are pretix-side and Portugal-side concerns — how pretix stores a NIF, what counts as
a valid one, which field is the invoice's recipient — so they belong here rather than in
whichever provider happened to need them first. Field *limits* stay with each provider,
since those are its API's rules.
"""

import logging

logger = logging.getLogger(__name__)

FINAL_CONSUMER_NIF = "999999990"


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


def client_name(order, limit=100):
    """
    Who the invoice is made out to.

    Neither provider has a separate company field, so a business buyer's company has to be
    the name — that is the entity being invoiced. Falls back to the person, then the email.
    """
    ia = getattr(order, "invoice_address", None)
    company = ia and getattr(ia, "company", None)
    person = ia and getattr(ia, "name", None)
    return one_line(company or person or order.email or "Consumidor Final", limit)
