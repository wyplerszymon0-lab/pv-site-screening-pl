"""python -m pvscreen --teryt <code>: the whole screening for one gmina."""

from __future__ import annotations

import argparse
from pathlib import Path

from pvscreen import pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pvscreen", description=__doc__)
    parser.add_argument("--teryt", required=True, help="7-digit TERYT code of the gmina, e.g. 3027062")
    parser.add_argument(
        "--crs", choices=["pl1992", "pl2000"], default="pl1992", help="coordinate system of the output"
    )
    parser.add_argument("--reports", type=Path, default=Path("reports"), help="where to write <teryt>.md")
    args = parser.parse_args(argv)

    result = pipeline.run(args.teryt)
    gpkg, note = pipeline.write_outputs(result, args.crs)
    args.reports.mkdir(parents=True, exist_ok=True)
    report = args.reports / f"{args.teryt}.md"
    report.write_text(pipeline.render_report(result, gpkg, note), encoding="utf-8")
    c = result.candidates
    print(f"{len(c)} candidates, {c['area_ha'].sum():,.0f} ha; wrote {gpkg} and {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
