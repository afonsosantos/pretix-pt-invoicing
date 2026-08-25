from django.dispatch import receiver
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.signals import order_paid
from pretix.control.signals import nav_event, nav_event_settings

from .tasks import generate_factpt_invoice


@receiver(order_paid, dispatch_uid="factpt_order_paid")
def factpt_order_paid(sender, order, **kwargs):
    # Only enqueues the Celery task — a slow/down Fact.pt never delays checkout.
    generate_factpt_invoice.apply_async(
        kwargs={"order_pk": order.pk, "event_pk": sender.pk}
    )


@receiver(nav_event, dispatch_uid="factpt_nav_event")
def factpt_nav_event(sender, request, **kwargs):
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_view_orders", request=request
    ):
        return []
    url = request.resolver_match
    return [
        {
            "label": _("Fact.pt"),
            "url": reverse(
                "plugins:pretix_factpt:index",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.organizer.slug,
                },
            ),
            "active": url
            and url.namespace == "plugins:pretix_factpt"
            and url.url_name == "index",
            "icon": "file-text-o",
        }
    ]


@receiver(nav_event_settings, dispatch_uid="factpt_nav_event_settings")
def factpt_nav_event_settings(sender, request, **kwargs):
    if not request.user.has_event_permission(
        request.organizer, request.event, "can_change_event_settings", request=request
    ):
        return []
    url = request.resolver_match
    return [
        {
            "label": _("Fact.pt"),
            "url": reverse(
                "plugins:pretix_factpt:settings",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.organizer.slug,
                },
            ),
            "active": url
            and url.namespace == "plugins:pretix_factpt"
            and url.url_name == "settings",
        }
    ]
