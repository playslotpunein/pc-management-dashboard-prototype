"""The dashboard's theme palette, checked as data.

The server mounts the dashboard, so a stylesheet that contradicts itself ships with it.

A viewer's theme has three states, not two. An explicit choice stamps ``data-theme`` on
the root; the default "system" setting stamps nothing at all and is resolved by
``prefers-color-scheme``. So every dark value is declared twice, in two places that have
to agree.

They did not agree. Updating the status palette changed the ``prefers-color-scheme``
copy and missed the ``[data-theme]`` one — the two are indented differently, so a
search-and-replace over one block of text matched only the first. That renders perfectly
in OS-dark and serves the old colours to anyone who used the toggle, which is exactly the
kind of fault nobody finds by looking at their own screen.

The file holds two such pairs (the base surfaces and the status palette) written with
slightly different selectors, so this walks the stylesheet rather than matching on a
fixed marker.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STYLES = Path(__file__).resolve().parents[2] / "dashboard" / "styles.css"

#: A rule that sets custom properties on the root itself, in either dark form. Descendant
#: rules like `:root[data-theme="dark"] .badge` are excluded — they style one component,
#: not the palette.
ROOT_DARK_TOGGLE = re.compile(r"(?:^|\})\s*:?root?\[data-theme=\"dark\"\]\s*\{|(?:^|\})\s*\[data-theme=\"dark\"\]\s*\{")
ROOT_DARK_MEDIA = re.compile(r":root:not\(\[data-theme=\"light\"\]\)\s*\{")

DECLARATION = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")

SHORT_HEX = re.compile(r"^#([0-9a-f])([0-9a-f])([0-9a-f])$", re.I)


def normalise(value: str) -> str:
    """Compare what the browser renders, not how it was typed.

    The two blocks were written at different times and differ harmlessly in spacing and
    hex length — `#fff` against `#ffffff`, `rgba(0,0,0,.4)` against `rgba(0, 0, 0, .4)`.
    Flagging those would bury the drift that actually changes a colour.
    """
    value = re.sub(r"\s+", "", value.strip().lower())
    short = SHORT_HEX.match(value)

    return f"#{short[1] * 2}{short[2] * 2}{short[3] * 2}" if short else value


def blocks_after(css: str, pattern: re.Pattern[str]) -> list[str]:
    """Bodies of every rule whose opening matches ``pattern``."""
    bodies = []

    for match in pattern.finditer(css):
        end = css.index("}", match.end())
        bodies.append(css[match.end() : end])

    return bodies


def properties(bodies: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}

    for body in bodies:
        for name, value in DECLARATION.findall(body):
            found[name] = normalise(value)

    return found


@pytest.fixture(scope="module")
def css() -> str:
    return STYLES.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def toggle(css) -> dict[str, str]:
    return properties(blocks_after(css, ROOT_DARK_TOGGLE))


@pytest.fixture(scope="module")
def media(css) -> dict[str, str]:
    return properties(blocks_after(css, ROOT_DARK_MEDIA))


class TestTheTwoDarkDeclarationsAgree:
    def test_both_forms_are_present(self, toggle, media):
        """Losing either one silently strands a third of viewers on the light palette."""
        assert toggle, "no root-level [data-theme=dark] custom properties found"
        assert media, "no root-level prefers-color-scheme custom properties found"

    def test_they_declare_the_same_properties(self, toggle, media):
        assert set(toggle) == set(media)

    def test_they_declare_the_same_values(self, toggle, media):
        drifted = {
            name: (media[name], toggle[name])
            for name in media
            if media.get(name) != toggle.get(name)
        }

        assert not drifted, (
            f"dark values have drifted between the two blocks: {drifted} — a manager "
            "using the theme toggle would see different colours from one whose OS is "
            "set to dark"
        )


class TestDarkIsItsOwnPalette:
    def test_every_state_colour_is_restepped_for_the_dark_surface(self, css, toggle):
        """Dark is selected, not an automatic flip; reusing a light step is the bug."""
        light = properties(blocks_after(css, re.compile(r"(?:^|\})\s*:root\s*\{")))

        states = {name: value for name, value in toggle.items() if name.startswith("--st-")}

        assert states, "no --st-* status colours declared for dark"

        reused = [name for name, value in states.items() if light.get(name) == value]

        assert not reused, f"these keep their light-mode step in dark: {reused}"
