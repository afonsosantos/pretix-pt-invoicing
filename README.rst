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
Portuguese electronic invoicing provider, automatically once an order is paid, and a
**credit note** once it's fully refunded — with a Control-panel dashboard to track each
issuance and retry the ones that failed.

**Documentation: https://docs.afonsosantos.me/pt-invoicing/** — configuration, providers,
credit notes, troubleshooting and writing a new provider, in English and Portuguese.

============ =========== ==================================================================
Provider     Identifier  Status
============ =========== ==================================================================
`Fact.pt`_   ``factpt``  Verified against a real sandbox account
`Moloni`_    ``moloni``  Built from the published API docs, **not yet run against a real
                         account** — verify before production use
============ =========== ==================================================================

* PyPI: https://pypi.org/project/pretix-pt-invoicing/
* Source: https://github.com/afonsosantos/pretix-pt-invoicing
* Issues: https://github.com/afonsosantos/pretix-pt-invoicing/issues

Installation
------------

Install the plugin into the same Python environment as your pretix instance, then apply its
migration::

    pip install pretix-pt-invoicing
    python -m pretix migrate

Restart pretix and enable **Portuguese invoicing** for an event under **Settings → Plugins**.
Then follow the `configuration guide`_.

Development
-----------

With a working `pretix development setup`_ and its virtualenv active::

    pip install -e .    # or: uv sync --extra test
    make                # compile translations
    make test
    make lint

See the `development guide`_ and `writing a provider`_.

License
-------

Copyright 2026 Afonso Santos

Released under the terms of the Apache License 2.0, see the LICENSE file for details.

.. _pretix: https://github.com/pretix/pretix
.. _Fact.pt: https://fact.pt
.. _Moloni: https://moloni.pt
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
.. _configuration guide: https://docs.afonsosantos.me/pt-invoicing/configuration/
.. _development guide: https://docs.afonsosantos.me/development/
.. _writing a provider: https://docs.afonsosantos.me/pt-invoicing/providers/
