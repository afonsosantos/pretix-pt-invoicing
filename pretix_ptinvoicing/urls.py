import importlib
import importlib.util

from django.urls import path

from . import views
from .providers import PROVIDERS

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
    path(
        _prefix + "<str:code>/credit/",
        views.IssueCreditNoteView.as_view(),
        name="credit",
    ),
    path(_prefix + "<int:pk>/download/", views.DownloadView.as_view(), name="download"),
]

# A provider's own URLs (Moloni's OAuth connect/callback) live in its package, as
# providers/<name>/urls.py; picked up here from the registry, so the core names none of
# them. find_spec, not try/except ImportError: an import error *inside* a provider's
# urls.py must surface, not read as "this provider has no URLs".
for _provider in PROVIDERS.values():
    _module = f"{_provider.__module__}.urls"
    if importlib.util.find_spec(_module):
        urlpatterns += importlib.import_module(_module).urlpatterns

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
