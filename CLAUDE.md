# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pretix-pt-invoicing` is a standalone, pip-installable [pretix](https://github.com/pretix/pretix) plugin
(importable as `pretix_ptinvoicing`) that issues AT-certified **Fatura-Recibo** invoices through a
Portuguese electronic invoicing provider once a pretix order is paid. The pretix-facing half is
provider-agnostic; providers are plug-in classes under `providers/`. Currently implemented:
[Fact.pt](https://api.fact.pt). Planned: Moloni.

Structured the same way as this author's other pretix plugin, `pretix-eupago` (uv + pyproject.toml,
pytest against a real pretix install, ruff, GitHub Actions CI, trusted-publishing PyPI release) — see
that project for the pattern this one follows, if it's checked out alongside this one.

**Status**: verified against a real pretix dev instance (`manage.py check`, migration applies cleanly)
and covered by a test suite. For the Fact.pt provider, the `GET /taxes` lookup has been exercised
against a real Fact.pt account; invoice issuance itself hasn't. See "Known rough edges" below before
relying on this in production.

**History**: this was `pretix-factpt` (package `pretix_factpt`) through 0.1.0, a Fact.pt-only plugin.
0.2.0 renamed it and split the Fact.pt specifics into a provider. There is deliberately **no data
migration** from the old app's table — the new app label means a fresh `0001_initial`, the issuance log
is rebuildable (the documents themselves live in the provider's account), and a cross-app data
migration isn't worth carrying for a plugin with no public release behind it. Event settings keys are
unchanged (`factpt_*`), so stored credentials survive; only `ptinvoicing_provider` has to be picked.

## Architecture

### The provider-agnostic core

- `pretix_ptinvoicing/__init__.py` — declares `__version__`, guards against `pip install` outside a
  pretix environment, and re-exports `PluginApp` from `apps.py`.
- `pretix_ptinvoicing/apps.py` — `PluginApp` (subclasses `pretix.base.plugins.PluginConfig`) declares
  `PretixPluginMeta` (category `INTEGRATION`, `compatibility = "pretix>=2024.1.0"`) and hooks up
  `signals.py` in `ready()`. **Must stay a real submodule**, not be inlined into `__init__.py` — same
  reason as in `pretix-eupago`'s `CLAUDE.md`: pretix's plugin loader only appends the entry point's
  *module* portion to `INSTALLED_APPS`, and Django's app-config autodiscovery for a bare app name looks
  for `apps.py`, not `__init__.py`.
- `pretix_ptinvoicing/orderdata.py` — reading buyer data off a pretix order, provider-independent:
  `one_line`, `is_valid_pt_nif`, `bare_tin`, `client_name`, `FINAL_CONSUMER_NIF`. These started out
  inside the Fact.pt provider; writing the Moloni one showed they are pretix-side and Portugal-side
  concerns, not Fact.pt's, and a provider importing from a sibling provider is the smell that says
  so. Field *limits* stay per provider — those are its API's rules.
- `pretix_ptinvoicing/models.py` — `IssuedInvoice`: one row per issuance *attempt* against an `Order`
  for one provider (`order` FK, not one-to-one — a retry after a network failure reuses the same row
  via `get_or_create(order=..., provider=..., identifier_id=...)`, but a config change that alters
  `identifier_id` would start a new row). Tracks `provider` (the provider's `identifier`), `status`
  (`pending`/`success`/`error`), the result (`document_id`, `document_link`, `permanent_url`), the
  error (`error_message`, `error_detail` — the provider's raw per-field errors), and `attempts`.
  `unique_together = [("provider", "identifier_id")]`, not a global unique on `identifier_id`, so the
  same order could in principle be issued by two providers.
  `IssuedInvoice.build_identifier_id(event, order)` — `f"pretix-{event.slug}-{order.code}"[:50]` —
  lives on the model because it *is* the model's key; it's a pretix-side value, not provider-specific,
  and providers receive it as an argument rather than deriving it.
- `pretix_ptinvoicing/signals.py` — `order_paid` receiver enqueues `issue_invoice` on Celery;
  `nav_event`/`nav_event_settings` receivers add the Control-panel sidebar entries (via the shared
  `_nav_entry` helper), gated on `can_view_orders` / `can_change_event_settings` respectively.
  Both are labelled **"Invoicing (PT)"**, not "Invoicing": pretix core already has its own
  "Invoicing" entry in both of those navs, and two identically named entries are indistinguishable
  in the sidebar. The pt_PT catalog translates it "Faturação PT" against core's "Facturação";
  `pretix.presale.signals.order_info_top` receiver renders `presale/order_info.html`, the buyer's
  download button (gated on `ptinvoicing_show_in_order`, and only once a document exists). It
  deliberately does **not** reuse pretix's own "Invoices" panel: that lists `order.invoices`, i.e.
  pretix's own `Invoice` records, and the provider's document is not one — pretix never generated it
  and doesn't own its number. Note the control and presale signals are both called `order_info`,
  hence the aliased imports.
  The button copies pretix's own markup exactly — `form.download-btn-form` wrapping a
  `button.btn.btn-lg` — because that is what pretix's CSS styles (`form.download-btn-form` is
  `display:inline`, and `.download-btn-form + .download-btn-form` gets a `.5em` left margin). Using
  anything else puts the button on its own line.
  The control-side `order_info` receiver renders `control/order_info.html` — the per-order panel on the Control-panel
  order page, showing what was issued for that order plus the "Issue invoice now" / "Retry issuance"
  button. The button only renders for a paid order that isn't already `success`; for an unpaid one the
  panel says so instead, matching the task's own guard.
- `pretix_ptinvoicing/tasks.py` — `issue_invoice` (Celery task, `bind=True, max_retries=3,
  default_retry_delay=120`) is the whole orchestration and knows nothing about any specific provider:
  loads the `Order`, returns early unless it's `STATUS_PAID` (the `order_paid` signal guarantees that,
  the admin's manual "issue now" button does not — and an invoice-receipt asserts the money was
  received), resolves the event's provider via `get_provider(event)`, bails out silently if
  there is none or it isn't `is_configured`, builds the idempotency key, `get_or_create`s the
  `IssuedInvoice` row, short-circuits if it's already `STATUS_SUCCESS`, then calls
  `provider.issue(order, identifier_id)`. A `ProviderError` is **terminal**: marks the invoice `error`
  and returns, since retrying a provider-level rejection without a config/data fix won't help. Any
  other exception (network/infra) marks `error` too but also `raise self.retry(exc=e)`, since those are
  expected to be transient. That split is the contract every provider has to respect — see
  `providers/base.py`.
- `pretix_ptinvoicing/forms.py` — `ProviderSelectForm(SettingsForm)`: the
  provider-agnostic settings. `ptinvoicing_provider` (a `ChoiceField` over
  `provider_choices()` plus a blank "none" option), `ptinvoicing_email_invoice` and
  `ptinvoicing_show_in_order`. Rendered **without** a form prefix (field name = storage
  key), unlike the provider forms below, and the settings template renders the whole form
  rather than named fields, so a new setting here needs no template edit.
- `pretix_ptinvoicing/mail.py` — `send_invoice_email(order, provider, invoice)`: downloads
  the PDF, parks it in a `CachedFile` and hands it to pretix's `mail()` as
  `attach_cached_files`. Lives in the core, not in a provider, so every provider gets it.
  **Every failure is logged and swallowed**: by the time this runs the document is already
  issued at the provider, and a mail problem must not flip a successful issuance to `error`
  or trigger the task's retry (which would re-enter issuance for an order that already has
  a document). **Named `mail.py`, never `email.py`**: a module called `email.py` in this package
  shadows the stdlib `email` package for anything run with the package directory on `sys.path`,
  which is exactly what the `Makefile`'s `translate` target does (`cd pretix_ptinvoicing && python
  -c ...`). It broke `make translate` with `ModuleNotFoundError: No module named 'email.message'`.
- `pretix_ptinvoicing/views.py`:
  - `SettingsView` (`EventSettingsViewMixin` + `EventPermissionRequiredMixin`,
    `permission = "can_change_event_settings"`) — plain `View`, not a generic `FormView`. Renders
    `ProviderSelectForm` plus *every* registered provider's `settings_form_class`, each with
    `prefix=<provider identifier>`, and lets `settings.js` show only the selected one's fieldset. On
    POST it binds **only the selected provider's form** to the POST data and leaves the others unbound:
    otherwise a provider the admin isn't editing would raise validation errors for its own required
    fields (e.g. Fact.pt's required token) and block every save. Consequence: a save only ever writes
    the selected provider's settings; the others keep whatever was stored.
  - `IndexView` (`ListView`, `permission = "can_view_orders"`) — one row per `IssuedInvoice` for the
    event, filterable by `status` via `?status=`. The provider column renders
    `IssuedInvoice.provider_label`, which falls back to the raw stored identifier if that provider has
    since been removed from `PROVIDERS`.
  - `IssueView` (`permission = "can_change_orders"`) — re-enqueues `issue_invoice` for one order,
    keyed on the **order code**, not on an `IssuedInvoice` pk. One endpoint therefore covers both the
    "retry after a validation failure" case (bad NIF, missing tax mapping — something the task's own
    automatic retry deliberately won't do) *and* issuing for an order that has no row at all: one paid
    before the plugin was configured, or paid by hand in the admin. Redirects to a POSTed `next` when
    it passes `url_has_allowed_host_and_scheme`, else to the index — that's how the order-page panel
    returns the admin to the order they were looking at.
  - `DownloadView` (`permission = "can_view_orders"`) — proxies the provider's download endpoint so
    the API token never reaches the browser. 404s if the event's *current* provider isn't the one that
    issued the row, since another provider's credentials can't fetch that document.
  - `OrderInvoiceDownloadView` — the same PDF for the **buyer**, on the presale side. No logged-in
    user there, so it authenticates the way pretix's own invoice download does: `OrderDetailMixin`
    checks the order secret in the URL. It is the only view registered through `event_patterns` in
    `urls.py` rather than a literal `control/...` path — pretix mounts a plugin's `event_patterns`
    under the event (`pretix/multidomain/maindomain_urlconf.py:63`), which is also what makes
    `{% eventurl %}` resolve it, including on a custom event domain.
  - `SettingsLookupsView` (`permission = "can_change_event_settings"`) — AJAX-only, POST-only endpoint
    backing live dropdowns on the settings page. Takes `provider` plus that provider's field values
    **straight from the POST body, prefix-stripped** (*not* from `event.settings`) so it reflects
    whatever the admin has currently typed into the form, before it's saved, then returns
    `{"fields": {<field name>: [{"id", "label"}, ...]}}`. A `ProviderError` (bad token, unreachable) is
    returned as `{"error": ...}` with HTTP 400; the settings-page JS surfaces that in the fieldset's
    `.ptinvoicing-lookup-status` line rather than failing silently.
- `pretix_ptinvoicing/urls.py` — all five views registered under
  `control/event/<organizer>/<event>/invoicing/...` (plain `urlpatterns`, not `event_patterns` —
  Control panel plugin pages use the full literal path, same convention as `pretix-eupago`'s
  settings/orders pages and pretix's own in-tree plugins).
- `static/pretix_ptinvoicing/presale.js` does the last hop for that button: `fragment_downloads.html`
  has no plugin signal inside it, so the nearest hook renders *above* the ticket buttons, and the
  script moves the form next to them. It appends to the **parent of the existing
  `form.download-btn-form`**, not to `.info-download`: that outer container also holds a help
  paragraph after the button row, so appending there drops the button onto a line below the help
  text. With no ticket buttons on the page, it stays where it rendered.
- **Template comments must be `{% comment %}`, not `{# #}`, whenever they span more than one line.**
  `{# #}` is single-line only, and a multi-line one renders straight onto the page as visible text.
  This shipped twice, on the settings page and the buyer's order page, and no test caught it until
  someone looked at the rendered page — `tests/test_views.py` now asserts nothing leaks.
- **`static/pretix_ptinvoicing/settings.js` must be a real static file, not an inline `<script>` in the
  template** — pretix's Control panel sends a nonce-based CSP (`script-src 'nonce-...' 'self' ...`)
  that silently blocks any inline script without a matching `nonce` attribute; every pretix core plugin
  with page JS (e.g. `banktransfer`) ships it as `static/<plugin>/*.js` loaded via `{% static %}` for
  exactly this reason — `'self'` covers same-origin script files with no nonce needed. It does two
  things, both provider-agnostic: hides every provider fieldset but the selected one, and POSTs the
  active fieldset's values to the lookups endpoint (on load, and on any `change` inside it) to swap the
  returned fields' `<input>`s for `<select>`s. It derives the lookups URL from
  `window.location.pathname` (current page + `lookups/`) rather than a Django `{% url %}` tag, since a
  plain static file has no template context to pull that from. Fields it populated are tracked in
  `lookupFields` so picking an option doesn't trigger another lookup.
- `pretix_ptinvoicing/migrations/0001_initial.py` — the one migration for `IssuedInvoice`. Depends on
  `pretixbase.0001_initial` (the `Order` FK target). `id` is a `BigAutoField` to match pretix's
  `DEFAULT_AUTO_FIELD`; with a plain `AutoField` every `makemigrations` run would want to write a
  spurious `0002`.

### The provider interface

`pretix_ptinvoicing/providers/base.py` holds the whole contract — three small things:

- `ProviderError(Exception)` — a provider-side *rejection* (bad NIF, missing tax mapping, bad token),
  carrying a `detail` dict of raw per-field errors alongside a human `as_text()` so the admin panel can
  show exactly what was rejected. Terminal by contract: `tasks.py` will not retry it. Transient
  failures must surface as some *other* exception type; that's the only signal the task has to tell the
  two apart.
- `IssuedDocument` — dataclass, `document_id` / `link` / `permanent_url`. What `issue()` returns.
- `InvoiceProvider` — `identifier`, `verbose_name`, `settings_form_class`,
  `deduplicates_issuance`, constructed with the `event`; `is_configured`,
  `issue(order, identifier_id)`, `download(document_id)`, and optional `lookups(data)`.
  `self.settings` is the event's hierarkey store and is **writable**, which is where a provider
  with expiring credentials (an OAuth access token) caches the refreshed one.
  `deduplicates_issuance` is the one part of the contract that isn't obvious: it says whether the
  provider itself rejects a second document for the same `identifier_id`. `tasks.py` only auto-
  retries a network failure when it does — otherwise a call that timed out after the document was
  created would be re-sent and issue a second official invoice.

`providers/__init__.py` is the registry: `PROVIDERS = {p.identifier: p for p in (FactptProvider,)}`,
plus `get_provider(event)` (reads `event.settings["ptinvoicing_provider"]`) and `provider_choices()`.
Adding a provider is one package plus one entry in that tuple — deliberately no entry points and no
autodiscovery for something that gains a new member once a year. The base classes live in `base.py`
rather than in `__init__.py` so provider modules can import them without an import cycle through the
registry.

Settings keys are namespaced per provider *by hand* (`factpt_token`, not `token`): pretix keeps all
event settings in one flat hierarkey namespace shared with core and every other plugin.

### The Fact.pt provider

`pretix_ptinvoicing/providers/factpt/`:

- `__init__.py` — `FactptSettingsForm` + `FactptProvider`.
  - `FactptSettingsForm(SettingsForm)`: `factpt_token`, `factpt_sandbox`, `factpt_default_tax_id`,
    `factpt_default_unit_id`, `factpt_default_type`. Field names are already prefixed `factpt_` so they
    land unprefixed in `event.settings` (pretix's `SettingsForm`/`HierarkeyForm` storage keys off the
    field name, not the Django form `prefix`) — the `prefix=<identifier>` that `SettingsView` passes
    only affects the rendered `<input name=...>`, not the storage key. Keep that consistent between
    the view's `GET` and `POST` handling (it already is) if you touch this — otherwise submitted values
    won't bind.
  - `factpt_default_tax_id` stays a plain `IntegerField` — the dropdown is a pure client-side
    enhancement (`settings.js` swaps the rendered `<input type=number>` for a `<select>` with the same
    `name`/`id` once a lookup succeeds), not a `ChoiceField`. Deliberately not a `TypedChoiceField`:
    that would validate the submitted value against choices computed at *render* time, which would
    break saving a previously-set value on any page load where the live lookup fails (network hiccup,
    Fact.pt down) — the plain `IntegerField` keeps that path working regardless of API availability.
    `settings.js` also guards this client-side: if the currently-set tax id isn't in the freshly
    fetched list (inactive, or the fetch is stale), it's kept as an extra pre-selected option instead
    of being silently dropped — swapping in a `<select>` with no matching option would otherwise
    default to the *first* option and silently change the saved setting on next Save.
  - `factpt_default_unit_id`, by contrast, **is** a real `TypedChoiceField` — Fact.pt's product units
    are a small, fixed, documented list (Units/Meters/Boxes/Kilograms/Liters, ids 1–5), identical for
    every account, so there's nothing to look up live and no `ChoiceField`-staleness risk like the tax
    id has.
  - `FactptProvider.lookups()` calls `list_taxes()` and returns
    `{"factpt_default_tax_id": [...]}`, filtered to `isActive` rates and labeled via `_describe_tax()`
    (`"{description} ({name})"`, e.g. `"Taxa normal (23%)"` — confirmed against Fact.pt's real
    `/taxes` response shape: `id`/`name`/`description`/`value`/`isActive`).
  - `FactptProvider._check_tax_rate_matches()` — runs before every issuance: fetches the
    configured rate via `list_taxes()` and refuses (with a `ProviderError`) if its `value` differs
    from any position's `tax_rate`, or if the id no longer exists in the account. Because `price`
    is sent net and Fact.pt adds its own rate back, a mismatch between the two sides produces a
    document for the wrong amount, silently — an event whose items carry no tax rule (pretix 0%)
    configured against Fact.pt's 23% issues an 18.45 invoice for a 15.00 order. Costs one extra
    `GET /taxes` per issuance; that is the price of not mis-invoicing. It also turns rough edge #1
    from a silent money bug into a visible, fixable error.
  - `FactptProvider._resolve_client_id()` — resolves an existing Fact.pt client so the block becomes
    `{"id": ...}` and Fact.pt never has to match one itself. `GET /clients?search=` matches on both
    `tin` and `name` (verified against a real account, which settles an older open question here).
    With a NIF: exactly one result whose `tin` matches exactly, else `None` — duplicates are a
    deliberate stop, see rough edge #5. Without a NIF: the lowest-id result that is
    `isFinalConsumer` with the same `name`. Reusing rather than creating is what keeps the
    final-consumer records from multiplying; the lowest id makes repeat issuance deterministic, and
    duplicates on this branch are the same fiscal entity (999999990) under the same name, so the
    choice is arbitrary but harmless. A search failure (`FactptAPIError`) is swallowed, not fatal —
    falls back to inline creation.
- `client.py` — `FactptClient`, a thin `requests` wrapper over the Fact.pt REST API, and
  `FactptAPIError(ProviderError)` (so `tasks.py`'s terminal-vs-retry split works with no Fact.pt
  knowledge). Picks `http://api.sandbox.fact.pt` vs `https://api.fact.pt` from the `sandbox`
  constructor arg. `search_clients(query)` hits `GET /clients?search=<query>` (confirmed to exist via
  `digfish/php-factpt-cli`'s `searchCustomers()`, though whether `search` matches against `tin`
  specifically — as opposed to only `name`/other fields — isn't confirmed by any public docs;
  `_resolve_client_id` re-checks each result's own `tin` field before trusting a match, so a `search`
  that turns out to be name-only just yields zero usable matches, not a wrong one).
  `download_document` wraps `requests` failures in `FactptAPIError` too, so `DownloadView`'s
  `except ProviderError` actually covers it.
- `payload.py` — pure functions mapping a pretix `Order` to a Fact.pt request body:
  - `bare_tin(order)` — `order.invoice_address.vat_id` with any leading `"PT"` stripped (Fact.pt's
    `tin` field wants the bare number), or `None` if there's no VAT id at all. Shared between
    `build_client_block` and `FactptProvider._resolve_client_id`, so the two can't drift.
  - `build_client_block(order, client_id=None)` — with `client_id` given, returns just `{"id":
    client_id}`, referencing an existing Fact.pt client instead of describing one inline. Otherwise
    (the default), reads `order.invoice_address` (a `OneToOneField`, so it can be `None`). No NIF →
    `finalConsumer: true`, `tin` omitted. NIF present → `tin` from `bare_tin(order)`, `finalConsumer:
    false`.
    `retention: false` and `ric: false` are sent **only on the NIF branch**. Fact.pt's docs are
    explicit that `tin`, `ric` and `retention` must *not* be declared when `finalConsumer` is
    true, and a sandbox document confirmed that omitting them works — Fact.pt then registers the
    client under the final-consumer NIF `999999990` by itself (verified: the created client came
    back as `{"tin": "999999990", "isFinalConsumer": true}`). An earlier note here claimed both
    were required on *every* block; that generalised an error only ever seen on the NIF branch.
    Both hardcoded `false` because event ticket sales never carry IRS/IRC withholding. No setting
    for the final-consumer NIF: Fact.pt supplies it, so there is nothing to configure.
    `forceTin: true` is sent **only on the NIF branch**, where it turns creation into an upsert for a
    NIF that already has a client on file, so a repeat buyer (or a retry after a prior attempt
    registered them) doesn't fail with `"clientBlock: Force update is not true."` A real NIF
    identifies one entity, so upserting is safe there.
    It must **not** be sent on the final-consumer branch. Fact.pt's docs define it as "duplicate a
    final consumer whose name+country combination already exists" — duplicate, not reuse — and since
    every no-NIF buyer is filed under 999999990, sending it created a fresh client record on every
    single issuance. After enough of them Fact.pt could resolve neither by tin nor by details and
    refused everything with `"clientBlock: Multiple clients with same tin. Specify an ID."` (and,
    without the flag, `"...same details..."`). A real account reached eleven clients sharing
    999999990, seven of them the same name, which bricked issuance for *every* final consumer.
    Reuse is handled by `_resolve_client_id` instead — see below.
  - `build_items_block(order, event_settings)` — one inline item per `order.positions.all()`
    (pretix's non-canceled-positions manager), with `taxId`/`unitId`/`type` all coming from the
    event's plugin settings (`factpt_default_tax_id`/`_unit_id`/`_type`), **not** derived from the
    item's own `tax_rate` — see "Known rough edges" below.
    `price` is the **net** unit price, `position.price - position.tax_value`. Fact.pt applies
    `taxId`'s VAT on top of whatever `price` it is given, while pretix's `position.price` is gross
    (core uses it as `gross_value`, `pretix/base/services/invoices.py:308`). Verified against the
    sandbox: `price: "15.00"` with a 23% `taxId` came back as `gross: "18.45"`. Sending the gross
    price would issue every VAT-bearing invoice above what the buyer actually paid.
  - `client_name(order)` and the `_one_line(value, limit)` helper — Fact.pt's client block has no
    company field, so a business buyer's company has to go in `name` (that is the entity the
    invoice is made out to), falling back to the person and then the e-mail. Every text field is
    collapsed to one line and truncated to Fact.pt's documented limits (`name`/`address` 100,
    `city` 50, `email` 100, all "1 linha"). The flattening is not cosmetic: pretix's `street` is a
    `TextField`, so a buyer pressing Enter in the address box would otherwise put a newline in
    `address` and have the whole document rejected — as a `ProviderError`, i.e. terminally.
  - `build_payload(order, event_settings, identifier_id, client_id=None)` — takes `identifier_id`
    from the caller (the core builds it) and sends it as `document.identifierId`. Fact.pt rejects a
    second document with the same `identifierId`, which is the actual duplicate-issuance guard.

### E-mailing the document

pretix's own order e-mails will never carry this invoice: the "attach invoices" machinery attaches
`order.invoices`, which are pretix's own records. So the plugin sends its own mail, from `tasks.py`
right after a successful issuance, when `ptinvoicing_email_invoice` is on. Off by default — an
organizer may not want a second e-mail, and enabling it after the fact is one checkbox. Issuing
manually from the Control panel sends it too, which is the only way an already-paid order gets the
document by mail at all.

### Why async (Celery)

`signals.py`'s `order_paid` receiver only calls `.apply_async(...)` — all HTTP traffic to the provider
happens inside the Celery worker, never in the request/response cycle that confirms the payment. Same
principle as `pretix-eupago`'s payment confirmation flow: a slow or down provider must never delay the
buyer-facing checkout response.

### Idempotency, two layers

1. **Plugin-level fast path**: `tasks.py` looks up (or creates) the `IssuedInvoice` row for
   `(order, provider, identifier_id)` and returns immediately if it's already `STATUS_SUCCESS` —
   protects against a duplicate `order_paid` signal or two workers racing on the same order.
2. **Provider-level hard guarantee**: the `identifier_id` handed to `issue()`, which each provider must
   pass through to whatever field its API dedupes on (Fact.pt: `document.identifierId`). Even if step 1
   were somehow bypassed, the provider itself rejects the second document.
   **This layer is optional and a provider must declare it.** An API with no such field (Moloni) sets
   `deduplicates_issuance = False`, and `tasks.py` then refuses to auto-retry, because a retry after a
   timeout could issue a second official invoice. Layer 1 alone does not protect against that: the row
   is already `pending`, and the failed attempt cannot tell whether the document was created.

### Displaying amounts

`payload.build_items_block` sends `str(position.price)` as `price` — a plain-decimal string, which is
what the Fact.pt API expects in the request body. Don't confuse this with rendering money for *display*
(e.g. in `templates/pretix_ptinvoicing/control/index.html`): pretix's `{% load money %}` /
`|money:event.currency` template filter is still the right tool there, same convention as
`pretix-eupago`.

## Translations

`pretix_ptinvoicing/locale/<lang>/LC_MESSAGES/` holds gettext catalogs (currently `pt_PT`; the source
strings are English, so English needs no catalog — add one only to override wording).
Since `pretix_ptinvoicing` is a real Django app, Django's i18n machinery discovers this automatically —
no pretix-specific wiring needed, `{% load i18n %}` / `_()` / `gettext_lazy()` calls throughout the
codebase just work once a `.mo` file exists.

The pt_PT catalog is complete (`msgfmt --statistics`: 66 translated, 0 fuzzy, 0 untranslated).
Two things to watch after a `make translate`:
- **Fuzzy entries are ignored at runtime.** `msgmerge` guesses a translation from a similar old
  string and flags it `#, fuzzy`; the guess is often wrong ("Last attempt" inherited "Última
  atualização" from "Last updated"). Fix the text and delete the flag, or the string silently falls
  back to English.
- **Plural entries** (`{% blocktrans count %}`) need both `msgstr[0]` and `msgstr[1]` filled; an
  empty one leaves the string untranslated.

- `make translate` — extracts `_()`/`gettext_lazy()`/`{% trans %}` strings into
  `locale/<lang>/LC_MESSAGES/django.po` (merging into any existing translations).
- `make compile-translations` — compiles `.po` → `.mo` with plain `msgfmt --check`, not Django's
  `compilemessages`. That is what `compilemessages` shells out to anyway, and calling it directly
  means the target needs neither a virtualenv nor Django — which is what lets **CI's `build` job run
  it before `uv build`**, so a release no longer depends on the committed `.mo` being current (only
  `.mo` files are loaded at runtime, and `package-data` just ships whatever is on disk). Verified
  byte-identical to what `compilemessages` produced. `--check` also fails on a translation whose
  format specifiers don't match the msgid, which would otherwise crash at runtime.
  Keep committing the `.mo` anyway: an editable install from a checkout doesn't run the Makefile.
- To add a language: `mkdir -p pretix_ptinvoicing/locale/<lang>/LC_MESSAGES`, add it to `LOCALES` in
  the `Makefile`, then `make translate`.
- Both targets deliberately configure a bare `django.conf.settings.configure(USE_I18N=True)` instead of
  pointing `DJANGO_SETTINGS_MODULE` at `tests.settings` (i.e. pretix's own settings) — same reason as
  `pretix-eupago`'s `CLAUDE.md`: pretix's own settings module points `LOCALE_PATHS` at pretix core's
  bundled locale directory, and `makemessages`/`compilemessages` treat every `LOCALE_PATHS` entry as a
  target, corrupting the installed pretix package's own translations with this plugin's strings.

## Tests

```bash
make install               # uv sync --extra test — pulls in real pretix core, not just this plugin
make test                  # uv run pytest — runs the whole suite
uv run pytest tests/test_tasks.py::test_successful_issuance_creates_invoice -v   # single test
make lint                  # uv run ruff check . && uv run ruff format --check .
make format                # uv run ruff check --fix . && uv run ruff format .
```

Same conventions as `pretix-eupago`:

- `tests/settings.py` is `from pretix.testutils.settings import *` (sqlite, Celery eager, migrations
  disabled unless `GITHUB_WORKFLOW` is set — inherited from pretix core, not controlled here). To
  actually exercise `0001_initial` locally, run `GITHUB_WORKFLOW=1 uv run pytest`.
- `pyproject.toml` sets `pythonpath = ["."]` under `[tool.pytest.ini_options]` — required so
  `tests.settings` imports correctly regardless of how `pytest` is invoked (see `pretix-eupago`'s
  `CLAUDE.md` for the exact failure mode this avoids).
- `tests/conftest.py` provides `organizer`/`event`/`order`/`item`/`position` fixtures; all model
  creation happens inside `with scopes_disabled():` (`django_scopes`) since fixtures run without an
  active scope.
- Provider-agnostic tests live in `tests/test_models.py`, `tests/test_tasks.py`, `tests/test_views.py`;
  Fact.pt-specific ones in `tests/test_factpt_client.py` / `tests/test_factpt_payload.py`. A second
  provider should follow the same split.
- Tests that expect issuance to happen must set **both** `event.settings.ptinvoicing_provider =
  "factpt"` and the provider's own credentials — with no provider selected the task now returns early
  and creates nothing.
- HTTP calls to providers (`requests` in `client.py`) are mocked with `responses`
  (`@responses.activate` + `responses.add(...)`), not `unittest.mock` — matches pretix core's own test
  suite convention.
- `tests/test_views.py` exercises `SettingsView` through the real Control-panel URL with the Django
  test `client`, including a permission check (no access → `404`, not `403`, matching pretix's own
  convention of not revealing a resource exists). Because `SettingsView` renders each provider's form
  with `prefix=<identifier>`, POSTed field names need that prefix (`"factpt-factpt_token"`), while the
  provider selector does not (`"ptinvoicing_provider"`) — see the `forms.py` note above.
- Because the plugin is installed (even `-e` editable) into the same environment pytest runs in, its
  `pretix.plugin` entry point is picked up automatically — no manual `INSTALLED_APPS` wiring needed.
  A stale `*.egg-info` directory from a previous package name will keep advertising the old entry point
  and break `django.setup()` with `ModuleNotFoundError`; delete it.

## Plugin registration mechanics

Same as `pretix-eupago`: discovered via the `pretix.plugin` setuptools entry point in `pyproject.toml`:

```toml
[project.entry-points."pretix.plugin"]
pretix_ptinvoicing = "pretix_ptinvoicing:PluginApp"
```

The entry point's *module* portion must resolve to a package with its own `apps.py` — see
"Architecture" above. Note this is the *plugin* entry point (one per distribution); invoicing providers
are **not** entry points, just entries in `providers.PROVIDERS`.

### The Moloni provider

`pretix_ptinvoicing/providers/moloni/` — the second provider, written as a proof that the core is
genuinely provider-agnostic. **Built from Moloni's published documentation and exercised only
against mocks**, unlike the Fact.pt one, which was settled against a real sandbox account. Treat
every shape here as unverified until someone runs it against a real Moloni account: in particular
whether `price` is net or gross (Moloni's docs don't say), and the exact `invoiceReceipts/insert`
entity name.

- `client.py` — OAuth rather than a static token: `GET /v1/grant/` with the password grant,
  renewed with a 14-day refresh token, and the access token passed as a **GET parameter** on every
  call. `on_token` hands each new pair back to the provider, which caches it in `event.settings` —
  without that, every issuance would burn a fresh password grant.
- `payload.py` — `customers/insert` shape plus the document's `products`/`payments` arrays. The
  customer is a separate object: Moloni takes only `customer_id`, there is no inline client block.
- `__init__.py` — `MoloniSettingsForm` (nine fields, four of them credentials) and
  `MoloniProvider`. Its `lookups()` returns **four** dropdowns at once (company, document set, tax,
  payment method) against credentials the admin hasn't saved yet, where Fact.pt's returns one —
  same `{field: [{id, label}]}` contract, no change to `settings.js`.
- `deduplicates_issuance = False`, which is the whole reason that flag exists.

## Would a second provider fit? (checked against Moloni's published API)

The core was checked against a real second API rather than assumed to be general.
`tests/test_tasks.py` also registers a `_DummyProvider` that shares nothing with Fact.pt and runs
the whole task through it, so a Fact.pt assumption leaking into the core fails a test.

Fits as-is:
- **Customer must exist first.** Moloni takes only `customer_id` on `documents/insert`; there is no
  inline client block. That is internal to `issue()` — `customers/getByVat` + `customers/insert`
  is the same shape as `FactptProvider._resolve_client_id` already has.
- **OAuth with expiring tokens.** `__init__(event)` plus a writable `self.settings` is enough to
  cache an access token; `is_configured` checks the credentials, and `lookups(data)` can do the
  token dance with the values the admin has typed but not yet saved.
- **Different field names, different price semantics.** Nothing in the core reads them:
  `build_items_block`/`build_client_block` live under `providers/factpt/`, and so would Moloni's
  own mapping. Moloni's docs don't state whether its `price` is net or gross — settle it the way
  the Fact.pt one was settled, with one sandbox document, not by assuming.
- **PDF.** `documents/getPDFLink` returns a URL; `download()` fetches it and returns bytes.
- **Lookups.** `document_set_id` and `tax_id` become dropdowns through the same
  `{field: [{id, label}]}` contract; `settings.js` needs no change.

What writing it actually changed in the core (nothing structural, two things):
- `orderdata.py` — the buyer-data helpers moved out of the Fact.pt provider, because Moloni needs
  exactly the same NIF handling and a provider must not import from a sibling.
- `deduplicates_issuance` — below.

Did **not** fit, and is why `deduplicates_issuance` exists:
- **No idempotency field.** `documents/insert` has nothing Moloni dedupes on — `our_reference` and
  `your_reference` are free text. The "two layers" below therefore collapse to one for Moloni, and
  blind retries become unsafe. A Moloni provider should set `deduplicates_issuance = False` (and,
  better, search for an existing document by reference before inserting).

## Verified against the real API

These were checked against a real Fact.pt **sandbox** account, not inferred from docs:

- `price` is net — `15.00` at a 23% `taxId` is issued as `gross: 18.45`.
- `finalConsumer: true` with no `tin`/`ric`/`retention` is accepted, and the client is created as
  `{"tin": "999999990", "isFinalConsumer": true}`.
- `GET /taxes` returns `id`/`name`/`description`/`value`/`isActive`; an account can hold two
  different 0% exemption rates.
- `GET /clients?search=` matches on `name` as well as `tin`.
- `forceTin` on a final consumer *duplicates* the client rather than reusing it; omitting it and
  referencing the existing record by id is what works.
- `GET /documents/{id}` returns `gross`, `number`, `paymentStatus`, `identifierId`;
  `GET /clients/{id}` returns the stored client, which is how the `999999990` behaviour was
  confirmed. `GET /documents/{id}/items` exists in the docs but returned a bare `ERR`.

## Known rough edges

1. **VAT rate mapping is one fixed `factpt_default_tax_id` per event.** If an event sells items at
   different VAT rates, `build_items_block` needs a pretix-tax-rate → Fact.pt-`taxId` mapping instead
   of a single value. Since `_check_tax_rate_matches` refuses to issue when the two sides disagree,
   such an event now *fails* rather than mis-invoicing — but it still fails, and the mapping is the
   real fix. Note an automatic mapping can't be fully derived: an account can hold several distinct
   0% rates (e.g. `Is.IVA M05` Art. 14.º and `Is.IVA M07` Art. 9.º), and only the admin knows which
   exemption applies.
2. **Two different retry paths, don't conflate them**: `tasks.py`'s `self.retry()` (3 attempts, 120s
   apart) is only for network/infra exceptions; the Control-panel "Retry"/"Issue invoice now" button
   (`IssueView`) is for `ProviderError`s that need a data/config fix first.
3. **`GET /taxes` and its response shape (`id`/`name`/`description`/`value`/`isActive`) are confirmed**
   against a real account — see `_describe_tax()` in `providers/factpt/__init__.py`. `list_taxes()`
   only fetches page 1 (the response also carries `totalPages`); fine for VAT rates in practice (a
   handful per account), but worth revisiting with real pagination via the response's paging info if an
   account ever has more.
4. **`forceTin: true` on the NIF branch still overwrites the Fact.pt-side client record** with
   whatever's currently in `order.invoice_address` — there's no "only update if different" mode. If a
   client's Fact.pt record has been manually corrected there (a fixed typo, an updated address) and a
   later pretix order carries the old data, that later order's invoice will silently revert it. This
   only applies when the client search comes up empty or ambiguous — a resolved `client_id` references
   the existing record as-is and sends no other fields to overwrite it with.
   The documented alternative, `forceUpdate: true` with `id`/`tin` and every other field optional
   ("Com atualização de Cliente"), was considered and skipped: `_resolve_client_id` already covers the
   normal case by id, so it would only help when the search misses an existing NIF, at the cost of a
   second round trip.
5. **Pre-existing duplicate clients still have to be cleaned up by hand.** The plugin no longer
   creates them, and the final-consumer branch now reuses the lowest matching id, so an account that
   was already polluted keeps working. But a NIF that matches several records still stops issuance.
6. **If a NIF already matches more than one client record in the account,
   `FactptProvider._resolve_client_id` doesn't try to pick one** — matches are only used when there's
   exactly one, otherwise it falls back to the same inline-`client`-block-plus-`forceTin` request as
   before, which itself will be rejected by Fact.pt (`"clientBlock: Multiple clients with same tin.
   Specify an ID."`) until the duplicates are resolved by hand in Fact.pt's Backoffice. This is a
   deliberate stop, not a bug: guessing which duplicate to attach an official invoice to isn't
   something to do silently.
7. **Saving the settings page only writes the selected provider's settings.** Edits typed into another
   provider's (hidden) fieldset are discarded, because `SettingsView` doesn't bind those forms. Fine
   while providers are configured one at a time; if per-provider independent saves are ever wanted,
   that's a per-provider POST, not a wider bind.
8. **Manual issuance is per-order, one at a time.** There's no bulk "issue everything that's paid and
   missing an invoice" action; for a backlog, loop `issue_invoice` in a shell. Deliberate: a bulk
   button that fires official documents at a whole event's order list is the kind of thing you press
   once and regret.
9. **Switching provider mid-event doesn't re-issue anything.** Past `IssuedInvoice` rows keep their
   original `provider`, and `DownloadView` 404s for rows whose provider is no longer the event's
   current one — the old provider's credentials aren't kept around to fetch the PDF.

## Commands

To manually exercise changes against a real pretix instance:

```bash
# From a pretix dev checkout with its virtualenv activated:
pip install -e /path/to/pretix-pt-invoicing
cd <pretix>/src
python manage.py migrate    # applies pretix_ptinvoicing's own migration
python manage.py runserver
```

Then enable "Portuguese invoicing" under an event's Settings → Plugins tab, pick a provider and
configure it under the event's Invoicing settings page, and check the Invoicing sidebar entry for the
issuance panel.

To exercise issuance without buying a ticket, open any **paid** order in the Control panel and use the
"Issue invoice now" button in its Invoicing panel (mark an order as paid by hand first if needed). The
same thing from a shell, which is handy when Celery isn't running — `CELERY_ALWAYS_EAGER` off means
`.apply_async()` only queues:

```python
# python manage.py shell
from django_scopes import scopes_disabled
from pretix.base.models import Order
from pretix_ptinvoicing.tasks import issue_invoice

with scopes_disabled():
    order = Order.objects.get(code="ABCDE", event__slug="my-event")
issue_invoice.apply(
    kwargs={"order_pk": order.pk, "event_pk": order.event.pk}
)  # runs inline
```

Note that a dev server does **not** pick up a newly `pip install -e`'d plugin on autoreload: the
`pretix.plugin` entry points are read at startup, so restart it. Check which interpreter actually runs
the server, too — an entry point registered in one virtualenv is invisible to a server started with
another.

Build distribution artifacts locally:

```bash
uv build
uvx twine check dist/*
```

## CI / Publishing

`.github/workflows/ci.yml` mirrors `pretix-eupago`'s: `lint`/`test` on every push/PR/release (`test`
matrixed across Python 3.11–3.14), `build`/`publish` only on `release` and gated on `needs: [lint,
test]`. Publishing uses PyPI trusted publishing (OIDC) — no token secret; the `publish` job's
`environment: pypi` must match a trusted publisher registered on PyPI for this project before the first
release. The PyPI project is `pretix-pt-invoicing`, not the old `pretix-factpt` — the trusted publisher
has to be registered against the new name.

Bump the version in both `pyproject.toml` and `pretix_ptinvoicing/__init__.py` via `make bump-version
<version>` rather than by hand, then `uv lock` to sync `uv.lock`'s own entry.
