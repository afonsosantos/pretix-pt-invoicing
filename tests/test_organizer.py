"""
Settings on the organizer, inherited by its events: the organizer's own settings page,
what an event's page then stores (only what differs), and a provider connection shared
by every event that hasn't made its own.
"""

import time
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from django.core import mail as django_mail
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Event, Order

from pretix_ptinvoicing.models import IssuedInvoice
from pretix_ptinvoicing.providers import get_provider
from pretix_ptinvoicing.providers.moloni import MoloniProvider
from pretix_ptinvoicing.signals import ptinvoicing_keepalive
from pretix_ptinvoicing.tasks import issue_invoice
from pretix_ptinvoicing.views import own_settings_keys
from tests.conftest import mock_factpt_taxes
from tests.test_moloni import CALLBACK_URL, GRANT, mock_grant

ORGANIZER_URL = "/control/organizer/{}/invoicing/settings/"
EVENT_URL = "/control/event/{}/{}/invoicing/settings/"

FACTPT_POST = {
    "ptinvoicing_provider": "factpt",
    "ptinvoicing_auto_issue": "on",
    "ptinvoicing_show_in_order": "on",
    "ptinvoicing_reference_prefix": "pretix-",
    "factpt-factpt_token": "organizer-token",
    "factpt-factpt_default_tax_id": "5",
    "factpt-factpt_default_unit_id": "1",
    "factpt-factpt_default_type": "service",
}


def _fresh(event):
    """The event as another request would load it: no cached settings, nor organizer."""
    with scopes_disabled():
        return Event.objects.select_related("organizer").get(pk=event.pk)


@pytest.fixture
def second_event(organizer):
    with scopes_disabled():
        return Event.objects.create(
            organizer=organizer,
            name="Second",
            slug="second",
            date_from=now(),
            live=True,
            currency="EUR",
            plugins="pretix_ptinvoicing",
        )


@pytest.fixture
def moloni_organizer(organizer):
    # Connected through "Connect to Moloni" on the organizer's page: no event stores
    # anything of its own.
    s = organizer.settings
    s.ptinvoicing_provider = "moloni"
    s.moloni_client_id = "cid"
    s.moloni_client_secret = "secret"
    s.moloni_company_id = 7
    s.moloni_document_set_id = 3
    s.moloni_credit_note_document_set_id = 4
    s.moloni_payment_method_id = 5
    s.moloni_maturity_date_id = 9
    s.moloni_unit_id = 1
    s.moloni_refresh_token = "ref-org"
    s.moloni_access_token = "tok-org"
    s.moloni_token_expires = str(time.time() + 3000)
    return organizer


@pytest.mark.django_db
def test_organizer_settings_page_needs_the_organizer_permission(
    logged_in_client, organizer_client, event
):
    url = ORGANIZER_URL.format(event.organizer.slug)
    response = organizer_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "defaults for every event of this organizer" in content
    assert "ptinvoicing_provider" in content
    # Which payment method maps to which is each event's: no table here.
    assert 'name="moloni-moloni_payment_method_id_' not in content
    assert "{#" not in content

    with scopes_disabled():
        event.organizer.teams.update(all_organizer_permissions=False)
    assert logged_in_client.get(url).status_code == 403


@pytest.mark.django_db
def test_events_inherit_what_is_saved_on_the_organizer(organizer_client, event):
    response = organizer_client.post(
        ORGANIZER_URL.format(event.organizer.slug), data=FACTPT_POST
    )
    assert response.status_code == 302
    assert response["Location"] == ORGANIZER_URL.format(event.organizer.slug)

    event = _fresh(event)
    with scopes_disabled():
        assert event.organizer.settings.get("factpt_token") == "organizer-token"
        provider = get_provider(event)
        assert provider.identifier == "factpt"
        assert provider.is_configured
        assert own_settings_keys(event) == []


@pytest.mark.django_db
@responses.activate
def test_an_order_is_invoiced_through_the_organizer_s_settings(
    organizer_client, event, order, position
):
    organizer_client.post(ORGANIZER_URL.format(event.organizer.slug), data=FACTPT_POST)
    with scopes_disabled():
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "77"}}},
        status=200,
    )
    responses.add(responses.GET, "https://api.fact.pt/documents/77", json={})

    issue_invoice.apply(kwargs={"order_pk": order.pk, "event_pk": event.pk})

    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=order)
    assert invoice.status == IssuedInvoice.STATUS_SUCCESS, invoice.error_message
    assert invoice.document_id == "77"
    sent = next(c.request for c in responses.calls if c.request.method == "POST")
    assert sent.headers["x-auth-token"] == "organizer-token"


@pytest.mark.django_db
def test_saving_an_event_stores_only_what_differs_from_the_organizer(
    organizer_client, event
):
    organizer_client.post(ORGANIZER_URL.format(event.organizer.slug), data=FACTPT_POST)
    url = EVENT_URL.format(event.organizer.slug, event.slug)

    page = organizer_client.get(url).content.decode()
    assert "organizer's invoicing" in page
    # The inherited values are what the event's form starts from...
    assert 'value="organizer-token"' in page
    # ...with nothing of its own to go back from yet.
    assert "/invoicing/settings/reset/" not in page

    # Saved as shown: nothing is copied down to the event.
    assert organizer_client.post(url, data=FACTPT_POST).status_code == 302
    assert own_settings_keys(_fresh(event)) == []

    # One value changed: that one, and only that one, becomes the event's own.
    organizer_client.post(url, data={**FACTPT_POST, "factpt-factpt_default_tax_id": 9})
    event = _fresh(event)
    assert own_settings_keys(event) == ["factpt_default_tax_id"]
    assert event.settings.get("factpt_default_tax_id", as_type=int) == 9
    assert event.organizer.settings.get("factpt_default_tax_id", as_type=int) == 5
    assert "/invoicing/settings/reset/" in organizer_client.get(url).content.decode()


@pytest.mark.django_db
def test_an_event_can_opt_out_of_what_the_organizer_set(organizer_client, event):
    organizer_client.post(ORGANIZER_URL.format(event.organizer.slug), data=FACTPT_POST)
    url = EVENT_URL.format(event.organizer.slug, event.slug)

    # "No invoicing" and an unticked checkbox are values of the event's own, not a
    # return to what it inherits.
    response = organizer_client.post(
        url,
        data={"ptinvoicing_provider": "", "ptinvoicing_reference_prefix": "pretix-"},
    )
    assert response.status_code == 302

    event = _fresh(event)
    with scopes_disabled():
        assert get_provider(event) is None
        assert get_provider(event.organizer).identifier == "factpt"
    assert event.settings.get("ptinvoicing_auto_issue", as_type=bool) is False
    assert event.organizer.settings.get("ptinvoicing_auto_issue", as_type=bool) is True
    page = organizer_client.get(url).content.decode()
    assert 'value="factpt" checked' not in page


@pytest.mark.django_db
def test_an_event_can_go_back_to_the_organizer_s_settings(organizer_client, event):
    organizer_client.post(ORGANIZER_URL.format(event.organizer.slug), data=FACTPT_POST)
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "moloni"
        event.settings.factpt_token = "event-token"
        event.settings.moloni_refresh_token = "ref-event"
        # Not the plugin's: must survive.
        event.settings.contact_mail = "event@example.org"
    url = EVENT_URL.format(event.organizer.slug, event.slug) + "reset/"

    confirm = organizer_client.get(url)
    assert confirm.status_code == 200
    assert b"its own provider connection included" in confirm.content
    assert own_settings_keys(_fresh(event))  # asked first: nothing removed yet

    assert organizer_client.post(url).status_code == 302
    event = _fresh(event)
    assert own_settings_keys(event) == []
    assert event.settings.get("ptinvoicing_provider") == "factpt"
    assert event.settings.get("factpt_token") == "organizer-token"
    assert not event.settings.get("moloni_refresh_token")
    assert event.settings.get("contact_mail") == "event@example.org"


@pytest.mark.django_db
def test_organizer_page_is_linked_from_the_organizer_s_navigation(
    organizer_client, event
):
    from pretix_ptinvoicing.signals import ptinvoicing_nav_organizer

    url = ORGANIZER_URL.format(event.organizer.slug)
    request = organizer_client.get(url).wsgi_request
    entries = ptinvoicing_nav_organizer(
        event.organizer, request=request, organizer=event.organizer
    )
    assert [e["url"] for e in entries] == [url]
    assert entries[0]["active"]


@pytest.mark.django_db
@responses.activate
def test_organizer_connect_flow_stores_the_tokens_on_the_organizer(
    organizer_client, event
):
    organizer = event.organizer
    response = organizer_client.post(
        ORGANIZER_URL.format(organizer.slug) + "moloni/connect/",
        {"moloni-moloni_client_id": "cid", "moloni-moloni_client_secret": "secret"},
    )
    assert response.status_code == 302
    state = parse_qs(urlparse(response["Location"]).query)["state"][0]
    mock_grant(access="tok-code", refresh="ref-code")

    response = organizer_client.get(CALLBACK_URL, {"code": "abc", "state": state})

    assert response["Location"] == (
        ORGANIZER_URL.format(organizer.slug) + "?provider=moloni"
    )
    event = _fresh(event)
    assert event.organizer.settings.get("moloni_refresh_token") == "ref-code"
    # Every event of the organizer is connected by it, with nothing of its own.
    provider = MoloniProvider(event)
    assert provider.connection_state == "connected"
    assert provider.connection_inherited
    assert own_settings_keys(event) == []


@pytest.mark.django_db
@responses.activate
def test_an_event_refreshing_an_inherited_token_writes_it_back_to_the_organizer(
    moloni_organizer, event
):
    # A rotation spends the old refresh token: kept on the event, the organizer's — and
    # so every other event's — would be dead.
    moloni_organizer.settings.moloni_token_expires = "0"
    mock_grant(access="tok-new", refresh="ref-new")
    responses.add(responses.POST, "https://api.moloni.pt/v1/companies/getAll/", json=[])

    event = _fresh(event)
    assert MoloniProvider(event).connection_holder == event.organizer
    MoloniProvider(event)._client().call("companies/getAll")

    event = _fresh(event)
    assert event.organizer.settings.get("moloni_refresh_token") == "ref-new"
    assert own_settings_keys(event) == []


@pytest.mark.django_db
@responses.activate
def test_an_event_connecting_on_its_own_leaves_the_organizer_s_connection_alone(
    organizer_client, moloni_organizer, event
):
    url = EVENT_URL.format(event.organizer.slug, event.slug)
    page = organizer_client.get(url + "?provider=moloni").content.decode()
    assert "Connected through the organizer" in page
    assert "Connect this event on its own" in page
    # Nothing of the event's to disconnect.
    assert "moloni/disconnect/" not in page

    response = organizer_client.post(
        url + "moloni/connect/",
        {"moloni-moloni_client_id": "cid", "moloni-moloni_client_secret": "secret"},
    )
    state = parse_qs(urlparse(response["Location"]).query)["state"][0]
    mock_grant(access="tok-event", refresh="ref-event")
    organizer_client.get(CALLBACK_URL, {"code": "abc", "state": state})

    event = _fresh(event)
    assert event.organizer.settings.get("moloni_refresh_token") == "ref-org"
    assert event.settings.get("moloni_refresh_token") == "ref-event"
    provider = MoloniProvider(event)
    assert provider.connection_holder == event
    assert not provider.connection_inherited
    # The credentials typed were the organizer's: still not copied down.
    assert "moloni_client_id" not in own_settings_keys(event)

    # Disconnecting the event's own falls back to the organizer's.
    confirm = organizer_client.get(url + "moloni/disconnect/")
    assert b"connection again" in confirm.content
    assert b"until you connect again" not in confirm.content
    organizer_client.post(url + "moloni/disconnect/")
    provider = MoloniProvider(_fresh(event))
    assert provider.connection_state == "connected"
    assert provider.connection_inherited


@pytest.mark.django_db
@responses.activate
def test_keepalive_refreshes_a_shared_connection_once(
    moloni_organizer, event, second_event
):
    mock_grant(access="tok-new", refresh="ref-new")

    ptinvoicing_keepalive.__wrapped__(sender=None)

    # One organizer, two events, one connection: one refresh, of the organizer's token.
    grants = [c for c in responses.calls if c.request.url.startswith(GRANT)]
    assert len(grants) == 1
    assert parse_qs(urlparse(grants[0].request.url).query)["refresh_token"] == [
        "ref-org"
    ]
    assert _fresh(event).organizer.settings.get("moloni_refresh_token") == "ref-new"
    assert own_settings_keys(_fresh(event)) == []


@pytest.mark.django_db
@responses.activate
def test_a_dead_shared_connection_is_reported_once_per_contact_address(
    moloni_organizer, event, second_event
):
    moloni_organizer.settings.contact_mail = "organizer@example.org"
    responses.add(
        responses.GET,
        GRANT,
        json={"error": "invalid_grant", "error_description": "expired"},
        status=400,
    )
    django_mail.outbox = []

    ptinvoicing_keepalive.__wrapped__(sender=None)

    # Both events take their contact address from the organizer: one mail, pointing at
    # the organizer's page — that is where the connection is.
    assert [m.to for m in django_mail.outbox] == [["organizer@example.org"]]
    assert ORGANIZER_URL.format(moloni_organizer.slug) in django_mail.outbox[0].body
    for holder in (_fresh(event), _fresh(second_event)):
        provider = MoloniProvider(holder)
        assert provider.connection_state == "expired"
        assert not provider.is_configured


@pytest.mark.django_db
def test_an_event_s_page_points_at_the_organizer_when_its_connection_expired(
    organizer_client, moloni_organizer, event
):
    moloni_organizer.settings.moloni_connection_expired = True
    page = organizer_client.get(
        EVENT_URL.format(event.organizer.slug, event.slug) + "?provider=moloni"
    ).content.decode()
    assert "The organizer's connection to Moloni has expired" in page
    assert ORGANIZER_URL.format(event.organizer.slug) + "?provider=moloni" in page


@pytest.mark.django_db
@responses.activate
def test_reconnecting_clears_an_inherited_expiry_for_the_event(moloni_organizer, event):
    # An event that connects on its own must stop showing its organizer's dead
    # connection: the flag is overridden, not just absent.
    moloni_organizer.settings.moloni_connection_expired = True
    mock_grant(access="tok-event", refresh="ref-event")

    MoloniProvider(_fresh(event)).connect("abc", "https://example.org/callback")

    assert MoloniProvider(_fresh(event)).connection_state == "connected"
    assert MoloniProvider(_fresh(event).organizer).connection_state == "expired"


@pytest.mark.django_db
def test_nothing_set_on_either_level_means_no_provider(event):
    assert get_provider(event) is None
    assert get_provider(event.organizer) is None
