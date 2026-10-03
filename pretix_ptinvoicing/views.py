from django import forms
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, View
from pretix.base.models import Order
from pretix.base.templatetags.money import money_filter
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.event import EventSettingsViewMixin
from pretix.presale.views import EventViewMixin
from pretix.presale.views.order import OrderDetailMixin

from .forms import EmailSettingsForm, ProviderSelectForm
from .models import IssuedInvoice
from .providers import PROVIDERS, ProviderError, ProviderUnreachable, get_provider
from .tasks import issue_credit_note, issue_invoice


class PluginEnabledMixin:
    """
    404 unless the plugin is enabled for the event. pretix only checks that for a
    plugin's event_patterns; these Control-panel URLs are plain urlpatterns, so without
    this they'd keep issuing documents for an event that turned the plugin off.
    """

    def dispatch(self, request, *args, **kwargs):
        if "pretix_ptinvoicing" not in request.event.get_plugins():
            raise Http404()
        return super().dispatch(request, *args, **kwargs)


class IndexView(EventPermissionRequiredMixin, PluginEnabledMixin, ListView):
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
        query = self.request.GET.get("query", "").strip()
        if query:
            qs = qs.filter(order__code__icontains=query)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["query"] = self.request.GET.get("query", "").strip()
        ctx["status_choices"] = IssuedInvoice.STATUS_CHOICES
        return ctx


class IssueView(EventPermissionRequiredMixin, PluginEnabledMixin, View):
    """
    Issue the invoice for one order, by order code: GET asks for confirmation, POST
    enqueues it.

    Keyed on the order rather than on an existing IssuedInvoice row so it covers both
    cases with one endpoint: retrying a row that failed, and issuing for an order that
    has no row at all (one paid before the plugin was configured, or paid manually).
    Preconditions are checked here, not only in the task, so the admin hears about a
    no-op instead of a "queued" that silently does nothing.
    """

    permission = "can_change_orders"
    kind = IssuedInvoice.KIND_INVOICE

    def get(self, request, *args, **kwargs):
        order, provider, problem = self.check(request, kwargs["code"])
        if problem:
            messages.error(request, problem)
            return redirect(self.redirect_url(request))
        return render(
            request,
            "pretix_ptinvoicing/control/confirm.html",
            {**self.confirmation(order, provider), "next": self.next_url(request)},
        )

    def post(self, request, *args, **kwargs):
        order, _provider, problem = self.check(request, kwargs["code"])
        if problem:
            messages.error(request, problem)
            return redirect(self.redirect_url(request))
        self.task().apply_async(
            kwargs={"order_pk": order.pk, "event_pk": request.event.pk}
        )
        messages.success(
            request,
            _(
                "Queued. The document is being issued in the background — reload the "
                "order page in a few seconds to see the result."
            ),
        )
        return redirect(self.redirect_url(request))

    def task(self):
        return issue_invoice

    def check(self, request, code):
        """(order, provider, problem): problem is why this can't run, or None."""
        order = get_object_or_404(Order, code=code, event=request.event)
        provider = get_provider(request.event)
        if provider is None:
            return order, None, _("No invoicing provider is selected for this event.")
        if not provider.is_configured:
            return (
                order,
                provider,
                _(
                    "%(provider)s is not fully configured, or its connection has "
                    "expired. Fix it in the invoicing settings first."
                )
                % {"provider": provider.verbose_name},
            )
        if any(
            row.in_flight
            for row in IssuedInvoice.objects.filter(
                order=order, kind=self.kind, status=IssuedInvoice.STATUS_PENDING
            )
        ):
            return (
                order,
                provider,
                _("This document is being issued right now. Reload in a moment."),
            )
        return order, provider, self.precondition(order, provider)

    def precondition(self, order, provider):
        if order.status != Order.STATUS_PAID:
            return _("Only paid orders can be invoiced.")
        if IssuedInvoice.current_cycle(order, provider.identifier)[1]:
            return _("This order already has an invoice.")
        return None

    def confirmation(self, order, provider):
        return {
            "order": order,
            "title": _("Issue invoice-receipt"),
            "text": _(
                "This issues an invoice-receipt at %(provider)s for order %(code)s, "
                "%(total)s. It is reported to the tax authority and cannot be undone."
            )
            % {
                "provider": provider.verbose_name,
                "code": order.code,
                "total": money_filter(order.total, order.event.currency),
            },
            "button": _("Issue invoice-receipt"),
        }

    def next_url(self, request):
        next_url = request.POST.get("next") or request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts=None, require_https=request.is_secure()
        ):
            return next_url
        return None

    def redirect_url(self, request):
        return self.next_url(request) or reverse(
            "plugins:pretix_ptinvoicing:index",
            kwargs={
                "event": request.event.slug,
                "organizer": request.organizer.slug,
            },
        )


class IssueCreditNoteView(IssueView):
    """
    The credit note for one order's latest uncredited invoice — IssueView's flow with
    another task. Also fired automatically on a full refund (signals.py); this is for
    everything else, like a partial refund the admin decides is worth a full credit note.
    """

    kind = IssuedInvoice.KIND_CREDIT_NOTE

    def task(self):
        return issue_credit_note

    def precondition(self, order, provider):
        if IssuedInvoice.uncredited_invoice(order, provider.identifier) is None:
            return _("This order has no issued invoice left to credit.")
        return None

    def confirmation(self, order, provider):
        invoice = IssuedInvoice.uncredited_invoice(order, provider.identifier)
        return {
            "order": order,
            "title": _("Issue credit note"),
            "text": _(
                "This issues a credit note at %(provider)s cancelling invoice-receipt "
                "%(number)s of order %(code)s in full. It is reported to the tax "
                "authority and cannot be undone."
            )
            % {
                "provider": provider.verbose_name,
                "number": invoice.display_number,
                "code": order.code,
            },
            "button": _("Issue credit note"),
        }


def _pdf_response(provider, invoice):
    pdf_bytes = provider.download(invoice.document_id)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{invoice.filename}"'
    return response


class DownloadView(EventPermissionRequiredMixin, PluginEnabledMixin, View):
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
            return _pdf_response(provider, invoice)
        except (ProviderError, ProviderUnreachable) as e:
            messages.error(
                request,
                _("Could not download the document: %(error)s") % {"error": e},
            )
            return redirect(
                reverse(
                    "control:event.order",
                    kwargs={
                        "event": request.event.slug,
                        "organizer": request.organizer.slug,
                        "code": invoice.order.code,
                    },
                )
            )


class OrderInvoiceDownloadView(EventViewMixin, OrderDetailMixin, View):
    """
    The same PDF as DownloadView, for the buyer instead of the organizer.

    Authenticated by the order secret in the URL (OrderDetailMixin), the way pretix's own
    invoice download is — there is no logged-in user here.
    """

    def get(self, request, *args, **kwargs):
        if self.order is None or self.order is False:
            raise Http404()
        if not request.event.settings.get(
            "ptinvoicing_show_in_order", as_type=bool, default=True
        ):
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
            return _pdf_response(provider, invoice)
        except (ProviderError, ProviderUnreachable):
            messages.error(
                request,
                _(
                    "The document could not be downloaded right now. Please try again "
                    "in a few minutes."
                ),
            )
            return redirect(self.get_order_url())


class SettingsLookupsView(EventPermissionRequiredMixin, PluginEnabledMixin, View):
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
        except ProviderUnreachable as e:
            return JsonResponse({"error": str(e)}, status=400)
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


def provider_forms(request, bound=None):
    """Every provider's settings form, prefixed by its identifier; only `bound` is bound."""
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


def save_valid_fields(request, provider):
    """
    Store whatever is valid on the posted settings page, ignoring what isn't: for
    leaving the page mid-setup (Moloni's "Connect" round-trip) without losing what was
    typed, when required fields can't all be filled yet. Never switches the event's
    provider — connecting one is not choosing it.
    """
    for form in (
        ProviderSelectForm(obj=request.event, data=request.POST),
        EmailSettingsForm(obj=request.event, data=request.POST),
        provider_forms(request, bound=provider)[provider],
    ):
        form.is_valid()
        for name, value in form.cleaned_data.items():
            if name == "ptinvoicing_provider":
                continue
            if value is None or value == "":
                request.event.settings.delete(name)
            else:
                request.event.settings.set(name, value)


class SettingsView(
    EventSettingsViewMixin, EventPermissionRequiredMixin, PluginEnabledMixin, View
):
    permission = "can_change_event_settings"
    template_name = "pretix_ptinvoicing/control/settings.html"

    def get(self, request, *args, **kwargs):
        return self.render(
            request,
            ProviderSelectForm(obj=request.event),
            EmailSettingsForm(obj=request.event),
            provider_forms(request),
        )

    def post(self, request, *args, **kwargs):
        select_form = ProviderSelectForm(obj=request.event, data=request.POST)
        email_form = EmailSettingsForm(obj=request.event, data=request.POST)
        selected = request.POST.get("ptinvoicing_provider", "")
        # Only the selected provider's form is bound: the others keep their stored
        # settings untouched and must not raise validation errors for fields the admin
        # isn't editing.
        forms_by_provider = provider_forms(request, bound=selected)
        active = forms_by_provider.get(selected)

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
        return self.render(request, select_form, email_form, forms_by_provider)

    def render(self, request, select_form, email_form, forms_by_provider):
        for form in (select_form, email_form, *forms_by_provider.values()):
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
                    for identifier, form in forms_by_provider.items()
                ],
            },
        )
