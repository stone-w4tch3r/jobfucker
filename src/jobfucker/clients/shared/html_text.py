"""Board-neutral HTML-to-plaintext parsing.

Job boards deliver vacancy descriptions as HTML (sometimes escaped). The core
contract wants full normalized plaintext (``Vacancy.description``), so each
client parses the board's description markup through :class:`DescriptionParser`
instead of returning a snippet or stripping tags with a regex.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Final

_BLOCK_ELEMENTS: Final = frozenset(
    {
        "br",
        "dd",
        "div",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "tr",
    }
)


class DescriptionParser(HTMLParser):
    """Convert trusted-as-text description HTML into normalized plaintext."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in _BLOCK_ELEMENTS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in _BLOCK_ELEMENTS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        self.fragments.append(data)

    def text(self) -> str:
        """Collapse inline whitespace while retaining meaningful block boundaries."""
        lines = (" ".join(line.split()) for line in "".join(self.fragments).splitlines())
        return "\n".join(line for line in lines if line)
