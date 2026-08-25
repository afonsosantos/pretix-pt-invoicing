import pytest
import responses
from django_scopes import scopes_disabled
from pretix.base.models import Team, User

from pretix_factpt.models import FactptInvoice

SETTINGS_URL = "/control/event/{}/{}/factpt/settings/"
LOOKUPS_URL = "/control/event/{}/{}/factpt/settings/lookups/"
INDEX_URL = "/control/event/{}/{}/factpt/"


@pytest.fixture
def logged_in_client(client, event):
    with scopes_disabled():
        user = User.objects.create_user("dummy@example.org", "dummy")
        team = Team.objects.create(
            organizer=event.organizer, all_event_permissions=True
        )
        team.members.add(user)
        team.limit_events.add(event)
    client.login(email="dummy@example.org", password="dummy")
    return client


@pytest.mark.django_db
def test_get_settings_page(logged_in_client, event):
    response = logged_in_client.get(
        SETTINGS_URL.format(event.organizer.slug, event.slug)
    )
    assert response.status_code == 200
    assert b"API token" in response.content


@pytest.mark.django_db
def test_post_settings_page_saves(logged_in_client, event):
    response = logged_in_client.post(
        SETTINGS_URL.format(event.organizer.slug, event.slug),
        data={
            "factpt-factpt_token": "posted-token",
            "factpt-factpt_default_tax_id": "5",
            "factpt-factpt_default_unit_id": "1",
            "factpt-factpt_default_type": "service",
        },
    )
    assert response.status_code == 302

    with scopes_disabled():
        event.settings.flush()
        assert event.settings.get("factpt_token") == "posted-token"
        assert event.settings.get("factpt_default_tax_id", as_type=int) == 5


@pytest.mark.django_db
def test_settings_page_requires_permission(client, event):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.get(SETTINGS_URL.format(event.organizer.slug, event.slug))
    assert response.status_code == 404


@pytest.mark.django_db
@responses.activate
def test_lookups_returns_taxes_and_units(logged_in_client, event):
    responses.add(
        responses.GET,
        "https://api.fact.pt/support/api?c=lists&s=taxes",
        json={
            "AppStatusCode": 200,
            "AppResponse": {"data": [{"id": 5, "name": "Normal - 23%"}]},
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.fact.pt/support/api?c=lists&s=product_unit",
        json={
            "AppStatusCode": 200,
            "AppResponse": {"data": [{"id": 1, "name": "Unit"}]},
        },
        status=200,
    )

    response = logged_in_client.post(
        LOOKUPS_URL.format(event.organizer.slug, event.slug),
        data={"token": "some-token", "sandbox": "false"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["taxes"] == [{"id": 5, "label": "Normal - 23%"}]
    assert data["units"] == [{"id": 1, "label": "Unit"}]


@pytest.mark.django_db
def test_lookups_requires_token(logged_in_client, event):
    response = logged_in_client.post(
        LOOKUPS_URL.format(event.organizer.slug, event.slug), data={}
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_lookups_requires_permission(client, event):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.post(
        LOOKUPS_URL.format(event.organizer.slug, event.slug), data={"token": "x"}
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_index_lists_invoices(logged_in_client, event, order):
    with scopes_disabled():
        FactptInvoice.objects.create(
            order=order,
            identifier_id="pretix-dummy-FOOBAR",
            status=FactptInvoice.STATUS_ERROR,
            error_message="tin: Invalid",
        )

    response = logged_in_client.get(INDEX_URL.format(event.organizer.slug, event.slug))
    assert response.status_code == 200
    assert b"tin: Invalid" in response.content
    assert b"FOOBAR" in response.content


@pytest.mark.django_db
def test_index_requires_permission(client, event):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.get(INDEX_URL.format(event.organizer.slug, event.slug))
    assert response.status_code == 404
