pretix-factpt
==============

.. image:: https://img.shields.io/pypi/v/pretix-factpt.svg
   :target: https://pypi.org/project/pretix-factpt/
   :alt: PyPI version

.. image:: https://img.shields.io/pypi/pyversions/pretix-factpt.svg
   :target: https://pypi.org/project/pretix-factpt/
   :alt: Supported Python versions

.. image:: https://img.shields.io/pypi/l/pretix-factpt.svg
   :target: https://github.com/afonsosantos/pretix-factpt/blob/main/LICENSE
   :alt: License

.. image:: https://github.com/afonsosantos/pretix-factpt/actions/workflows/ci.yml/badge.svg
   :target: https://github.com/afonsosantos/pretix-factpt/actions/workflows/ci.yml
   :alt: CI status

This is a plugin for `pretix`_ that issues AT-certified **invoice-receipts** via `Fact.pt`_,
a Portuguese electronic invoicing provider, automatically once an order is paid — with a
Control-panel dashboard to track each issuance and retry the ones that failed.

* PyPI: https://pypi.org/project/pretix-factpt/
* Source: https://github.com/afonsosantos/pretix-factpt
* Issues: https://github.com/afonsosantos/pretix-factpt/issues

Installation
------------

Install the plugin into the same Python environment as your pretix instance::

    pip install pretix-factpt

Then restart your pretix server. The plugin registers itself with pretix's plugin registry
automatically via a setuptools entry point — no changes to ``INSTALLED_APPS`` are needed. You can now
enable it for an event under the "plugins" tab in that event's settings.

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

Enable the plugin for an event, then configure it under **Settings → Fact.pt**:

* **API token (x-auth-token)** — your Fact.pt API token, sandbox or production.
* **Use sandbox environment** — issue against Fact.pt's sandbox instead of production.
* **VAT rate ID** and **Unit ID** — dropdowns populated live from your Fact.pt account
  (``GET /support/api?c=lists&s=taxes`` / ``...&s=product_unit``) as soon as a valid API token is
  entered, applied to every line of every invoice issued for this event.
* **Item type** — ``service`` or ``product``, per Fact.pt's own item model.

Once an order is paid, an invoice-receipt is issued asynchronously via Celery — see the "Fact.pt"
entry in the event's Control-panel sidebar for the issuance dashboard, including a "Retry" action for
any issuance that failed.

License
-------

Copyright 2026 Afonso Santos

Released under the terms of the Apache License 2.0, see the LICENSE file for details.

.. _pretix: https://github.com/pretix/pretix
.. _Fact.pt: https://fact.pt
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
.. _uv: https://docs.astral.sh/uv/
.. _ruff: https://docs.astral.sh/ruff/
