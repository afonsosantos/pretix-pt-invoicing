import requests
from django.utils.translation import gettext_lazy as _

API_VERSION = "1.0.0"


class FactptAPIError(Exception):
    # errors holds AppResponse.errors as returned by the API, for display in the control panel.
    def __init__(self, message, errors=None, http_status=None):
        super().__init__(message)
        self.message = message
        self.errors = errors or {}
        self.http_status = http_status

    def as_text(self):
        if self.errors:
            return "; ".join(f"{field}: {msg}" for field, msg in self.errors.items())
        return self.message


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
            raise FactptAPIError(_("Could not reach Fact.pt: %(error)s") % {"error": e})

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
                errors=app_response.get("errors"),
                http_status=response.status_code,
            )

        return app_response

    def create_invoice_receipt(self, payload):
        return self._request("POST", "/documents/invoicereceipt", payload)

    def get_document(self, document_id):
        return self._request("GET", f"/documents/{document_id}")

    def list_taxes(self):
        # Confirmed against a third-party client (digfish/php-factpt-cli) — the
        # /support/api?c=lists&s=taxes path this used to hit doesn't exist.
        return self._request("GET", "/taxes").get("data") or []

    def list_units(self):
        # Unconfirmed: no public client documents a units-listing endpoint. Guessed by
        # analogy with /taxes, /products, /clients, /documents (this API's other flat,
        # plural-noun list endpoints). Fails gracefully like any other lookup call.
        return self._request("GET", "/units").get("data") or []

    def download_document(self, document_id):
        url = f"{self.base_url}/documents/{document_id}/download"
        response = requests.get(url, headers=self._headers(), timeout=self.timeout)
        response.raise_for_status()
        return response.content
