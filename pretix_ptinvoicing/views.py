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

from .forms import ProviderSelectForm
from .models import IssuedInvoice
from .providers import PROVIDERS, ProviderError, get_provider
from .tasks import issue_invoice


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


class SettingsView(EventSettingsViewMixin, EventPermissionRequiredMixin, View):
    permission = "can_change_event_settings"
    template_name = "pretix_ptinvoicing/control/settings.html"

    def get(self, request, *args, **kwargs):
        return self.render(
            request,
            ProviderSelectForm(obj=request.event),
            self.provider_forms(request),
        )

    def post(self, request, *args, **kwargs):
        select_form = ProviderSelectForm(obj=request.event, data=request.POST)
        selected = request.POST.get("ptinvoicing_provider", "")
        # Only the selected provider's form is bound: the others keep their stored
        # settings untouched and must not raise validation errors for fields the admin
        # isn't editing.
        provider_forms = self.provider_forms(request, bound=selected)
        active = provider_forms.get(selected)

        if select_form.is_valid() and (active is None or active.is_valid()):
            select_form.save()
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
        return self.render(request, select_form, provider_forms)

    def provider_forms(self, request, bound=None):
        return {
            identifier: cls.settings_form_class(
                obj=request.event,
                prefix=identifier,
                data=request.POST if identifier == bound else None,
            )
            for identifier, cls in PROVIDERS.items()
        }

    def render(self, request, select_form, provider_forms):
        return render(
            request,
            self.template_name,
            {
                "select_form": select_form,
                "provider_blocks": [
                    {
                        "identifier": identifier,
                        "label": PROVIDERS[identifier].verbose_name,
                        "form": form,
                    }
                    for identifier, form in provider_forms.items()
                ],
            },
        )
