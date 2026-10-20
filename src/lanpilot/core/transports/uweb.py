"""UWeb: the web administration of Unreal Engine servers (UT99, UT2003, UT2004, UT3).

The protocol, as measured against UT2004 build 3369 (see docs/games/unreal-tournament.md):

* Plain HTTP on the WebAdmin port (UT2004: ``ListenPort`` in ``[UWeb.WebServer]``), pages
  under ``/ServerAdmin``. The server closes the connection after every answer.
* HTTP Basic authentication, realm "UT Remote Admin Server"; without valid credentials
  every page is 401. The password travels in clear text with every request.
* Pages are ISO-8859-1 HTML with forms; actions are form posts
  (``application/x-www-form-urlencoded``). Answers to actions are pages too, usually
  "Please wait" while the server travels to another map.

Everything the server sends is untrusted: answers are size-limited and parsed with the
standard library's tolerant HTML parser, which never executes anything.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlencode

import aiohttp

from lanpilot.core.adapters import (
    AuthenticationError,
    ProtocolError,
    ServerUnreachableError,
)

MAX_PAGE_BYTES = 2 * 1024 * 1024
# What a WebAdmin page may contain. Real pages have a few thousand elements; the bounds keep
# a hostile page from costing gigabytes and minutes (a <td colspan=64> per few bytes did).
MAX_TAGS = 50_000
MAX_CELLS = 20_000  # table cells, colspan padding included
MAX_TABLE_DEPTH = 16
ENCODING = "iso-8859-1"


class PageNotFoundError(ProtocolError):
    """HTTP 404: the WebAdmin does not offer this page (some are switched off by default)."""


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    selected: bool = False


@dataclass
class Cell:
    """One table cell: its text (whitespace collapsed) and the inputs in it, name -> value
    (checkboxes too, checked or not)."""

    text: str = ""
    inputs: dict[str, str] = field(default_factory=dict)


@dataclass
class Page:
    """What LANPilot needs from a page: its selects, inputs and table rows."""

    selects: dict[str, list[Option]] = field(default_factory=dict)
    inputs: dict[str, str] = field(default_factory=dict)  # name -> value (last one wins)
    # Checkboxes and radio buttons that are checked: name -> values.
    checked: dict[str, list[str]] = field(default_factory=dict)
    # Definition lists: <dt> text -> the text pieces of its <dd> (UT3's status page).
    definitions: dict[str, list[str]] = field(default_factory=dict)
    # Tables, each a list of rows, in the order they end (a nested table comes before the
    # table around it). A cell spanning several columns is followed by empty cells, so the
    # cells of every row line up with the header row.
    tables: list[list[list[Cell]]] = field(default_factory=list)
    #: The name of every input of the page, in order (duplicates kept).
    input_names: list[str] = field(default_factory=list)

    def options(self, name: str) -> list[Option]:
        return self.selects.get(name, [])


class _TooComplexError(Exception):
    pass


class _BoundedParser(HTMLParser):
    """Counts elements and stops a page that has too many."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tags = 0

    def _count_tag(self) -> None:
        self._tags += 1
        if self._tags > MAX_TAGS:
            raise _TooComplexError


def _feed(parser: HTMLParser, html: str) -> None:
    try:
        parser.feed(html)
        parser.close()
    except _TooComplexError as exc:
        raise ProtocolError("The WebAdmin's page is too large to read.") from exc


class _FormParser(_BoundedParser):
    def __init__(self) -> None:
        super().__init__()
        self.page = Page()
        self._cells = 0
        self._select: str | None = None
        self._option: dict[str, str | None] | None = None
        self._label: list[str] = []
        self._term: list[str] | None = None  # text of the current <dt>
        self._last_term = ""
        self._detail: list[str] | None = None  # text pieces of the current <dd>
        # One entry per open <table>: its open row and the open cell's text pieces.
        self._tables: list[_Table] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._count_tag()
        values = dict(attrs)
        if tag == "select":
            self._finish_option()
            self._select = values.get("name") or values.get("id") or ""
            self.page.selects.setdefault(self._select, [])
        elif tag == "option" and self._select is not None:
            self._finish_option()
            self._option = values
            self._label = []
        elif tag == "dt":
            self._term = []
        elif tag == "dd":
            self._detail = []
        elif tag == "table":
            if len(self._tables) >= MAX_TABLE_DEPTH:
                raise _TooComplexError
            self._tables.append(_Table())
        elif tag == "tr" and self._tables:
            self._tables[-1].finish_row()
            self._tables[-1].row = []
        elif tag in ("td", "th") and self._tables:
            table = self._tables[-1]
            table.finish_cell()
            if table.row is None:  # a cell outside <tr>
                table.row = []
            try:
                span = int(values.get("colspan") or 1)
            except ValueError:
                span = 1
            table.cell, table.span, table.text = Cell(), min(max(span, 1), 64), []
            self._cells += table.span
            if self._cells > MAX_CELLS:
                raise _TooComplexError
        elif tag == "input" and values.get("name"):
            name, value = values["name"] or "", values.get("value") or ""
            self.page.input_names.append(name)
            if self._tables and self._tables[-1].cell is not None:
                self._tables[-1].cell.inputs[name] = value
            if (values.get("type") or "").lower() in ("checkbox", "radio"):
                if "checked" in values:
                    self.page.checked.setdefault(name, []).append(value)
            else:
                self.page.inputs[name] = value

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._tables:
            self._tables[-1].finish_cell()
        elif tag == "tr" and self._tables:
            self._tables[-1].finish_row()
        elif tag == "table" and self._tables:
            self._finish_table()
        elif tag == "dt" and self._term is not None:
            self._last_term = " ".join("".join(self._term).split())
            self._term = None
        elif tag == "dd" and self._detail is not None:
            if self._last_term:
                self.page.definitions[self._last_term] = self._detail
            self._detail = None
        elif tag == "option":
            self._finish_option()
        elif tag == "select":
            self._finish_option()
            self._select = None

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._label.append(data)
        if self._term is not None:
            self._term.append(data)
        if self._detail is not None and data.strip():
            self._detail.append(data.strip())
        if self._tables and self._tables[-1].cell is not None:
            self._tables[-1].text.append(data)

    def _finish_table(self) -> None:
        table = self._tables.pop()
        table.finish_row()
        if table.rows:
            self.page.tables.append(table.rows)

    def _finish_option(self) -> None:
        if self._option is None or self._select is None:
            self._option = None
            return
        label = "".join(self._label).strip()
        value = self._option.get("value")
        self.page.selects[self._select].append(
            Option(label if value is None else value, label, "selected" in self._option)
        )
        self._option = None


class _Table:
    def __init__(self) -> None:
        self.rows: list[list[Cell]] = []
        self.row: list[Cell] | None = None
        self.cell: Cell | None = None
        self.span = 1
        self.text: list[str] = []

    def finish_cell(self) -> None:
        if self.cell is None or self.row is None:
            self.cell = None
            return
        self.cell.text = " ".join("".join(self.text).split())
        self.row.append(self.cell)
        self.row.extend(Cell() for _ in range(self.span - 1))
        self.cell = None

    def finish_row(self) -> None:
        self.finish_cell()
        if self.row:
            self.rows.append(self.row)
        self.row = None


def parse(html: str) -> Page:
    parser = _FormParser()
    _feed(parser, html)
    parser._finish_option()
    while parser._tables:  # tables left open at the end of the page
        parser._finish_table()
    return parser.page


def find_table(page: Page, *headers: str) -> tuple[dict[str, int], list[list[Cell]]]:
    """The first table with a row that has all of ``headers``: the column of every header
    (lower case, without a trailing colon) and all rows of that table."""
    wanted = {header.lower() for header in headers}
    for rows in page.tables:
        for row in rows:
            columns: dict[str, int] = {}
            for column, cell in enumerate(row):
                columns.setdefault(cell.text.rstrip(":").strip().lower(), column)
            if wanted <= columns.keys():
                return columns, rows
    return {}, []


def table(page: Page, *headers: str) -> tuple[dict[str, int], list[list[Cell]]]:
    """Like :func:`find_table`, but only the rows after the header row that have its width
    (others, such as "No players", are left out)."""
    columns, rows = find_table(page, *headers)
    for index, row in enumerate(rows):
        named = {cell.text.rstrip(":").strip().lower() for cell in row}
        if {header.lower() for header in headers} <= named:
            return columns, [other for other in rows[index + 1 :] if len(other) == len(row)]
    return {}, []


VOID_TAGS = frozenset({"br", "hr", "img", "input", "link", "meta", "col", "area", "base", "wbr"})
# Tags that start a new line of text.
BLOCK_TAGS = frozenset(
    {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li", "table", "pre", "form", "dt", "dd"}
)


class _TextParser(_BoundedParser):
    """The text of a page, or of the element with a given id, with ``<br>`` as a line
    break."""

    def __init__(self, element_id: str | None) -> None:
        super().__init__()
        self._id = element_id
        self._depth = 0 if element_id is None else -1  # -1: not inside the element yet
        self.found = element_id is None
        self.pieces: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._count_tag()
        if tag == "br":
            if self._depth >= 0:
                self.pieces.append("\n")
            return
        if tag in VOID_TAGS:
            return
        if tag in BLOCK_TAGS and self._depth >= 0:
            self.pieces.append("\n")
        if self._depth >= 0:
            self._depth += 1
        elif self._id is not None and not self.found and dict(attrs).get("id") == self._id:
            self.found = True
            self._depth = 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and self._depth >= 0:
            self.pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCK_TAGS and self._depth >= 0:
            self.pieces.append("\n")
        if tag in VOID_TAGS or self._depth < 0 or self._id is None:
            return
        self._depth -= 1
        if self._depth == 0:
            self._depth = -1

    def handle_data(self, data: str) -> None:
        if self._depth >= 0 and (self._depth > 0 or self._id is None):
            self.pieces.append(data)


def text_lines(html: str, element_id: str | None = None) -> list[str] | None:
    """The lines of text of the page, or of the element with id ``element_id`` (``None``
    if the page has none); ``<br>`` and line breaks end a line, blank lines are dropped."""
    parser = _TextParser(element_id)
    _feed(parser, html)
    if not parser.found:
        return None
    lines = "".join(parser.pieces).splitlines()
    return [line.replace("\xa0", " ").strip() for line in lines if line.strip()]


class UWebAdmin:
    """Requests to one server's WebAdmin. Every request carries the credentials."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base = f"http://{host}:{port}"
        credentials = f"{username}:{password}".encode(ENCODING, errors="replace")
        self._headers = {"Authorization": "Basic " + base64.b64encode(credentials).decode()}
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._refused = False
        # UT3's WebAdmin opens a session for every request without its cookie and keeps it
        # for a day; keeping the cookie avoids piling up thousands of them.
        self._cookies: aiohttp.CookieJar | None = None

    def refuse(self) -> None:
        """Record that the server refused the credentials (for WebAdmins that answer with a
        login page instead of 401); nothing is sent to it any more."""
        self._refused = True

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> str:
        query = f"?{urlencode(params)}" if params else ""
        return await self._request("GET", path + query)

    async def post(self, path: str, fields: Mapping[str, str]) -> str:
        return await self._request("POST", path, fields)

    async def _request(
        self, method: str, path: str, fields: Mapping[str, str] | None = None
    ) -> str:
        if self._refused:
            # As with the other protocols: refused credentials are not sent again.
            raise AuthenticationError(
                "The server refused the user name or password. Edit the server to enter other ones."
            )
        try:
            if self._cookies is None:
                self._cookies = aiohttp.CookieJar(unsafe=True)  # unsafe: allows IP addresses
            async with (
                aiohttp.ClientSession(
                    timeout=self._timeout, trust_env=False, cookie_jar=self._cookies
                ) as session,
                session.request(
                    method,
                    self._base + path,
                    data=dict(fields) if fields is not None else None,
                    headers=self._headers,
                    allow_redirects=False,
                ) as response,
            ):
                if response.status == 401:
                    self._refused = True
                    raise AuthenticationError(
                        "The server refused the WebAdmin user name or password."
                    )
                if response.status == 404:
                    raise PageNotFoundError("The WebAdmin does not offer this page (HTTP 404).")
                if response.status != 200:
                    raise ProtocolError(f"The WebAdmin answered with HTTP {response.status}.")
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        raise ProtocolError("The WebAdmin's page is too large.")
        except (aiohttp.ClientConnectionError, TimeoutError) as exc:
            raise ServerUnreachableError("The WebAdmin cannot be reached.") from exc
        except aiohttp.ClientError as exc:
            raise ProtocolError("The WebAdmin's answer could not be read.") from exc
        return bytes(body).decode(ENCODING, errors="replace")
