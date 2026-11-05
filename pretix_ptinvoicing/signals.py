import logging
from decimal import Decimal

from django import forms
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.template.loader import get_template
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext
from django_scopes import scopes_disabled
from pretix.base.models import (
    Event,
    Event_SettingsStore,
    Order,
    OrderPayment,
    OrderRefund,
    Organizer,
    Organizer_SettingsStore,
)
from pretix.base.services.orders import OrderError
from pretix.base.signals import (
    item_copy_data,
    order_modified,
    order_paid,
    periodic_task,
    validate_order,
)
from pretix.control.signals import (
    item_forms,
    nav_event,
    nav_event_settings,
    nav_organizer,
)
from pretix.control.signals import order_info as control_order_info
from pretix.helpers.periodic import minimum_interval
from pretix.presale.signals import contact_form_fields, html_head
from pretix.presale.signals import order_info_top as presale_order_info_top
from pretix.presale.views import get_cart_is_free

from .forms import ItemSettingsForm
from .mail import send_billing_changed_email, send_connection_expired_email
from .models import IssuedInvoice, ItemSettings
from .orderdata import billing_changed, is_free, item_settings, nif_problem
from .providers import get_provider
from .tasks import issue_credit_note, issue_invoice

logger = logging.getLogger(__name__)


@receiver(periodic_task, dispatch_uid="ptinvoicing_keepalive")
@minimum_interval(minutes_after_success=24 * 60, minutes_after_error=60)
def ptinvoicing_keepalive(sender, **kwargs):
    """
    Daily InvoiceProvider.keepalive() for every organizer and event with a provider
    selected — what stops Moloni's 14-day refresh token from lapsing where there are no
    sales. Once per connection: an organizer's is shared by the events inheriting it,
    and each refresh spends the token the next one would have used.
    """
    with scopes_disabled():
        organizer_pks = Organizer_SettingsStore.objects.filter(
            key="ptinvoicing_provider"
        ).values_list("object_id", flat=True)
        event_pks = Event_SettingsStore.objects.filter(
            key="ptinvoicing_provider"
        ).values_list("object_id", flat=True)
        holders = [
            # Organizers first, and whether or not any event has the plugin on yet: a
            # connection set up ahead of its events must still be there for them.
            *Organizer.objects.filter(pk__in=organizer_pks),
            *Event.objects.filter(
                Q(pk__in=event_pks) | Q(organizer_id__in=organizer_pks)
            ).select_related("organizer"),
        ]
        dead = {}  # connection holder -> its keepalive() just found it dead
        mailed = set()
        for holder in holders:
            is_event = isinstance(holder, Event)
            if is_event and "pretix_ptinvoicing" not in holder.get_plugins():
                continue
            provider = get_provider(holder)
            if provider is None:
                continue
            connection = provider.connection_holder
            if connection not in dead:
                try:
                    dead[connection] = provider.keepalive()
                except Exception:
                    logger.exception("ptinvoicing: keepalive failed for %s", holder)
                    dead[connection] = False
            # Every event left without a connection hears about it, at its own contact
            # address — once per address, however many events share it.
            recipient = is_event and holder.settings.get("contact_mail")
            if dead[connection] and is_event and (connection, recipient) not in mailed:
                mailed.add((connection, recipient))
                send_connection_expired_email(holder, provider)


@receiver(order_paid, dispatch_uid="ptinvoicing_order_paid")
def ptinvoicing_order_paid(sender, order, **kwargs):
    # Only enqueues the Celery task — a slow/down provider never delays checkout. On
    # commit, not now: pretix sends order_paid inside the payment's transaction, and a
    # worker that loads the order before it commits sees it unpaid and skips it for good.
    if not _auto_issue(sender):
        return
    transaction.on_commit(
        lambda: issue_invoice.apply_async(
            kwargs={"order_pk": order.pk, "event_pk": sender.pk}
        )
    )


def _auto_issue(event):
    # Off = manual only: documents are issued from the order page's buttons and nowhere
    # else. Only the signals check it — the tasks themselves still run when asked to.
    return event.settings.get("ptinvoicing_auto_issue", as_type=bool, default=True)


def _refunded_in_full(order):
    """
    Confirmed payments all refunded by refunds that are actually *done*. Not pretix's
    payment_refund_sum: that also subtracts refunds merely created or in transit, which
    may still fail — and crediting money that was never returned can't be undone.
    """
    paid = order.payments.filter(
        state__in=(
            OrderPayment.PAYMENT_STATE_CONFIRMED,
            OrderPayment.PAYMENT_STATE_REFUNDED,
        )
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    refunded = order.refunds.filter(state=OrderRefund.REFUND_STATE_DONE).aggregate(
        s=Sum("amount")
    )["s"] or Decimal("0.00")
    return paid - refunded <= Decimal("0.00")


@receiver(post_save, sender=OrderRefund, dispatch_uid="ptinvoicing_refund_done")
def ptinvoicing_refund_done(sender, instance, **kwargs):
    """
    Auto-issue a credit note once a refund actually completes.

    OrderRefund has no EventPluginSignal the way order_paid/order_canceled do, so this is a
    raw Django model signal instead — pretix's own bundled sendmail plugin hooks SubEvent
    creation the same way. Unlike an EventPluginSignal it fires for every event regardless
    of whether this plugin is enabled there, so that has to be checked by hand below.

    Only fires once *done* refunds have returned everything paid (_refunded_in_full):
    every provider here can only credit a document's *full* value, so a partial refund —
    this one, or an earlier partial one that this one completes — is left for the admin's
    manual "Issue credit note" button instead of crediting more than was actually refunded.
    Checked and enqueued on commit, for the same reason as order_paid above.
    """
    if instance.state != OrderRefund.REFUND_STATE_DONE:
        return
    order_pk = instance.order_id

    def enqueue():
        with scopes_disabled():
            order = Order.objects.select_related("event").get(pk=order_pk)
            if "pretix_ptinvoicing" not in order.event.get_plugins():
                return
            if not _auto_issue(order.event) or not _refunded_in_full(order):
                return
        issue_credit_note.apply_async(
            kwargs={"order_pk": order.pk, "event_pk": order.event_id}
        )

    transaction.on_commit(enqueue)


@receiver(order_modified, dispatch_uid="ptinvoicing_order_modified")
def ptinvoicing_order_modified(sender, order, **kwargs):
    """
    The buyer changed their billing data after the invoice was issued. An issued
    document can't be changed, so accepting the change means a credit note and a new
    invoice — never done here without staff approval, whatever pretix's own
    "reissue after address change" setting says (that one only covers pretix's own
    invoices). The organizer is e-mailed, and approves with the order page's buttons.
    """
    provider = get_provider(sender)
    invoice = provider and IssuedInvoice.uncredited_invoice(order, provider.identifier)
    # ponytail: re-sent on every later save while the data still differs; store what
    # was alerted if that gets noisy.
    if invoice and billing_changed(order, invoice):
        send_billing_changed_email(order, invoice)


@receiver(contact_form_fields, dispatch_uid="ptinvoicing_contact_form_fields")
def ptinvoicing_contact_form_fields(sender, request, **kwargs):
    # Saved by pretix into order.meta_info's contact_form_data; read by invoice_lines.
    if not sender.settings.get("ptinvoicing_itemize_option", as_type=bool):
        return {}
    return {
        "ptinvoicing_itemize": forms.BooleanField(
            label=_("Itemize the invoice by attendee"),
            help_text=_("Each ticket's line on the invoice shows the attendee's name."),
            required=False,
        )
    }


@receiver(item_forms, dispatch_uid="ptinvoicing_item_forms")
def ptinvoicing_item_forms(sender, request, item, **kwargs):
    # The "Invoicing (PT)" box on the product page: the product's catalog reference and
    # the provider's own per-product fields. Nothing to set without a provider.
    provider = get_provider(sender)
    if provider is None:
        return None
    return ItemSettingsForm(
        item=item,
        provider=provider,
        data=request.POST if request.method == "POST" else None,
        prefix="ptinvoicing",
    )


@receiver(item_copy_data, dispatch_uid="ptinvoicing_item_copy")
def ptinvoicing_item_copy(sender, source, target, **kwargs):
    # A copied product keeps the provider fields (its type) but not the reference: that
    # names one catalog product, and two pretix products sharing it would be invoiced
    # under one name. The copy falls back to its own pretix id.
    settings = item_settings(source)
    if settings and settings.data:
        ItemSettings.objects.create(item=target, data=dict(settings.data))


@receiver(html_head, dispatch_uid="ptinvoicing_html_head")
def ptinvoicing_html_head(sender, request, **kwargs):
    # With the custom field as the NIF, hides pretix's own VAT ID field (checkout.js).
    # Not on payment pages (PCI DSS 6.4.3, see the signal's docs).
    settings = sender.settings
    if getattr(request, "pci_dss_payment_page", False) or not settings.get(
        "ptinvoicing_nif_custom_field", as_type=bool
    ):
        return ""
    # checkout.js is a static file: what it needs to know about the event's tax number
    # rule (orderdata.nif_problem, which it mirrors) travels on its own <script> tag.
    nif_required = settings.get(
        "ptinvoicing_nif_required", as_type=bool
    ) and not _cart_is_free(request)
    return format_html(
        '<link rel="stylesheet" href="{}">'
        '<script type="application/javascript" src="{}" defer'
        ' data-nif-required="{}" data-address-required="{}" data-text-invalid="{}"'
        ' data-text-required="{}"></script>',
        static("pretix_ptinvoicing/checkout.css"),
        static("pretix_ptinvoicing/checkout.js"),
        "true" if nif_required else "",
        "true" if settings.get("invoice_address_required", as_type=bool) else "",
        _("This is not a valid Portuguese tax number (NIF)."),
        # The marker pretix itself renders on a required field's label, in its own
        # words (its JavaScript catalog has no translation for it).
        pgettext("form", "required"),
    )


def _cart_is_free(request):
    # Only worth working out at checkout — html_head runs on every page of the shop.
    match = getattr(request, "resolver_match", None)
    if not match or not (match.url_name or "").startswith("event.checkout"):
        return False
    return get_cart_is_free(request)


@receiver(validate_order, dispatch_uid="ptinvoicing_validate_order")
def ptinvoicing_validate_order(sender, positions, invoice_address, **kwargs):
    # pretix has no hook into the invoice address form, so the tax number rule is asked
    # for in the browser (checkout.js) and held here, when the order is placed: the one
    # place a buyer without the script still can't get past.
    problem = nif_problem(
        sender, invoice_address, free=not any(p.price for p in positions)
    )
    if problem:
        raise OrderError(problem)


def _nav_entry(request, url_name, label, icon=None):
    resolved = request.resolver_match
    entry = {
        "label": label,
        "url": reverse(
            f"plugins:pretix_ptinvoicing:{url_name}",
            kwargs={
                "event": request.event.slug,
                "organizer": request.organizer.slug,
            },
        ),
        "active": resolved
        and resolved.namespace == "plugins:pretix_ptinvoicing"
        and resolved.url_name == url_name,
    }
    if icon:
        entry["icon"] = icon
    return entry


@receiver(nav_event, dispatch_uid="ptinvoicing_nav_event")
def ptinvoicing_nav_event(sender, request, **kwargs):
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_view_orders", request=request
    ):
        return []
    return [_nav_entry(request, "index", _("Invoicing (PT)"), icon="file-text-o")]


@receiver(nav_event_settings, dispatch_uid="ptinvoicing_nav_event_settings")
def ptinvoicing_nav_event_settings(sender, request, **kwargs):
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_change_event_settings", request=request
    ):
        return []
    return [_nav_entry(request, "settings", _("Invoicing (PT)"))]


@receiver(nav_organizer, dispatch_uid="ptinvoicing_nav_organizer")
def ptinvoicing_nav_organizer(sender, request, organizer, **kwargs):
    # The organizer's own invoicing settings page (what its events inherit), under the
    # organizer's "Settings" like pretix's own organizer-wide settings.
    if not request.user.has_organizer_permission(
        organizer, "can_change_organizer_settings", request=request
    ):
        return []
    resolved = request.resolver_match
    return [
        {
            "label": _("Invoicing (PT)"),
            "url": reverse(
                "plugins:pretix_ptinvoicing:organizer_settings",
                kwargs={"organizer": organizer.slug},
            ),
            "parent": reverse(
                "control:organizer.edit", kwargs={"organizer": organizer.slug}
            ),
            "active": resolved
            and resolved.namespace == "plugins:pretix_ptinvoicing"
            and resolved.url_name.startswith("organizer_"),
        }
    ]


@receiver(control_order_info, dispatch_uid="ptinvoicing_order_info")
def ptinvoicing_order_info(sender, order, request, **kwargs):
    # Panel on the Control-panel order page: what was issued for this order, plus a button
    # to issue it now. Covers orders paid before the plugin was configured, and re-runs
    # after fixing whatever the provider rejected.
    provider = get_provider(sender)
    # One invoice + its credit note per cycle, oldest first: paid → refunded → paid again
    # leaves a credited invoice behind and needs a new one.
    cycles = [
        {"invoice": invoice, "credit_note": invoice.credit_notes.first()}
        for invoice in IssuedInvoice.objects.filter(
            order=order, kind=IssuedInvoice.KIND_INVOICE
        ).order_by("created")
    ]
    if provider is None and not cycles:
        # "No invoicing" picked and nothing issued before: nothing to show.
        return ""
    latest_credit = cycles[-1]["credit_note"] if cycles else None
    is_paid = order.status == order.STATUS_PAID
    uncredited = provider and IssuedInvoice.uncredited_invoice(
        order, provider.identifier
    )
    ctx = {
        "order": order,
        "request": request,
        "event": sender,
        "provider": provider,
        # Rendered even when not configured: the panel then says so, rather than
        # vanishing while paid orders quietly go without documents.
        "configured": bool(provider and provider.is_configured),
        "settings_url": reverse(
            "plugins:pretix_ptinvoicing:settings",
            kwargs={"organizer": sender.organizer.slug, "event": sender.slug},
        ),
        "cycles": cycles,
        "is_paid": is_paid,
        # A free order is "paid" to pretix but has nothing to invoice (tasks.py skips
        # it); the panel says so instead of offering a button that would do nothing.
        "is_free": is_free(order),
        "billing_changed": uncredited and billing_changed(order, uncredited),
        "changed_invoice": uncredited,
        # Paid for something, and nothing uncredited covers it: no invoice yet, or the
        # last one was credited (a refund) before this payment.
        "needs_invoice": is_paid
        and not is_free(order)
        and (
            not cycles
            or bool(
                latest_credit and latest_credit.status == IssuedInvoice.STATUS_SUCCESS
            )
        ),
    }
    # request= is required, not decoration: without it the template renders with a plain
    # Context, no context processors run, and {% csrf_token %} silently emits an empty
    # token — the issue button then dies on CSRF verification.
    return get_template("pretix_ptinvoicing/control/order_info.html").render(
        ctx, request=request
    )


@receiver(presale_order_info_top, dispatch_uid="ptinvoicing_presale_order_info")
def ptinvoicing_presale_order_info(sender, order, request, **kwargs):
    # A download button for the buyer, placed with pretix's ticket download buttons (the
    # static JS does the last hop — see presale.js). It can't reuse pretix's own "Invoices"
    # panel: that lists order.invoices, i.e. pretix's own invoice records, and the
    # provider's document isn't one of those — pretix never generated it and doesn't own
    # its number.
    if not sender.settings.get("ptinvoicing_show_in_order", as_type=bool, default=True):
        return ""
    provider = get_provider(sender)
    if provider is None:
        return ""

    # Invoices and credit notes alike — a refunded buyer needs the credit note as much
    # as the invoice. Only the current provider's: another one's credentials aren't
    # around to fetch the PDF, so its button would only lead to an error.
    documents = IssuedInvoice.objects.filter(
        order=order,
        provider=provider.identifier,
        status=IssuedInvoice.STATUS_SUCCESS,
    ).order_by("created")
    if not documents:
        return ""

    return get_template("pretix_ptinvoicing/presale/order_info.html").render(
        {"order": order, "request": request, "event": sender, "documents": documents},
        request=request,
    )
