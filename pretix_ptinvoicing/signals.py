from django.dispatch import receiver
from django.template.loader import get_template
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.signals import order_paid
from pretix.control.signals import nav_event, nav_event_settings
from pretix.control.signals import order_info as control_order_info
from pretix.presale.signals import order_info_top as presale_order_info_top

from .models import IssuedInvoice
from .providers import get_provider
from .tasks import issue_invoice


@receiver(order_paid, dispatch_uid="ptinvoicing_order_paid")
def ptinvoicing_order_paid(sender, order, **kwargs):
    # Only enqueues the Celery task — a slow/down provider never delays checkout.
    issue_invoice.apply_async(kwargs={"order_pk": order.pk, "event_pk": sender.pk})


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
    ctx = {
        "order": order,
        "request": request,
        "event": sender,
        "provider": provider,
        "configured": bool(provider and provider.is_configured),
        "invoice": IssuedInvoice.objects.filter(order=order).first(),
        "is_paid": order.status == order.STATUS_PAID,
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

    invoice = IssuedInvoice.objects.filter(
        order=order, status=IssuedInvoice.STATUS_SUCCESS
    ).first()
    if not invoice:
        return ""

    return get_template("pretix_ptinvoicing/presale/order_info.html").render(
        {"order": order, "request": request, "event": sender, "invoice": invoice},
        request=request,
    )
