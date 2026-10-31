"""Year-to-year variability of the screened energy: P50 / P90 per gmina and powiat.

    python -m pvscreen.variability 3027

Reads the candidates of every gmina of the powiat already exported for the web
map (site/data/<teryt>/candidates.geojson), so the screening does not have to run
again. Each candidate's yield is computed for every year since 2001 from NASA
POWER's monthly data, at the candidate's optimal tilt, and turned into energy with
the configured MWp per hectare. A gmina's (and the powiat's) energy in a year is
the sum over its candidates in that year; P50 and P90 come from those yearly
totals. That is the right way round: neighbouring candidates share the weather, so
their poor years coincide and the P90 of a total is not a sum of independent P90s.
Writes reports/variability_<powiat>.md.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

from pvscreen import WORK_CRS, __version__
from pvscreen import energy as E
from pvscreen.county import GMINA_TYPE
from pvscreen.suitability import load_config

SITE_DATA = Path("site/data")


def yearly_energy(
    candidates: gpd.GeoDataFrame,
    mwp_per_ha: float,
    last_year: int,
    opts: E.ModelOptions,
    cache_dir: Path = E.DEFAULT_CACHE,
    session=None,
    seen: set | None = None,
) -> pd.Series:
    """Total energy of the candidates in each year, MWh, indexed by year.

    seen, if given, collects the distinct irradiance series used (NASA POWER's grid
    is much coarser than the 0.1° cache cells).
    """
    centroids = candidates.to_crs(WORK_CRS).geometry.centroid.to_crs(4326)
    total: pd.Series | None = None
    for pt, area in zip(centroids, candidates["area_ha"], strict=True):
        clim = E.fetch_climatology(pt.y, pt.x, cache_dir, session)
        tilt, _ = E.optimal_tilt(clim, pt.y, opts=opts)
        series = E.fetch_monthly(pt.y, pt.x, last_year, cache_dir, session)
        if seen is not None:
            seen.add(tuple(c.ghi for c in series.values()))
        az = 180 if pt.y >= 0 else 0
        mwh = pd.Series(E.yearly_yields(series, pt.y, tilt, az, opts)) * area * mwp_per_ha  # kWh/kWp x MWp
        total = mwh if total is None else total.add(mwh, fill_value=0.0)
    return total if total is not None else pd.Series(dtype=float)


def gmina_name(teryt: str, meta: dict, names: list[str]) -> str:
    """Disambiguate gminas sharing a name by type, as in the powiat report."""
    name = meta.get("name", teryt)
    return f"{name} ({GMINA_TYPE.get(teryt[-1], teryt)})" if names.count(name) > 1 else name


def summary_row(name: str, teryt: str, years: pd.Series, average_mwh: float) -> dict:
    e = E.exceedance(years.values)
    return {
        "gmina": name,
        "teryt": teryt,
        "average_gwh": average_mwh / 1000,  # from the climatology, as in the screening reports
        "p50_gwh": e.p50 / 1000,
        "p90_gwh": e.p90 / 1000,
        "p90_empirical_gwh": e.p90_empirical / 1000,
        "cov": e.cov,
        "worst": (int(years.index[int(years.argmin())]), float(years.min()) / 1000),
        "best": (int(years.index[int(years.argmax())]), float(years.max()) / 1000),
    }


def render_markdown(
    powiat: str,
    rows: list[dict],
    total: pd.Series,
    sum_of_p90s_gwh: float,
    distinct_series: int,
    today: date | None = None,
) -> str:
    today = today or date.today()
    n = lambda x, d=0: f"{x:,.{d}f}"  # noqa: E731
    first, last = int(total.index.min()), int(total.index.max())
    lines = [
        f"# Year-to-year energy: powiat {powiat}",
        "",
        f"_Generated {today.isoformat()} by `python -m pvscreen.variability {powiat}` "
        f"(pvscreen {__version__}), from the candidates on the web map and NASA POWER monthly "
        f"data {first}–{last}. Energy if every candidate hectare were built: a theoretical upper bound._",
        "",
        "| Gmina | TERYT | Average year (GWh) | P50 (GWh) | P90 (GWh) | P90 / P50 | Year-to-year σ "
        "| Empirical 10th pct. | Worst year | Best year |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- | :--- |",
    ]
    for r in rows:
        bold = "**" if r["teryt"] == "" else ""
        lines.append(
            f"| {bold}{r['gmina']}{bold} | {r['teryt']} | {n(r['average_gwh'])} | {n(r['p50_gwh'])} "
            f"| {n(r['p90_gwh'])} | {r['p90_gwh'] / r['p50_gwh']:.1%} | {r['cov']:.1%} "
            f"| {n(r['p90_empirical_gwh'])} | {r['worst'][0]}: {n(r['worst'][1])} "
            f"| {r['best'][0]}: {n(r['best'][1])} |"
        )
    powiat_row = rows[-1]
    lines += [
        "",
        "*Average year*: the screening's energy, from the 2001–2020 climatology. "
        f"*P50*: the mean of the yearly totals {first}–{last}. *P90*: the energy exceeded in 9 years "
        "out of 10, P50 − 1.2816 σ (normal approximation); the empirical 10th percentile is the check "
        "on that assumption.",
        "",
        f"**Correlation.** Summing the gminas' P90s gives {n(sum_of_p90s_gwh)} GWh; the P90 of the "
        f"powiat's yearly total is {n(powiat_row['p90_gwh'])} GWh, almost the same. Pooling only narrows "
        "the spread when sites have different poor years; independent sites would pool to a P90 well "
        f"above the sum of their P90s. Here {distinct_series} distinct NASA POWER irradiance series "
        "cover the whole powiat (its grid is about 1°), so the gminas are close to perfectly correlated "
        "by construction. Real weather 40 km apart is strongly but not perfectly correlated, so the "
        "true pooled P90 is a little higher.",
        "",
        "**What this P90 leaves out.** Only weather variability. A bankable P90 adds the uncertainty "
        "of the yield model and of the satellite irradiance, a few percent each, which would lower it.",
        "",
        "## Powiat total by year (GWh)",
        "",
        "| " + " | ".join(str(y) for y in total.index) + " |",
        "| " + " | ".join("---:" for _ in total.index) + " |",
        "| " + " | ".join(n(v / 1000) for v in total.values) + " |",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pvscreen.variability", description=__doc__)
    parser.add_argument("powiat", help="4-digit powiat TERYT code, e.g. 3027")
    parser.add_argument("--site-data", type=Path, default=SITE_DATA)
    parser.add_argument("--out", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    cfg = load_config()
    opts = E.ModelOptions(system_loss=cfg.system_loss_pct / 100)
    last_year = date.today().year - 1  # incomplete years are dropped by the parser
    dirs = sorted(
        d
        for d in args.site_data.iterdir()
        if d.is_dir() and d.name.startswith(args.powiat) and len(d.name) == 7
    )
    if not dirs:
        parser.error(f"no gminas of powiat {args.powiat} in {args.site_data}")
    metas = {d.name: json.loads((d / "meta.json").read_text(encoding="utf-8")) for d in dirs}
    names = [m.get("name") for m in metas.values()]

    rows: list[dict] = []
    total = pd.Series(dtype=float)
    sum_p90 = 0.0
    seen: set = set()
    for d in dirs:
        cands = gpd.read_file(d / "candidates.geojson")
        years = yearly_energy(cands, cfg.mwp_per_ha, last_year, opts, seen=seen)
        row = summary_row(
            gmina_name(d.name, metas[d.name], names), d.name, years, float(cands["energy_mwh_year"].sum())
        )
        rows.append(row)
        sum_p90 += row["p90_gwh"]
        total = total.add(years, fill_value=0.0)
        print(f"{row['gmina']}: P50 {row['p50_gwh']:,.0f} GWh, P90 {row['p90_gwh']:,.0f} GWh")
    average = sum(r["average_gwh"] for r in rows) * 1000
    rows.append(summary_row("All gminas", "", total, average))

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"variability_{args.powiat}.md"
    path.write_text(render_markdown(args.powiat, rows, total, sum_p90, len(seen)), encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
