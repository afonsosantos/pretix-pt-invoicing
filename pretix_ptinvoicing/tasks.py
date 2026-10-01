import logging

from django.db import transaction
from django.utils.translation import gettext as _
from django_scopes import scopes_disabled
from pretix.base.i18n import language
from pretix.base.models import Order
from pretix.celery_app import app

from .mail import send_credit_note_email, send_invoice_email
from .models import IssuedInvoice
from .providers import ProviderError, get_provider

logger = logging.getLogger(__name__)


def _load(order_pk):
    try:
        return Order.objects.select_related("event").get(pk=order_pk)
    except Order.DoesNotExist:
        logger.warning(
            "ptinvoicing: order %s no longer exists, skipping task", order_pk
        )
        return None


def _not_configured(row, provider):
    # A provider is selected but can't issue (half set up, or Moloni's connection
    # expired). Recorded rather than skipped, so the order shows up as needing a retry
    # instead of silently never getting its document.
    if row.status == IssuedInvoice.STATUS_SUCCESS:
        return
    row.status = IssuedInvoice.STATUS_ERROR
    with language(provider.event.settings.locale):
        row.error_message = _(
            "Not issued: %(provider)s is not fully configured, or its connection has "
            "expired. Fix it in the invoicing settings, then retry."
        ) % {"provider": provider.verbose_name}
    row.error_detail = None
    row.save(update_fields=["status", "error_message", "error_detail", "modified"])


def _attempt(task, row, provider, order, call):
    """
    One issuance attempt against `row`, shared by invoices and credit notes:
    pending → success/error. Returns the row on success, else None.

    A ProviderError is terminal (retrying a rejection without a fix won't help — the
    Control panel's Retry is the way back in). Anything else is transient, and only
    retried when the provider dedupes on identifier_id: otherwise a call that timed out
    *after* the document was created would issue it twice.
    """
    # Claimed under a row lock: a duplicate signal, a double click or an acks_late
    # redelivery must not run a second attempt while one is in flight.
    with transaction.atomic():
        row = IssuedInvoice.objects.select_for_update().get(pk=row.pk)
        if row.status == IssuedInvoice.STATUS_SUCCESS or row.in_flight:
            return None
        row.attempts += 1
        row.status = IssuedInvoice.STATUS_PENDING
        row.error_message = None
        row.error_detail = None
        row.save(
            update_fields=[
                "attempts",
                "status",
                "error_message",
                "error_detail",
                "modified",
            ]
        )

    try:
        document = call()
    except ProviderError as e:
        row.status = IssuedInvoice.STATUS_ERROR
        # Stored in the event's language: it's read in the Control panel, not here.
        with language(provider.event.settings.locale):
            row.error_message = str(e.message)
        row.error_detail = e.detail or None
        row.save(update_fields=["status", "error_message", "error_detail", "modified"])
        logger.warning(
            "ptinvoicing: %s rejected %s for %s: %s",
            provider.identifier,
            row.kind,
            order.code,
            e.as_text(),
        )
        return None
    except Exception as e:  # network/infra failure
        retry = (
            provider.deduplicates_issuance and task.request.retries < task.max_retries
        )
        with language(provider.event.settings.locale):
            params = {"provider": provider.verbose_name, "error": str(e)}
            if retry:
                message = _("%(error)s — retrying automatically in a few minutes.")
            elif provider.deduplicates_issuance:
                message = _("%(error)s — gave up after several attempts.")
            else:
                message = _(
                    "%(error)s — the document may or may not have been created. Check "
                    "in %(provider)s before retrying: if it was, retrying issues it "
                    "twice."
                )
            row.error_message = message % params
        row.status = IssuedInvoice.STATUS_ERROR
        row.save(update_fields=["status", "error_message", "modified"])
        logger.exception(
            "ptinvoicing: unexpected failure issuing %s for %s", row.kind, order.code
        )
        if retry:
            raise task.retry(exc=e)
        return None

    row.status = IssuedInvoice.STATUS_SUCCESS
    row.document_id = document.document_id
    row.document_number = document.number
    row.document_link = document.link
    row.permanent_url = document.permanent_url
    row.error_message = None
    row.error_detail = None
    row.save()
    logger.info(
        "ptinvoicing: %s issued for %s via %s (doc %s)",
        row.kind,
        order.code,
        provider.identifier,
        row.document_id,
    )
    return row


def _email(row, send):
    row.email_sent = send()
    row.save(update_fields=["email_sent"])


@app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
)
def issue_invoice(self, order_pk, event_pk=None):
    # No request/organizer here (Celery task), hence scopes_disabled()
    with scopes_disabled():
        order = _load(order_pk)
        if order is None:
            return

        if order.status != Order.STATUS_PAID:
            # An invoice-receipt states the money was received, so never issue for an
            # order that isn't paid. order_paid guarantees it once the payment has
            # committed (signals.py enqueues on commit); the admin's button does not.
            logger.info(
                "ptinvoicing: order %s is not paid (status %s), skipping",
                order.code,
                order.status,
            )
            return

        event = order.event
        provider = get_provider(event)
        if provider is None:
            # "No invoicing" picked: nothing to do, deliberately.
            return

        # Each successful credit note closes a cycle: an order paid again after its
        # invoice was credited (refund, then a new payment) gets a new invoice.
        cycle, invoiced = IssuedInvoice.current_cycle(order, provider.identifier)
        if invoiced:
            # This cycle already has its invoice — whatever key it was issued under
            # (keys for very long event slugs changed format once).
            return
        identifier_id = IssuedInvoice.build_identifier_id(event, order, cycle)

        invoice, _created = IssuedInvoice.objects.get_or_create(
            order=order,
            provider=provider.identifier,
            identifier_id=identifier_id,
            defaults={"status": IssuedInvoice.STATUS_PENDING},
        )
        if not provider.is_configured:
            _not_configured(invoice, provider)
            return

        invoice = _attempt(
            self, invoice, provider, order, lambda: provider.issue(order, identifier_id)
        )
        if invoice and event.settings.get(
            "ptinvoicing_email_invoice", as_type=bool, default=False
        ):
            _email(invoice, lambda: send_invoice_email(order, provider, invoice))


@app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
)
def issue_credit_note(self, order_pk, event_pk=None):
    """
    Issue a credit note reversing the order's latest uncredited invoice.

    Fired automatically once refunds bring an order back to fully refunded
    (signals.ptinvoicing_refund_done), and by hand from the Control panel for anything
    else — a partial refund, say, since no provider here can credit less than a whole
    document.
    """
    with scopes_disabled():
        order = _load(order_pk)
        if order is None:
            return

        event = order.event
        provider = get_provider(event)
        if provider is None:
            return

        # The latest invoice not credited yet — earlier cycles are already closed.
        original = IssuedInvoice.uncredited_invoice(order, provider.identifier)
        if original is None:
            logger.info(
                "ptinvoicing: no uncredited invoice to credit for %s, skipping",
                order.code,
            )
            return

        identifier_id = IssuedInvoice.build_credit_identifier_id(original.identifier_id)

        credit_note, _created = IssuedInvoice.objects.get_or_create(
            order=order,
            provider=provider.identifier,
            identifier_id=identifier_id,
            defaults={
                "status": IssuedInvoice.STATUS_PENDING,
                "kind": IssuedInvoice.KIND_CREDIT_NOTE,
                "credits": original,
            },
        )
        if not provider.is_configured:
            _not_configured(credit_note, provider)
            return

        credit_note = _attempt(
            self,
            credit_note,
            provider,
            order,
            lambda: provider.credit(order, original.document_id, identifier_id),
        )
        if credit_note and event.settings.get(
            "ptinvoicing_email_credit_note", as_type=bool, default=False
        ):
            _email(
                credit_note,
                lambda: send_credit_note_email(order, provider, credit_note),
            )
