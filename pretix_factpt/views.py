from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, View
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.event import EventSettingsViewMixin

from .client import FactptAPIError, FactptClient
from .forms import FactptSettingsForm
from .models import FactptInvoice
from .tasks import generate_factpt_invoice


def _describe(item):
    return (
        item.get("name")
        or item.get("designation")
        or item.get("description")
        or str(item.get("id"))
    )


class IndexView(EventPermissionRequiredMixin, ListView):
    model = FactptInvoice
    template_name = "pretix_factpt/control/index.html"
    context_object_name = "invoices"
    permission = "can_view_orders"
    paginate_by = 50

    def get_queryset(self):
        qs = FactptInvoice.objects.filter(
            order__event=self.request.event
        ).select_related("order")
        status = self.request.GET.get("status")
        if status in dict(FactptInvoice.STATUS_CHOICES):
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["status_choices"] = FactptInvoice.STATUS_CHOICES
        return ctx


class RetryView(EventPermissionRequiredMixin, View):
    permission = "can_change_orders"

    def post(self, request, *args, **kwargs):
        invoice = get_object_or_404(
            FactptInvoice, pk=kwargs["pk"], order__event=request.event
        )
        generate_factpt_invoice.apply_async(
            kwargs={"order_pk": invoice.order.pk, "event_pk": request.event.pk}
        )
        messages.success(request, _("Re-issuance request sent to Fact.pt."))
        return redirect(
            reverse(
                "plugins:pretix_factpt:index",
                kwargs={
                    "event": request.event.slug,
                    "organizer": request.organizer.slug,
                },
            )
        )


class DownloadView(EventPermissionRequiredMixin, View):
    permission = "can_view_orders"

    def get(self, request, *args, **kwargs):
        invoice = get_object_or_404(
            FactptInvoice,
            pk=kwargs["pk"],
            order__event=request.event,
            status=FactptInvoice.STATUS_SUCCESS,
        )
        settings = request.event.settings
        client = FactptClient(
            token=settings.get("factpt_token"),
            sandbox=settings.get("factpt_sandbox", as_type=bool, default=False),
        )
        try:
            pdf_bytes = client.download_document(invoice.factpt_document_id)
        except FactptAPIError:
            raise Http404()

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{invoice.order.code}.pdf"'
        return response


class SettingsLookupsView(EventPermissionRequiredMixin, View):
    # Powers the live VAT-rate/unit dropdowns on the settings page: the browser posts whatever
    # token is currently typed (not necessarily saved yet), so admins see options before hitting Save.
    permission = "can_change_event_settings"

    def post(self, request, *args, **kwargs):
        token = request.POST.get("token", "").strip()
        if not token:
            return JsonResponse({"error": str(_("No API token provided."))}, status=400)

        client = FactptClient(
            token=token, sandbox=request.POST.get("sandbox") == "true"
        )
        try:
            taxes = client.list_taxes()
            units = client.list_units()
        except FactptAPIError as e:
            return JsonResponse({"error": e.as_text()}, status=400)

        return JsonResponse(
            {
                "taxes": [{"id": t.get("id"), "label": _describe(t)} for t in taxes],
                "units": [{"id": u.get("id"), "label": _describe(u)} for u in units],
            }
        )


class SettingsView(EventSettingsViewMixin, EventPermissionRequiredMixin, View):
    permission = "can_change_event_settings"
    form_class = FactptSettingsForm
    template_name = "pretix_factpt/control/settings.html"

    def get(self, request, *args, **kwargs):
        form = self.form_class(obj=request.event, prefix="factpt")
        return self.render(request, form)

    def post(self, request, *args, **kwargs):
        form = self.form_class(obj=request.event, prefix="factpt", data=request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, _("Fact.pt settings saved."))
            return redirect(
                reverse(
                    "plugins:pretix_factpt:settings",
                    kwargs={
                        "event": request.event.slug,
                        "organizer": request.organizer.slug,
                    },
                )
            )
        return self.render(request, form)

    def render(self, request, form):
        from django.shortcuts import render

        return render(request, self.template_name, {"form": form})
