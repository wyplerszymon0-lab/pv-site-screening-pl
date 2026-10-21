"""The Markdown report for one gmina, in English or Polish.

One template, two string tables: both languages print the same figures from the
same values, only the words and the number format change (Polish uses a space
between thousands and a decimal comma: 5 634 ha, 50,9 %).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from pvscreen import WORK_CRS, __version__, osm, protected

if TYPE_CHECKING:
    from pvscreen.pipeline import Result

LANGUAGES = ("en", "pl")
SOURCE_NAMES = {"osm": "OpenStreetMap", "bdot10k": "BDOT10k"}

TEXT = {
    "en": {
        "title": "Solar-farm screening: gmina {name} (TERYT {teryt})",
        "generated": "_Generated {date} by `python -m pvscreen` (pvscreen {version}). "
        "A first-pass screening from open data, not a site assessment._",
        "gmina": "Gmina",
        "area": "Area: {area} ha (official {official} ha)",
        "output": "Output: `{path}`, {crs}",
        "thresholds": "Thresholds",
        "setting": "Setting",
        "value": "Value",
        "max_slope": "Maximum slope",
        "min_area": "Minimum candidate area",
        "strip": "Narrowest strip kept",
        "weights": "Weights: slope / aspect / grid",
        "grid_zero": "Grid score reaches 0 at",
        "grid_zero_value": "{km} km from a substation",
        "capacity": "Capacity / system losses",
        "capacity_value": "{mwp} MWp per ha / {loss} %",
        "sources": "Sources and reasons for every value: `pvscreen/screening.toml`.",
        "exclusions": "Exclusions",
        "source_line": "{source} (buildings, roads, water, forest, built-up land, with buffers): ",
        "source_value": "{area} ha, {share} of the gmina",
        "none": "none",
        "protected_excluded": "Protected nature areas excluded: ",
        "protected_kept": "Protected areas kept as constraints: ",
        "candidates": "Candidates",
        "no_candidates": "No area passes the screening.",
        "candidates_summary": "{n} candidates, {area} ha ({share} of the gmina), {mwp} MWp, about {gwh} GWh per "
        "year if every hectare were built (a theoretical upper bound). Yield {ymin} – {ymax} kWh/kWp.",
        "table": "| Rank | Area (ha) | Score | Mean slope | Substation | In protected landscape | Energy (MWh/yr) |",
        "farms": "Existing solar farms",
        "no_farms": "OpenStreetMap maps no solar farm of at least 1 ha in this gmina, so there is nothing to check.",
        "farms_summary": "{n} farms ({area} ha). {inside} of their area lies in candidates, against {chance} "
        "expected by chance (lift {lift}); {mostly} of {n} are mostly inside. "
        "See the README for why this is weaker evidence than it looks.",
        "data": "Data",
        "data_text": "Boundary: PRG (GUGiK). Terrain: NMT (GUGiK). Protected areas: GDOŚ. ",
        "data_bdot": "Buildings, roads, water and forest: BDOT10k (GUGiK). Power grid and solar farms: ",
        "data_osm": "Buildings, roads, water, forest, power grid and solar farms: ",
        "data_end": "© OpenStreetMap contributors, ODbL. Climate: NASA POWER.",
    },
    "pl": {
        "title": "Wstępna analiza lokalizacji farm fotowoltaicznych: gmina {name} (TERYT {teryt})",
        "generated": "_Wygenerowano {date} przez `python -m pvscreen` (pvscreen {version}). "
        "Wstępna selekcja terenów na podstawie otwartych danych, a nie ocena konkretnej lokalizacji._",
        "gmina": "Gmina",
        "area": "Powierzchnia: {area} ha (urzędowo {official} ha)",
        "output": "Wynik: `{path}`, {crs}",
        "thresholds": "Progi",
        "setting": "Parametr",
        "value": "Wartość",
        "max_slope": "Maksymalny spadek terenu",
        "min_area": "Minimalna powierzchnia obszaru",
        "strip": "Najwęższy zachowany pas",
        "weights": "Wagi: spadek / ekspozycja / sieć",
        "grid_zero": "Ocena sieci spada do 0",
        "grid_zero_value": "{km} km od stacji elektroenergetycznej",
        "capacity": "Moc / straty systemowe",
        "capacity_value": "{mwp} MWp na ha / {loss} %",
        "sources": "Źródła i uzasadnienie każdej wartości: `pvscreen/screening.toml`.",
        "exclusions": "Wykluczenia",
        "source_line": "{source} (budynki, drogi, wody, lasy, tereny zabudowy, z buforami): ",
        "source_value": "{area} ha, {share} powierzchni gminy",
        "none": "brak",
        "protected_excluded": "Wykluczone obszary chronione przyrody: ",
        "protected_kept": "Obszary chronione uwzględnione jako ograniczenie: ",
        "candidates": "Obszary kandydackie",
        "no_candidates": "Żaden teren nie spełnia kryteriów.",
        "candidates_summary": "{n} {areas_word}, {area} ha ({share} powierzchni gminy), {mwp} MWp, około {gwh} GWh "
        "rocznie przy zabudowie każdego hektara (teoretyczna górna granica). Uzysk {ymin} – {ymax} kWh/kWp.",
        "table": "| Nr | Powierzchnia (ha) | Ocena | Średni spadek | Stacja | W obszarze chronionego krajobrazu "
        "| Energia (MWh/rok) |",
        "farms": "Istniejące farmy fotowoltaiczne",
        "no_farms": "OpenStreetMap nie zawiera w tej gminie żadnej farmy fotowoltaicznej o powierzchni co "
        "najmniej 1 ha, więc nie ma czego sprawdzać.",
        "farms_summary": "{n} {farms_word} ({area} ha). {inside} ich powierzchni leży w obszarach kandydackich, wobec "
        "{chance} oczekiwanych przy losowym rozmieszczeniu (lift {lift}); {mostly} z {n} leży w większości "
        "wewnątrz. Dlaczego to słabszy dowód, niż się wydaje, wyjaśnia README.",
        "data": "Dane",
        "data_text": "Granica: PRG (GUGiK). Teren: NMT (GUGiK). Obszary chronione: GDOŚ. ",
        "data_bdot": "Budynki, drogi, wody i lasy: BDOT10k (GUGiK). Sieć elektroenergetyczna i farmy: ",
        "data_osm": "Budynki, drogi, wody, lasy, sieć elektroenergetyczna i farmy fotowoltaiczne: ",
        "data_end": "© współtwórcy OpenStreetMap, ODbL. Klimat: NASA POWER.",
    },
}

# GDOŚ categories by their Polish legal names (ustawa o ochronie przyrody).
CATEGORY_PL = {
    "national_park": "park narodowy",
    "nature_reserve": "rezerwat przyrody",
    "natura2000_habitats": "Natura 2000, obszar siedliskowy",
    "natura2000_birds": "Natura 2000, obszar ptasi",
    "ecological_site": "użytek ekologiczny",
    "nature_landscape_complex": "zespół przyrodniczo-krajobrazowy",
    "landscape_park": "park krajobrazowy",
    "protected_landscape_area": "obszar chronionego krajobrazu",
}


def plural_pl(n: int, one: str, few: str, many: str) -> str:
    """Polish noun form for a count: 1 obszar, 2–4 obszary, 5–21 obszarów, 22 obszary..."""
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def number(x: float, digits: int = 0, lang: str = "en") -> str:
    """5,634.5 in English; 5 634,5 in Polish."""
    text = f"{x:,.{digits}f}"
    if lang == "pl":
        text = text.replace(",", " ").replace(".", ",")
    return text


def percent(x: float, lang: str = "en") -> str:
    return f"{number(100 * x, 1, lang)} %"


def crs_note(note: str, lang: str) -> str:
    """The CRS note from pvscreen.crs, translated for the Polish report."""
    if lang != "pl":
        return note
    for pattern, repl in (
        (
            r"crosses a zone boundary \(([^)]*)\); zone (\d) chosen by the centroid at ([\d.]+)° E",
            r"przecina granicę stref (\1); strefa \2 wybrana według centroidu (\3° E)",
        ),
        (r"inside zone (\d) \(([^)]*)\)", r"w całości w strefie \1 (\2)"),
        (r"PL-2000 zone (\d)", r"PL-2000, strefa \1"),
    ):
        note = re.sub(pattern, repl, note)
    return note


def render(result: Result, gpkg: Path, note: str, today: date | None = None, lang: str = "en") -> str:
    if lang not in LANGUAGES:
        raise ValueError(f"unknown language {lang!r}; expected one of {LANGUAGES}")
    t = TEXT[lang]
    n = lambda x, d=0: number(x, d, lang)  # noqa: E731
    pct = lambda x: percent(x, lang)  # noqa: E731
    cv = lambda x: f"{x:g}".replace(".", ",") if lang == "pl" else f"{x:g}"  # noqa: E731  (settings)
    category = (lambda c: CATEGORY_PL.get(c, c)) if lang == "pl" else (lambda c: c)

    g = result.gmina.iloc[0]
    area_ha = result.gmina.to_crs(WORK_CRS).geometry.area.sum() / 10_000
    c, cfg = result.candidates, result.cfg
    zones = result.osm_zones
    union = osm.summarize(zones, result.gmina).iloc[-1] if not zones.empty else None
    excl = result.protected_areas[result.protected_areas["kind"] == protected.EXCLUDE]
    cons = result.protected_areas[result.protected_areas["kind"] == protected.CONSTRAINT]

    def areas(rows) -> str:
        return ", ".join(f"{r.name} ({category(r.category)}, {n(r.area_ha)} ha)" for r in rows.itertuples())

    lines = [
        f"# {t['title'].format(name=g['name'], teryt=g['teryt'])}",
        "",
        t["generated"].format(date=(today or date.today()).isoformat(), version=__version__),
        "",
        f"## {t['gmina']}",
        "",
        f"- {t['area'].format(area=n(area_ha), official=n(g['official_area_ha']))}",
        f"- {t['output'].format(path=gpkg.as_posix(), crs=crs_note(note, lang))}",
        "",
        f"## {t['thresholds']}",
        "",
        f"| {t['setting']} | {t['value']} |",
        "|---|---:|",
        f"| {t['max_slope']} | {cv(cfg.max_slope_deg)}° |",
        f"| {t['min_area']} | {cv(cfg.min_area_ha)} ha |",
        f"| {t['strip']} | {cv(2 * cfg.min_half_width_m)} m |",
        f"| {t['weights']} | {cv(cfg.weight_slope)} / {cv(cfg.weight_aspect)} / {cv(cfg.weight_grid)} |",
        f"| {t['grid_zero']} | {t['grid_zero_value'].format(km=cv(cfg.grid_zero_at_m / 1000))} |",
        f"| {t['capacity']} | {t['capacity_value'].format(mwp=cv(cfg.mwp_per_ha), loss=cv(cfg.system_loss_pct))} |",
        "",
        t["sources"],
        "",
        f"## {t['exclusions']}",
        "",
        "- "
        + t["source_line"].format(source=SOURCE_NAMES[result.source])
        + (
            t["source_value"].format(area=n(union["area_ha"]), share=pct(union["share_of_gmina"]))
            if union is not None
            else t["none"]
        ),
        "- " + t["protected_excluded"] + (areas(excl) or t["none"]),
        "- " + t["protected_kept"] + (areas(cons) or t["none"]),
        "",
        f"## {t['candidates']}",
        "",
    ]
    if c.empty:
        lines += [t["no_candidates"], ""]
    else:
        lines += [
            t["candidates_summary"].format(
                n=len(c),
                areas_word=plural_pl(len(c), "obszar", "obszary", "obszarów"),
                area=n(c["area_ha"].sum()),
                share=pct(c["area_ha"].sum() / area_ha),
                mwp=n(c["capacity_mwp"].sum()),
                gwh=n(c["energy_mwh_year"].sum() / 1000),
                ymin=n(c["yield_kwh_per_kwp"].min()),
                ymax=n(c["yield_kwh_per_kwp"].max()),
            ),
            "",
            t["table"],
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for r in c.head(10).itertuples():
            lines.append(
                f"| {r.rank} | {n(r.area_ha, 1)} | {n(r.score, 2)} | {n(r.mean_slope_deg, 1)}° | "
                f"{n(r.substation_distance_m / 1000, 1)} km | {pct(r.constraint_share)} | {n(r.energy_mwh_year)} |"
            )
        lines.append("")
    lines += [f"## {t['farms']}", ""]
    if result.validation is None:
        lines += [t["no_farms"], ""]
    else:
        v = result.validation
        lines += [
            t["farms_summary"].format(
                n=v["farms"],
                farms_word=plural_pl(v["farms"], "farma", "farmy", "farm"),
                area=n(v["farm_area_ha"]),
                inside=pct(v["farm_area_in_candidates"]),
                chance=pct(v["expected_by_chance"]),
                lift=n(v["lift"], 2),
                mostly=v["farms_mostly_inside"],
            ),
            "",
        ]
    lines += [
        f"## {t['data']}",
        "",
        t["data_text"] + (t["data_bdot"] if result.source == "bdot10k" else t["data_osm"]) + t["data_end"],
        "",
    ]
    return "\n".join(lines)
