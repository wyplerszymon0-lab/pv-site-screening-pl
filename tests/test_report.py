import re
from pathlib import Path

import pytest

from pvscreen import report
from tests.test_pipeline import result


def numbers(md: str, lang: str) -> list[float]:
    """Every number in a report, as floats, whatever the language's format."""
    if lang == "pl":
        found = re.findall(r"\d{1,3}(?: \d{3})+(?:,\d+)?|\d+(?:,\d+)?", md)
        return [float(x.replace(" ", "").replace(",", ".")) for x in found]
    found = re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", md)
    return [float(x.replace(",", "")) for x in found]


def test_both_languages_print_the_same_figures():
    r = result()
    en = report.render(r, Path("x.gpkg"), "EPSG:2180 (PL-1992)", lang="en")
    pl = report.render(r, Path("x.gpkg"), "EPSG:2180 (PL-1992)", lang="pl")
    # Leave out the lines whose numbers are not figures: the version stamp and the
    # protected-area names ("Natura 2000" appears only in the Polish category label).
    skip = ("_Generated", "_Wygenerowano", "- Protected", "- Wykluczone", "- Obszary chronione")

    def figures(md, lang):
        return numbers("\n".join(line for line in md.splitlines() if not line.startswith(skip)), lang)

    assert figures(en, "en") == figures(pl, "pl")
    assert len(figures(en, "en")) > 40  # the thresholds, candidate table and farms all take part
    assert "## Obszary kandydackie" in pl and "## Candidates" in en


def test_polish_report_uses_polish_formats_names_and_plurals():
    pl = report.render(result(), Path("x.gpkg"), "EPSG:2180 (PL-1992)", lang="pl")
    assert "2 obszary, 16 ha (15,5 % powierzchni gminy)" in pl
    assert "Nadwarciański (obszar chronionego krajobrazu, 30 ha)" in pl
    assert "Dolina (Natura 2000, obszar ptasi, 5 ha)" in pl
    assert "| 1 | 12,0 | 0,91 | 0,8° | 0,5 km | 0,0 % | 11 880 |" in pl
    assert "2 farmy (8 ha). 75,0 % ich powierzchni leży w obszarach kandydackich" in pl
    assert "| Wagi: spadek / ekspozycja / sieć | 0,3 / 0,2 / 0,5 |" in pl


@pytest.mark.parametrize(
    "n,word",
    [
        (1, "obszar"),
        (2, "obszary"),
        (4, "obszary"),
        (5, "obszarów"),
        (12, "obszarów"),
        (22, "obszary"),
        (107, "obszarów"),
        (112, "obszarów"),
        (124, "obszary"),
    ],
)
def test_polish_plurals(n, word):
    assert report.plural_pl(n, "obszar", "obszary", "obszarów") == word


def test_crs_note_is_translated_for_polish():
    note = "EPSG:2177 (PL-2000 zone 6); inside zone 6 (18.512° - 18.712° E)"
    assert (
        report.crs_note(note, "pl")
        == "EPSG:2177 (PL-2000, strefa 6); w całości w strefie 6 (18.512° - 18.712° E)"
    )
    assert report.crs_note(note, "en") == note
    straddle = "crosses a zone boundary (16.4° - 16.6° E); zone 5 chosen by the centroid at 16.45° E"
    assert report.crs_note(straddle, "pl").startswith("przecina granicę stref (16.4° - 16.6° E); strefa 5")


def test_unknown_language_is_rejected():
    with pytest.raises(ValueError, match="unknown language"):
        report.render(result(), Path("x.gpkg"), "", lang="de")
