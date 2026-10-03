import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from i18nfield.strings import LazyI18nString
from pretix.base.models import CachedFile
from pretix.base.services.mail import mail

logger = logging.getLogger(__name__)


def _send_document_email(order, provider, invoice, subject, text, context=None):
    """
    E-mail the document's PDF to the buyer; True if it was handed to pretix's mailer.

    Named mail.py, not email.py: email.py here would shadow stdlib email on sys.path,
    which is what `make translate` puts this directory on, breaking it.
    pretix's own order e-mails can't attach this: they only attach order.invoices.
    *Every* failure is logged and swallowed — the document is already issued, and a
    mail problem must neither flip it to error nor make the task retry issuance.
    """
    try:
        pdf_bytes = provider.download(invoice.document_id)
        cached = CachedFile.objects.create(
            filename=invoice.filename, type="application/pdf", web_download=False
        )
        cached.file.save(invoice.filename, ContentFile(pdf_bytes))
        mail(
            order.email,
            subject,
            text,
            {
                "code": order.code,
                "event": str(order.event.name),
                "number": invoice.display_number,
                **(context or {}),
            },
            event=order.event,
            order=order,
            locale=order.locale,
            attach_cached_files=[cached],
        )
    except Exception:
        logger.exception(
            "ptinvoicing: could not e-mail %s for %s", invoice.filename, order.code
        )
        return False
    return True


def send_invoice_email(order, provider, invoice):
    return _send_document_email(
        order,
        provider,
        invoice,
        subject=LazyI18nString.from_gettext(
            _("Your invoice-receipt {number} for order {code}")
        ),
        text=LazyI18nString.from_gettext(
            _(
                "Hello,\n\n"
                "your invoice-receipt {number} for order {code} is attached to this "
                "e-mail.\n\n"
                "Best regards,\n"
                "Your {event} team"
            )
        ),
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
    return _send_document_email(
        order,
        provider,
        credit_note,
        subject=LazyI18nString.from_gettext(
            _("Your credit note {number} for order {code}")
        ),
        text=LazyI18nString.from_gettext(
            _(
                "Hello,\n\n"
                "attached to this e-mail is credit note {number} for order {code}. It "
                "cancels invoice-receipt {invoice}.\n\n"
                "Best regards,\n"
                "Your {event} team"
            )
        ),
        context={"invoice": credit_note.credits.display_number},
    )
