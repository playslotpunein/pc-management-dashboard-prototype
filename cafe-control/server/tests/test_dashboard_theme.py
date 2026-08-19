"""The dashboard's theme palettes, checked as data.

The server mounts the dashboard, so a stylesheet that contradicts itself ships with it.

The manager picks from four named themes, each a ``[data-palette]`` block that sets the
whole palette in one place. Two properties matter and neither is visible by looking at
one screen:

* **Every palette is complete.** A palette that forgets ``--st-locked`` doesn't error —
  it silently inherits the previous theme's red, so switching themes half-changes the
  floor. This asserts each palette declares the full token set.

* **The three dark themes share one status ramp.** The whole promise of the picker is
  that switching theme never changes what a colour *means* — "locked" is the same red in
  Midnight, Indigo and Slate. This asserts those three declare byte-identical ``--st-*``
  values, so a manager never has to relearn the ramp.

An earlier version checked that a light block and a system-dark block agreed, back when a
theme was a light/dark toggle. That whole class of bug is gone now that a palette is
declared once rather than three times.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STYLES = Path(__file__).resolve().parents[2] / "dashboard" / "styles.css"

#: An innermost rule: a prelude then a body with no further braces. Excluding braces from
#: the body makes it nesting-proof — an @media wrapper never matches, the rule inside does.
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
DECLARATION = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")

#: The names of the four themes, and the selector each is declared under. Daylight shares
#: its rule with :root (it is also the no-JS default).
PALETTES = {
    "daylight": re.compile(r'^\[data-palette="daylight"\]$'),
    "midnight": re.compile(r'^\[data-palette="midnight"\]$'),
    "indigo": re.compile(r'^\[data-palette="indigo"\]$'),
    "slate": re.compile(r'^\[data-palette="slate"\]$'),
}

DARK = ("midnight", "indigo", "slate")

#: Every token a palette must set for the page to be fully coloured by it alone.
REQUIRED = {
    "--plane", "--surface", "--surface-2",
    "--ink", "--ink-2", "--muted",
    "--hairline", "--hairline-2",
    "--accent", "--ring",
    "--st-available", "--st-scheduled", "--st-active",
    "--st-warning", "--st-overtime", "--st-locked", "--st-maintenance",
    "--ut-pc", "--ut-ps5", "--ut-sim", "--ut-pool", "--ut-snooker",
}

STATUS = tuple(t for t in REQUIRED if t.startswith("--st-"))


def properties_under(css: str, pattern: re.Pattern[str]) -> dict[str, str]:
    """Custom properties from every rule one of whose selectors matches ``pattern``."""
    found: dict[str, str] = {}

    for prelude, body in RULE.findall(css):
        lines = re.sub(r"/\*.*?\*/", "", prelude, flags=re.S).strip().splitlines()
        selectors = (lines[-1] if lines else "").split(",")

        if any(pattern.match(part.strip()) for part in selectors):
            for name, value in DECLARATION.findall(body):
                found[name] = re.sub(r"\s+", "", value.strip().lower())

    return found


@pytest.fixture(scope="module")
def css() -> str:
    return STYLES.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def palettes(css) -> dict[str, dict[str, str]]:
    return {name: properties_under(css, pat) for name, pat in PALETTES.items()}


class TestEveryPaletteIsComplete:
    @pytest.mark.parametrize("name", list(PALETTES))
    def test_it_declares_the_full_token_set(self, palettes, name):
        missing = REQUIRED - set(palettes[name])

        assert not missing, (
            f"the {name} theme is missing {sorted(missing)} — those tokens would inherit "
            "whatever theme was active before, so switching to it half-changes the floor"
        )


class TestTheDarkThemesShareOneStatusRamp:
    def test_locked_is_the_same_red_in_every_dark_theme(self, palettes):
        """The load-bearing one: a manager must not relearn 'locked' per theme."""
        reds = {name: palettes[name]["--st-locked"] for name in DARK}

        assert len(set(reds.values())) == 1, f"--st-locked differs across dark themes: {reds}"

    @pytest.mark.parametrize("token", STATUS)
    def test_the_whole_ramp_is_identical(self, palettes, token):
        values = {name: palettes[name][token] for name in DARK}

        assert len(set(values.values())) == 1, (
            f"{token} differs across the dark themes: {values} — the status ramp is meant "
            "to be constant so only the ground and accent change between them"
        )

    def test_daylight_has_its_own_ramp(self, palettes):
        """Light needs darker status hues for contrast on white; it is not the dark ramp.

        Guards the opposite mistake: sharing the dark ramp onto the light ground, where
        several of the hues would fail contrast.
        """
        shared_dark = {palettes["midnight"][t] for t in STATUS}
        daylight = {palettes["daylight"][t] for t in STATUS}

        assert not (shared_dark & daylight), "daylight reuses dark-mode status hues"
