import hashlib
from datetime import timedelta
from typing import ClassVar

from django.db import models
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _


class IssuedInvoice(models.Model):
    """One row per issuance *attempt* against an Order, for one provider."""

    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"
    STATUS_CHOICES: ClassVar = [
        (STATUS_PENDING, _("Processing")),
        (STATUS_SUCCESS, _("Issued")),
        (STATUS_ERROR, _("Error")),
    ]

    KIND_INVOICE = "invoice"
    KIND_CREDIT_NOTE = "credit_note"
    KIND_CHOICES: ClassVar = [
        (KIND_INVOICE, _("Invoice")),
        (KIND_CREDIT_NOTE, _("Credit note")),
    ]

    order = models.ForeignKey(
        "pretixbase.Order",
        related_name="ptinvoicing_invoices",
        on_delete=models.CASCADE,
    )
    provider = models.CharField(max_length=32)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_INVOICE)
    # Set only on a credit-note row: the invoice row it reverses. Every provider so far
    # (Fact.pt confirmed, Moloni by doc) only allows one credit note per document, so this
    # is effectively one-to-one, but a plain FK needs no extra constraint to say that — the
    # task that creates credit notes already refuses to make a second one.
    credits = models.ForeignKey(
        "self",
        related_name="credit_notes",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )

    # Idempotency key handed to the provider (Fact.pt: identifierId).
    identifier_id = models.CharField(max_length=50)

    document_id = models.CharField(max_length=64, blank=True, null=True)
    # What people read, e.g. "FR M2026/20"; document_id is only the API's key.
    document_number = models.CharField(max_length=64, blank=True, null=True)
    document_link = models.CharField(max_length=255, blank=True, null=True)
    permanent_url = models.CharField(max_length=255, blank=True, null=True)

    error_message = models.TextField(blank=True, null=True)
    error_detail = models.JSONField(blank=True, null=True)

    attempts = models.PositiveIntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    # A row stuck in "pending" this long is taken to belong to a dead worker, and may be
    # claimed again. Until then it's in flight, and running it twice could issue twice.
    STALE_AFTER = timedelta(minutes=10)

    # Whether the buyer was e-mailed the PDF: None = not attempted (e-mail off).
    email_sent = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering: ClassVar = ["-created"]
        unique_together: ClassVar = [("provider", "identifier_id")]

    def __str__(self):
        return f"{self.order.code} — {self.get_status_display()}"

    @staticmethod
    def _fit(key, suffix=""):
        """
        key + suffix within the field's 50 characters, without ever cutting off what
        tells keys apart: an over-long key keeps its head and swaps the tail for a hash
        of the whole. Keys that already fit are unchanged, so existing rows still match.
        """
        if len(key) + len(suffix) <= 50:
            return key + suffix
        digest = hashlib.sha1(key.encode()).hexdigest()[:10]
        return f"{key[: 50 - len(suffix) - 11]}-{digest}{suffix}"

    @staticmethod
    def build_identifier_id(event, order, cycle=0):
        # Stable per (event, order, cycle) so a retry reuses the same row and the
        # provider's own duplicate check (Fact.pt rejects a repeated identifierId) backs
        # it up. A cycle is one invoice + its credit note: an order paid again after a
        # credited refund needs a new invoice, hence a new key ("-r1", ...). Cycle 0 keeps
        # the original format, so rows issued before cycles existed still match.
        suffix = f"-r{cycle}" if cycle else ""
        return IssuedInvoice._fit(f"pretix-{event.slug}-{order.code}", suffix)

    @staticmethod
    def build_credit_identifier_id(invoice_identifier_id):
        # Derived from the invoice it credits: distinct from it under the same
        # unique_together(provider, identifier_id), one per cycle, and stable so a retry
        # reuses the credit note's row.
        return IssuedInvoice._fit(invoice_identifier_id, "-credit")

    @classmethod
    def current_cycle(cls, order, provider):
        """
        (cycle, invoiced): the order's current cycle — how many invoices were credited
        so far — and whether that cycle already has its invoice.
        """
        issued = cls.objects.filter(
            order=order, provider=provider, status=cls.STATUS_SUCCESS
        )
        cycle = issued.filter(kind=cls.KIND_CREDIT_NOTE).count()
        return cycle, issued.filter(kind=cls.KIND_INVOICE).count() > cycle

    @classmethod
    def uncredited_invoice(cls, order, provider):
        """The latest successful invoice not credited yet — what a credit note reverses."""
        return (
            cls.objects.filter(
                order=order,
                provider=provider,
                kind=cls.KIND_INVOICE,
                status=cls.STATUS_SUCCESS,
            )
            .exclude(credit_notes__status=cls.STATUS_SUCCESS)
            .order_by("-created")
            .first()
        )

    @property
    def in_flight(self):
        """Pending and recent: a worker is (probably) issuing it right now."""
        return (
            self.status == self.STATUS_PENDING
            and self.attempts > 0
            and self.modified > now() - self.STALE_AFTER
        )

    @property
    def error_items(self):
        """The provider's per-field errors as "field: message" lines."""
        items = [f"{k}: {v}" for k, v in (self.error_detail or {}).items()]
        # Rows from before the message and the detail were stored apart hold the joined
        # detail as their message; don't show it twice.
        return [] if "; ".join(items) == self.error_message else items

    @property
    def error_text(self):
        """Message and per-field errors on one line, for the overview table."""
        return "; ".join(filter(None, [self.error_message, *self.error_items]))

    @property
    def filename(self):
        suffix = "-credit" if self.kind == self.KIND_CREDIT_NOTE else ""
        return f"{self.order.code}{suffix}.pdf"

    @property
    def display_number(self):
        # Rows issued before numbers were stored (or whose lookup failed) show the id.
        return self.document_number or self.document_id

    @property
    def provider_label(self):
        from .providers import PROVIDERS

        cls = PROVIDERS.get(self.provider)
        return cls.verbose_name if cls else self.provider
