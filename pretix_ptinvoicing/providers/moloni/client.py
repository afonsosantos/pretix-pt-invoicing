import logging
import time

import requests
from django.utils.translation import gettext_lazy as _

from ..base import ProviderError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.moloni.pt/v1"

# Moloni's access token lasts an hour; refresh a little early so a long task can't be
# caught mid-call by an expiry.
EXPIRY_MARGIN = 120


class MoloniAPIError(ProviderError):
    pass


class MoloniClient:
    """
    Thin wrapper over Moloni's REST API (https://api.moloni.pt/v1).

    Unlike Fact.pt's static token, Moloni is OAuth: a 1-hour access token obtained with the
    password grant and renewed with a 14-day refresh token, passed as a **GET parameter**
    on every call. `on_token` is called whenever a new pair is obtained so the caller can
    persist it — otherwise every issuance would burn a fresh password grant.
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
        self.timeout = timeout

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
            raise MoloniAPIError(_("Could not reach Moloni: %(error)s") % {"error": e})
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
                # Refresh tokens expire after 14 days; fall back to the password grant
                # rather than failing an issuance over it.
                logger.info(
                    "moloni: refresh failed, falling back to the password grant"
                )
        if not (self.username and self.password):
            raise MoloniAPIError(_("Moloni credentials are incomplete."))
        return self._grant(
            grant_type="password", username=self.username, password=self.password
        )

    def call(self, endpoint, payload=None):
        """POST to <endpoint>, e.g. "customers/getByVat". Returns the decoded body."""
        url = f"{BASE_URL}/{endpoint}/"
        try:
            response = requests.post(
                url,
                params={"access_token": self.token(), "json": "true"},
                json=payload or {},
                timeout=self.timeout,
            )
            data = response.json()
        except requests.RequestException as e:
            raise MoloniAPIError(_("Could not reach Moloni: %(error)s") % {"error": e})
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
        if response.status_code >= 400:
            raise MoloniAPIError(
                _("Moloni returned HTTP %(status)s") % {"status": response.status_code},
                http_status=response.status_code,
            )
        return data
