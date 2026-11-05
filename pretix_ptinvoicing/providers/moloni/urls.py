from django.urls import path

from . import views

# Mounted by the core urls.py under the plugin's namespace, so these names reverse as
# "plugins:pretix_ptinvoicing:moloni_connect" etc. Once for an event's settings page and
# once for its organizer's.
_event = "control/event/<str:organizer>/<str:event>/invoicing/settings/moloni/"
_organizer = "control/organizer/<str:organizer>/invoicing/settings/moloni/"

urlpatterns = [
    path(_event + "connect/", views.ConnectView.as_view(), name="moloni_connect"),
    path(
        _event + "disconnect/",
        views.DisconnectView.as_view(),
        name="moloni_disconnect",
    ),
    path(
        _organizer + "connect/",
        views.OrganizerConnectView.as_view(),
        name="organizer_moloni_connect",
    ),
    path(
        _organizer + "disconnect/",
        views.OrganizerDisconnectView.as_view(),
        name="organizer_moloni_disconnect",
    ),
    # Global, not per event: Moloni may require an exact redirect_uri match. See views.py.
    path(
        "control/ptinvoicing/moloni/callback/",
        views.CallbackView.as_view(),
        name="moloni_callback",
    ),
]
