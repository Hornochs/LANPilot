"""Translations of the user interface (English and German).

Texts in code are written in English and marked so they can be extracted: ``M("...")`` for
messages that are created in one place and shown in another (exceptions, notices, form
labels), ``_("...")`` in templates. A message keeps its English text and is translated
where it is shown, into the language of the request that shows it:

    raise CommandRejectedError(M("The server does not have the map {name}.", name=key))
    ...
    {{ error | tr }}

Values put into a message (``name`` above) come from users and game servers; they are
inserted after the translation, so they never pass through it, and are escaped like all
other output. The language of a request is chosen by :func:`choose`: the user's own
choice, else the browser's preference, else ``default_language``.

Catalogues live in ``locale/<language>/LC_MESSAGES/lanpilot.po`` and are compiled to
``.mo`` by ``scripts/update_translations.py``; both are committed.
"""

from __future__ import annotations

import gettext as _gettext
from collections.abc import Mapping
from contextvars import ContextVar
from functools import cache
from pathlib import Path

DOMAIN = "lanpilot"
LOCALE_DIR = Path(__file__).parent / "locale"
LANGUAGES = {"en": "English", "de": "Deutsch"}
DEFAULT_LANGUAGE = "en"

_language: ContextVar[str] = ContextVar("language", default=DEFAULT_LANGUAGE)


class Message(str):
    """A text to translate where it is shown, with the values to insert into it.

    It is the English text with the values filled in, so it can be used like any string
    (logs, comparisons, tests); :func:`translate` turns it into the current language.
    """

    msgid: str
    params: Mapping[str, object]

    def __new__(cls, msgid: str, /, **params: object) -> Message:
        text = msgid.format(**params) if params else msgid
        message = super().__new__(cls, text)
        message.msgid = msgid
        message.params = params
        return message

    def __reduce__(self) -> tuple[object, ...]:  # keeps msgid and params when copied
        return (_rebuild, (self.msgid, dict(self.params)))


def _rebuild(msgid: str, params: dict[str, object]) -> Message:
    return Message(msgid, **params)


def M(msgid: str, /, **params: object) -> Message:  # noqa: N802 - the extraction keyword
    """Mark a text for translation; it is translated where it is shown."""
    return Message(msgid, **params)


@cache
def catalogue(language: str) -> _gettext.NullTranslations:
    if language == DEFAULT_LANGUAGE:
        return _gettext.NullTranslations()
    return _gettext.translation(DOMAIN, LOCALE_DIR, languages=[language], fallback=True)


def current() -> str:
    return _language.get()


def activate(language: str) -> None:
    """Use ``language`` for the rest of the current request (or task)."""
    _language.set(language if language in LANGUAGES else DEFAULT_LANGUAGE)


def gettext(text: str) -> str:
    return catalogue(current()).gettext(text)


def ngettext(singular: str, plural: str, count: int) -> str:
    return catalogue(current()).ngettext(singular, plural, count)


def translate(value: object) -> str:
    """A message, or any text, in the current language.

    A :class:`Message` is translated by its English text and filled in afterwards; a plain
    string is looked up as it is, so English texts from elsewhere (such as a third-party
    adapter) are shown translated if the catalogue knows them and unchanged otherwise.
    """
    if isinstance(value, Message):
        translated = gettext(value.msgid)
        if not value.params:
            return translated
        try:
            params = {
                key: translate(item) if isinstance(item, Message) else item
                for key, item in value.params.items()
            }
            return translated.format(**params)
        except (KeyError, IndexError, ValueError):  # a broken translation: show English
            return str(value)
    return gettext(str(value)) if value is not None else ""


def message_of(error: BaseException) -> str:
    """The message of an exception, as raised: a :class:`Message` stays one."""
    if error.args and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def choose(user_language: str | None, accept_language: str | None, default: str) -> str:
    """The language for a request: the user's choice, else the first of the browser's
    languages LANPilot has (``Accept-Language``, by quality), else ``default``."""
    if user_language in LANGUAGES:
        return user_language
    for language in _accepted(accept_language or ""):
        if language in LANGUAGES:
            return language
    return default if default in LANGUAGES else DEFAULT_LANGUAGE


def _accepted(header: str) -> list[str]:
    ranked: list[tuple[float, int, str]] = []
    for position, part in enumerate(header.split(",")[:20]):
        tag, _, rest = part.strip().partition(";")
        quality = 1.0
        if rest.strip().startswith("q="):
            try:
                quality = float(rest.strip()[2:])
            except ValueError:
                continue
        language = tag.strip().split("-")[0].lower()
        if language and quality > 0:
            ranked.append((-quality, position, language))
    return [language for _, _, language in sorted(ranked)]
