"""Month parsing for the catalogue titles.

The first tests in this repo, and they exist because this parser already shipped a
bug that no amount of looking at the map would have caught: it accepted only the
dictionary spelling "Septiembre", while the feed writes "Setiembre" from 2021
through 2024. Four Septembers vanished from a five-year panel silently, which
would have left September absent from four of six years and quietly corrupted any
seasonal term fitted on it.

Run with:  uv run pytest
"""

from __future__ import annotations

import pytest

from mvdspeed.fetch import month_from_title


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # The two spellings the catalogue actually uses for September, which is
        # the whole reason this test exists.
        ("Velocidad promedio - Setiembre 2021", "2021-09"),
        ("Velocidad promedio - Septiembre 2025", "2025-09"),
        # One per era, to pin the format changes down.
        ("Velocidad promedio - Enero 2021", "2021-01"),
        ("Velocidad promedio - Agosto 2026", "2026-08"),
        ("Velocidad promedio - Diciembre 2025", "2025-12"),
        # Separator and spacing vary across eras; the month name is the anchor.
        ("Velocidad promedio  -  Marzo  2024", "2024-03"),
        ("Velocidad promedio de Marzo de 2024", "2024-03"),
        ("velocidad promedio - marzo 2024", "2024-03"),
    ],
)
def test_reads_the_month(title: str, expected: str) -> None:
    assert month_from_title(title) == expected


def test_every_month_name_parses() -> None:
    """All twelve, so a typo in the table cannot hide behind the ones we spot-check."""
    names = [
        "Enero",
        "Febrero",
        "Marzo",
        "Abril",
        "Mayo",
        "Junio",
        "Julio",
        "Agosto",
        "Setiembre",
        "Octubre",
        "Noviembre",
        "Diciembre",
    ]
    got = [month_from_title(f"Velocidad promedio - {n} 2024") for n in names]
    assert got == [f"2024-{i:02d}" for i in range(1, 13)]


@pytest.mark.parametrize(
    "title",
    [
        "Metadata",
        "",
        "Velocidad promedio - Marzo",  # no year
        "Velocidad promedio - 2024",  # no month
    ],
)
def test_returns_none_rather_than_guessing(title: str) -> None:
    """A title with no month must be None, not a plausible wrong month.

    `resources()` turns None into a hard error listing the offending titles, so
    silence here is what makes the loud failure possible.
    """
    assert month_from_title(title) is None
