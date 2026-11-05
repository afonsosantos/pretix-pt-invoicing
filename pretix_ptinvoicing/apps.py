from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy
from pretix.base.plugins import PluginConfig

from . import __version__

# The plugin's documentation, linked from its card on the plugins page and from the
# settings pages. Translatable: the site has a version per language.
DOCS_URL = gettext_lazy("https://docs.afonsosantos.me/pt-invoicing/")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_ptinvoicing"
    verbose_name = "Portuguese invoicing"

    class PretixPluginMeta:
        name = gettext_lazy("Portuguese invoicing")
        author = "Afonso Santos"
        # Names every provider under providers/: keep in step when one is added. pretix
        # renders the description as HTML, which is what lets it end in a link.
        description = format_lazy(
            '{} <a href="{}" target="_blank" rel="noopener">{}</a>',
            gettext_lazy(
                "Issues AT-certified invoice-receipts and credit notes through a "
                "Portuguese invoicing provider (Fact.pt or Moloni) asynchronously after "
                "payment confirmation, with a tracking and retry panel."
            ),
            DOCS_URL,
            gettext_lazy("Documentation"),
        )
        visible = True
        version = __version__
        category = "INTEGRATION"
        compatibility = "pretix>=2024.1.0"

    def ready(self):
        from . import signals  # NOQA
