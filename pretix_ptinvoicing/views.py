from django import forms
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, View
from pretix.base.models import Order
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.event import EventSettingsViewMixin
from pretix.presale.views import EventViewMixin
from pretix.presale.views.order import OrderDetailMixin

from .forms import EmailSettingsForm, ProviderSelectForm
from .models import IssuedInvoice
from .providers import PROVIDERS, ProviderError, get_provider
from .tasks import issue_credit_note, issue_invoice


class IndexView(EventPermissionRequiredMixin, ListView):
    model = IssuedInvoice
    template_name = "pretix_ptinvoicing/control/index.html"
    context_object_name = "invoices"
    permission = "can_view_orders"
    paginate_by = 50

    def get_queryset(self):
        qs = IssuedInvoice.objects.filter(
            order__event=self.request.event
        ).select_related("order")
        status = self.request.GET.get("status")
        if status in dict(IssuedInvoice.STATUS_CHOICES):
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["status_choices"] = IssuedInvoice.STATUS_CHOICES
        return ctx


class IssueView(EventPermissionRequiredMixin, View):
    """
    Enqueue issuance for one order, by order code.

    Keyed on the order rather than on an existing IssuedInvoice row so it covers both
    cases with one endpoint: retrying a row that failed, and issuing for an order that
    has no row at all (one paid before the plugin was configured, or paid manually).
    The task itself refuses to re-issue an order that already succeeded.
    """

    permission = "can_change_orders"

    def post(self, request, *args, **kwargs):
        order = get_object_or_404(Order, code=kwargs["code"], event=request.event)
        issue_invoice.apply_async(
            kwargs={"order_pk": order.pk, "event_pk": request.event.pk}
        )
        messages.success(request, _("Issuance request sent to the provider."))
        return redirect(self.redirect_url(request))

    def redirect_url(self, request):
        next_url = request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts=None, require_https=request.is_secure()
        ):
            return next_url
        return reverse(
            "plugins:pretix_ptinvoicing:index",
            kwargs={
                "event": request.event.slug,
                "organizer": request.organizer.slug,
            },
        )


class IssueCreditNoteView(IssueView):
    """
    Enqueue a credit note for one order's already-issued invoice.

    Shares IssueView's redirect handling; only the task differs. Deliberately manual —
    unlike issue_invoice, nothing triggers this automatically: there's no reliable pretix
    signal for "this refund is worth a full credit note", so an admin decides each time.
    """

    def post(self, request, *args, **kwargs):
        order = get_object_or_404(Order, code=kwargs["code"], event=request.event)
        issue_credit_note.apply_async(
            kwargs={"order_pk": order.pk, "event_pk": request.event.pk}
        )
        messages.success(request, _("Credit note request sent to the provider."))
        return redirect(self.redirect_url(request))


class DownloadView(EventPermissionRequiredMixin, View):
    permission = "can_view_orders"

    def get(self, request, *args, **kwargs):
        # Proxied rather than linked directly so the provider's API token never reaches
        # the browser.
        invoice = get_object_or_404(
            IssuedInvoice,
            pk=kwargs["pk"],
            order__event=request.event,
            status=IssuedInvoice.STATUS_SUCCESS,
        )
        provider = get_provider(request.event)
        if provider is None or provider.identifier != invoice.provider:
            raise Http404()
        try:
            pdf_bytes = provider.download(invoice.document_id)
        except ProviderError:
            raise Http404()

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{invoice.order.code}.pdf"'
        return response


class OrderInvoiceDownloadView(EventViewMixin, OrderDetailMixin, View):
    """
    The same PDF as DownloadView, for the buyer instead of the organizer.

    Authenticated by the order secret in the URL (OrderDetailMixin), the way pretix's own
    invoice download is — there is no logged-in user here.
    """

    def get(self, request, *args, **kwargs):
        if self.order is None or self.order is False:
            raise Http404()

        invoice = get_object_or_404(
            IssuedInvoice,
            pk=kwargs["pk"],
            order=self.order,
            status=IssuedInvoice.STATUS_SUCCESS,
        )
        provider = get_provider(request.event)
        if provider is None or provider.identifier != invoice.provider:
            raise Http404()
        try:
            pdf_bytes = provider.download(invoice.document_id)
        except ProviderError:
            raise Http404()

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{invoice.order.code}.pdf"'
        return response


class SettingsLookupsView(EventPermissionRequiredMixin, View):
    # Powers the live dropdowns on the settings page: the browser posts whatever the admin
    # has currently typed (not necessarily saved), so options show up before hitting Save.
    permission = "can_change_event_settings"

    def post(self, request, *args, **kwargs):
        cls = PROVIDERS.get(request.POST.get("provider", ""))
        if cls is None:
            return JsonResponse(
                {"error": str(_("Unknown invoicing provider."))}, status=400
            )
        try:
            fields = cls(request.event).lookups(request.POST)
        except ProviderError as e:
            return JsonResponse({"error": e.as_text()}, status=400)
        return JsonResponse({"fields": fields})


def _no_autofill(form):
    """
    Keep browsers and password managers out of the settings inputs: they'd fill the
    admin's pretix login into a provider's username/password or token. Browsers ignore
    autocomplete="off" on password fields, hence "new-password" there; the data-*
    attributes are the opt-outs Bitwarden, 1Password, LastPass and Dashlane each honour.
    """
    for field in form.fields.values():
        widget = field.widget
        widget.attrs.update(
            {
                "autocomplete": "new-password"
                if isinstance(widget, forms.PasswordInput)
                else "off",
                "data-bwignore": "true",
                "data-1p-ignore": "true",
                "data-lpignore": "true",
                "data-form-type": "other",
            }
        )


class SettingsView(EventSettingsViewMixin, EventPermissionRequiredMixin, View):
    permission = "can_change_event_settings"
    template_name = "pretix_ptinvoicing/control/settings.html"

    def get(self, request, *args, **kwargs):
        return self.render(
            request,
            ProviderSelectForm(obj=request.event),
            EmailSettingsForm(obj=request.event),
            self.provider_forms(request),
        )

    def post(self, request, *args, **kwargs):
        select_form = ProviderSelectForm(obj=request.event, data=request.POST)
        email_form = EmailSettingsForm(obj=request.event, data=request.POST)
        selected = request.POST.get("ptinvoicing_provider", "")
        # Only the selected provider's form is bound: the others keep their stored
        # settings untouched and must not raise validation errors for fields the admin
        # isn't editing.
        provider_forms = self.provider_forms(request, bound=selected)
        active = provider_forms.get(selected)

        if (
            select_form.is_valid()
            and email_form.is_valid()
            and (active is None or active.is_valid())
        ):
            select_form.save()
            email_form.save()
            if active:
                active.save()
            messages.success(request, _("Invoicing settings saved."))
            return redirect(
                reverse(
                    "plugins:pretix_ptinvoicing:settings",
                    kwargs={
                        "event": request.event.slug,
                        "organizer": request.organizer.slug,
                    },
                )
            )
        return self.render(request, select_form, email_form, provider_forms)

    def provider_forms(self, request, bound=None):
        forms_by_provider = {}
        for identifier, cls in PROVIDERS.items():
            form = cls.settings_form_class(
                obj=request.event,
                prefix=identifier,
                data=request.POST if identifier == bound else None,
            )
            for name in cls(request.event).hidden_settings_fields():
                form.fields.pop(name, None)
            forms_by_provider[identifier] = form
        return forms_by_provider

    def render(self, request, select_form, email_form, provider_forms):
        for form in (select_form, email_form, *provider_forms.values()):
            _no_autofill(form)
        # Which card starts selected: what was just posted, else ?provider= (the Moloni
        # connect flow returns with it, so a provider being set up stays shown even
        # though it isn't saved as the event's provider yet), else what's saved.
        selected = request.POST.get("ptinvoicing_provider")
        if selected is None:
            selected = request.GET.get("provider")
        if selected not in PROVIDERS and selected != "":
            selected = request.event.settings.get("ptinvoicing_provider") or ""
        return render(
            request,
            self.template_name,
            {
                "select_form": select_form,
                "email_form": email_form,
                "selected_provider": selected,
                "provider_cards": [
                    {"identifier": "", "label": _("No invoicing"), "logo": None}
                ]
                + [
                    {
                        "identifier": identifier,
                        "label": cls.verbose_name,
                        "logo": cls.logo,
                    }
                    for identifier, cls in PROVIDERS.items()
                ],
                "provider_blocks": [
                    {
                        "identifier": identifier,
                        "label": PROVIDERS[identifier].verbose_name,
                        "form": form,
                        "provider": PROVIDERS[identifier](request.event),
                    }
                    for identifier, form in provider_forms.items()
                ],
            },
        )
