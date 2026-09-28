from django.urls import path

from . import views

_prefix = "control/event/<str:organizer>/<str:event>/invoicing/"

urlpatterns = [
    path(_prefix, views.IndexView.as_view(), name="index"),
    path(_prefix + "settings/", views.SettingsView.as_view(), name="settings"),
    path(
        _prefix + "settings/lookups/",
        views.SettingsLookupsView.as_view(),
        name="settings_lookups",
    ),
    path(_prefix + "<str:code>/issue/", views.IssueView.as_view(), name="issue"),
    path(_prefix + "<int:pk>/download/", views.DownloadView.as_view(), name="download"),
]

# Buyer-facing URL. Unlike the Control-panel ones above it has to go through
# event_patterns: pretix mounts a plugin's event_patterns under the event (and under a
# custom event domain, if there is one), which is what makes {% eventurl %} resolve.
event_patterns = [
    path(
        "order/<str:order>/<str:secret>/invoicing/<int:pk>/download/",
        views.OrderInvoiceDownloadView.as_view(),
        name="order_download",
    ),
]
