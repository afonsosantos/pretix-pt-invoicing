import importlib
import inspect
import pkgutil

from .base import InvoiceProvider, IssuedDocument, ProviderError

__all__ = [
    "PROVIDERS",
    "InvoiceProvider",
    "IssuedDocument",
    "ProviderError",
    "get_provider",
    "provider_choices",
]


def _discover_providers():
    """
    Import every subpackage under providers/ and collect its InvoiceProvider subclass.

    Adding a provider is then just writing it under providers/<name>/ — nothing to
    register by hand here. `base.py` is a plain module, not a package, so `is_pkg` already
    excludes it. pkgutil.iter_modules yields subpackages in sorted (alphabetical) order,
    so PROVIDERS' iteration order — which the settings page uses to lay out its provider
    fieldsets — stays deterministic across runs.
    """
    found = {}
    for _, name, is_pkg in pkgutil.iter_modules(__path__):
        if not is_pkg:
            continue
        module = importlib.import_module(f".{name}", __name__)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, InvoiceProvider)
                and obj is not InvoiceProvider
                and obj.identifier
            ):
                found[obj.identifier] = obj
    return found


PROVIDERS = _discover_providers()


def get_provider(event):
    """The provider configured for this event, or None if none is selected."""
    cls = PROVIDERS.get(event.settings.get("ptinvoicing_provider") or "")
    return cls(event) if cls else None


def provider_choices():
    return [(identifier, p.verbose_name) for identifier, p in PROVIDERS.items()]
