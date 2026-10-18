"""Screen every gmina of a powiat and compare them.

    python -m pvscreen.county 3027 [--webmap]

Runs the screening for each gmina in the powiat (PRG lists them; downloads are
cached as usual), writes each gmina's GeoPackage and report, then
reports/powiat_<code>.md with one row per gmina plus the pooled values, and an
overview map docs/powiat_<code>.png. With --webmap every gmina is also exported
for the interactive map.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

from pvscreen import WORK_CRS, __version__, pipeline, validate
from pvscreen.boundary import list_gminas

SCORE_BINS = [0.0, 0.6, 0.7, 0.8, 0.9, 1.01]
SCORE_COLOURS = ["#ffffd4", "#fee391", "#fec44f", "#fe9929", "#d95f0e"]  # as on the QGIS and web maps


GMINA_TYPE = {"1": "town", "2": "rural", "3": "town and rural"}  # last digit of the TERYT code


def disambiguate(results: list[pipeline.Result]) -> None:
    """Name gminas that share a name by their type, e.g. Turek (town) and Turek (rural)."""
    names = [r.gmina.iloc[0]["name"] for r in results]
    for r in results:
        g = r.gmina.iloc[0]
        if names.count(g["name"]) > 1:
            r.gmina = r.gmina.assign(name=f"{g['name']} ({GMINA_TYPE.get(g['teryt'][-1], g['teryt'])})")


def gmina_row(result: pipeline.Result) -> dict:
    """One gmina's figures for the county table."""
    g = result.gmina.iloc[0]
    c = result.candidates
    v = result.validation or {"farms": 0, "farm_area_ha": 0.0, "farm_area_in_candidates": float("nan")}
    return {
        "teryt": g["teryt"],
        "name": g["name"],
        "gmina_ha": float(result.gmina.to_crs(WORK_CRS).geometry.area.sum() / 10_000),
        "candidates": len(c),
        "candidate_ha": float(c["area_ha"].sum()) if len(c) else 0.0,
        "largest_ha": float(c["area_ha"].max()) if len(c) else 0.0,
        "high_score_ha": float(c.loc[c["score"] >= 0.9, "area_ha"].sum()) if len(c) else 0.0,
        "energy_gwh": float(c["energy_mwh_year"].sum() / 1000) if len(c) else 0.0,
        "farms": int(v["farms"]),
        "farm_ha": float(v["farm_area_ha"]),
        "farm_ha_in": float(v["farm_area_ha"] * v["farm_area_in_candidates"]) if v["farms"] else 0.0,
    }


def county_table(rows: list[dict]) -> pd.DataFrame:
    """Per-gmina rows plus a pooled row, with the validation columns of validate.pooled_table."""
    table = validate.pooled_table(rows)  # keeps the extra columns; its pooled row leaves them empty
    gminas, last = table.iloc[:-1], table.index[-1]
    table.loc[last, "candidates"] = gminas["candidates"].sum()
    table.loc[last, "largest_ha"] = gminas["largest_ha"].max()
    table.loc[last, "high_score_ha"] = gminas["high_score_ha"].sum()
    table.loc[last, "energy_gwh"] = gminas["energy_gwh"].sum()
    table["candidates"] = table["candidates"].astype(int)
    table["candidate_share"] = table["candidate_ha"] / table["gmina_ha"]
    return table


def _pct(x: float) -> str:
    return "–" if pd.isna(x) else f"{100 * x:.1f} %"


def render_markdown(code: str, table: pd.DataFrame, today: date | None = None) -> str:
    lines = [
        f"# Solar-farm screening: powiat {code}",
        "",
        f"_Generated {(today or date.today()).isoformat()} by `python -m pvscreen.county {code}` "
        f"(pvscreen {__version__}). A first-pass screening from open data, not a site assessment._",
        "",
        "| Gmina | TERYT | Area (ha) | Candidates | Candidate area | Share | Score ≥ 0.9 (ha) | Largest (ha) "
        "| Energy if built (GWh/yr) | Farms (ha) | Farm area in candidates | By chance | Lift |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in table.to_dict("records"):
        name = "**All gminas**" if r["name"] == "all" else r["name"]
        lift = "–" if pd.isna(r["lift"]) else f"{r['lift']:.2f}"
        lines.append(
            f"| {name} | {r['teryt']} | {r['gmina_ha']:,.0f} | {r['candidates']} | "
            f"{r['candidate_ha']:,.0f} ha | "
            f"{_pct(r['candidate_share'])} | {r['high_score_ha']:,.0f} | {r['largest_ha']:,.0f} | "
            f"{r['energy_gwh']:,.0f} | {r['farms']} ({r['farm_ha']:,.0f}) | "
            f"{_pct(r['in_candidates'])} | {_pct(r['by_chance'])} | {lift} |"
        )
    lines += [
        "",
        "*Share*: candidate area over gmina area. *Farm area in candidates*: share of the area of existing "
        "solar farms (OpenStreetMap, at least 1 ha) that lies in candidates; *by chance*: what a random "
        "placement would give (the candidate share; pooled, weighted by farm area); *lift*: their ratio. "
        "Energy assumes every candidate hectare were built, a theoretical upper bound.",
        "",
    ]
    return "\n".join(lines)


def overview_map(results: list[pipeline.Result], path: Path, title: str) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    gminas = gpd.GeoDataFrame(pd.concat([r.gmina for r in results]), crs=WORK_CRS)
    cands = gpd.GeoDataFrame(pd.concat([r.candidates for r in results if len(r.candidates)]), crs=WORK_CRS)
    farms = [r.farms for r in results if len(r.farms)]

    fig, ax = plt.subplots(figsize=(11, 10), dpi=130)
    gminas.plot(ax=ax, color="#f5f5f5", edgecolor="none")
    cands["cls"] = pd.cut(cands["score"], SCORE_BINS, labels=False, right=False)
    for i, colour in enumerate(SCORE_COLOURS):
        sub = cands[cands["cls"] == i]
        if len(sub):
            sub.plot(ax=ax, color=colour, edgecolor="none")
    if farms:
        gpd.GeoDataFrame(pd.concat(farms), crs=WORK_CRS).plot(
            ax=ax, facecolor="none", edgecolor="black", linewidth=0.7
        )
    gminas.boundary.plot(ax=ax, color="#212121", linewidth=1.1)
    for g in gminas.itertuples():
        p = g.geometry.representative_point()
        ax.annotate(
            g.name,
            (p.x, p.y),
            ha="center",
            fontsize=9,
            weight="bold",
            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.75},
        )
    handles = [
        Patch(color=c, label=f"score {lo:.1f}–{min(hi, 1):.1f}")
        for lo, hi, c in zip(SCORE_BINS, SCORE_BINS[1:], SCORE_COLOURS, strict=False)  # 6 edges, 5 classes
    ]
    handles.append(Patch(facecolor="none", edgecolor="black", label="existing solar farm (OSM)"))
    ax.legend(handles=handles, loc="lower left", fontsize=8, frameon=True)
    xmin, ymin, xmax, ymax = gminas.total_bounds
    ax.plot([xmin + 1000, xmin + 6000], [ymax - 1500] * 2, color="black", linewidth=3)
    ax.text(xmin + 3500, ymax - 1100, "5 km", ha="center", fontsize=8)
    ax.set_title(title, fontsize=13, loc="left")
    ax.set_axis_off()
    ax.text(
        0,
        -0.02,
        "Data: PRG, NMT (GUGiK); GDOŚ; © OpenStreetMap contributors (ODbL). PL-1992 (EPSG:2180). "
        "A first-pass screening from open data, not a site assessment.",
        transform=ax.transAxes,
        fontsize=7,
        color="#616161",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pvscreen.county", description=__doc__)
    parser.add_argument("powiat", help="4-digit TERYT code of the powiat, e.g. 3027")
    parser.add_argument("--webmap", action="store_true", help="also export every gmina for the web map")
    args = parser.parse_args(argv)

    results = []
    for teryt, name in list_gminas(args.powiat):
        print(f"{teryt} {name} …", flush=True)
        result = pipeline.run(teryt)
        gpkg, note = pipeline.write_outputs(result)
        Path("reports").mkdir(exist_ok=True)
        Path("reports", f"{teryt}.md").write_text(
            pipeline.render_report(result, gpkg, note), encoding="utf-8"
        )
        if args.webmap:
            from pvscreen import webmap

            webmap.export(teryt)
        results.append(result)

    disambiguate(results)
    table = county_table([gmina_row(r) for r in results])
    report = Path("reports") / f"powiat_{args.powiat}.md"
    report.write_text(render_markdown(args.powiat, table), encoding="utf-8")
    png = overview_map(
        results, Path("docs") / f"powiat_{args.powiat}.png", f"Candidate areas, powiat {args.powiat}"
    )
    print(f"wrote {report} and {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
