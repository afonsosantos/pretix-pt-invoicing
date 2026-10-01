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
  `bare_tin` strips spaces, dots and dashes first ("PT 237.892.294" is a valid NIF, not a final
  consumer). `invoice_lines(order)` is what both providers invoice: every position **and every
  fee** (payment/service/shipping… — leaving fees out issued invoices below the order total), as
  `Line`s. `Line.net` is the net unit price **to 4 decimals, derived from gross and rate**, not
  `price - tax_value`: pretix rounds `tax_value` to cents, the provider adds its VAT back on top,
  and a cent-rounded net came back a cent high for ~1 price in 5 at 23% (15.00 → 12.20 → 15.01).
  `tests/test_factpt_payload.py` sweeps 0.01–200.00 at 23/13/6/0% to prove the round trip. The
  4-decimal net itself is **unverified against either provider's sandbox** — confirm 15.00/23%
  comes back as 15.00 before trusting it.
- `pretix_ptinvoicing/models.py` — `IssuedInvoice`: one row per issuance *attempt* against an `Order`
  for one provider (`order` FK, not one-to-one — a retry after a network failure reuses the same row
  via `get_or_create(order=..., provider=..., identifier_id=...)`, but a config change that alters
  `identifier_id` would start a new row). Tracks `provider` (the provider's `identifier`), `status`
  (`pending`/`success`/`error`), the result (`document_id`, `document_number`, `document_link`,
  `permanent_url`; `document_number` is the human number — Moloni `"FR M2026/20"` from
  `documents/getOne`'s `saft_code`/`document_set_name`/`number`, Fact.pt `"2025QG/9"` exactly as
  `GET /documents/{id}` returns it, **no mapping tables** — fetched by the provider's
  `document_number()` right after issuance, which must never raise since the document already
  exists; templates show `display_number`, falling back to the id), the
  error (`error_message` — the provider's message — and `error_detail`, its raw per-field errors,
  stored apart and shown together: `error_items`/`error_text`), `attempts`, and `email_sent`
  (`None` = mail off, else whether the buyer got the PDF).
  `unique_together = [("provider", "identifier_id")]`, not a global unique on `identifier_id`, so the
  same order could in principle be issued by two providers.
  `IssuedInvoice.build_identifier_id(event, order)` — `pretix-{event.slug}-{order.code}` —
  lives on the model because it *is* the model's key; it's a pretix-side value, not provider-specific,
  and providers receive it as an argument rather than deriving it. Keys go through `_fit()`: one
  that fits 50 characters is unchanged (existing rows still match), an over-long one keeps its head
  and swaps the tail for a hash of the whole. Plain truncation used to cut the order code (slugs
  can be 50 long), so two orders shared a key — `IntegrityError`, no invoice — and a second cycle's
  credit note shared the first's and was silently skipped. `current_cycle()` and
  `uncredited_invoice()` are shared by the tasks and the views' precondition checks. `in_flight`
  is a `pending` row touched within `STALE_AFTER` (10 min): a worker is on it, don't start another.
- `pretix_ptinvoicing/signals.py` — `order_paid` receiver enqueues `issue_invoice` on Celery,
  **via `transaction.on_commit`**: pretix sends `order_paid` inside the payment's transaction, and
  a worker that loaded the order before the commit saw it unpaid and skipped it for good (pretix's
  own `TransactionAwareTask` exists for the same reason). Tests must use
  `django_capture_on_commit_callbacks(execute=True)` — the test transaction never commits.
  `ptinvoicing_refund_done`, on Django's own `post_save` for `OrderRefund` (not an
  `EventPluginSignal` — pretix has none for this), enqueues `issue_credit_note` once refunds have
  brought an order back to fully refunded — see "Credit notes (refunds)" below for the full story.
  `nav_event`/`nav_event_settings` receivers add the Control-panel sidebar entries (via the shared
  `_nav_entry` helper), gated on `can_view_orders` / `can_change_event_settings` respectively.
  Both are labelled **"Invoicing (PT)"**, not "Invoicing": pretix core already has its own
  "Invoicing" entry in both of those navs, and two identically named entries are indistinguishable
  in the sidebar. The pt_PT catalog translates it "Faturação PT" against core's "Facturação";
  `pretix.presale.signals.order_info_top` receiver renders `presale/order_info.html`, the buyer's
  download buttons — one per successful document of the event's *current* provider, credit notes
  included (gated on `ptinvoicing_show_in_order`; another provider's documents are left out, its
  credentials aren't around to fetch them). It
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
  action (each row via `order_document_row.html`). The action only renders for a paid order that
  isn't already `success`, and never while the row is `in_flight` (it says "In progress" instead —
  a second click mid-attempt could issue twice on Moloni). It is **always rendered** once a
  provider is selected: when that provider isn't configured (or Moloni's connection expired) it
  shows a warning with a link to the settings instead of disappearing.
- `pretix_ptinvoicing/tasks.py` — `issue_invoice` (Celery task, `bind=True, max_retries=3,
  default_retry_delay=120`) is the whole orchestration and knows nothing about any specific provider:
  loads the `Order`, returns early unless it's `STATUS_PAID` (the `order_paid` signal guarantees that,
  the admin's manual "issue now" button does not — and an invoice-receipt asserts the money was
  received), resolves the event's provider via `get_provider(event)`, bails out silently if there
  is none ("No invoicing" is deliberate), returns if the current cycle already has a successful
  invoice (whatever key it was issued under), builds the idempotency key, `get_or_create`s the
  `IssuedInvoice` row — and if the provider isn't `is_configured`, records an `error` row saying so
  rather than skipping silently (an expired Moloni connection used to leave paid orders with no
  trace). Then `_attempt()`, shared with `issue_credit_note`: **claims the row under
  `select_for_update`**, refusing a `success` or `in_flight` one — a duplicate signal, a double
  click or an `acks_late` redelivery must not run a second attempt — then calls
  `provider.issue(order, identifier_id)`. A `ProviderError` is **terminal**: marks the invoice `error`
  and returns, since retrying a provider-level rejection without a config/data fix won't help. Any
  other exception (network/infra) marks `error` too but also `raise self.retry(exc=e)`, since those are
  expected to be transient — the stored message says which ("retrying automatically", "gave up",
  or, for a provider that can't dedupe, "check before retrying: it may have been created"). Messages
  are stored in the event's language (`pretix.base.i18n.language`). That split is the contract every
  provider has to respect — see `providers/base.py`.
- `pretix_ptinvoicing/forms.py` — two `SettingsForm`s, both rendered **without** a form
  prefix (field name = storage key), unlike the provider forms below, and both rendered
  whole rather than as named fields, so a new setting on either needs no template edit:
  - `ProviderSelectForm`: `ptinvoicing_provider` (a `ChoiceField` over `provider_choices()`
    plus a blank "none" option), `ptinvoicing_auto_issue`, `ptinvoicing_nif_custom_field`,
    `ptinvoicing_show_in_order`. `ptinvoicing_auto_issue` (default on) gates **only the two
    signal receivers** (`signals._auto_issue`): off means nothing is issued on payment or full
    refund, and documents come only from the order page's buttons — the tasks themselves don't
    check it, or the manual buttons would stop working too.
  - `EmailSettingsForm`: `ptinvoicing_email_invoice` and `ptinvoicing_email_credit_note` —
    split into their own form (and its own fieldset in the template, legend "E-mail") so the
    two documents' e-mails can be turned on independently; both off by default. A separate
    form because `SettingsView` validates and saves it alongside `ProviderSelectForm` on
    every POST — unlike the provider forms, it's provider-agnostic so there's no "only the
    selected one" binding rule to apply to it.
- `pretix_ptinvoicing/mail.py` — `send_invoice_email(order, provider, invoice)` and
  `send_credit_note_email(order, provider, credit_note)`, both thin wrappers around
  `_send_document_email(...)`: downloads the PDF, parks it in a `CachedFile` and hands it to
  pretix's `mail()` as `attach_cached_files`; returns whether it went out (stored as
  `email_sent`, shown on the order panel). The texts name the document number, and the credit
  note's the invoice it cancels.
  Lives in the core, not in a provider, so every provider gets it. **Every failure is logged
  and swallowed** — the `CachedFile` storage included, inside the same `try`: by the time this
  runs the document is already issued at the provider, and
  a mail problem must not flip a successful issuance to `error` or trigger the task's retry
  (which would re-enter issuance for an order that already has a document). Filenames come from
  `IssuedInvoice.filename` (also used by both download views): the credit note gets a `-credit`
  suffix (`ABCDE-credit.pdf`) so it doesn't collide with the invoice's `ABCDE.pdf`.
  **Named `mail.py`, never `email.py`**: a module called `email.py` in this package
  shadows the stdlib `email` package for anything run with the package directory on `sys.path`,
  which is exactly what the `Makefile`'s `translate` target does (`cd pretix_ptinvoicing && python
  -c ...`). It broke `make translate` with `ModuleNotFoundError: No module named 'email.message'`.
- `pretix_ptinvoicing/views.py`:
  - `PluginEnabledMixin` on every Control-panel view (Moloni's too): 404 unless the plugin is
    enabled for the event. pretix only enforces that for `event_patterns`; these are plain
    `urlpatterns`, so without it a disabled plugin kept issuing documents. The test `event`
    fixture enables the plugin for this reason.
  - `SettingsView` (`EventSettingsViewMixin` + `EventPermissionRequiredMixin`,
    `permission = "can_change_event_settings"`) — plain `View`, not a generic `FormView`. Renders
    `ProviderSelectForm`, `EmailSettingsForm`, plus *every* registered provider's
    `settings_form_class`, each with `prefix=<provider identifier>`, and lets `settings.js` show
    only the selected provider's fieldset (the e-mail fieldset is always visible — it isn't
    per-provider). On POST, `select_form` and `email_form` are both always bound and validated, but
    **only the selected provider's form** is: otherwise a provider the admin isn't editing would
    raise validation errors for its own required fields (e.g. Fact.pt's required token) and block
    every save. Consequence: a save always writes both settings forms, but only the selected
    provider's; other providers keep whatever was stored. `provider_forms()` and
    `save_valid_fields()` are module-level so Moloni's `ConnectView` can reuse them.
  - `IndexView` (`ListView`, `permission = "can_view_orders"`) — one row per `IssuedInvoice` for the
    event, filterable by `status` (`?status=`) and order code (`?query=`) through a form with a real
    Filter button — **no inline `onchange`/`style`**: the Control panel's CSP blocks both, which had
    left the filter dead. Use pretix's `helper-*` classes for layout. The provider column renders
    `IssuedInvoice.provider_label`, which falls back to the raw stored identifier if that provider has
    since been removed from `PROVIDERS`.
  - `IssueView` (`permission = "can_change_orders"`) — **GET renders `control/confirm.html`, POST
    enqueues** `issue_invoice` for one order: issuing an official document reported to the AT
    can't be undone, so it is never one click. Keyed on the **order code**, not on an
    `IssuedInvoice` pk. One endpoint therefore covers both the
    "retry after a validation failure" case (bad NIF, missing tax mapping — something the task's own
    automatic retry deliberately won't do) *and* issuing for an order that has no row at all: one paid
    before the plugin was configured, or paid by hand in the admin. `check()` refuses up front, with
    an error message, whatever the task would silently skip — no provider, not configured, an
    attempt in flight, unpaid, already invoiced — instead of a "queued" that does nothing. Redirects
    to `next` (POSTed, or the GET's query string) when it passes `url_has_allowed_host_and_scheme`,
    else to the index — that's how the order-page panel returns the admin to the order they were
    looking at. `IssueCreditNoteView` is the same flow with another task, precondition and text.
  - `DownloadView` (`permission = "can_view_orders"`) — proxies the provider's download endpoint so
    the API token never reaches the browser. 404s if the event's *current* provider isn't the one that
    issued the row, since another provider's credentials can't fetch that document. A provider
    failure redirects back to the order page with the error, rather than a bare 404.
  - `OrderInvoiceDownloadView` — the same PDF for the **buyer**, on the presale side. No logged-in
    user there, so it authenticates the way pretix's own invoice download does: `OrderDetailMixin`
    checks the order secret in the URL. 404s when `ptinvoicing_show_in_order` is off; a provider
    failure sends the buyer back to the order page with a message. It is the only view registered through `event_patterns` in
    `urls.py` rather than a literal `control/...` path — pretix mounts a plugin's `event_patterns`
    under the event (`pretix/multidomain/maindomain_urlconf.py:63`), which is also what makes
    `{% eventurl %}` resolve it, including on a custom event domain.
  - `SettingsLookupsView` (`permission = "can_change_event_settings"`) — AJAX-only, POST-only endpoint
    backing live dropdowns on the settings page. Takes `provider` plus that provider's field values
    **straight from the POST body, prefix-stripped** (*not* from `event.settings`) so it reflects
    whatever the admin has currently typed into the form, before it's saved, then returns
    `{"fields": {<field name>: [{"id", "label"}, ...]}}`. A `ProviderError` (bad token) or
    `ProviderUnreachable` is returned as `{"error": ...}` with HTTP 400; the settings-page JS
    surfaces that in the fieldset's `.ptinvoicing-lookup-status` line (`role="status"`,
    `aria-live`) rather than failing silently.
- `pretix_ptinvoicing/urls.py` — all five views registered under
  `control/event/<organizer>/<event>/invoicing/...` (plain `urlpatterns`, not `event_patterns` —
  Control panel plugin pages use the full literal path, same convention as `pretix-eupago`'s
  settings/orders pages and pretix's own in-tree plugins). **Provider-specific URLs live in
  the provider**, as `providers/<name>/urls.py` (Moloni's OAuth connect/disconnect/callback);
  the core appends each registered provider's `urlpatterns`, found with `importlib.util.find_spec`
  (not `try/except ImportError`, which would hide a real import error inside one). They join
  the plugin namespace directly, so names reverse as `plugins:pretix_ptinvoicing:moloni_connect`.
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
  `lookupFields` so picking an option doesn't trigger another lookup — except a provider's
  `lookup_triggers` (Moloni's company, which scopes its other dropdowns), rendered as the
  fieldset's `data-lookup-triggers`; a trigger the lookup filled in by itself re-runs it once too.
  Being static, it can't call gettext: its user-facing strings come translated from the template,
  as `data-text-*` attributes on the settings `<form>`.
- The provider picker is **logo cards, not a `<select>`**: `settings.html` renders
  `ptinvoicing_provider` by hand as radio inputs (`bootstrap_form ... exclude=`), each card showing
  the provider's `logo` (a static path; `static/pretix_ptinvoicing/logos/`) and styled by
  `settings.css`. `SettingsView` preselects the posted value, else `?provider=` (the Moloni connect
  flow returns with `?provider=moloni`, so a provider being set up stays shown without being saved
  as the event's provider), else the saved one.
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
  two apart. Both clients raise `ProviderUnreachable` (deliberately *not* a `ProviderError`) for
  `requests` failures — Fact.pt's used to wrap timeouts in `FactptAPIError`, which made its
  automatic retry dead code. Code that swallows lookup errors must catch both.
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

`providers/__init__.py` is the registry: `PROVIDERS`, built by `_discover_providers()`, plus
`get_provider(event)` (reads `event.settings["ptinvoicing_provider"]`) and `provider_choices()`.
Autodiscovered, not a hand-maintained tuple: `_discover_providers()` walks the package's immediate
subpackages with `pkgutil.iter_modules(__path__)` (`base.py` is a plain module, not a package, so
`is_pkg` already excludes it without a name check), imports each, and collects every
`InvoiceProvider` subclass it finds by `inspect.getmembers`. Adding a provider is then just writing
it under `providers/<name>/` — nothing to register here by hand. `pkgutil.iter_modules` yields
subpackages in sorted (alphabetical) order, which is what keeps `PROVIDERS`' iteration order — used
to lay out the settings page's provider fieldsets — deterministic across runs; still no setuptools
entry points, this is pure filesystem discovery within the package. The base classes live in
`base.py` rather than in `__init__.py` so provider modules can import them without an import cycle
through the registry.

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
  - `build_items_block(order, event_settings)` — one inline item per `invoice_lines(order)` (every
    non-canceled position, plus every fee), with `taxId`/`unitId`/`type` all coming from the
    event's plugin settings (`factpt_default_tax_id`/`_unit_id`/`_type`), **not** derived from the
    item's own `tax_rate` — see "Known rough edges" below.
    `price` is the **net** unit price, `Line.net` (4 decimals, from gross and rate — see
    `orderdata.py` above for why not `price - tax_value`). Fact.pt applies
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

### E-mailing the documents

pretix's own order e-mails will never carry either document: the "attach invoices" machinery
attaches `order.invoices`, which are pretix's own records. So the plugin sends its own mail, from
`tasks.py` right after a successful issuance — `send_invoice_email` gated on
`ptinvoicing_email_invoice`, `send_credit_note_email` gated on `ptinvoicing_email_credit_note`,
independently. Both off by default — an organizer may not want either extra e-mail, and turning one
on after the fact is one checkbox in the settings page's "E-mail" section. Issuing manually from the
Control panel sends the mail too (for both invoice and credit note), which is the only way an
already-paid order — or an order refunded before the plugin was configured — gets a document by mail
at all.

### Why async (Celery)

`signals.py`'s `order_paid` and `ptinvoicing_refund_done` receivers only ever call `.apply_async(...)`
— all HTTP traffic to the provider happens inside the Celery worker, never in the request/response
cycle that confirms the payment or completes the refund. Same principle as `pretix-eupago`'s payment
confirmation flow: a slow or down provider must never delay the buyer-facing checkout response, and
here it must equally never delay whatever admin action (or payment-provider webhook) just completed
the refund.

### Idempotency, two layers

1. **Plugin-level fast path**: `tasks.py` looks up (or creates) the `IssuedInvoice` row for
   `(order, provider, identifier_id)` and returns immediately if it's already `STATUS_SUCCESS` —
   protects against a duplicate `order_paid` signal or two workers racing on the same order.
   **Cycles**: one invoice + its credit note. An order paid again after its invoice was
   credited (paid → refunded → paid again) needs a *new* invoice, so the key carries the
   cycle — the number of successful credit notes so far: cycle 0 keeps the original
   `pretix-<event>-<code>` (existing rows still match), later ones get `-r1`, `-r2`… A credit
   note's key is derived from the invoice it credits (`<invoice key>-credit`), and
   `issue_credit_note` targets the latest invoice not yet successfully credited. The order
   panel renders one invoice/credit-note pair per cycle plus an "Issue invoice now" row when
   the order is paid and the latest invoice is credited.
2. **Provider-level hard guarantee**: the `identifier_id` handed to `issue()`, which each provider must
   pass through to whatever field its API dedupes on (Fact.pt: `document.identifierId`). Even if step 1
   were somehow bypassed, the provider itself rejects the second document.
   **This layer is optional and a provider must declare it.** An API with no such field (Moloni) sets
   `deduplicates_issuance = False`, and `tasks.py` then refuses to auto-retry, because a retry after a
   timeout could issue a second official invoice. Layer 1 alone does not protect against that: the row
   is already `pending`, and the failed attempt cannot tell whether the document was created.

### Credit notes (refunds)

A refund is represented as a credit note, issued through the *same* `IssuedInvoice` model and
provider-agnostic task machinery as the original invoice, not a parallel concept:

- `IssuedInvoice` gained `kind` (`invoice`/`credit_note`, default `invoice`) and a self-FK
  `credits`, set only on a credit-note row, pointing at the invoice row it reverses
  (`invoice.credit_notes` is the reverse accessor). No new model — a credit note is another
  document a provider issues, referencing the original.
  `IssuedInvoice.build_credit_identifier_id(event, order)` is `build_identifier_id(...)` plus a
  `"-credit"` suffix (also truncated to 50): a distinct idempotency key from the invoice's own, so
  both rows coexist under `unique_together(provider, identifier_id)` and a retry reuses the
  credit note's row the same way a retried invoice does.
- `InvoiceProvider.credit(order, document_id, identifier_id)` is the fourth provider method,
  alongside `issue`/`download`/`lookups`. Every provider implemented so far only allows crediting a
  document's **full** value — Fact.pt's docs are explicit ("só é possível a emissão de uma Nota de
  Crédito por documento e de valor igual ao total do documento a creditar"), so there is no
  partial-refund shape anywhere in this contract. A partial refund is not representable as a credit
  note under either provider; that's a real gap, not an oversight — see rough edge #10.
- `tasks.py`'s `issue_credit_note` mirrors `issue_invoice`'s whole state machine (pending → success/
  error, `ProviderError` terminal, other exceptions retried only when
  `provider.deduplicates_issuance`) against a second `IssuedInvoice` row instead of a second model.
  It looks up the order's successful invoice row itself (`kind=KIND_INVOICE, status=STATUS_SUCCESS`)
  rather than taking a document id as an argument, so the Control panel only ever has to pass an
  order code — same shape as `issue_invoice`.
- **Auto-fired on a full refund, via a raw Django model signal, not an `EventPluginSignal`.**
  `order_paid` (used for the original invoice) has no counterpart for refunds — no pretix signal
  fires for "this refund/cancellation is worth a full credit note": `order_canceled` fires on
  cancellation regardless of whether money actually moved, and `OrderRefund` going `done` has no
  plugin signal at all (see `pretix.base.models.orders.OrderRefund.done()`). `signals.py`'s
  `ptinvoicing_refund_done` instead listens to Django's own `post_save` on `OrderRefund` — the same
  mechanism pretix's bundled `sendmail` plugin uses for its `SubEvent` hook — checking
  `instance.state == REFUND_STATE_DONE`. Unlike an `EventPluginSignal`, a raw model signal fires for
  *every* event regardless of whether this plugin is enabled there, so the receiver checks
  `"pretix_ptinvoicing" in order.event.get_plugins()` itself before doing anything.
  It only enqueues `issue_credit_note` once `_refunded_in_full(order)`: confirmed payments minus
  refunds that are **done** reach zero — i.e. once refunds have actually brought the order back to
  fully refunded, whether that took one refund or several partial ones. Deliberately **not**
  pretix's `payment_refund_sum`, which also subtracts refunds merely created or in transit: those
  can still fail, and a credit note for money never returned can't be undone. The check and the
  enqueue both run on commit, like `order_paid`'s. A partial refund that doesn't (yet) zero it out is left alone: crediting more than was
  actually refunded, just because *a* refund completed, would be wrong, and no provider here can
  credit less than a document's full value anyway (rough edge #10).
- Control panel: `order_info.html`'s per-order panel is a `table.table-condensed` — one row per
  document (invoice, then credit note if any), each with its own inline `btn-xs` actions —
  deliberately matching the shape of pretix's own "Payments" panel elsewhere on the order page
  (`table-responsive` > `table.table-condensed`, one row per item, actions inline per row, an
  error/detail line as a `colspan` sub-row underneath) rather than the earlier one-`<dl>`-per-
  document layout, which stopped reading well once a credit note could sit alongside the invoice.
  The credit-note row's "Issue credit note" / "Retry credit note" button — for a *partial* refund,
  or for crediting an order the admin has another reason to credit, regardless of pretix's own
  refund records — is enabled once the invoice is `success`, regardless of `is_paid`; crediting is
  expected to happen *after* the order stops being paid. `IssueCreditNoteView` (`.../<code>/credit/`)
  mirrors `IssueView`, sharing its `redirect_url`; it's also what `ptinvoicing_refund_done` ends up
  driving indirectly, via `issue_credit_note` — same task, whether triggered by the auto-refund hook
  or by hand.
  `IndexView`'s listing grew a "Kind" column since invoice and credit-note rows for the same order
  now share the table; its "Retry" link there branches between the `issue` and `credit` URLs by
  `invoice.kind`.
  Each row's action `<form>`s carry `style="display: inline-block"` so a form-based action (retry,
  issue) sits beside an `<a>`-based one (download) in the same cell instead of the form dropping to
  its own line: a `<form>` is block-level by default, and Bootstrap's `.form-inline` only changes
  layout *inside* the form, not the form element's own display type.
  Both `control_order_info` and `presale_order_info_top` in `signals.py` now filter their
  `IssuedInvoice` lookups by `kind=KIND_INVOICE` — without it, `.first()` (ordered `-created`) could
  return a *credit note* row as "the" invoice once one exists, since both kinds live in one table.
- Fact.pt (`providers/factpt/`): `POST /documents/{id}/credit`
  (`FactptClient.create_credit_note`) takes no client or items block — `build_credit_payload(order,
  identifier_id)` sends only `date`/`reference`/`identifierId`. Not yet exercised against a real
  Fact.pt account (issuance itself hasn't been either — see "Status" above); the shape is read
  straight off Fact.pt's own API docs page for "Criar Nota de Crédito".
- Moloni (`providers/moloni/`): has no equivalent one-call endpoint. `MoloniProvider.credit()` fetches
  the original document via the generic `documents/getOne` (not `invoiceReceipts/getOne` — its own
  `products` don't carry the line id a credit note needs), then `documents/getUnrelatedProducts` for
  the creditable line items, and calls `creditNotes/insert` with an `associated_documents:
  [{associated_id, value}]` entry and a `products` array built from those lines. Two shapes here were
  settled by reading the official Moloni WooCommerce plugin's source
  (`moloni-pt/woocommerce`, `src/Services/Orders/CreateCreditNote.php` and
  `src/Enums/DocumentTypes.php`) rather than the published docs, which don't cover them:
  - `products[].related_id` maps to each line's `document_product_id` from
    `getUnrelatedProducts` — confirmed by `CreateCreditNote.php` sending exactly
    `'related_id' => $matchedDocumentProduct['document_product_id']`. An earlier version of this
    code used `product_id` here, the closest *documented* value but not the right one.
  - `associated_documents[0].value` is hardcoded `0`, not the original's total: invoice-receipts are
    one of Moloni's "self-paid" document types (`DocumentTypes::TYPES_SELF_PAID` includes
    `invoiceReceipts`), and `CreateCreditNote.php` sends `0` as the associated value for exactly
    that case. An earlier version sent the original's `gross_value`, which is what a *non*-self-paid
    document type's credit note would need instead.
  Credit notes also need their own Moloni document set — `moloni_credit_note_document_set_id`,
  distinct from the invoice-receipt's `moloni_document_set_id` — since a Moloni series is scoped to
  one document type; reusing the invoice's series for a credit note isn't valid. Both are now
  required settings, surfaced as separate dropdowns in `lookups()` (both backed by the same
  `documentSets/getAll` call — Moloni has no per-type filter on it, so the admin picks distinct
  series from the same list).
  `customers/insert` also needs a `number` — one of several fields the docs mark required that
  `build_customer` wasn't sending — fetched via `customers/getNextNumber` and falling back to a
  random one if that call fails, matching the official plugin's own
  `OrderCustomer::getCustomerNextNumber`. `moloni_payment_method_id` is likewise now a required
  setting, not optional: invoice-receipts are in Moloni's `TYPES_WITH_PAYMENTS`, so the document
  always needs a payment recorded against it.
  Net-vs-gross `price` on `invoiceReceipts/insert` and `creditNotes/insert` remains genuinely
  unconfirmed — Moloni's docs don't state it either way, unlike the two gaps above, which had a
  confirmable answer in the official plugin's source. Confirm against a real account before trusting
  a Moloni document in production, same bar as the rest of this provider.

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

The pt_PT catalog is complete (`msgfmt --statistics`: 165 translated, 0 fuzzy, 0 untranslated).
"Provider" is "serviço de faturação", never "fornecedor": on an invoice, *fornecedor* is the
seller. "Retry issuance" is "Tentar emitir novamente", not "Reemitir" (which reads as issuing
again).
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
- Provider-agnostic tests live in `tests/test_models.py`, `tests/test_tasks.py`, `tests/test_views.py`,
  `tests/test_signals.py` (the `order_paid` and `ptinvoicing_refund_done` receivers); Fact.pt-specific
  ones in `tests/test_factpt_client.py` / `tests/test_factpt_payload.py`. A second provider should
  follow the same split.
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
genuinely provider-agnostic. **Built from Moloni's published documentation, cross-checked against
the official `moloni-pt/woocommerce` plugin's source for the shapes the docs don't fully cover, and
exercised only against mocks** — unlike the Fact.pt one, which was settled against a real sandbox
account. The two credit-note shapes the docs left ambiguous (`related_id`, and the self-paid
`associated_documents` value — see "Credit notes (refunds)" below) were settled that way; `price`
net-vs-gross was not, since the official plugin doesn't resolve it either — that one still needs a
real account.

- `client.py` — OAuth rather than a static token: `GET /v1/grant/` with the password grant,
  renewed with a 14-day refresh token, and the access token passed as a **GET parameter** on every
  call. `on_token` hands each new pair back to the provider, which caches it in `event.settings` —
  without that, every issuance would burn a fresh password grant.
- `views.py` — "Connect to Moloni", the authorization-code grant, so no password has to be
  stored (username/password stay as an optional fallback). `ConnectView` saves every *valid*
  field typed on the settings page (`save_valid_fields`: the client id/secret above all, but
  nothing else is lost to the round-trip either — never the provider choice, though: connecting
  isn't choosing) and redirects to Moloni; `DisconnectView` asks first (GET confirms, POST
  disconnects — without a password it stops issuance); `CallbackView` sits on a **global** URL
  (`control/ptinvoicing/moloni/callback/`, no event in the path) because Moloni may require an
  exact `redirect_uri` match — the event and a `state` value travel in the session instead.
  `state` is checked when Moloni echoes it; when it doesn't (undocumented either way), the
  one-shot session entry is the CSRF guard. Both of those are **still unconfirmed against a
  real Moloni account**. The settings page shows the state through the provider's
  `settings_template` (`moloni_connection.html`), with Connect using `formaction` since it
  lives inside the settings `<form>`. Connecting doesn't make Moloni the event's provider, and the
  company/series dropdowns only fill in once connected, so issuance waits on a Save that's easy
  to miss: until `MoloniProvider.in_use` (saved as the provider *and* `is_configured`), the block
  says so and the callback's success message repeats it. The redirect URI has a copy button
  (`.ptinvoicing-copy`, wired up in `settings.js`).
  The expiry e-mail is sent **only by the daily keepalive**: an issuance that finds the
  connection dead records an error row, but neither marks it expired nor e-mails.
  The refresh token lasts 14 days, so `signals.py`'s `ptinvoicing_keepalive` (`periodic_task`,
  `minimum_interval` daily) calls every selected provider's `keepalive()`; Moloni's rotates the
  refresh token. Only a refusal *from Moloni* (`MoloniAPIError` with `detail`) with no password
  to fall back to marks `moloni_connection_expired` and e-mails the event's `contact_mail`; a
  network failure (`ProviderUnreachable`) just waits for tomorrow's run.
  **Concurrent refreshes**: rotation spends the old refresh token, so when two workers (or the
  keepalive and an issuance) refresh at once the loser gets refused. Before treating that as a
  dead connection, `MoloniClient.adopt_rotated()` re-reads the stored pair (`reload_tokens`,
  bypassing the settings cache with `flush()`) and adopts it if another worker rotated it.
- `payload.py` — `customers/insert` shape plus the document's `products`/`payments` arrays, and
  `creditNotes/insert`'s shape (see "Credit notes" below). The customer is a separate object: Moloni
  takes only `customer_id`, there is no inline client block.
- Every document line needs a `product_id` — Moloni only invoices **catalog products**
  (`invoiceReceipts/insert` rejected lines without one), where Fact.pt takes lines inline.
  `MoloniProvider._resolve_product_ids` gives each pretix item one, by reference
  `pretix-item-<item.pk>` — and each fee type one, `pretix-fee-<fee_type>` (keyed on
  `Line.catalog_key`): `products/getByReference` (exact; a failed lookup raises rather than
  inserting a duplicate), else `products/insert` — the
  official WooCommerce plugin's approach. Created with the event's `moloni_product_category_id`
  (top-level categories only in the dropdown), `moloni_product_type` (service/product) and
  `moloni_unit_id`, plus the tax/exemption settings; `products/insert` wants the tax's rate
  `value` too, fetched from `taxes/getAll` only when a product is actually created. Item pks are
  install-wide unique, so events sharing a Moloni company share products per item, not per event.
- `__init__.py` — `MoloniSettingsForm` (thirteen fields, four of them credentials) and
  `MoloniProvider`. `_next_customer_number()` calls `customers/getNextNumber` before every
  `customers/insert` — one of several fields Moloni's docs mark required on that endpoint that
  earlier versions of this provider weren't sending; falls back to a random number if the call fails,
  matching the official plugin's `OrderCustomer::getCustomerNextNumber` rather than letting a lookup
  hiccup block the whole issuance. `customers/insert` also needs `maturity_date_id` (its own
  required setting, a real id) and `payment_method_id` (reuses the document's), and a **real
  account rejected** the absence of `salesman_id`/`payment_day`/`discount`/`credit_limit`/
  `delivery_method_id` even though the docs mark them optional — `build_customer` sends 0 for each.
  `_resolve_customer_id` always looks the buyer up with `customers/getByVat` first, and a failed
  lookup raises instead of falling through to insert. With a NIF: one match is reused, several
  stop issuance ("merge them in Moloni") — inserting another would only add to the duplicates.
  Without one: the lowest-id 999999990 customer with the same name is reused, Fact.pt's
  final-consumer approach, so buyers don't each get a new customer. `country_id` comes from
  `countries/getAll` by ISO code for anyone outside Portugal — never defaulted to Portugal.
  Its `lookups()` returns the company list plus tax exemptions (global), and once a company is set,
  both document sets, tax, payment method and maturity date — same `{field: [{id, label}]}`
  contract.
- Line taxes follow the rate **pretix actually charged** on each position (`payload.line_tax`, used
  by both document lines and `products/insert`): a taxed line sends `moloni_tax_id` **with its rate
  as `value`** (a real account rejected line taxes without one, "must be float, greater than 0"),
  a 0% line sends no tax at all but `moloni_exemption_reason`. `MoloniProvider._check_taxes` runs
  first, before anything is created in Moloni, and refuses on a missing exemption reason or a
  rate that differs from pretix's — Fact.pt's `_check_tax_rate_matches` guard, for the same
  reason (net price + the provider's own rate = a different total than the buyer paid).
- `client.py`'s `call()` sends `human_errors=true` and treats a **list** body as a validation
  error — Moloni returns those with HTTP 200. From a write (anything not `get*`) **any** list is
  an error, whatever its entries look like (a real `invoiceReceipts/insert` rejection matched
  neither documented form); from a read only bare `"1 name"` strings or exact
  `{code, description}` dicts count, so a `getAll`'s rows aren't mistaken for errors. Before
  this, rejections crashed with `'list' object has no attribute 'get'`. Rejected bodies are
  logged at INFO.
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
10. **Partial refunds have no credit-note representation.** Both providers only support crediting a
    document's full value (Fact.pt says so explicitly; Moloni's `creditNotes/insert` credits every
    line `getUnrelatedProducts` returns). `ptinvoicing_refund_done` (see "Credit notes" above) only
    auto-issues once done refunds add up to everything paid —
    a single partial refund leaves an order with no provider-side credit-note document, silently, by
    design, until either a later refund completes it or the admin issues one by hand from the
    Control panel. The admin has to judge whether a full credit note (crediting more than was
    actually refunded by that point) or no document at all is the lesser problem, same spirit as
    rough edge #1's tax-mismatch stance: fail visibly rather than issue something wrong silently. A
    future fix would need pretix-tax-rule-style partial support from the providers themselves, which
    Fact.pt's API v1.0.0 doesn't offer.
11. **A Fact.pt document created by a call that then timed out isn't linked back.** The retry is
    rejected as a duplicate `identifierId` — so no second invoice, which is the point — but the
    row stays `error` with no `document_id`, and the admin has to find the document in Fact.pt.
    Recovering it needs a lookup by `identifierId`, and no such endpoint is documented.

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
