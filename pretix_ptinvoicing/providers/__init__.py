from .base import InvoiceProvider, IssuedDocument, ProviderError
from .factpt import FactptProvider
from .moloni import MoloniProvider

# Adding a provider: write it under providers/<name>/ and add it here. That's the whole
# registration story — no entry points, no autodiscovery.
PROVIDERS = {p.identifier: p for p in (FactptProvider, MoloniProvider)}

__all__ = [
    "PROVIDERS",
    "InvoiceProvider",
    "IssuedDocument",
    "ProviderError",
    "get_provider",
    "provider_choices",
]


def get_provider(event):
    """The provider configured for this event, or None if none is selected."""
    cls = PROVIDERS.get(event.settings.get("ptinvoicing_provider") or "")
    return cls(event) if cls else None


def provider_choices():
    return [(identifier, p.verbose_name) for identifier, p in PROVIDERS.items()]
