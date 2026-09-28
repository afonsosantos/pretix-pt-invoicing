.PHONY: install test lint format bump-version translate compile-translations

install:
	uv sync --extra test

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

# Extract translatable strings into pretix_ptinvoicing/locale/<lang>/LC_MESSAGES/django.po.
# Needs GNU gettext (xgettext/msguniq/msgmerge) installed. To add a new language, first
# create its directory, e.g.: mkdir -p pretix_ptinvoicing/locale/de/LC_MESSAGES
#
# Deliberately configures a bare Django settings object instead of using pretix's own
# settings module: pretix.settings sets LOCALE_PATHS to pretix core's own bundled locale
# directory, and makemessages/compilemessages happily write/recompile *every* locale they
# find there too if it's on that path — i.e. running this against pretix's settings
# corrupts the installed pretix package's own translations with this plugin's strings.
LOCALES = pt_PT
translate:
	cd pretix_ptinvoicing && $(CURDIR)/.venv/bin/python3 -c "\
import django; \
from django.conf import settings; \
settings.configure(USE_I18N=True); \
django.setup(); \
from django.core.management import call_command; \
call_command('makemessages', locale='$(LOCALES)'.split(), extensions=['html', 'txt', 'py'], no_location=True)"

# Compile .po -> .mo so translations actually load at runtime. Must run before uv build,
# since only .mo files are shipped/read — package-data in pyproject.toml only picks up
# whatever's already on disk. CI runs this in the build job, so a release never depends on
# someone having remembered to recompile.
#
# Plain msgfmt, not Django's compilemessages: that is what compilemessages shells out to
# anyway, and calling it directly means this needs neither a virtualenv nor Django — which
# is what lets the CI build job run it without installing pretix. --check also validates
# the catalogue, so a translation with a broken format specifier (a %(name)s that the
# msgid doesn't have) fails here instead of at runtime.
LOCALE_DIR = pretix_ptinvoicing/locale
compile-translations:
	@for loc in $(LOCALES); do \
		echo "compiling $$loc"; \
		msgfmt --check -o $(LOCALE_DIR)/$$loc/LC_MESSAGES/django.mo \
			$(LOCALE_DIR)/$$loc/LC_MESSAGES/django.po; \
	done

bump-version:
	@version="$(filter-out $@,$(MAKECMDGOALS))"; \
	if [ -z "$$version" ]; then \
		echo "Usage: make bump-version <version>" >&2; \
		exit 1; \
	fi; \
	sed -i.bak -E "s/^version = \".*\"/version = \"$$version\"/" pyproject.toml && rm pyproject.toml.bak; \
	sed -i.bak -E "s/^__version__ = \".*\"/__version__ = \"$$version\"/" pretix_ptinvoicing/__init__.py && rm pretix_ptinvoicing/__init__.py.bak; \
	echo "Bumped version to $$version in pyproject.toml and pretix_ptinvoicing/__init__.py"

# Swallow the version argument so make doesn't try to build it as a target.
%:
	@:
