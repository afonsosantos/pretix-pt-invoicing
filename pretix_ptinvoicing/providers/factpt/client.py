from urllib.parse import quote

import requests
from django.utils.translation import gettext_lazy as _

from ..base import ProviderError, ProviderUnreachable

API_VERSION = "1.0.0"


class FactptAPIError(ProviderError):
    # `detail` (inherited) holds AppResponse.errors as returned by the API.
    pass


class FactptClient:
    def __init__(self, token, sandbox=False, timeout=20):
        self.token = token
        self.timeout = timeout
        # Sandbox is plain HTTP per Fact.pt's docs, unlike production.
        self.base_url = (
            "http://api.sandbox.fact.pt" if sandbox else "https://api.fact.pt"
        )

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "x-auth-token": self.token,
            "api-version": API_VERSION,
        }

    def _request(self, method, path, payload=None):
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, json=payload, headers=self._headers(), timeout=self.timeout
            )
        except requests.RequestException as e:
            # Not a FactptAPIError: a timeout is transient and may have landed, so the
            # task must be free to retry it (the identifierId guards the retry).
            raise ProviderUnreachable(
                _("Could not reach Fact.pt: %(error)s") % {"error": e}
            ) from e

        try:
            data = response.json()
        except ValueError:
            raise FactptAPIError(
                _("Invalid response from Fact.pt (HTTP %(status)s)")
                % {"status": response.status_code},
                http_status=response.status_code,
            )

        app_response = data.get("AppResponse") or {}
        app_status = data.get("AppStatusCode")

        if response.status_code >= 400 or (
            app_status is not None and app_status != 200
        ):
            message = (
                app_response.get("message")
                or data.get("AppStatusMsg")
                or str(_("Unknown error from Fact.pt"))
            )
            raise FactptAPIError(
                message,
                detail=app_response.get("errors"),
                http_status=response.status_code,
            )

        return app_response

    def create_invoice_receipt(self, payload):
        return self._request("POST", "/documents/invoicereceipt", payload)

    def create_credit_note(self, document_id, payload):
        return self._request("POST", f"/documents/{document_id}/credit", payload)

    def get_document(self, document_id):
        return self._request("GET", f"/documents/{document_id}")

    def list_taxes(self):
        return self._request("GET", "/taxes").get("data") or []

    def search_clients(self, query):
        return (
            self._request("GET", f"/clients?search={quote(str(query))}").get("data")
            or []
        )

    def download_document(self, document_id):
        url = f"{self.base_url}/documents/{document_id}/download"
        try:
            response = requests.get(url, headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as e:
            raise FactptAPIError(
                _("Could not download the document from Fact.pt: %(error)s")
                % {"error": e}
            )
        return response.content
