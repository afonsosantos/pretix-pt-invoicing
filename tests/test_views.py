import re
from unittest import mock

import pytest
import responses
from django_scopes import scopes_disabled
from pretix.base.models import Order, User

from pretix_ptinvoicing.models import IssuedInvoice
from pretix_ptinvoicing.providers import PROVIDERS
from tests.conftest import mock_factpt_taxes

SETTINGS_URL = "/control/event/{}/{}/invoicing/settings/"
LOOKUPS_URL = "/control/event/{}/{}/invoicing/settings/lookups/"
INDEX_URL = "/control/event/{}/{}/invoicing/"
DOWNLOAD_URL = "/control/event/{}/{}/invoicing/{}/download/"
ISSUE_URL = "/control/event/{}/{}/invoicing/{}/issue/"
CREDIT_URL = "/control/event/{}/{}/invoicing/{}/credit/"


@pytest.mark.django_db
def test_get_settings_page(logged_in_client, event):
    response = logged_in_client.get(
        SETTINGS_URL.format(event.organizer.slug, event.slug)
    )
    assert response.status_code == 200
    assert b"ptinvoicing_provider" in response.content
    assert b"API token" in response.content
    # The whole provider-agnostic form renders, not just the provider selector.
    assert b"ptinvoicing_email_invoice" in response.content
    assert b"ptinvoicing_email_credit_note" in response.content
    assert b"ptinvoicing_show_in_order" in response.content
    # On by default: an unticked box would turn automatic issuance off on the first Save.
    auto = re.search(
        rb'<input[^>]*name="ptinvoicing_auto_issue"[^>]*>', response.content
    ).group(0)
    assert b"checked" in auto


@pytest.mark.django_db
def test_post_settings_page_saves(logged_in_client, event):
    response = logged_in_client.post(
        SETTINGS_URL.format(event.organizer.slug, event.slug),
        data={
            "ptinvoicing_provider": "factpt",
            "ptinvoicing_email_invoice": "on",
            "factpt-factpt_token": "posted-token",
            "factpt-factpt_default_tax_id": "5",
            "factpt-factpt_default_unit_id": "1",
            "factpt-factpt_default_type": "service",
        },
    )
    assert response.status_code == 302

    with scopes_disabled():
        event.settings.flush()
        assert event.settings.get("ptinvoicing_provider") == "factpt"
        assert event.settings.get("factpt_token") == "posted-token"
        assert event.settings.get("factpt_default_tax_id", as_type=int) == 5
        # The e-mail settings are a separate form from the provider selector and must be
        # saved alongside it, not silently dropped.
        assert event.settings.get("ptinvoicing_email_invoice", as_type=bool) is True
        assert (
            event.settings.get("ptinvoicing_email_credit_note", as_type=bool) is False
        )


@pytest.mark.django_db
def test_settings_page_asks_for_a_save_until_factpt_is_in_use(logged_in_client, event):
    url = SETTINGS_URL.format(event.organizer.slug, event.slug)
    # Chosen but without a token: still not in use.
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
    assert "fill in the Fact.pt settings" in logged_in_client.get(url).content.decode()

    with scopes_disabled():
        event.settings.factpt_token = "tok"
    content = logged_in_client.get(url).content.decode()
    assert "fill in the Fact.pt settings" not in content


@pytest.mark.django_db
def test_settings_page_requires_permission(client, event):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.get(SETTINGS_URL.format(event.organizer.slug, event.slug))
    assert response.status_code == 404


@pytest.mark.django_db
@responses.activate
def test_lookups_returns_active_taxes_with_combined_label(logged_in_client, event):
    responses.add(
        responses.GET,
        "https://api.fact.pt/taxes",
        json={
            "AppStatusCode": 200,
            "AppResponse": {
                "data": [
                    {
                        "id": "25",
                        "name": "23%",
                        "description": "Taxa normal",
                        "value": "23.00",
                        "isActive": True,
                    },
                    {
                        "id": "9",
                        "name": "6%",
                        "description": "Taxa reduzida (descontinuada)",
                        "value": "6.00",
                        "isActive": False,
                    },
                ],
                "totalItems": 2,
                "totalPages": 1,
            },
        },
        status=200,
    )

    response = logged_in_client.post(
        LOOKUPS_URL.format(event.organizer.slug, event.slug),
        data={
            "provider": "factpt",
            "factpt_token": "some-token",
            "factpt_sandbox": "false",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["fields"]["factpt_default_tax_id"] == [
        {"id": "25", "label": "Taxa normal (23%)"}
    ]


@pytest.mark.django_db
def test_lookups_requires_token(logged_in_client, event):
    response = logged_in_client.post(
        LOOKUPS_URL.format(event.organizer.slug, event.slug),
        data={"provider": "factpt"},
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_lookups_rejects_unknown_provider(logged_in_client, event):
    response = logged_in_client.post(
        LOOKUPS_URL.format(event.organizer.slug, event.slug), data={"provider": "nope"}
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_lookups_requires_permission(client, event):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.post(
        LOOKUPS_URL.format(event.organizer.slug, event.slug),
        data={"provider": "factpt"},
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_index_lists_invoices(logged_in_client, event, order):
    with scopes_disabled():
        IssuedInvoice.objects.create(
            order=order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_ERROR,
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


@pytest.fixture
def issued_invoice(order):
    with scopes_disabled():
        return IssuedInvoice.objects.create(
            order=order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="12345",
        )


@pytest.mark.django_db
@responses.activate
def test_download_proxies_the_provider_pdf(logged_in_client, event, issued_invoice):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345/download",
        body=b"%PDF-1.4 fake",
        status=200,
        content_type="application/pdf",
    )

    response = logged_in_client.get(
        DOWNLOAD_URL.format(event.organizer.slug, event.slug, issued_invoice.pk)
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content == b"%PDF-1.4 fake"


@pytest.mark.django_db
def test_download_404s_when_the_event_switched_provider(
    logged_in_client, event, issued_invoice
):
    # No provider selected any more: its credentials are gone, so there is nothing to
    # fetch the PDF with. Registered no HTTP mock — a call out would blow up the test.
    response = logged_in_client.get(
        DOWNLOAD_URL.format(event.organizer.slug, event.slug, issued_invoice.pk)
    )
    assert response.status_code == 404


@pytest.fixture
def paid_order(order, event):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        event.settings.factpt_default_tax_id = 5
        order.status = Order.STATUS_PAID
        order.save(update_fields=["status"])
    return order


@pytest.mark.django_db
@responses.activate
def test_issue_view_issues_an_order_with_no_invoice_yet(
    logged_in_client, event, paid_order, position
):
    mock_factpt_taxes()
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "12345"}}},
        status=200,
    )

    response = logged_in_client.post(
        ISSUE_URL.format(event.organizer.slug, event.slug, paid_order.code)
    )

    assert response.status_code == 302
    with scopes_disabled():
        invoice = IssuedInvoice.objects.get(order=paid_order)
    assert invoice.status == IssuedInvoice.STATUS_SUCCESS
    assert invoice.document_id == "12345"


@pytest.mark.django_db
def test_issue_view_redirects_back_to_a_local_next(logged_in_client, event, paid_order):
    back = (
        f"/control/event/{event.organizer.slug}/{event.slug}/orders/{paid_order.code}/"
    )

    with scopes_disabled():
        event.settings.factpt_token = (
            ""  # unconfigured: the task returns before any HTTP
        )
    response = logged_in_client.post(
        ISSUE_URL.format(event.organizer.slug, event.slug, paid_order.code),
        data={"next": back},
    )
    assert response.status_code == 302
    assert response["Location"] == back


@pytest.mark.django_db
def test_issue_view_ignores_an_offsite_next(logged_in_client, event, paid_order):
    with scopes_disabled():
        event.settings.factpt_token = ""
    response = logged_in_client.post(
        ISSUE_URL.format(event.organizer.slug, event.slug, paid_order.code),
        data={"next": "https://evil.example.com/"},
    )
    assert response.status_code == 302
    assert response["Location"] == INDEX_URL.format(event.organizer.slug, event.slug)


@pytest.mark.django_db
def test_issue_view_404s_for_an_order_of_another_event(logged_in_client, event):
    response = logged_in_client.post(
        ISSUE_URL.format(event.organizer.slug, event.slug, "NOPE1")
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_issue_view_requires_permission(client, event, paid_order):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.post(
        ISSUE_URL.format(event.organizer.slug, event.slug, paid_order.code)
    )
    assert response.status_code == 404


@pytest.mark.django_db
@responses.activate
def test_credit_view_issues_a_credit_note_for_an_invoiced_order(
    logged_in_client, event, issued_invoice
):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/12345/credit",
        json={"AppStatusCode": 200, "AppResponse": {"data": {"id": "999"}}},
        status=200,
    )

    response = logged_in_client.post(
        CREDIT_URL.format(event.organizer.slug, event.slug, issued_invoice.order.code)
    )

    assert response.status_code == 302
    with scopes_disabled():
        credit_note = IssuedInvoice.objects.get(kind=IssuedInvoice.KIND_CREDIT_NOTE)
    assert credit_note.status == IssuedInvoice.STATUS_SUCCESS
    assert credit_note.document_id == "999"


@pytest.mark.django_db
def test_credit_view_requires_permission(client, event, issued_invoice):
    with scopes_disabled():
        User.objects.create_user("noaccess@example.org", "dummy")
    client.login(email="noaccess@example.org", password="dummy")

    response = client.post(
        CREDIT_URL.format(event.organizer.slug, event.slug, issued_invoice.order.code)
    )
    assert response.status_code == 404


@pytest.fixture
def plugin_enabled_event(event):
    # order_info is an EventPluginSignal: it only reaches plugins active for the event.
    with scopes_disabled():
        event.plugins = "pretix_ptinvoicing"
        event.save(update_fields=["plugins"])
    return event


@pytest.mark.django_db
def test_order_page_panel_offers_a_working_issue_button(
    logged_in_client, plugin_enabled_event, paid_order
):
    response = logged_in_client.get(
        f"/control/event/{plugin_enabled_event.organizer.slug}/"
        f"{plugin_enabled_event.slug}/orders/{paid_order.code}/"
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert "Issue invoice now" in body
    # {# #} is single-line only; a multi-line one renders as visible text (shipped twice
    # before — see settings.html's own guard below).
    assert "{#" not in body

    issue_url = ISSUE_URL.format(
        plugin_enabled_event.organizer.slug,
        plugin_enabled_event.slug,
        paid_order.code,
    )
    # A link to a confirmation page, not a direct POST: it issues a legal document.
    link = re.search(rf'href="({re.escape(issue_url)}\?next=[^"]*)"', body)
    assert link, "panel has no link to the confirmation page"

    confirm = logged_in_client.get(link.group(1).replace("&amp;", "&"))
    assert confirm.status_code == 200
    page = confirm.content.decode()
    assert "cannot be undone" in page
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]*)"', page)
    assert token and token.group(1), "confirmation form has no CSRF token"
    assert 'name="next" value="/control/event/' in page

    with mock.patch("pretix_ptinvoicing.views.issue_invoice") as task:
        posted = logged_in_client.post(
            issue_url, data={"csrfmiddlewaretoken": token.group(1), "next": "/control/"}
        )
    assert posted.status_code == 302
    assert posted["Location"] == "/control/"
    task.apply_async.assert_called_once()


@pytest.mark.django_db
def test_order_page_panel_offers_a_credit_note_button_once_invoiced(
    logged_in_client, plugin_enabled_event, paid_order
):
    with scopes_disabled():
        IssuedInvoice.objects.create(
            order=paid_order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="12345",
        )

    response = logged_in_client.get(
        f"/control/event/{plugin_enabled_event.organizer.slug}/"
        f"{plugin_enabled_event.slug}/orders/{paid_order.code}/"
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert "Issue credit note" in body
    assert (
        CREDIT_URL.format(
            plugin_enabled_event.organizer.slug,
            plugin_enabled_event.slug,
            paid_order.code,
        )
        in body
    )


@pytest.mark.django_db
@responses.activate
def test_buyer_can_download_the_invoice_with_the_order_secret(client, event, order):
    with scopes_disabled():
        event.plugins = "pretix_ptinvoicing"
        event.save(update_fields=["plugins"])
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        invoice = IssuedInvoice.objects.create(
            order=order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="12345",
        )
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345/download",
        body=b"%PDF-1.4 fake",
        status=200,
        content_type="application/pdf",
    )

    url = (
        f"/{event.organizer.slug}/{event.slug}/order/{order.code}/{order.secret}"
        f"/invoicing/{invoice.pk}/download/"
    )
    response = client.get(url)

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response.content == b"%PDF-1.4 fake"


@pytest.mark.django_db
def test_buyer_download_rejects_a_wrong_secret(client, event, order):
    with scopes_disabled():
        event.plugins = "pretix_ptinvoicing"
        event.save(update_fields=["plugins"])
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
        invoice = IssuedInvoice.objects.create(
            order=order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="12345",
        )

    # No HTTP mock registered: reaching the provider would blow up the test.
    response = client.get(
        f"/{event.organizer.slug}/{event.slug}/order/{order.code}/wrongsecret"
        f"/invoicing/{invoice.pk}/download/"
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_no_template_comment_leaks_onto_the_settings_page(logged_in_client, event):
    # {# ... #} is single-line only in Django: a multi-line one renders as visible text.
    body = logged_in_client.get(
        SETTINGS_URL.format(event.organizer.slug, event.slug)
    ).content.decode()
    assert "{#" not in body
    assert "provider-agnostic setting" not in body


@pytest.mark.django_db
def test_saving_one_provider_leaves_the_other_untouched(logged_in_client, event):
    # Only testable now that a second provider exists: SettingsView binds just the selected
    # provider's form, so the others keep their stored settings and can't block a save with
    # their own required fields.
    with scopes_disabled():
        event.settings.factpt_token = "keep-me"
        event.settings.factpt_default_tax_id = 5

    response = logged_in_client.post(
        SETTINGS_URL.format(event.organizer.slug, event.slug),
        data={
            "ptinvoicing_provider": "moloni",
            "moloni-moloni_client_id": "cid",
            "moloni-moloni_client_secret": "secret",
            "moloni-moloni_username": "user",
            "moloni-moloni_password": "pass",
            "moloni-moloni_company_id": "7",
            "moloni-moloni_document_set_id": "3",
            "moloni-moloni_credit_note_document_set_id": "4",
            "moloni-moloni_payment_method_id": "5",
            "moloni-moloni_maturity_date_id": "9",
            "moloni-moloni_product_category_id": "20",
            "moloni-moloni_product_type": "2",
            "moloni-moloni_unit_id": "1",
        },
    )
    assert response.status_code == 302

    with scopes_disabled():
        event.settings.flush()
        assert event.settings.get("ptinvoicing_provider") == "moloni"
        assert event.settings.get("moloni_company_id", as_type=int) == 7
        # Fact.pt's required token was neither validated nor cleared.
        assert event.settings.get("factpt_token") == "keep-me"


@pytest.mark.django_db
def test_settings_page_lists_every_registered_provider(logged_in_client, event):
    body = logged_in_client.get(
        SETTINGS_URL.format(event.organizer.slug, event.slug)
    ).content.decode()
    for identifier in PROVIDERS:
        assert f'data-provider="{identifier}"' in body


def _checked_provider(content):
    return re.search(
        rb'name="ptinvoicing_provider" value="([^"]*)"\s+checked', content
    ).group(1)


@pytest.mark.django_db
def test_provider_cards_show_logos_and_preselect(logged_in_client, event):
    url = SETTINGS_URL.format(event.organizer.slug, event.slug)
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"

    content = logged_in_client.get(url).content
    assert b"logos/factpt.svg" in content
    assert b"logos/moloni.svg" in content
    assert _checked_provider(content) == b"factpt"
    # Coming back from the Moloni connect flow: Moloni stays shown, nothing is saved.
    assert _checked_provider(
        logged_in_client.get(url + "?provider=moloni").content
    ) == (b"moloni")
    assert _checked_provider(logged_in_client.get(url + "?provider=bogus").content) == (
        b"factpt"
    )
    with scopes_disabled():
        event.settings.flush()
        assert event.settings.get("ptinvoicing_provider") == "factpt"


@pytest.mark.django_db
def test_settings_inputs_opt_out_of_password_managers(logged_in_client, event):
    content = logged_in_client.get(
        SETTINGS_URL.format(event.organizer.slug, event.slug)
    ).content.decode()
    token = re.search(r'<input[^>]*name="factpt-factpt_token"[^>]*>', content).group(0)
    assert 'data-bwignore="true"' in token
    assert 'data-1p-ignore="true"' in token
    password = re.search(
        r'<input[^>]*name="moloni-moloni_password"[^>]*>', content
    ).group(0)
    assert 'autocomplete="new-password"' in password


@pytest.mark.django_db
def test_order_page_panel_offers_a_new_invoice_after_a_credited_refund(
    logged_in_client, plugin_enabled_event, paid_order
):
    # Paid → invoiced → refunded → credited → paid again: both old documents stay
    # listed, and the panel offers a new invoice instead of looking finished.
    with scopes_disabled():
        invoice = IssuedInvoice.objects.create(
            order=paid_order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="1",
            document_number="FR A/1",
        )
        IssuedInvoice.objects.create(
            order=paid_order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR-credit",
            kind=IssuedInvoice.KIND_CREDIT_NOTE,
            credits=invoice,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="2",
            document_number="NC A/1",
        )

    body = logged_in_client.get(
        f"/control/event/{plugin_enabled_event.organizer.slug}/"
        f"{plugin_enabled_event.slug}/orders/{paid_order.code}/"
    ).content.decode()
    assert "FR A/1" in body
    assert "NC A/1" in body
    assert "Issue invoice now" in body


def _order_page(client, event, order):
    return client.get(
        f"/control/event/{event.organizer.slug}/{event.slug}/orders/{order.code}/"
    ).content.decode()


@pytest.mark.django_db
def test_order_page_panel_hides_retry_while_an_attempt_is_in_flight(
    logged_in_client, event, paid_order
):
    # Retrying while the first attempt runs could issue twice (Moloni can't dedupe).
    with scopes_disabled():
        IssuedInvoice.objects.create(
            order=paid_order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_PENDING,
            attempts=1,
        )

    body = _order_page(logged_in_client, event, paid_order)
    assert "In progress" in body
    assert "Retry issuance" not in body


@pytest.mark.django_db
def test_order_page_panel_says_when_the_provider_is_not_configured(
    logged_in_client, event, paid_order
):
    # Used to vanish entirely — exactly when the admin most needs to know.
    with scopes_disabled():
        event.settings.factpt_token = ""

    body = _order_page(logged_in_client, event, paid_order)
    assert "not fully configured" in body
    assert "Issue invoice now" not in body


@pytest.mark.django_db
def test_order_page_panel_shows_the_providers_per_field_errors(
    logged_in_client, event, paid_order
):
    with scopes_disabled():
        IssuedInvoice.objects.create(
            order=paid_order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_ERROR,
            error_message="Moloni rejected the request.",
            error_detail={"vat": "Invalid"},
        )

    body = _order_page(logged_in_client, event, paid_order)
    assert "Moloni rejected the request." in body
    assert "<li>vat: Invalid</li>" in body


@pytest.mark.django_db
def test_issue_view_refuses_an_unpaid_order_up_front(logged_in_client, event, order):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"

    with mock.patch("pretix_ptinvoicing.views.issue_invoice") as task:
        response = logged_in_client.post(
            ISSUE_URL.format(event.organizer.slug, event.slug, order.code),
            follow=True,
        )
    task.apply_async.assert_not_called()
    assert "Only paid orders can be invoiced." in response.content.decode()


@pytest.mark.django_db
def test_credit_view_refuses_when_nothing_is_left_to_credit(
    logged_in_client, event, paid_order
):
    with mock.patch("pretix_ptinvoicing.views.issue_credit_note") as task:
        response = logged_in_client.post(
            CREDIT_URL.format(event.organizer.slug, event.slug, paid_order.code),
            follow=True,
        )
    task.apply_async.assert_not_called()
    assert "no issued invoice left to credit" in response.content.decode()


@pytest.mark.django_db
def test_views_404_when_the_plugin_is_disabled(logged_in_client, event, paid_order):
    with scopes_disabled():
        event.plugins = ""
        event.save(update_fields=["plugins"])

    for url in (
        INDEX_URL.format(event.organizer.slug, event.slug),
        SETTINGS_URL.format(event.organizer.slug, event.slug),
        ISSUE_URL.format(event.organizer.slug, event.slug, paid_order.code),
    ):
        assert logged_in_client.get(url).status_code == 404


@pytest.mark.django_db
def test_index_filters_by_order_code(logged_in_client, event, issued_invoice):
    url = INDEX_URL.format(event.organizer.slug, event.slug)
    assert "FOOBAR" in logged_in_client.get(url + "?query=foo").content.decode()
    body = logged_in_client.get(url + "?query=nope").content.decode()
    assert "No documents match this filter." in body
    # No inline handler: the Control panel's CSP would block it.
    assert "onchange=" not in logged_in_client.get(url).content.decode()


@pytest.mark.django_db
@responses.activate
def test_a_failed_download_returns_to_the_order_with_a_message(
    logged_in_client, event, issued_invoice
):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
        event.settings.factpt_token = "test-token"
    responses.add(
        responses.GET, "https://api.fact.pt/documents/12345/download", status=500
    )

    response = logged_in_client.get(
        DOWNLOAD_URL.format(event.organizer.slug, event.slug, issued_invoice.pk)
    )
    assert response.status_code == 302
    assert response["Location"].endswith(f"/orders/{issued_invoice.order.code}/")


def _buyer_order_url(event, order):
    return f"/{event.organizer.slug}/{event.slug}/order/{order.code}/{order.secret}/"


@pytest.mark.django_db
def test_buyer_sees_the_credit_note_too(client, event, paid_order, issued_invoice):
    with scopes_disabled():
        IssuedInvoice.objects.create(
            order=paid_order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR-credit",
            kind=IssuedInvoice.KIND_CREDIT_NOTE,
            credits=issued_invoice,
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="999",
        )

    body = client.get(_buyer_order_url(event, paid_order)).content.decode()
    assert "Download invoice" in body
    assert "Download credit note" in body


@pytest.mark.django_db
def test_buyer_download_honours_the_show_in_order_setting(
    client, event, paid_order, issued_invoice
):
    with scopes_disabled():
        event.settings.ptinvoicing_show_in_order = False

    # No HTTP mock registered: reaching the provider would blow up the test.
    response = client.get(
        _buyer_order_url(event, paid_order) + f"invoicing/{issued_invoice.pk}/download/"
    )
    assert response.status_code == 404
