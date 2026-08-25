from django.urls import path

from . import views

urlpatterns = [
    path(
        "control/event/<str:organizer>/<str:event>/factpt/",
        views.IndexView.as_view(),
        name="index",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/factpt/settings/",
        views.SettingsView.as_view(),
        name="settings",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/factpt/settings/lookups/",
        views.SettingsLookupsView.as_view(),
        name="settings_lookups",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/factpt/<int:pk>/retry/",
        views.RetryView.as_view(),
        name="retry",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/factpt/<int:pk>/download/",
        views.DownloadView.as_view(),
        name="download",
    ),
]
