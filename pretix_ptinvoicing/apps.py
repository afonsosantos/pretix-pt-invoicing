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
        description = gettext_lazy(
            "Issues AT-certified invoice-receipts through a Portuguese invoicing "
            "provider (currently Fact.pt) asynchronously after payment confirmation, "
            "with a tracking and retry panel."
        )
        visible = True
        version = __version__
        category = "INTEGRATION"
        compatibility = "pretix>=2024.1.0"

    def ready(self):
        from . import signals  # NOQA
