from datetime import timedelta
from decimal import Decimal

import pytest
import responses
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Event, Item, Order, OrderPosition, Organizer


@pytest.fixture
def organizer():
    with scopes_disabled():
        return Organizer.objects.create(name="Dummy", slug="dummy")


@pytest.fixture
def event(organizer):
    with scopes_disabled():
        return Event.objects.create(
            organizer=organizer,
            name="Dummy",
            slug="dummy",
            date_from=now(),
            live=True,
            currency="EUR",
        )


@pytest.fixture
def order(event):
    with scopes_disabled():
        return Order.objects.create(
            code="FOOBAR",
            event=event,
            email="dummy@example.org",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=Decimal("23.00"),
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )


@pytest.fixture
def item(event):
    with scopes_disabled():
        return Item.objects.create(
            event=event, name="Ticket", default_price=Decimal("23.00")
        )


@pytest.fixture
def position(order, item):
    with scopes_disabled():
        return OrderPosition.objects.create(
            order=order,
            item=item,
            price=Decimal("23.00"),
        )


def mock_factpt_taxes(tax_id=5, value="0.00"):
    """
    Register the GET /taxes response every issuance now makes.

    FactptProvider checks the configured Fact.pt rate against each position's tax_rate
    before issuing, so any test that reaches the API needs this.
    """
    responses.add(
        responses.GET,
        "https://api.fact.pt/taxes",
        json={
            "AppStatusCode": 200,
            "AppResponse": {
                "data": [
                    {
                        "id": str(tax_id),
                        "name": f"{value}%",
                        "description": "Taxa de teste",
                        "value": value,
                        "isActive": True,
                    }
                ],
                "totalPages": 1,
            },
        },
        status=200,
    )
