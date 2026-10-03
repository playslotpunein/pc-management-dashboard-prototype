"""The dashboard's font: every file the stylesheet asks for is shipped and served.

Poppins is self-hosted so the dashboard looks the same with the café's internet down. The
failure this guards against is silent: rename or drop a .woff2 and nothing errors — the
browser falls back to the system font. The rupee sign is the first thing to go, because
it lives in a different file (Latin Extended) from the digits printed beside it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from playslot import main

DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard"
STYLES = DASHBOARD / "styles.css"

FACE = re.compile(r"@font-face\s*\{([^}]*)\}")
FONT_URL = re.compile(r'url\("(fonts/[^"]+)"\)')
RANGE = re.compile(r"U\+([0-9A-Fa-f]+)(?:-([0-9A-Fa-f]+))?")

RUPEE = 0x20B9


def faces() -> list[dict[str, str]]:
    """Each @font-face as its weight, file and unicode-range."""
    found = []

    for body in FACE.findall(STYLES.read_text(encoding="utf-8")):
        weight = re.search(r"font-weight:\s*(\d+)", body)
        url = FONT_URL.search(body)
        ranges = re.search(r"unicode-range:\s*([^;]+);", body)

        found.append({
            "weight": weight.group(1) if weight else "",
            "path": url.group(1) if url else "",
            "ranges": ranges.group(1) if ranges else "",
        })

    return found


def covers(ranges: str, codepoint: int) -> bool:
    for start, end in RANGE.findall(ranges):
        if int(start, 16) <= codepoint <= int(end or start, 16):
            return True

    return False


def test_the_page_font_is_poppins():
    css = STYLES.read_text(encoding="utf-8")

    assert re.search(r'--font:\s*"Poppins",', css), "--font does not lead with Poppins"
    assert faces(), "no @font-face rules: Poppins would come from the OS, if at all"


@pytest.mark.parametrize("path", [face["path"] for face in faces()])
def test_each_font_file_is_served(path):
    """Through the real app, so a file outside the mounted directory fails here too."""
    response = TestClient(main.app).get(f"/{path}")

    assert response.status_code == 200, f"{path} is not served"
    assert response.headers["content-type"] == "font/woff2"
    assert response.content[:4] == b"wOF2", f"{path} is not a woff2 file"


@pytest.mark.parametrize("weight", sorted({face["weight"] for face in faces()}))
def test_every_weight_can_draw_the_rupee_sign(weight):
    """Shipping only the Latin subset would print every price with a fallback ₹."""
    at_weight = [face for face in faces() if face["weight"] == weight]

    assert any(covers(face["ranges"], RUPEE) for face in at_weight), (
        f"no Poppins {weight} face covers U+20B9, so ₹ falls back to the system font"
    )


def test_the_licence_ships_with_the_fonts():
    """The SIL Open Font License asks for it to travel with the font files."""
    assert "SIL OPEN FONT LICENSE" in (DASHBOARD / "fonts" / "OFL.txt").read_text(
        encoding="utf-8"
    )
