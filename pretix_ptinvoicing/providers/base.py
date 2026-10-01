from dataclasses import dataclass


class ProviderError(Exception):
    """
    A provider-side rejection of the request (bad NIF, missing tax mapping, bad token).

    Terminal by contract: tasks.py marks the issuance as failed and does *not* retry,
    since retrying an API-level rejection without a config/data fix won't help. Transient
    failures (network, timeouts) must surface as their own exception type, not this one.
    """

    def __init__(self, message, detail=None, http_status=None):
        super().__init__(message)
        self.message = message
        # Raw per-field errors as returned by the provider, shown in the control panel.
        self.detail = detail or {}
        self.http_status = http_status

    def as_text(self):
        if self.detail:
            return "; ".join(f"{field}: {msg}" for field, msg in self.detail.items())
        return self.message


@dataclass
class IssuedDocument:
    """What every provider returns from issue(): the bits worth storing."""

    document_id: str = None
    link: str = None
    permanent_url: str = None
    # The number people read, e.g. "FR M2026/20" — document_id is the API's internal key.
    number: str = None


class InvoiceProvider:
    identifier = ""  # settings value + IssuedInvoice.provider, e.g. "factpt"
    verbose_name = ""
    # A pretix SettingsForm holding this provider's credentials/defaults. Its field names
    # must be namespaced by hand (e.g. factpt_token) — pretix stores event settings in one
    # flat namespace shared with core and every other plugin.
    settings_form_class = None
    # Optional template rendered under the provider's fieldset on the settings page, with
    # the provider instance as `provider` — for anything that isn't a form field (Moloni's
    # "Connect" button and connection state).
    settings_template = None
    # Static path of the logo on the settings page's provider card; None shows the name.
    logo = None
    # lookups() fields whose value changes what lookups() returns (Moloni's company scopes
    # its document sets, taxes and payment methods) — picking one re-runs the lookup.
    lookup_triggers = ()

    # True when the provider itself refuses a second document for the same identifier_id
    # (Fact.pt does, via document.identifierId). That is what makes it safe for tasks.py to
    # retry after a network failure: if the first call actually landed, the retry bounces
    # instead of issuing twice.
    #
    # Set False for a provider with no such field — Moloni's documents/insert has none, its
    # our_reference/your_reference are free text it does not dedupe on. tasks.py then stops
    # after one attempt and leaves the retry to a human via the Control panel, because a
    # timeout there is genuinely ambiguous: the document may or may not exist, and issuing a
    # second official invoice is worse than waiting for someone to look.
    deduplicates_issuance = True

    def __init__(self, event):
        self.event = event
        # The event's settings store. Writable: a provider whose credentials expire (an
        # OAuth access token, say) may cache the refreshed one here.
        self.settings = event.settings

    @property
    def is_configured(self):
        """False = plugin enabled but not set up yet; tasks.py then skips silently."""
        raise NotImplementedError

    def issue(self, order, identifier_id):
        """
        Issue the document for `order` and return an IssuedDocument.

        `identifier_id` is the caller's idempotency key — pass it through to whatever
        field the provider dedupes on, so a duplicate call can't double-issue.
        Raise ProviderError for a rejection; let anything transient propagate as-is.
        """
        raise NotImplementedError

    def credit(self, order, document_id, identifier_id):
        """
        Issue a credit note reversing `document_id` (the provider's id for the invoice
        previously returned by issue()) and return an IssuedDocument.

        `identifier_id` is this credit note's own idempotency key — same contract as
        issue(). Every provider implemented so far only allows crediting a document's
        *full* value, so there is no partial-refund shape here; a partial refund is the
        caller's problem to reconcile some other way.
        Raise ProviderError for a rejection (already credited, document not found); let
        anything transient propagate as-is.
        """
        raise NotImplementedError

    def document_number(self, document_id):
        """
        The document's human-readable number, built only from what the provider's API
        returns (no mapping tables), or None. Called right after issuance, when the document already
        exists — so it must never raise: a lookup failure has to leave a successful
        issuance successful. Optional.
        """
        return

    def download(self, document_id):
        """Return the document's PDF bytes, or raise ProviderError."""
        raise NotImplementedError

    def lookups(self, data):
        """
        Live options for the settings page, as {field_name: [{"id", "label"}, ...]}.

        `data` is the settings form's posted values with the form prefix stripped — i.e.
        whatever the admin has currently typed, not necessarily what's saved. Optional.
        """
        return {}

    def hidden_settings_fields(self):
        """
        Settings form fields to leave off the settings page right now (Moloni's password
        while connected via OAuth). Dropped from the form, not just hidden, so saving
        leaves their stored values alone. Optional.
        """
        return ()

    def keepalive(self):
        """
        Called about once a day for every event using this provider, e.g. to rotate an
        expiring refresh token. Return True if the connection turned out to be dead, and
        the organizer is e-mailed to reconnect. Optional.
        """
        return False
