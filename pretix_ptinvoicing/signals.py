import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.template.loader import get_template
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django_scopes import scopes_disabled
from pretix.base.models import (
    Event,
    Event_SettingsStore,
    Order,
    OrderPayment,
    OrderRefund,
)
from pretix.base.signals import order_paid, periodic_task
from pretix.control.signals import nav_event, nav_event_settings
from pretix.control.signals import order_info as control_order_info
from pretix.helpers.periodic import minimum_interval
from pretix.presale.signals import order_info_top as presale_order_info_top

from .mail import send_connection_expired_email
from .models import IssuedInvoice
from .providers import get_provider
from .tasks import issue_credit_note, issue_invoice

logger = logging.getLogger(__name__)


@receiver(periodic_task, dispatch_uid="ptinvoicing_keepalive")
@minimum_interval(minutes_after_success=24 * 60, minutes_after_error=60)
def ptinvoicing_keepalive(sender, **kwargs):
    """
    Daily InvoiceProvider.keepalive() for every event with a provider selected — what
    stops Moloni's 14-day refresh token from lapsing on an event with no sales.
    """
    with scopes_disabled():
        event_pks = Event_SettingsStore.objects.filter(
            key="ptinvoicing_provider"
        ).values_list("object_id", flat=True)
        for event in Event.objects.filter(pk__in=event_pks):
            if "pretix_ptinvoicing" not in event.get_plugins():
                continue
            provider = get_provider(event)
            if provider is None:
                continue
            try:
                dead = provider.keepalive()
            except Exception:
                logger.exception("ptinvoicing: keepalive failed for %s", event)
                continue
            if dead:
                send_connection_expired_email(event, provider)


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
        # Paid, and nothing uncredited covers it: no invoice yet, or the last one was
        # credited (a refund) before this payment.
        "needs_invoice": is_paid
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
