import re
from decimal import Decimal
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
def test_buyer_download_rejects_a_disabled_plugin(client, event, order):
    with scopes_disabled():
        invoice = IssuedInvoice.objects.create(
            order=order,
            provider="factpt",
            identifier_id="pretix-dummy-FOOBAR",
            status=IssuedInvoice.STATUS_SUCCESS,
            document_id="12345",
        )

    # The buyer-facing event_patterns URL remains routable after plugin deactivation.
    # No HTTP mock: a disabled event must 404 before contacting the provider.
    response = client.get(
        f"/{event.organizer.slug}/{event.slug}/order/{order.code}/{order.secret}"
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
def test_settings_page_shows_the_prefix_and_the_limit_note(logged_in_client, event):
    url = SETTINGS_URL.format(event.organizer.slug, event.slug)
    body = logged_in_client.get(url).content.decode()
    # The default prefix prefilled, no note yet: no provider is selected.
    assert 'name="ptinvoicing_reference_prefix"' in body
    assert 'value="pretix-"' in body
    assert '<span id="ptinvoicing-prefix-note"></span>' in body
    # Moloni's fieldset tells settings.js which field holds the limit.
    assert 'data-reference-limit-field="moloni_reference_limit"' in body
    assert 'data-reference-limit-field=""' in body  # Fact.pt keeps no catalog

    with scopes_disabled():
        event.settings.ptinvoicing_provider = "moloni"
        event.settings.ptinvoicing_reference_prefix = "ev-"
    body = logged_in_client.get(url).content.decode()
    assert (
        "With Moloni, prefix and product reference together may be at most 30" in body
    )
    assert "27 left for the reference" in body


def _item_form(event, item, data=None):
    from django.test import RequestFactory

    from pretix_ptinvoicing.signals import ptinvoicing_item_forms

    request = RequestFactory().post("/", data) if data is not None else None
    if request is None:
        request = RequestFactory().get("/")
    return ptinvoicing_item_forms(event, request=request, item=item)


@pytest.mark.django_db
def test_product_page_form_only_with_a_provider(event, item):
    assert _item_form(event, item) is None
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "moloni"
    form = _item_form(event, item)
    assert form.title == "Invoicing (PT)"
    assert list(form.fields) == ["reference", "moloni_type"]
    # 30 for Moloni, 7 taken by "pretix-".
    assert form.fields["reference"].max_length == 23
    assert f"pretix-item-{item.pk}" in form.fields["reference"].help_text

    with scopes_disabled():
        event.settings.ptinvoicing_provider = "factpt"
    form = _item_form(event, item)
    # Fact.pt invoices lines inline: no per-product fields, no limit on the reference.
    assert list(form.fields) == ["reference"]
    assert form.fields["reference"].max_length == 50


@pytest.mark.django_db
def test_product_page_form_saves_validates_and_clears(event, item):
    from pretix.base.models import Item

    from pretix_ptinvoicing.models import ItemSettings
    from pretix_ptinvoicing.orderdata import item_reference

    with scopes_disabled():
        event.settings.ptinvoicing_provider = "moloni"
        other = Item.objects.create(event=event, name="Other", default_price=1)
        ItemSettings.objects.create(item=other, reference="taken")

    def post(**data):
        form = _item_form(event, item, {f"ptinvoicing-{k}": v for k, v in data.items()})
        valid = form.is_valid()
        if valid:
            form.save()
        return form, valid

    # Too long for what the prefix leaves.
    form, valid = post(reference="x" * 24)
    assert not valid and "reference" in form.errors
    # The fallback shapes are reserved: they'd collide with another product's.
    form, valid = post(reference="item-99")
    assert not valid and "reserved" in str(form.errors["reference"])
    # Another product of the event already has it.
    form, valid = post(reference="taken")
    assert not valid and "Other" in str(form.errors["reference"])
    # A type Moloni doesn't know.
    form, valid = post(moloni_type="9")
    assert not valid and "moloni_type" in form.errors

    form, valid = post(reference=" bilhete ", moloni_type="1")
    assert valid
    with scopes_disabled():
        saved = ItemSettings.objects.get(item=item)
        assert saved.reference == "bilhete"
        assert saved.data == {"moloni_type": 1}
        assert item_reference(item) == "bilhete"
        # Prefilled next time.
        form = _item_form(event, item)
        assert form.initial == {"reference": "bilhete", "moloni_type": 1}

    # Cleared: the row goes, the reference falls back to the pretix id.
    form, valid = post(reference="", moloni_type="")
    assert valid
    with scopes_disabled():
        assert not ItemSettings.objects.filter(item=item).exists()
        assert item_reference(Item.objects.get(pk=item.pk)) == f"item-{item.pk}"


@pytest.mark.django_db
def test_lines_carry_the_product_reference_and_settings(event, order, item, position):
    from pretix_ptinvoicing.models import ItemSettings
    from pretix_ptinvoicing.orderdata import invoice_lines

    with scopes_disabled():
        [line] = invoice_lines(order)
        assert line.catalog_key == f"item-{item.pk}"
        assert line.item_settings == {}

        ItemSettings.objects.create(
            item=item, reference="geral", data={"moloni_type": 3}
        )
        [line] = invoice_lines(order)
        assert line.catalog_key == "geral"
        assert line.item_settings == {"moloni_type": 3}


@pytest.mark.django_db
def test_copying_a_product_keeps_the_type_but_not_the_reference(event, item):
    from pretix.base.models import Item

    from pretix_ptinvoicing.models import ItemSettings
    from pretix_ptinvoicing.signals import ptinvoicing_item_copy

    with scopes_disabled():
        ItemSettings.objects.create(
            item=item, reference="geral", data={"moloni_type": 1}
        )
        copy = Item.objects.create(event=event, name="Copy", default_price=1)
        ptinvoicing_item_copy(event, source=item, target=copy)
        copied = ItemSettings.objects.get(item=copy)
        assert copied.reference == ""
        assert copied.data == {"moloni_type": 1}

        # Nothing to copy: no row.
        plain = Item.objects.create(event=event, name="Plain", default_price=1)
        another = Item.objects.create(event=event, name="Another", default_price=1)
        ptinvoicing_item_copy(event, source=plain, target=another)
        assert not ItemSettings.objects.filter(item=another).exists()


@pytest.mark.django_db
def test_product_page_renders_the_invoicing_form(logged_in_client, event, item):
    with scopes_disabled():
        event.settings.ptinvoicing_provider = "moloni"
    body = logged_in_client.get(
        f"/control/event/{event.organizer.slug}/{event.slug}/items/{item.pk}/"
    ).content.decode()
    assert "Invoicing (PT)" in body
    assert 'name="ptinvoicing-reference"' in body
    assert 'name="ptinvoicing-moloni_type"' in body


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


@pytest.fixture
def free_paid_order(paid_order):
    with scopes_disabled():
        paid_order.total = Decimal("0.00")
        paid_order.save(update_fields=["total"])
    return paid_order


@pytest.mark.django_db
def test_issue_view_refuses_a_free_order(logged_in_client, event, free_paid_order):
    with mock.patch("pretix_ptinvoicing.views.issue_invoice") as task:
        response = logged_in_client.post(
            ISSUE_URL.format(event.organizer.slug, event.slug, free_paid_order.code),
            follow=True,
        )
    task.apply_async.assert_not_called()
    assert "there is nothing to invoice" in response.content.decode()


@pytest.mark.django_db
def test_order_page_panel_says_a_free_order_has_nothing_to_invoice(
    logged_in_client, plugin_enabled_event, free_paid_order
):
    body = _order_page(logged_in_client, plugin_enabled_event, free_paid_order)
    assert "there is nothing to invoice" in body
    assert "Issue invoice now" not in body
