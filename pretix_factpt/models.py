from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _


class FactptInvoice(models.Model):
    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"
    STATUS_CHOICES: ClassVar = [
        (STATUS_PENDING, _("Processing")),
        (STATUS_SUCCESS, _("Issued")),
        (STATUS_ERROR, _("Error")),
    ]

    order = models.ForeignKey(
        "pretixbase.Order", related_name="factpt_invoices", on_delete=models.CASCADE
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )

    # Idempotency key sent to Fact.pt as identifierId.
    identifier_id = models.CharField(max_length=50, unique=True)

    factpt_document_id = models.CharField(max_length=64, blank=True, null=True)
    factpt_link = models.CharField(max_length=255, blank=True, null=True)
    permanent_url = models.CharField(max_length=255, blank=True, null=True)

    error_message = models.TextField(blank=True, null=True)
    error_detail = models.JSONField(blank=True, null=True)

    attempts = models.PositiveIntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar = ["-created"]

    def __str__(self):
        return f"{self.order.code} — {self.get_status_display()}"
