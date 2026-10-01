import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from i18nfield.strings import LazyI18nString
from pretix.base.models import CachedFile
from pretix.base.services.mail import mail

logger = logging.getLogger(__name__)


def _send_document_email(order, provider, invoice, subject, text, filename):
    # Named mail.py, not email.py: email.py here would shadow stdlib email on sys.path,
    # which is what `make translate` puts this directory on, breaking it.
    # pretix's own order e-mails can't attach this: they only attach order.invoices.
    # Failures are logged and swallowed — the document is already issued.

    try:
        pdf_bytes = provider.download(invoice.document_id)
    except Exception:
        logger.exception(
            "ptinvoicing: could not download %s for %s to e-mail it",
            invoice.document_id,
            order.code,
        )
        return

    cached = CachedFile.objects.create(
        filename=filename, type="application/pdf", web_download=False
    )
    cached.file.save(filename, ContentFile(pdf_bytes))

    try:
        mail(
            order.email,
            subject,
            text,
            {"code": order.code, "event": str(order.event.name)},
            event=order.event,
            order=order,
            locale=order.locale,
            attach_cached_files=[cached],
        )
    except Exception:
        logger.exception(
            "ptinvoicing: could not e-mail %s for %s", filename, order.code
        )


def send_invoice_email(order, provider, invoice):
    _send_document_email(
        order,
        provider,
        invoice,
        subject=LazyI18nString.from_gettext(_("Your invoice for order {code}")),
        text=LazyI18nString.from_gettext(
            _(
                "Hello,\n\n"
                "your invoice for order {code} is attached to this e-mail.\n\n"
                "Best regards,\n"
                "Your {event} team"
            )
        ),
        filename=f"{order.code}.pdf",
    )


def send_connection_expired_email(event, provider):
    """Tell the organizer a provider connection died; issuance stops until reconnected."""
    recipient = event.settings.get("contact_mail")
    if not recipient:
        logger.warning(
            "ptinvoicing: %s connection expired for %s, and no contact e-mail is set",
            provider.verbose_name,
            event,
        )
        return
    settings_url = settings.SITE_URL + reverse(
        "plugins:pretix_ptinvoicing:settings",
        kwargs={"organizer": event.organizer.slug, "event": event.slug},
    )
    try:
        mail(
            recipient,
            LazyI18nString.from_gettext(
                _("Invoicing for {event}: the {provider} connection has expired")
            ),
            LazyI18nString.from_gettext(
                _(
                    "Hello,\n\n"
                    "the connection between {event} and {provider} has expired, so no "
                    "invoices will be issued until you connect again here:\n\n"
                    "{url}\n"
                )
            ),
            {
                "event": str(event.name),
                "provider": provider.verbose_name,
                "url": settings_url,
            },
            event=event,
        )
    except Exception:
        logger.exception(
            "ptinvoicing: could not e-mail the expiry notice for %s", event
        )


def send_credit_note_email(order, provider, credit_note):
    _send_document_email(
        order,
        provider,
        credit_note,
        subject=LazyI18nString.from_gettext(_("Your credit note for order {code}")),
        text=LazyI18nString.from_gettext(
            _(
                "Hello,\n\n"
                "a credit note for order {code} is attached to this e-mail.\n\n"
                "Best regards,\n"
                "Your {event} team"
            )
        ),
        filename=f"{order.code}-credit.pdf",
    )
