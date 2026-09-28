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


class InvoiceProvider:
    identifier = ""  # settings value + IssuedInvoice.provider, e.g. "factpt"
    verbose_name = ""
    # A pretix SettingsForm holding this provider's credentials/defaults. Its field names
    # must be namespaced by hand (e.g. factpt_token) — pretix stores event settings in one
    # flat namespace shared with core and every other plugin.
    settings_form_class = None

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
