import logging

from django_scopes import scopes_disabled
from pretix.base.models import Order
from pretix.celery_app import app

from .client import FactptAPIError, FactptClient
from .models import FactptInvoice
from .payload import bare_tin, build_identifier_id, build_payload

logger = logging.getLogger(__name__)


@app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
)
def generate_factpt_invoice(self, order_pk, event_pk=None):
    # No request/organizer here (Celery task), hence scopes_disabled() —
    # same as pretix-eupago's webhook.
    with scopes_disabled():
        try:
            order = Order.objects.select_related("event").get(pk=order_pk)
        except Order.DoesNotExist:
            logger.warning("factpt: order %s no longer exists, skipping task", order_pk)
            return

        event = order.event
        settings = event.settings

        token = settings.get("factpt_token")
        if not token:
            logger.info(
                "factpt: plugin not configured for event %s, skipping", event.slug
            )
            return

        identifier_id = build_identifier_id(event, order)

        invoice, _created = FactptInvoice.objects.get_or_create(
            order=order,
            identifier_id=identifier_id,
            defaults={"status": FactptInvoice.STATUS_PENDING},
        )

        if invoice.status == FactptInvoice.STATUS_SUCCESS:
            return

        invoice.attempts += 1
        invoice.status = FactptInvoice.STATUS_PENDING
        invoice.error_message = None
        invoice.error_detail = None
        invoice.save(
            update_fields=["attempts", "status", "error_message", "error_detail"]
        )

        client = FactptClient(
            token=token,
            sandbox=settings.get("factpt_sandbox", as_type=bool, default=False),
        )

        # If this NIF already has an existing client on Fact.pt, reference it by id instead
        # of sending an inline block — sidesteps forceTin's one blind spot: it can upsert a
        # single matching client, but if the tin already matches *more than one* (duplicate
        # client records already in the account), Fact.pt refuses to guess and requires an
        # explicit id ("clientBlock: Multiple clients with same tin. Specify an ID.").
        client_id = None
        tin = bare_tin(order)
        if tin:
            try:
                matches = [
                    c for c in client.search_clients(tin) if str(c.get("tin")) == tin
                ]
            except FactptAPIError:
                matches = []
            if len(matches) == 1:
                client_id = matches[0].get("id")

        payload, _ = build_payload(order, settings, client_id=client_id)

        try:
            result = client.create_invoice_receipt(payload)
        except FactptAPIError as e:
            invoice.status = FactptInvoice.STATUS_ERROR
            invoice.error_message = e.as_text()
            invoice.error_detail = e.errors
            invoice.save(update_fields=["status", "error_message", "error_detail"])
            logger.warning(
                "factpt: error issuing invoice for %s: %s", order.code, e.as_text()
            )
            return
        except Exception as e:  # network/infra failure — worth retrying
            invoice.status = FactptInvoice.STATUS_ERROR
            invoice.error_message = str(e)
            invoice.save(update_fields=["status", "error_message"])
            logger.exception(
                "factpt: unexpected failure issuing invoice for %s", order.code
            )
            raise self.retry(exc=e)

        data = result.get("data") or {}
        invoice.status = FactptInvoice.STATUS_SUCCESS
        invoice.factpt_document_id = data.get("id")
        invoice.factpt_link = result.get("link")
        invoice.permanent_url = result.get("permanentUrl")
        invoice.error_message = None
        invoice.error_detail = None
        invoice.save()
        logger.info(
            "factpt: invoice-receipt issued for %s (doc %s)",
            order.code,
            invoice.factpt_document_id,
        )
