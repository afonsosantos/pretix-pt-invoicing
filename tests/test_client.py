import pytest
import responses

from pretix_factpt.client import FactptAPIError, FactptClient


@responses.activate
def test_create_invoice_receipt_success():
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={
            "AppStatusCode": 200,
            "AppStatusMsg": "OK",
            "AppResponse": {
                "data": {"id": "12345"},
                "link": "https://fact.pt/doc/12345",
                "permanentUrl": "https://fact.pt/permanent/12345",
            },
        },
        status=200,
    )

    client = FactptClient(token="abc")
    result = client.create_invoice_receipt({"document": {}})

    assert result["data"]["id"] == "12345"
    assert result["link"] == "https://fact.pt/doc/12345"


@responses.activate
def test_create_invoice_receipt_uses_sandbox_url():
    responses.add(
        responses.POST,
        "http://api.sandbox.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 200, "AppResponse": {}},
        status=200,
    )

    client = FactptClient(token="abc", sandbox=True)
    client.create_invoice_receipt({"document": {}})

    assert len(responses.calls) == 1


@responses.activate
def test_create_invoice_receipt_raises_on_app_error():
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={
            "AppStatusCode": 400,
            "AppStatusMsg": "Validation error",
            "AppResponse": {"message": "Invalid VAT", "errors": {"tin": "Invalid"}},
        },
        status=200,
    )

    client = FactptClient(token="abc")
    with pytest.raises(FactptAPIError) as excinfo:
        client.create_invoice_receipt({"document": {}})

    assert excinfo.value.errors == {"tin": "Invalid"}
    assert excinfo.value.as_text() == "tin: Invalid"


@responses.activate
def test_create_invoice_receipt_raises_on_http_error_status():
    responses.add(
        responses.POST,
        "https://api.fact.pt/documents/invoicereceipt",
        json={"AppStatusCode": 500, "AppResponse": {}},
        status=500,
    )

    client = FactptClient(token="abc")
    with pytest.raises(FactptAPIError):
        client.create_invoice_receipt({"document": {}})


@responses.activate
def test_download_document_returns_bytes():
    responses.add(
        responses.GET,
        "https://api.fact.pt/documents/12345/download",
        body=b"%PDF-1.4 fake",
        status=200,
        content_type="application/pdf",
    )

    client = FactptClient(token="abc")
    assert client.download_document("12345") == b"%PDF-1.4 fake"
