"""
"Connect to Moloni": the OAuth authorization-code flow, for an event or — so that all its
events share one connection — for an organizer.

The callback URL is global (no organizer/event in the path) because Moloni may require
the redirect_uri to match the one registered for the developer app exactly — one URL then
serves every event. Which event or organizer the flow belongs to travels in the session
instead, along with a `state` value: checked when Moloni sends it back, and when it
doesn't, the session entry alone still ties the callback to an admin who actually started
the flow.
"""

import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django_scopes import scopes_disabled
from pretix.base.models import Event, Organizer

from ...views import (
    EventHolderMixin,
    OrganizerHolderMixin,
    save_valid_fields,
    settings_url,
)
from ..base import ProviderUnreachable
from . import MoloniAPIError, MoloniProvider
from .client import AUTHORIZE_URL

SESSION_KEY = "ptinvoicing_moloni_oauth"


def callback_url():
    # SITE_URL, not request.build_absolute_uri: it has to be identical on the way out and
    # back, and identical to what the admin registered in Moloni.
    return settings.SITE_URL + reverse("plugins:pretix_ptinvoicing:moloni_callback")


def flow_url(holder, action):
    """The "connect" or "disconnect" URL of an event's or organizer's settings page."""
    if isinstance(holder, Event):
        return reverse(
            f"plugins:pretix_ptinvoicing:moloni_{action}",
            kwargs={"organizer": holder.organizer.slug, "event": holder.slug},
        )
    return reverse(
        f"plugins:pretix_ptinvoicing:organizer_moloni_{action}",
        kwargs={"organizer": holder.slug},
    )


def _settings_url(holder):
    # ?provider=moloni keeps the Moloni card selected even if the saved provider is
    # still another one — connecting doesn't switch issuance over by itself.
    return settings_url(holder, provider=MoloniProvider.identifier)


class ConnectMixin:
    def post(self, request, *args, **kwargs):
        holder = self.holder
        s = holder.settings
        # Keep everything typed on the settings page (the client id and secret above
        # all), so the admin needn't Save first and loses nothing to the round-trip.
        save_valid_fields(holder, request.POST, MoloniProvider.identifier)
        if not (s.get("moloni_client_id") and s.get("moloni_client_secret")):
            messages.error(
                request, _("Fill in the developer ID and client secret first.")
            )
            return redirect(_settings_url(holder))

        state = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = {
            "state": state,
            "event" if isinstance(holder, Event) else "organizer": holder.pk,
        }
        query = urlencode(
            {
                "response_type": "code",
                "client_id": s.get("moloni_client_id"),
                "redirect_uri": callback_url(),
                "state": state,
            }
        )
        return redirect(f"{AUTHORIZE_URL}?{query}")


class ConnectView(EventHolderMixin, ConnectMixin, View):
    pass


class OrganizerConnectView(OrganizerHolderMixin, ConnectMixin, View):
    pass


class DisconnectMixin:
    def get(self, request, *args, **kwargs):
        # Asked first: without a password fallback, disconnecting stops issuance.
        return render(
            request,
            "pretix_ptinvoicing/control/confirm.html",
            {
                "base_template": self.base_template,
                "title": _("Disconnect from Moloni"),
                "text": self.confirmation(),
                "button": _("Disconnect"),
                "danger": True,
                "next": _settings_url(self.holder),
            },
        )

    def post(self, request, *args, **kwargs):
        MoloniProvider(self.holder).disconnect()
        messages.success(request, _("Disconnected from Moloni."))
        return redirect(_settings_url(self.holder))


class DisconnectView(EventHolderMixin, DisconnectMixin, View):
    base_template = "pretixcontrol/event/base.html"

    def confirmation(self):
        if MoloniProvider(self.request.organizer).connection_state:
            return _(
                "This removes the event's own connection to Moloni. It then uses the "
                "organizer's connection again."
            )
        return _(
            "No invoices or credit notes can be issued through Moloni for this "
            "event until you connect again."
        )


class OrganizerDisconnectView(OrganizerHolderMixin, DisconnectMixin, View):
    base_template = "pretixcontrol/organizers/base.html"

    def confirmation(self):
        return _(
            "No invoices or credit notes can be issued through Moloni for any event "
            "using the organizer's connection until you connect again."
        )


class CallbackView(View):
    # Under /control/, so pretix's middleware has already required a logged-in user.

    def get(self, request, *args, **kwargs):
        pending = request.session.pop(SESSION_KEY, None)
        if not pending:
            raise Http404()
        returned_state = request.GET.get("state")
        if returned_state is not None and not secrets.compare_digest(
            returned_state, pending["state"]
        ):
            raise Http404()

        holder = self.holder(request, pending)
        if holder is None:
            raise Http404()

        code = request.GET.get("code")
        if not code:
            messages.error(request, _("Moloni did not authorize the connection."))
            return redirect(_settings_url(holder))
        try:
            MoloniProvider(holder).connect(code, callback_url())
        except (MoloniAPIError, ProviderUnreachable) as e:
            messages.error(
                request,
                _("Could not connect to Moloni: %(error)s")
                % {"error": e.as_text() if isinstance(e, MoloniAPIError) else e},
            )
        else:
            provider = MoloniProvider(holder)
            if provider.in_use:
                messages.success(request, _("Connected to Moloni."))
            else:
                messages.success(
                    request,
                    _(
                        "Connected to Moloni. Now choose the company and the other "
                        "Moloni settings, then click Save — nothing is issued through "
                        "Moloni until you do."
                    ),
                )
        return redirect(_settings_url(holder))

    def holder(self, request, pending):
        """The event or organizer the flow was started for, if this user may still set it up."""
        with scopes_disabled():
            if "organizer" in pending:
                organizer = Organizer.objects.filter(pk=pending["organizer"]).first()
                if organizer and request.user.has_organizer_permission(
                    organizer, "can_change_organizer_settings", request=request
                ):
                    return organizer
                return None
            event = (
                Event.objects.select_related("organizer")
                .filter(pk=pending["event"])
                .first()
            )
        if (
            event is None
            or "pretix_ptinvoicing" not in event.get_plugins()
            or not request.user.has_event_permission(
                event.organizer, event, "can_change_event_settings", request=request
            )
        ):
            return None
        return event
