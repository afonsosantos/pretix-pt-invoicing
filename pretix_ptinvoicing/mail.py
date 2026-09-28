import logging

from django.core.files.base import ContentFile
from django.utils.translation import gettext_lazy as _
from i18nfield.strings import LazyI18nString
from pretix.base.models import CachedFile
from pretix.base.services.mail import mail

logger = logging.getLogger(__name__)


def send_invoice_email(order, provider, invoice):
    """
    E-mail the issued document to the buyer, as a PDF attachment.

    Named mail.py, not email.py, like pretix's own services: a module called email.py inside
    the package shadows the stdlib email package for anything run with this directory on
    sys.path — which is exactly what the Makefile's translate target does.

    pretix's own order e-mails can't do this: they attach `order.invoices`, i.e. pretix's
    own invoice records, and the provider's document isn't one. Failures here are logged
    and swallowed — the document is already issued, and a mail problem must not flip a
    successful issuance into an error or trigger the task's retry.
    """
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
        filename=f"{order.code}.pdf",
        type="application/pdf",
        web_download=False,
    )
    cached.file.save(f"{order.code}.pdf", ContentFile(pdf_bytes))

    try:
        mail(
            order.email,
            _("Your invoice for order %(code)s") % {"code": order.code},
            LazyI18nString.from_gettext(
                _(
                    "Hello,\n\n"
                    "your invoice for order {code} is attached to this e-mail.\n\n"
                    "Best regards,\n"
                    "Your {event} team"
                )
            ),
            {"code": order.code, "event": str(order.event.name)},
            event=order.event,
            order=order,
            locale=order.locale,
            attach_cached_files=[cached],
        )
    except Exception:
        logger.exception("ptinvoicing: could not e-mail the invoice for %s", order.code)
