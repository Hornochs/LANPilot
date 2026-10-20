#!/usr/bin/env python3
"""Extract the texts of the user interface, update the catalogues and compile them.

Texts are marked with ``M("...")`` in Python and ``_("...")`` or ``{% trans %}`` in
templates (see src/lanpilot/i18n.py). This script

1. extracts them into ``src/lanpilot/locale/lanpilot.pot``,
2. merges new and changed texts into ``locale/<language>/LC_MESSAGES/lanpilot.po`` (new
   texts arrive untranslated, changed ones marked fuzzy: both are what to translate next),
3. compiles every catalogue to ``lanpilot.mo``, which LANPilot reads at run time.

The .po and .mo files are committed; ``tests/test_i18n.py`` fails while a text is missing a
translation or the .mo is older than its .po. Needs Babel (a development dependency).

Usage: python3 scripts/update_translations.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from babel.messages.frontend import CommandLineInterface

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src/lanpilot"
LOCALE = PACKAGE / "locale"
TEMPLATE = LOCALE / "lanpilot.pot"
LANGUAGES = ("de",)
MAPPING = """\
[python: **.py]
[jinja2: **/templates/**.html]
extensions = jinja2.ext.i18n
trimmed = true
"""


def babel(*arguments: str) -> None:
    CommandLineInterface().run(["pybabel", "--quiet", *arguments])


def main() -> None:
    mapping = LOCALE / "babel.cfg"
    mapping.write_text(MAPPING)
    try:
        babel(
            "extract",
            "--mapping-file", str(mapping),
            "--keywords", "M _ gettext ngettext:1,2",
            "--no-default-keywords",
            "--sort-by-file",
            "--no-wrap",
            "--project", "LANPilot",
            "--copyright-holder", "LANPilot contributors",
            "--output-file", str(TEMPLATE),
            str(PACKAGE),
        )  # fmt: skip
    finally:
        mapping.unlink()
    for language in LANGUAGES:
        catalogue = LOCALE / language / "LC_MESSAGES" / "lanpilot.po"
        action = "update" if catalogue.exists() else "init"
        arguments = ["--no-wrap"] if action == "update" else []
        babel(
            action,
            "--domain", "lanpilot",
            "--input-file", str(TEMPLATE),
            "--output-dir", str(LOCALE),
            "--locale", language,
            *arguments,
        )  # fmt: skip
    babel("compile", "--domain", "lanpilot", "--directory", str(LOCALE), "--use-fuzzy")
    print(f"catalogues in {LOCALE.relative_to(ROOT)} updated", file=sys.stderr)  # noqa: T201


if __name__ == "__main__":
    main()
