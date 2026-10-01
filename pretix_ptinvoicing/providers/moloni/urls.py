from django.urls import path

from . import views

# Mounted by the core urls.py under the plugin's namespace, so these names reverse as
# "plugins:pretix_ptinvoicing:moloni_connect" etc.
_settings = "control/event/<str:organizer>/<str:event>/invoicing/settings/moloni/"

urlpatterns = [
    path(_settings + "connect/", views.ConnectView.as_view(), name="moloni_connect"),
    path(
        _settings + "disconnect/",
        views.DisconnectView.as_view(),
        name="moloni_disconnect",
    ),
    # Global, not per event: Moloni may require an exact redirect_uri match. See views.py.
    path(
        "control/ptinvoicing/moloni/callback/",
        views.CallbackView.as_view(),
        name="moloni_callback",
    ),
]
