from django.utils.translation import gettext_lazy
from pretix.base.plugins import PluginConfig

from . import __version__


class PluginApp(PluginConfig):
    default = True
    name = "pretix_ptinvoicing"
    verbose_name = "Portuguese invoicing"

    class PretixPluginMeta:
        name = gettext_lazy("Portuguese invoicing")
        author = "Afonso Santos"
        # Names every provider under providers/: keep in step when one is added.
        description = gettext_lazy(
            "Issues AT-certified invoice-receipts and credit notes through a "
            "Portuguese invoicing provider (Fact.pt or Moloni) asynchronously after "
            "payment confirmation, with a tracking and retry panel."
        )
        visible = True
        version = __version__
        category = "INTEGRATION"
        compatibility = "pretix>=2024.1.0"

    def ready(self):
        from . import signals  # NOQA
