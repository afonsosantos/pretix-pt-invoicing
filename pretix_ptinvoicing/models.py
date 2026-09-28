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

    order = models.ForeignKey(
        "pretixbase.Order",
        related_name="ptinvoicing_invoices",
        on_delete=models.CASCADE,
    )
    provider = models.CharField(max_length=32)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
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

    @property
    def provider_label(self):
        from .providers import PROVIDERS

        cls = PROVIDERS.get(self.provider)
        return cls.verbose_name if cls else self.provider
