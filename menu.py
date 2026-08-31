"""Build the week's lunch table from the two cafeteria PDFs.

    uv run menu.py                         # the coming week, as a table
    uv run menu.py --week 2026-08-17
    uv run menu.py --date 2026-09-02
    uv run menu.py --out lunch.csv
    uv run menu.py --url sprout=https://.../SPROUT-WEEKLY-MENU-W.C-31.8.26.pdf

Every run also saves the full week to data/menu-<week>.csv.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import sys
from pathlib import Path

import download
import parse
from parse import Dish

CSV_DIR = Path(__file__).parent / "data"

COLUMNS = [
    "date",
    "canteen",
    "section",
    "dish",
    "calories",
    "price_small_gbp",
    "price_large_gbp",
    "vegetarian",
    "vegan",
]


def collect(
    monday: dt.date,
    cache_dir: Path,
    *,
    urls: dict[str, str] | None = None,
    force: bool = False,
) -> tuple[list[Dish], list[str]]:
    """Download both PDFs for a week and parse them into dishes."""
    pdfs, problems = download.download_week(monday, cache_dir, urls=urls, force=force)

    dishes: list[Dish] = []
    for pdf in pdfs:
        dishes += parse.parse(pdf.canteen, pdf.path, monday)

        # The file name is a guess; the date printed inside the PDF is not.
        printed = parse.week_printed_in(pdf.path)
        if printed and download.week_commencing(printed) != monday:
            problems.append(
                f"{pdf.canteen} PDF is stamped {printed:%Y-%m-%d}, but week commencing "
                f"{monday:%Y-%m-%d} was requested — the file name may be misleading"
            )
    return sorted(dishes, key=parse.sort_key), problems


def _cell(dish: Dish, column: str) -> str:
    value = getattr(dish, column)
    if value is None:
        return ""
    if column == "date":
        return value.isoformat()
    if column == "calories":
        return f"{value}{'/100g' if dish.per == '100g' else ''}"
    if column in {"price_small_gbp", "price_large_gbp"}:
        return f"{value:.2f}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render_table(dishes: list[Dish]) -> str:
    rows = [[_cell(d, c) for c in COLUMNS] for d in dishes]
    widths = [
        max(len(c), *(len(r[i]) for r in rows)) if rows else len(c)
        for i, c in enumerate(COLUMNS)
    ]

    def line(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

    out = [line(COLUMNS), line(["-" * w for w in widths])]
    previous = None
    for dish, row in zip(dishes, rows):
        if previous is not None and dish.date != previous:
            out.append("")
        out.append(line(row))
        previous = dish.date
    return "\n".join(out)


def render_csv(dishes: list[Dish]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for dish in dishes:
        writer.writerow([_cell(dish, c) for c in COLUMNS])
    return buffer.getvalue()


def render_json(dishes: list[Dish]) -> str:
    return json.dumps([d.as_row() for d in dishes], indent=2)


def parse_week(value: str) -> dt.date:
    """A week, given as this/next/last or any date inside it."""
    today = dt.date.today()
    if value in {"this", "current"}:
        return download.current_week(today)
    if value == "next":
        return download.current_week(today) + dt.timedelta(days=7)
    if value in {"last", "previous"}:
        return download.current_week(today) - dt.timedelta(days=7)
    return download.week_commencing(dt.date.fromisoformat(value))


def parse_url(value: str) -> tuple[str, str]:
    canteen, separator, url = value.partition("=")
    if not separator or canteen not in download.CANTEENS:
        raise argparse.ArgumentTypeError(
            f"expected CANTEEN=URL with canteen one of {', '.join(download.CANTEENS)}"
        )
    return canteen, url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--week",
        default="this",
        help="this | next | last | any date in the week (YYYY-MM-DD). "
        "At the weekend 'this' means the week about to start.",
    )
    parser.add_argument("--date", help="show one day only (YYYY-MM-DD)")
    parser.add_argument(
        "--canteen", choices=list(parse.CANTEEN_NAMES), help="filter by canteen"
    )
    parser.add_argument("--format", choices=["table", "csv", "json"], default="table")
    parser.add_argument(
        "--url",
        action="append",
        type=parse_url,
        metavar="CANTEEN=URL",
        help="download this URL instead of guessing the name; repeatable",
    )
    parser.add_argument(
        "--out", type=Path, metavar="PATH", help="where to save the CSV "
        "(default: data/menu-<week>.csv)"
    )
    parser.add_argument("--no-save", action="store_true", help="do not write a CSV")
    parser.add_argument("--force", action="store_true", help="re-download even if cached")
    parser.add_argument("--cache-dir", type=Path, default=download.DEFAULT_CACHE_DIR)
    args = parser.parse_args(argv)

    monday = parse_week(args.week)
    dishes, problems = collect(
        monday, args.cache_dir, urls=dict(args.url or []), force=args.force
    )

    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    if not dishes:
        print("error: no menu could be read for this week", file=sys.stderr)
        return 1

    # Saved before filtering: the CSV is the week's dataset, so a one-day lookup
    # never overwrites the full week with a subset.
    if not args.no_save:
        out = args.out or CSV_DIR / f"menu-{monday:%Y-%m-%d}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_csv(dishes))
        print(f"saved {len(dishes)} rows to {out}", file=sys.stderr)

    if args.date:
        wanted = dt.date.fromisoformat(args.date)
        dishes = [d for d in dishes if d.date in (wanted, None)]  # None = all week
    if args.canteen:
        dishes = [d for d in dishes if d.canteen == args.canteen]

    if args.format == "csv":
        print(render_csv(dishes), end="")
    elif args.format == "json":
        print(render_json(dishes))
    else:
        friday = monday + dt.timedelta(days=4)
        print(f"Week {monday:%Y-%m-%d} to {friday:%Y-%m-%d}  ({len(dishes)} items)\n")
        print(render_table(dishes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
