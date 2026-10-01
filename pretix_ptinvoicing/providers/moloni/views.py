"""
"Connect to Moloni": the OAuth authorization-code flow.

The callback URL is global (no organizer/event in the path) because Moloni may require
the redirect_uri to match the one registered for the developer app exactly — one URL then
serves every event. Which event the flow belongs to travels in the session instead, along
with a `state` value: checked when Moloni sends it back, and when it doesn't, the session
entry alone still ties the callback to an admin who actually started the flow.
"""

import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django_scopes import scopes_disabled
from pretix.base.models import Event
from pretix.control.permissions import EventPermissionRequiredMixin

from . import MoloniAPIError, MoloniProvider
from .client import AUTHORIZE_URL

SESSION_KEY = "ptinvoicing_moloni_oauth"


def callback_url():
    # SITE_URL, not request.build_absolute_uri: it has to be identical on the way out and
    # back, and identical to what the admin registered in Moloni.
    return settings.SITE_URL + reverse("plugins:pretix_ptinvoicing:moloni_callback")


def _settings_url(event):
    # ?provider=moloni keeps the Moloni card selected even if the event's saved provider
    # is still another one — connecting doesn't switch issuance over by itself.
    url = reverse(
        "plugins:pretix_ptinvoicing:settings",
        kwargs={"organizer": event.organizer.slug, "event": event.slug},
    )
    return f"{url}?provider=moloni"


class ConnectView(EventPermissionRequiredMixin, View):
    permission = "can_change_event_settings"

    def post(self, request, *args, **kwargs):
        s = request.event.settings
        # Take the ids as typed on the settings page, so the admin needn't Save first.
        for key in ("moloni_client_id", "moloni_client_secret"):
            value = request.POST.get(f"moloni-{key}", "").strip()
            if value:
                s.set(key, value)
        if not (s.get("moloni_client_id") and s.get("moloni_client_secret")):
            messages.error(
                request, _("Fill in the developer ID and client secret first.")
            )
            return redirect(_settings_url(request.event))

        state = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = {"state": state, "event": request.event.pk}
        query = urlencode(
            {
                "response_type": "code",
                "client_id": s.get("moloni_client_id"),
                "redirect_uri": callback_url(),
                "state": state,
            }
        )
        return redirect(f"{AUTHORIZE_URL}?{query}")


class DisconnectView(EventPermissionRequiredMixin, View):
    permission = "can_change_event_settings"

    def post(self, request, *args, **kwargs):
        MoloniProvider(request.event).disconnect()
        messages.success(request, _("Disconnected from Moloni."))
        return redirect(_settings_url(request.event))


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

        with scopes_disabled():
            event = (
                Event.objects.select_related("organizer")
                .filter(pk=pending["event"])
                .first()
            )
        if event is None or not request.user.has_event_permission(
            event.organizer, event, "can_change_event_settings", request=request
        ):
            raise Http404()

        code = request.GET.get("code")
        if not code:
            messages.error(request, _("Moloni did not authorize the connection."))
            return redirect(_settings_url(event))
        try:
            MoloniProvider(event).connect(code, callback_url())
        except MoloniAPIError as e:
            messages.error(
                request,
                _("Could not connect to Moloni: %(error)s") % {"error": e.as_text()},
            )
        else:
            messages.success(request, _("Connected to Moloni."))
        return redirect(_settings_url(event))
