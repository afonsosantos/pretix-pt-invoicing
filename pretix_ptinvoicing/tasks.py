import logging

from django_scopes import scopes_disabled
from pretix.base.models import Order
from pretix.celery_app import app

from .mail import send_invoice_email
from .models import IssuedInvoice
from .providers import ProviderError, get_provider

logger = logging.getLogger(__name__)


@app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
)
def issue_invoice(self, order_pk, event_pk=None):
    # No request/organizer here (Celery task), hence scopes_disabled()
    with scopes_disabled():
        try:
            order = Order.objects.select_related("event").get(pk=order_pk)
        except Order.DoesNotExist:
            logger.warning(
                "ptinvoicing: order %s no longer exists, skipping task", order_pk
            )
            return

        if order.status != Order.STATUS_PAID:
            # order_paid guarantees this; the admin's manual "issue now" button does not.
            # An invoice-receipt states the money was received, so never issue for an
            # order that isn't paid.
            logger.info(
                "ptinvoicing: order %s is not paid (status %s), skipping",
                order.code,
                order.status,
            )
            return

        event = order.event
        provider = get_provider(event)
        if provider is None or not provider.is_configured:
            logger.info(
                "ptinvoicing: no configured provider for event %s, skipping", event.slug
            )
            return

        identifier_id = IssuedInvoice.build_identifier_id(event, order)

        invoice, _created = IssuedInvoice.objects.get_or_create(
            order=order,
            provider=provider.identifier,
            identifier_id=identifier_id,
            defaults={"status": IssuedInvoice.STATUS_PENDING},
        )

        if invoice.status == IssuedInvoice.STATUS_SUCCESS:
            return

        invoice.attempts += 1
        invoice.status = IssuedInvoice.STATUS_PENDING
        invoice.error_message = None
        invoice.error_detail = None
        invoice.save(
            update_fields=["attempts", "status", "error_message", "error_detail"]
        )

        try:
            document = provider.issue(order, identifier_id)
        except ProviderError as e:
            # Provider-side rejection: retrying without a config/data fix won't help, so
            # this is terminal — the control panel's Retry button is the way back in.
            invoice.status = IssuedInvoice.STATUS_ERROR
            invoice.error_message = e.as_text()
            invoice.error_detail = e.detail
            invoice.save(update_fields=["status", "error_message", "error_detail"])
            logger.warning(
                "ptinvoicing: %s rejected invoice for %s: %s",
                provider.identifier,
                order.code,
                e.as_text(),
            )
            return
        except Exception as e:  # network/infra failure
            invoice.status = IssuedInvoice.STATUS_ERROR
            invoice.error_message = str(e)
            invoice.save(update_fields=["status", "error_message"])
            logger.exception(
                "ptinvoicing: unexpected failure issuing invoice for %s", order.code
            )
            # Only retry when the provider would reject a duplicate. Without that, a call
            # that timed out *after* the document was created would be re-sent and issue a
            # second official invoice — see InvoiceProvider.deduplicates_issuance.
            if provider.deduplicates_issuance:
                raise self.retry(exc=e)
            return

        invoice.status = IssuedInvoice.STATUS_SUCCESS
        invoice.document_id = document.document_id
        invoice.document_link = document.link
        invoice.permanent_url = document.permanent_url
        invoice.error_message = None
        invoice.error_detail = None
        invoice.save()
        logger.info(
            "ptinvoicing: invoice issued for %s via %s (doc %s)",
            order.code,
            provider.identifier,
            invoice.document_id,
        )

        if event.settings.get("ptinvoicing_email_invoice", as_type=bool, default=False):
            send_invoice_email(order, provider, invoice)
