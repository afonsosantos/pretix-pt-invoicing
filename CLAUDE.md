# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pretix-factpt` is a standalone, pip-installable [pretix](https://github.com/pretix/pretix) plugin
(importable as `pretix_factpt`) that issues AT-certified **Fatura-Recibo** invoices via the
[Fact.pt](https://api.fact.pt) API once a pretix order is paid. Structured the same way as this
author's other pretix plugin, `pretix-eupago` (uv + pyproject.toml, pytest against a real pretix
install, ruff, GitHub Actions CI, trusted-publishing PyPI release) — see that project for the pattern
this one follows, if it's checked out alongside this one.

**Status**: verified against a real pretix dev instance (`manage.py check`, migration applies cleanly)
and covered by a 19-test suite, but not yet exercised against a real Fact.pt account. See "Known rough
edges" below before relying on this in production.

## Architecture

- `pretix_factpt/__init__.py` — declares `__version__`, guards against `pip install` outside a pretix
  environment, and re-exports `PluginApp` from `apps.py`.
- `pretix_factpt/apps.py` — `PluginApp` (subclasses `pretix.base.plugins.PluginConfig`) declares
  `PretixPluginMeta` (category `INTEGRATION`, `compatibility = "pretix>=2024.1.0"`) and hooks up
  `signals.py` in `ready()`. **Must stay a real submodule**, not be inlined into `__init__.py` — same
  reason as in `pretix-eupago`'s `CLAUDE.md`: pretix's plugin loader only appends the entry point's
  *module* portion to `INSTALLED_APPS`, and Django's app-config autodiscovery for a bare app name looks
  for `apps.py`, not `__init__.py`.
- `pretix_factpt/models.py` — `FactptInvoice`: one row per issuance *attempt* against an `Order`
  (`order` FK, not one-to-one — a retry after a network failure reuses the same row via
  `get_or_create(order=..., identifier_id=...)`, but a config change that alters `identifier_id`
  would start a new row). Tracks `status` (`pending`/`success`/`error`), the Fact.pt response
  (`factpt_document_id`, `factpt_link`, `permanent_url`), the error (`error_message`, `error_detail` —
  the raw `AppResponse.errors` dict), and `attempts`.
- `pretix_factpt/signals.py` — `order_paid` receiver enqueues `generate_factpt_invoice` on Celery;
  `nav_event`/`nav_event_settings` receivers add the Control-panel sidebar entries, gated on
  `can_view_orders` / `can_change_event_settings` respectively.
- `pretix_factpt/tasks.py` — `generate_factpt_invoice` (Celery task, `bind=True, max_retries=3,
  default_retry_delay=120`) does the actual work: loads the `Order`, bails out silently if
  `factpt_token` isn't configured for the event, builds the idempotency key via
  `payload.build_identifier_id`, `get_or_create`s the `FactptInvoice` row, and short-circuits if it's
  already `STATUS_SUCCESS`. A `FactptAPIError` (validation-type failure from Fact.pt — bad NIF, missing
  tax mapping, etc.) is terminal: marks the invoice `error` and returns, since retrying an API-level
  rejection without a config/data fix won't help. Any other exception (network/infra) marks `error`
  too but also `raise self.retry(exc=e)`, since those are expected to be transient.
- `pretix_factpt/client.py` — `FactptClient`, a thin `requests` wrapper over the Fact.pt REST API.
  Picks `http://api.sandbox.fact.pt` vs `https://api.fact.pt` from the `sandbox` constructor arg.
  `FactptAPIError` carries the raw `AppResponse.errors` dict (`errors`) alongside a human `.as_text()`,
  so the admin panel can show exactly what Fact.pt rejected.
- `pretix_factpt/payload.py` — pure functions mapping a pretix `Order` to a Fact.pt request body:
  - `build_identifier_id(event, order)` — `f"pretix-{event.slug}-{order.code}"[:50]`, sent as
    `document.identifierId`. Fact.pt rejects a second document with the same `identifierId`, which is
    the actual duplicate-issuance guard (the plugin-level `FactptInvoice.status == success` check in
    `tasks.py` is a fast-path in front of that, not a replacement for it).
  - `build_client_block(order)` — reads `order.invoice_address` (a `OneToOneField`, so it can be
    `None`). No NIF → `finalConsumer: true`; NIF present → strips a leading `"PT"` (Fact.pt's `tin`
    field wants the bare number) and sets `finalConsumer: false`.
  - `build_items_block(order, event_settings)` — one inline item per `order.positions.all()`
    (pretix's non-canceled-positions manager), with `taxId`/`unitId`/`type` all coming from the
    event's plugin settings (`factpt_default_tax_id`/`_unit_id`/`_type`), **not** derived from the
    item's own `tax_rate` — see "Known rough edges" below.
- `pretix_factpt/forms.py` — `FactptSettingsForm(SettingsForm)`: `factpt_token`, `factpt_sandbox`,
  `factpt_default_tax_id`, `factpt_default_unit_id`, `factpt_default_type`. Field names are already
  prefixed `factpt_` so they land unprefixed in `event.settings` (pretix's `SettingsForm`/`HierarkeyForm`
  storage keys off the field name, not the Django form `prefix`) — the `prefix="factpt"` passed to this
  form in `views.SettingsView` only affects the rendered `<input name=...>`, not the storage key. Keep
  that `prefix` argument consistent between the view's `GET` and `POST` handling (it already is) if you
  touch this — otherwise submitted values won't bind.
- `pretix_factpt/views.py`:
  - `SettingsView` (`EventSettingsViewMixin` + `EventPermissionRequiredMixin`,
    `permission = "can_change_event_settings"`) — plain `View`, not a generic `FormView`; renders/parses
    `FactptSettingsForm` by hand.
  - `IndexView` (`ListView`, `permission = "can_view_orders"`) — one row per `FactptInvoice` for the
    event, filterable by `status` via `?status=`.
  - `RetryView` (`permission = "can_change_orders"`) — re-enqueues `generate_factpt_invoice` for an
    existing invoice's order; this is for *validation* failures (bad NIF, missing tax mapping) that need
    a config/data fix before retrying, as opposed to the task's own automatic retry for transient
    network errors.
  - `DownloadView` (`permission = "can_view_orders"`) — proxies `GET /documents/{id}/download` so the
    Fact.pt token never reaches the browser.
  - `SettingsLookupsView` (`permission = "can_change_event_settings"`) — AJAX-only, POST-only endpoint
    backing the live VAT-rate/unit dropdowns on the settings page. Takes `token`/`sandbox` straight from
    the POST body (**not** `event.settings`) so it reflects whatever the admin has currently typed into
    the form, before it's saved — calls `FactptClient.list_taxes()`/`list_units()` and returns
    `{"taxes": [...], "units": [...]}` (each item `{"id": ..., "label": ...}`, via the `_describe()`
    helper which tries `name`/`designation`/`description` in turn — the exact key Fact.pt's list
    endpoints use for the human-readable label hasn't been confirmed against a live account). A
    `FactptAPIError` (bad token, unreachable) is returned as `{"error": ...}` with HTTP 400; the
    settings-page JS surfaces that in a `#factpt-lookup-status` line under the sandbox toggle rather than
    failing silently — a bad/not-yet-valid token just leaves the plain number inputs in place.
  - `factpt_default_tax_id`/`factpt_default_unit_id` stay plain `IntegerField`s in `forms.py` — the
    dropdown is a pure client-side enhancement (`static/pretix_factpt/settings.js` swaps the rendered
    `<input type=number>` for a `<select>` with the same `name`/`id` once a lookup succeeds), not a
    `ChoiceField`. **Must be a real static file, not an inline `<script>` in the template** — pretix's
    Control panel sends a nonce-based CSP (`script-src 'nonce-...' 'self' ...`) that silently blocks any
    inline script without a matching `nonce` attribute; every pretix core plugin with page JS (e.g.
    `banktransfer`) ships it as `static/<plugin>/*.js` loaded via `{% static %}` for exactly this reason
    — `'self'` covers same-origin script files with no nonce needed. `settings.js` derives the lookups
    URL from `window.location.pathname` (current page + `lookups/`) rather than a Django `{% url %}` tag,
    since a plain static file has no template context to pull that from.
    Deliberately not a `TypedChoiceField`: that would validate
    the submitted value against choices computed at *render* time, which would break saving a
    previously-set value on any page load where the live Fact.pt lookup fails (network hiccup, Fact.pt
    down) — the plain `IntegerField` keeps that path working regardless of API availability.
- `pretix_factpt/urls.py` — all five views registered under
  `control/event/<organizer>/<event>/factpt/...` (plain `urlpatterns`, not `event_patterns` — Control
  panel plugin pages use the full literal path, same convention as `pretix-eupago`'s settings/orders
  pages and pretix's own in-tree plugins).
- `pretix_factpt/migrations/0001_initial.py` — the one migration for `FactptInvoice`. Depends on
  `pretixbase.0001_initial` (the `Order` FK target).

### Why async (Celery)

`signals.py`'s `order_paid` receiver only calls `.apply_async(...)` — all HTTP traffic to Fact.pt
happens inside the Celery worker, never in the request/response cycle that confirms the payment. Same
principle as `pretix-eupago`'s payment confirmation flow: a slow or down Fact.pt must never delay the
buyer-facing checkout response.

### Idempotency, two layers

1. **Plugin-level fast path**: `tasks.py` looks up (or creates) the `FactptInvoice` row for
   `(order, identifier_id)` and returns immediately if it's already `STATUS_SUCCESS` — protects against
   a duplicate `order_paid` signal or two workers racing on the same order.
2. **Fact.pt-level hard guarantee**: `identifierId` in the request body. Even if step 1 were somehow
   bypassed, Fact.pt itself rejects a second document with the same `identifierId` for the same
   `document.reference`+account.

### Displaying amounts

`payload.build_items_block` sends `str(position.price)` as `price` — a plain-decimal string, which is
what the Fact.pt API expects in the request body. Don't confuse this with rendering money for *display*
(e.g. in `templates/pretix_factpt/control/index.html`): pretix's `{% load money %}` /
`|money:event.currency` template filter is still the right tool there, same convention as
`pretix-eupago`.

## Translations

`pretix_factpt/locale/<lang>/LC_MESSAGES/` holds gettext catalogs (currently `pt_PT`, the language the
UI strings originally shipped in before they were moved to English source strings + this catalog).
Since `pretix_factpt` is a real Django app, Django's i18n machinery discovers this automatically — no
pretix-specific wiring needed, `{% load i18n %}` / `_()` / `gettext_lazy()` calls throughout the
codebase just work once a `.mo` file exists.

- `make translate` — extracts `_()`/`gettext_lazy()`/`{% trans %}` strings into
  `locale/<lang>/LC_MESSAGES/django.po` (merging into any existing translations).
- `make compile-translations` — compiles `.po` → `.mo`. **Must run before `uv build`** — only
  `.mo` files are loaded at runtime, and `package-data` in `pyproject.toml` just ships whatever's
  already on disk; it doesn't compile anything.
- To add a language: `mkdir -p pretix_factpt/locale/<lang>/LC_MESSAGES`, add it to `LOCALES` in
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
  disabled unless `GITHUB_WORKFLOW` is set — inherited from pretix core, not controlled here).
- `pyproject.toml` sets `pythonpath = ["."]` under `[tool.pytest.ini_options]` — required so
  `tests.settings` imports correctly regardless of how `pytest` is invoked (see `pretix-eupago`'s
  `CLAUDE.md` for the exact failure mode this avoids).
- `tests/conftest.py` provides `organizer`/`event`/`order`/`item`/`position` fixtures; all model
  creation happens inside `with scopes_disabled():` (`django_scopes`) since fixtures run without an
  active scope.
- HTTP calls to Fact.pt (`requests` in `client.py`) are mocked with `responses`
  (`@responses.activate` + `responses.add(...)`), not `unittest.mock` — matches pretix core's own test
  suite convention.
- `tests/test_views.py` exercises `SettingsView` through the real Control-panel URL with the Django
  test `client`, including a permission check (no access → `404`, not `403`, matching pretix's own
  convention of not revealing a resource exists). Because `SettingsView` renders `FactptSettingsForm`
  with `prefix="factpt"`, POSTed field names in tests need that prefix too
  (`"factpt-factpt_token"`, not `"factpt_token"`) — see the `forms.py` note above.
- Because the plugin is installed (even `-e` editable) into the same environment pytest runs in, its
  `pretix.plugin` entry point is picked up automatically — no manual `INSTALLED_APPS` wiring needed.

## Plugin registration mechanics

Same as `pretix-eupago`: discovered via the `pretix.plugin` setuptools entry point in `pyproject.toml`:

```toml
[project.entry-points."pretix.plugin"]
pretix_factpt = "pretix_factpt:PluginApp"
```

The entry point's *module* portion must resolve to a package with its own `apps.py` — see
"Architecture" above.

## Known rough edges (from the original skeleton)

Confirm the following against a real Fact.pt account before production use — these were written from
the API docs, not verified against live traffic:

1. **VAT rate mapping is one fixed `factpt_default_tax_id` per event.** If an event sells items at
   different VAT rates, `build_items_block` needs a pretix-tax-rate → Fact.pt-`taxId` mapping instead
   of a single value.
2. **`taxId`/`unitId` come from the settings-page dropdowns** (`SettingsLookupsView`, populated from
   `GET /support/api?c=lists&s=taxes` / `...&s=product_unit`), but the label field the code reads off
   each item (`_describe()` in `views.py`, trying `name`/`designation`/`description`) hasn't been
   confirmed against a real account — if the dropdowns render with blank or `"None"` labels, that's the
   first thing to check.
3. **Two different retry paths, don't conflate them**: `tasks.py`'s `self.retry()` (3 attempts, 120s
   apart) is only for network/infra exceptions; the Control-panel "Retry" button
   (`RetryView`) is for `FactptAPIError`s that need a data/config fix first.

## Commands

To manually exercise changes against a real pretix instance:

```bash
# From a pretix dev checkout with its virtualenv activated:
pip install -e /path/to/pretix-factpt
cd <pretix>/src
python manage.py migrate    # applies pretix_factpt's own migration
python manage.py runserver
```

Then enable "Fact.pt" under an event's Settings → Plugins tab, configure the token/sandbox toggle and
default tax/unit IDs under the event's Fact.pt settings page, and check the Fact.pt sidebar entry for
the issuance panel.

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
release.

Bump the version in both `pyproject.toml` and `pretix_factpt/__init__.py` via `make bump-version
<version>` rather than by hand, then `uv lock` to sync `uv.lock`'s own entry.
