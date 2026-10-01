import logging
import time

import requests
from django.utils.translation import gettext_lazy as _

from ..base import ProviderError, ProviderUnreachable

logger = logging.getLogger(__name__)

BASE_URL = "https://api.moloni.pt/v1"
AUTHORIZE_URL = "https://www.moloni.pt/ac/root/oauth/"

# Moloni's access token lasts an hour; refresh a little early so a long task can't be
# caught mid-call by an expiry.
EXPIRY_MARGIN = 120


class MoloniAPIError(ProviderError):
    pass


class MoloniClient:
    """
    Thin wrapper over Moloni's REST API (https://api.moloni.pt/v1).

    OAuth: a 1-hour access token obtained with the authorization-code grant (the "Connect
    to Moloni" button) or the password grant, renewed with a 14-day refresh token, and
    passed as a **GET parameter** on every call. `on_token` is called
    whenever a new pair is obtained so the caller can persist it — otherwise every issuance
    would burn a fresh password grant.
    """

    def __init__(
        self,
        client_id,
        client_secret,
        username=None,
        password=None,
        access_token=None,
        refresh_token=None,
        expires_at=0,
        on_token=None,
        reload_tokens=None,
        timeout=20,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at or 0
        self.on_token = on_token
        # Returns the stored (access, refresh, expires_at) as they are *now* — see
        # adopt_rotated().
        self.reload_tokens = reload_tokens
        self.timeout = timeout

    def adopt_rotated(self):
        """
        Moloni rotates the refresh token on every refresh, so when two workers (or the
        daily keepalive and an issuance) refresh at once, the loser's token has just been
        spent. Before calling that a dead connection, pick up the pair the winner stored.
        True if there was a newer one.
        """
        if not self.reload_tokens:
            return False
        access, refresh, expires_at = self.reload_tokens()
        if not refresh or refresh == self.refresh_token:
            return False
        self.access_token, self.refresh_token = access, refresh
        self.expires_at = expires_at or 0
        return True

    def _grant(self, **params):
        params.update(
            {"client_id": self.client_id, "client_secret": self.client_secret}
        )
        try:
            response = requests.get(
                f"{BASE_URL}/grant/", params=params, timeout=self.timeout
            )
            data = response.json()
        except requests.RequestException as e:
            raise ProviderUnreachable(
                _("Could not reach Moloni: %(error)s") % {"error": e}
            ) from e
        except ValueError:
            raise MoloniAPIError(
                _("Invalid response from Moloni (HTTP %(status)s)")
                % {"status": response.status_code}
            )

        if "access_token" not in data:
            raise MoloniAPIError(
                str(
                    data.get("error_description")
                    or data.get("error")
                    or _("Moloni refused the credentials.")
                ),
                detail=data if isinstance(data, dict) else None,
            )

        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.expires_at = time.time() + int(data.get("expires_in", 3600))
        if self.on_token:
            self.on_token(self.access_token, self.refresh_token, self.expires_at)
        return self.access_token

    def token(self):
        if self.access_token and time.time() < self.expires_at - EXPIRY_MARGIN:
            return self.access_token
        if self.refresh_token:
            try:
                return self._grant(
                    grant_type="refresh_token", refresh_token=self.refresh_token
                )
            except MoloniAPIError:
                if self.adopt_rotated():
                    return self.token()
                # Refresh tokens expire after 14 days; fall back to the password grant
                # rather than failing an issuance over it.
                logger.info(
                    "moloni: refresh failed, falling back to the password grant"
                )
        if not (self.username and self.password):
            raise MoloniAPIError(
                _(
                    "Not connected to Moloni, or the connection has expired. Connect "
                    "again from the invoicing settings."
                )
            )
        return self._grant(
            grant_type="password", username=self.username, password=self.password
        )

    def refresh(self):
        """Force a refresh-token grant: rotates the 14-day refresh token."""
        return self._grant(grant_type="refresh_token", refresh_token=self.refresh_token)

    def exchange_code(self, code, redirect_uri):
        """Authorization-code grant: the second half of the "Connect to Moloni" flow."""
        return self._grant(
            grant_type="authorization_code", code=code, redirect_uri=redirect_uri
        )

    def call(self, endpoint, payload=None):
        """POST to <endpoint>, e.g. "customers/getByVat". Returns the decoded body."""
        url = f"{BASE_URL}/{endpoint}/"
        try:
            response = requests.post(
                url,
                params={
                    "access_token": self.token(),
                    "json": "true",
                    # Validation errors as {code, description} instead of bare codes.
                    "human_errors": "true",
                },
                json=payload or {},
                timeout=self.timeout,
            )
            data = response.json()
        except requests.RequestException as e:
            raise ProviderUnreachable(
                _("Could not reach Moloni: %(error)s") % {"error": e}
            ) from e
        except ValueError:
            raise MoloniAPIError(
                _("Invalid response from Moloni (HTTP %(status)s)")
                % {"status": response.status_code}
            )

        # Moloni reports business errors in the body, not always via the status code.
        if isinstance(data, dict) and (data.get("error") or data.get("errors")):
            errors = data.get("errors") or {}
            raise MoloniAPIError(
                str(
                    data.get("error_description")
                    or data.get("error")
                    or _("Moloni rejected the request.")
                ),
                detail=errors if isinstance(errors, dict) else {"error": str(errors)},
                http_status=response.status_code,
            )
        validation = _validation_errors(endpoint, data)
        if validation:
            logger.info("moloni: %s rejected: %s", endpoint, data)
            raise MoloniAPIError(
                _("Moloni rejected the request."),
                detail=validation,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise MoloniAPIError(
                _("Moloni returned HTTP %(status)s") % {"status": response.status_code},
                http_status=response.status_code,
            )
        return data


def _validation_errors(endpoint, data):
    """
    Moloni's validation errors, as {field: message}, or None.

    They come back with HTTP 200 as a *list* — ["1 name", "8 vat"], or with human_errors
    [{"code": "1 name", "description": "..."}]. Only reads (get*) legitimately return a
    list, so from a write (insert, update, ...) *any* list is an error, whatever shape
    its entries take — a real invoiceReceipts/insert rejection didn't match either form.
    From a read, only the two known forms count: a getAll's rows must not be mistaken
    for errors (taxExemptions/getAll rows carry code and description, plus more keys).
    """
    if not isinstance(data, list) or not data:
        return None
    is_read = endpoint.rsplit("/", 1)[-1].startswith("get")
    known = all(isinstance(e, str) for e in data) or all(
        isinstance(e, dict) and set(e) == {"code", "description"} for e in data
    )
    if is_read and not known:
        return None

    detail = {}
    for index, entry in enumerate(data):
        # A document's per-line errors come nested, one list per line:
        # [[[{"code": "2 value 0 0", ...}]], ...] — label them "#<line> <field>".
        nested = isinstance(entry, list)
        for leaf in _leaves(entry):
            if isinstance(leaf, dict) and "code" in leaf:
                code, message = leaf["code"], leaf.get("description") or leaf["code"]
            elif isinstance(leaf, str):
                code, message = leaf, leaf
            else:
                # Unknown shape: show it raw rather than hide what Moloni said.
                detail[f"error {index}"] = str(leaf)
                continue
            parts = str(code).split()  # "2 language_id 1 0" → field "language_id"
            field = parts[1] if len(parts) > 1 else str(code)
            detail[f"#{index + 1} {field}" if nested else field] = str(message)
    return detail


def _leaves(entry):
    if isinstance(entry, list):
        for item in entry:
            yield from _leaves(item)
    else:
        yield entry
