from typing import ClassVar

from django.db import models
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
    document_link = models.CharField(max_length=255, blank=True, null=True)
    permanent_url = models.CharField(max_length=255, blank=True, null=True)

    error_message = models.TextField(blank=True, null=True)
    error_detail = models.JSONField(blank=True, null=True)

    attempts = models.PositiveIntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar = ["-created"]
        unique_together: ClassVar = [("provider", "identifier_id")]

    def __str__(self):
        return f"{self.order.code} — {self.get_status_display()}"

    @staticmethod
    def build_identifier_id(event, order):
        # Stable per (event, order) so a retry reuses the same row and the provider's own
        # duplicate check (Fact.pt rejects a repeated identifierId) backs it up.
        return f"pretix-{event.slug}-{order.code}"[:50]

    @staticmethod
    def build_credit_identifier_id(event, order):
        # A distinct key from the invoice's own, so both rows can coexist under the same
        # unique_together(provider, identifier_id) and a retry reuses the credit note's row.
        return f"{IssuedInvoice.build_identifier_id(event, order)}-credit"[:50]

    @property
    def provider_label(self):
        from .providers import PROVIDERS

        cls = PROVIDERS.get(self.provider)
        return cls.verbose_name if cls else self.provider
