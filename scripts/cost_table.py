"""Print and save the notional API cost table from saved eval reports."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from guardsql.evals.cost import PRICES, parse_report, render_document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="eval report .md files")
    parser.add_argument("--hit-rates", default="0,0.25,0.5,0.75")
    parser.add_argument(
        "--basis", type=int, default=0, help="index of the run the cache table uses"
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    runs = [parse_report(path.read_text(encoding="utf-8"), str(path)) for path in args.reports]
    rates = [float(value) for value in args.hit_rates.split(",")]
    document = render_document(runs, runs[args.basis], rates, PRICES)
    out = args.out or Path("evals/results") / f"cost-{date.today():%Y%m%d}.md"
    out.write_text(document, encoding="utf-8")
    print(document)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
