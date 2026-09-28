pretix-pt-invoicing
===================

.. image:: https://img.shields.io/pypi/v/pretix-pt-invoicing.svg
   :target: https://pypi.org/project/pretix-pt-invoicing/
   :alt: PyPI version

.. image:: https://img.shields.io/pypi/pyversions/pretix-pt-invoicing.svg
   :target: https://pypi.org/project/pretix-pt-invoicing/
   :alt: Supported Python versions

.. image:: https://img.shields.io/pypi/l/pretix-pt-invoicing.svg
   :target: https://github.com/afonsosantos/pretix-pt-invoicing/blob/main/LICENSE
   :alt: License

.. image:: https://github.com/afonsosantos/pretix-pt-invoicing/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/afonsosantos/pretix-pt-invoicing/actions/workflows/ci.yml
   :alt: CI status

This is a plugin for `pretix`_ that issues AT-certified **invoice-receipts** through a
Portuguese electronic invoicing provider, automatically once an order is paid — with a
Control-panel dashboard to track each issuance and retry the ones that failed.

The pretix-facing half (the ``order_paid`` hook, the Celery task, idempotency, the tracking
model, the dashboard and the settings page) is provider-agnostic; each provider only supplies
its own API client, payload mapping and settings fields.

Providers
---------

============ =========== ==================================================================
Provider     Identifier  Status
============ =========== ==================================================================
`Fact.pt`_   ``factpt``  Implemented, verified against a real sandbox account
Moloni       ``moloni``  Implemented from the published API docs, **not yet run against a
                         real account** — verify before production use
============ =========== ==================================================================

One provider is active per event.

* PyPI: https://pypi.org/project/pretix-pt-invoicing/
* Source: https://github.com/afonsosantos/pretix-pt-invoicing
* Issues: https://github.com/afonsosantos/pretix-pt-invoicing/issues

Status
------

Verified against a real pretix instance and covered by a test suite. For the Fact.pt
provider, the ``GET /taxes`` lookup has been exercised against a real account; invoice
issuance itself hasn't — check the "Known rough edges" section of ``CLAUDE.md`` before
relying on this in production.

Installation
------------

Install the plugin into the same Python environment as your pretix instance::

    pip install pretix-pt-invoicing

Then restart your pretix server. The plugin registers itself with pretix's plugin registry
automatically via a setuptools entry point — no changes to ``INSTALLED_APPS`` are needed. You can now
enable it for an event under the "plugins" tab in that event's settings.

.. note::

   Upgrading from ``pretix-factpt`` 0.1.x: the package was renamed, so uninstall
   ``pretix-factpt`` first. Its table (``pretix_factpt_factptinvoice``) is *not* migrated —
   the new app starts with an empty issuance log, and your existing invoices stay in your
   provider's account, untouched. Re-select the provider under the event's invoicing settings
   after upgrading; the Fact.pt credentials you already stored are read under the same
   setting names.

Development setup
------------------

1. Make sure that you have a working `pretix development setup`_.

2. Clone this repository.

3. Activate the virtual environment you use for pretix development.

4. Execute ``pip install -e .`` (or, if you use `uv`_, ``uv sync --extra test``) within this directory
   to register this application with pretix's plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local pretix server. You can now use the plugin from this repository for your events by
   enabling it in the 'plugins' tab in the settings.

Run ``make test`` to run the test suite and ``make lint`` to run `ruff`_.

Configuration
-------------

Enable the plugin for an event, then open **Settings → Invoicing** and pick an invoicing
provider. Only the selected provider's fields are saved and used for issuance.

For **Fact.pt**:

* **API token (x-auth-token)** — your Fact.pt API token, sandbox or production.
* **Use sandbox environment** — issue against Fact.pt's sandbox instead of production.
* **VAT rate** — a dropdown populated live from your Fact.pt account (``GET /taxes``) as soon as a
  valid API token is entered, applied to every line of every invoice issued for this event.
* **Unit** — Fact.pt's fixed product-unit list (Units / Meters / Boxes / Kilograms / Liters).
* **Item type** — ``service`` or ``product``, per Fact.pt's own item model.
* **Send the buyer's e-mail address to Fact.pt** — off by default; when on, the buyer's e-mail is
  stored on the Fact.pt client record, which lets Fact.pt send them the document.

The configured VAT rate must match the rate pretix charged on the order: Fact.pt is sent the *net*
line price and applies its own rate on top, so a mismatch would issue a document for an amount the
buyer never paid. Issuance refuses, with an explicit error, rather than letting that happen.

Once an order is paid, an invoice-receipt is issued asynchronously via Celery — see the
"Invoicing" entry in the event's Control-panel sidebar for the issuance dashboard, including a
"Retry" action for any issuance that failed.

Two provider-independent options sit above the provider's own settings:

* **E-mail the invoice to the buyer** — off by default. Sends the issued document as a PDF
  attachment once it is issued. pretix's own order e-mails can't carry it: they attach pretix's own
  invoice records, and the provider's document isn't one.
* **Show the invoice on the buyer's order page** — on by default. Adds a download link next to
  pretix's own invoice list, served through the plugin so the provider's API token never leaves the
  server. It is authenticated by the order secret, like every other buyer-facing order link.

Every order's page in the Control panel also gets an **Invoicing** panel showing what was issued for
that order, with an **Issue invoice now** button. Use it for orders that were paid before the plugin
was configured, for orders marked as paid by hand, or to re-run an issuance the provider rejected once
the underlying data is fixed. Unpaid orders are never issued, whichever route triggers it.

Adding a provider
-----------------

Write one package under ``pretix_ptinvoicing/providers/<name>/`` with a subclass of
``providers.base.InvoiceProvider``:

* ``identifier`` / ``verbose_name`` — the settings value and the label shown to admins.
* ``settings_form_class`` — a pretix ``SettingsForm`` with this provider's credentials and
  defaults. Field names must be namespaced by hand (e.g. ``moloni_client_id``), since pretix
  keeps all event settings in one flat namespace.
* ``is_configured`` — ``False`` while the provider isn't set up; issuance then skips silently.
* ``issue(order, identifier_id)`` — return an ``IssuedDocument``. Pass ``identifier_id``
  through to whatever field the API dedupes on. Raise ``ProviderError`` for a rejection the
  provider won't accept on retry; let transient failures propagate so the task retries them.
* ``download(document_id)`` — the document's PDF bytes.
* ``lookups(data)`` — optional; ``{field_name: [{"id", "label"}, ...]}`` to turn settings
  fields into live dropdowns, using the values the admin has currently typed in.
* ``deduplicates_issuance`` — whether the API itself rejects a second document for the same
  ``identifier_id``. Leave it ``True`` only if that is really the case: it is what allows an
  automatic retry after a network failure. Set it ``False`` for an API with no such field, and
  a timed-out issuance is then left for a human to retry from the Control panel.

``self.settings`` is the event's settings store and is writable, so a provider with expiring
credentials can cache a refreshed token there.

Then add the class to ``PROVIDERS`` in ``pretix_ptinvoicing/providers/__init__.py``. That's the
whole registration story — no entry points, no autodiscovery, no migration.

License
-------

Copyright 2026 Afonso Santos

Released under the terms of the Apache License 2.0, see the LICENSE file for details.

.. _pretix: https://github.com/pretix/pretix
.. _Fact.pt: https://fact.pt
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
.. _uv: https://docs.astral.sh/uv/
.. _ruff: https://docs.astral.sh/ruff/
