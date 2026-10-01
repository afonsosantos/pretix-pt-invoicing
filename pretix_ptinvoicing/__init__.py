try:
    from pretix.base.plugins import PluginConfig  # NOQA
except ImportError:
    raise RuntimeError("Please, don't run pip install directly. Use pip install -e .")

__version__ = "0.6.0"

from .apps import PluginApp  # NOQA isort:skip
