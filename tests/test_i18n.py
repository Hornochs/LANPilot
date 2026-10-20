"""Translations: language choice, messages, and complete, consistent catalogues."""

import gettext
import re
from io import BytesIO

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from fastapi.testclient import TestClient

from lanpilot import i18n
from lanpilot.auth.roles import Role
from lanpilot.i18n import LOCALE_DIR, M, Message
from tests.conftest import ClientFactory, Query, UserFactory, csrf_token, sign_in

PLACEHOLDER = re.compile(r"\{(\w+)\}|%\((\w+)\)[sd]")


# --- language choice ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "header", "default", "expected"),
    [
        ("de", "en-US,en;q=0.9", "en", "de"),  # the user's choice wins
        (None, "de-DE,de;q=0.9,en;q=0.8", "en", "de"),
        (None, "fr-FR, en;q=0.5, de;q=0.8", "en", "de"),  # by quality, unknown ones skipped
        (None, "fr, it", "de", "de"),  # nothing LANPilot has: the default
        (None, "", "en", "en"),
        (None, "de;q=0, en", "de", "en"),  # q=0 means "not this one"
        (None, "de;q=garbage, en", "de", "en"),
        ("xx", None, "en", "en"),  # an unknown stored choice is ignored
    ],
)
def test_choose(user: str | None, header: str | None, default: str, expected: str) -> None:
    assert i18n.choose(user, header, default) == expected


# --- messages -----------------------------------------------------------------------------


def test_messages_read_as_english_and_translate_later() -> None:
    message = M("The server does not have the map {name}.", name="de_<b>")
    assert message == "The server does not have the map de_<b>."
    assert isinstance(message, Message) and message.msgid.endswith("{name}.")
    error = ValueError(message)
    assert i18n.message_of(error) is message


def test_values_are_inserted_after_the_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    class Catalogue(gettext.NullTranslations):
        def gettext(self, message: str) -> str:
            return {"Hello {name}": "Hallo {name}", "Broken {name}": "Kaputt {nmae}"}.get(
                message, message
            )

    monkeypatch.setattr(i18n, "catalogue", lambda language: Catalogue())
    # A value that looks like a placeholder is not expanded again.
    assert i18n.translate(M("Hello {name}", name="{name}")) == "Hallo {name}"
    # A broken translation falls back to English rather than failing the page.
    assert i18n.translate(M("Broken {name}", name="x")) == "Broken x"
    assert i18n.translate("Hello {name}") == "Hallo {name}"  # plain strings are looked up
    assert i18n.translate(None) == ""


# --- catalogues ---------------------------------------------------------------------------


def _catalogue(language: str) -> Catalog:
    path = LOCALE_DIR / language / "LC_MESSAGES" / "lanpilot.po"
    with path.open("rb") as handle:
        return read_po(handle, locale=language)


def _template() -> Catalog:
    with (LOCALE_DIR / "lanpilot.pot").open("rb") as handle:
        return read_po(handle)


@pytest.mark.parametrize("language", sorted(set(i18n.LANGUAGES) - {i18n.DEFAULT_LANGUAGE}))
def test_every_text_is_translated_consistently(language: str) -> None:
    template, catalogue = _template(), _catalogue(language)
    missing, fuzzy, placeholders = [], [], []
    for message in template:
        if not message.id:
            continue
        translated = catalogue.get(message.id)
        if (
            translated is None
            or not translated.string
            or (isinstance(translated.string, tuple) and not all(translated.string))
        ):
            missing.append(message.id)
            continue
        if translated.fuzzy:
            fuzzy.append(message.id)
        ids, strings = _texts(message.id), _texts(translated.string)
        wanted = {name for text in ids for name in _names(text)}
        for text in strings:
            if _names(text) - wanted or (len(ids) == 1 and _names(text) != wanted):
                placeholders.append(message.id)
    assert not missing, f"untranslated: {missing[:10]} (run scripts/update_translations.py)"
    assert not fuzzy, f"fuzzy, check them: {fuzzy[:10]}"
    assert not placeholders, f"placeholders differ: {placeholders[:10]}"


def _texts(value: object) -> tuple[str, ...]:
    """A message id or translation as a tuple: plural forms come as a sequence."""
    return (value,) if isinstance(value, str) else tuple(str(item) for item in value)  # type: ignore[attr-defined]


def _names(text: str) -> set[str]:
    return {first or second for first, second in PLACEHOLDER.findall(text)}


@pytest.mark.parametrize("language", sorted(set(i18n.LANGUAGES) - {i18n.DEFAULT_LANGUAGE}))
def test_compiled_catalogue_matches_its_source(language: str) -> None:
    compiled = BytesIO()
    write_mo(compiled, _catalogue(language))
    compiled.seek(0)
    expected = gettext.GNUTranslations(compiled)
    with (LOCALE_DIR / language / "LC_MESSAGES" / "lanpilot.mo").open("rb") as handle:
        shipped = gettext.GNUTranslations(handle)
    assert shipped._catalog == expected._catalog, "run scripts/update_translations.py"  # type: ignore[attr-defined]


def test_template_is_up_to_date_with_the_code(tmp_path: pytest.TempPathFactory) -> None:
    """Every marked text is in the catalogue template (nobody forgot to extract)."""
    from babel.messages.extract import extract_from_dir

    method_map = [("**.py", "python"), ("**/templates/**.html", "jinja2")]
    options = {"**/templates/**.html": {"extensions": "jinja2.ext.i18n", "trimmed": "true"}}
    keywords = {"M": None, "_": None, "gettext": None, "ngettext": (1, 2)}
    found = {
        message if isinstance(message, str) else message[0]
        for _, _, message, _, _ in extract_from_dir(
            str(LOCALE_DIR.parent), method_map, options, keywords
        )
    }
    known = {
        message.id if isinstance(message.id, str) else message.id[0] for message in _template()
    }
    assert found <= known, f"not extracted yet: {sorted(found - known)[:10]}"


# --- pages ---------------------------------------------------------------------------------


def test_pages_follow_the_browser_and_the_users_choice(
    make_client: ClientFactory, make_user: UserFactory, query: Query
) -> None:
    make_user("root", Role.ADMIN)
    client: TestClient = make_client()

    german = client.get("/login", headers={"Accept-Language": "de-DE,de;q=0.9"}).text
    assert '<html lang="de">' in german
    english = client.get("/login", headers={"Accept-Language": "en"}).text
    assert '<html lang="en">' in english

    sign_in(client, "root")
    response = client.post(
        "/account/language", data={"language": "de", "csrf_token": csrf_token(client)}
    )
    assert response.status_code == 303
    assert query("SELECT language FROM users WHERE username = 'root'") == [("de",)]
    page = client.get("/account", headers={"Accept-Language": "en"}).text
    assert '<html lang="de">' in page  # the choice wins over the browser

    client.post("/account/language", data={"language": "", "csrf_token": csrf_token(client)})
    assert query("SELECT language FROM users WHERE username = 'root'") == [(None,)]
    response = client.post(
        "/account/language", data={"language": "xx", "csrf_token": csrf_token(client)}
    )
    assert response.status_code == 400
