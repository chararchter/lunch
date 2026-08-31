"""Turn the menu PDFs into structured `Dish` rows.

Neither PDF tags its content, and the text stream interleaves the columns, so the
text alone is not enough. Both files do *draw* their table, though, and those
rules give exact cell boundaries -- more reliable than guessing at whitespace,
since the gap between two days varies week to week and is sometimes smaller than
the line spacing inside a single cell.

Each cell is then read top to bottom, where a "NNN kcal per portion" line closes
off a dish. The layouts differ:

* Main Event -- one day per row, the columns are Main / Side. Reported as
                "Murrays", which is what the counter is actually called; the PDF
                only ever titles itself "MAIN EVENT".
* Sprout     -- one day per cell, and each page is a price tier.

Sprout's tiers are *not* in a fixed page order (pages 1 and 2 swapped between
w/c 17.08.26 and w/c 31.08.26), so the tier is identified by its price band
rather than by which page it landed on.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import pdfplumber

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

DAY_TOKENS = {
    "MON": 0, "MONDAY": 0,
    "TUE": 1, "TUES": 1, "TUESDAY": 1,
    "WED": 2, "WEDS": 2, "WEDNESDAY": 2,
    "THU": 3, "THUR": 3, "THURS": 3, "THURSDAY": 3,
    "FRI": 4, "FRIDAY": 4,
}

# "570 kcal per portion" / "193kcal per portion" / "14 kcal per 100g".
# "kal" is a typo that shows up in the source PDFs often enough to accept.
KCAL_RE = re.compile(r"(\d+)\s*(?:kcal|kal|cal)\s*per\s*(portion|100\s*g)", re.I)
# "3.50/4.20" -- small/large price
PRICE_RE = re.compile(r"^£?(\d+\.\d{2})\s*/\s*£?(\d+\.\d{2})$")
# Dietary flags trailing a dish name. The spacing is inconsistent: "( Ve)" occurs.
TAG_RE = re.compile(r"\(\s*(VE|V|GF|VEG)\s*\)", re.I)
# The week the PDF is for, printed inside the Main Event page ("31/08/26").
PDF_DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{2})\b")

NOISE_LINE_RE = re.compile(
    r"^(main\s*event|sprout|flourishes|subject\s*to\s*change|top\s*picks.*|"
    r"w\.?c\.?|\d{2}[/.]\d{2}[/.]\d{2})$",
    re.I,
)

# The counters as they are reported. The PDF titles its first counter "MAIN
# EVENT"; it is reported as Murrays, which is what it is actually called.
MURRAYS = "Murrays"
SPROUT = "Sprout"
CANTEEN_NAMES = (MURRAYS, SPROUT)

MAIN_EVENT_SECTIONS = ["Main", "Side"]
# Sprout's priced tiers, most expensive first -- that ranking is what identifies
# them, since the page order is not stable.
SPROUT_TIERS = ["Hot dish", "Salad & grains", "Vegetable sides"]
FLOURISHES = "Flourishes"
SECTION_ORDER = MAIN_EVENT_SECTIONS + SPROUT_TIERS + [FLOURISHES]

LINE_TOL = 3.0
RULE_TOL = 2.0
MERGE_TOL = 3.0
MIN_V_RULE = 0.30
MIN_H_RULE = 0.50


@dataclass
class Dish:
    """One orderable item.

    `date` is None for items available all week (Sprout's garnish bar).
    `vegetarian` and `vegan` are None when the PDF does not say -- Murrays did
    not label at all before the w/c 31.08.26 redesign, and Sprout's hot dish tier
    labels only erratically (0 of 8 dishes one week, 1 of 8 the next), so a
    missing tag is not evidence of meat.
    """

    date: dt.date | None
    canteen: str
    section: str
    dish: str
    calories: int | None
    price_small_gbp: float | None = None
    price_large_gbp: float | None = None
    vegetarian: bool | None = None
    vegan: bool | None = None
    per: str = "portion"

    def as_row(self) -> dict:
        row = asdict(self)
        row["date"] = self.date.isoformat() if self.date else None
        return row


# --------------------------------------------------------------------------
# the drawn grid
# --------------------------------------------------------------------------

def _merge(positions: list[float], lo: float, hi: float) -> list[tuple[float, float]]:
    edges: list[float] = []
    for value in sorted([lo, *positions, hi]):
        if not edges or value - edges[-1] > MERGE_TOL:
            edges.append(value)
    return list(zip(edges, edges[1:]))


def _grid(page) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Column and row bands, read from the table rules the PDF draws."""
    verticals, horizontals = [], []
    for rule in page.lines:
        width = abs(rule["x1"] - rule["x0"])
        height = abs(rule["bottom"] - rule["top"])
        if width <= RULE_TOL and height >= MIN_V_RULE * page.height:
            verticals.append(rule["x0"])
        elif height <= RULE_TOL and width >= MIN_H_RULE * page.width:
            horizontals.append(rule["top"])
    return _merge(verticals, 0, page.width), _merge(horizontals, 0, page.height)


def _band(value: float, bands: list[tuple[float, float]]) -> int | None:
    for index, (start, end) in enumerate(bands):
        if start <= value < end:
            return index
    return None


# --------------------------------------------------------------------------
# words, lines, text
# --------------------------------------------------------------------------

def _day_index(text: str) -> int | None:
    return DAY_TOKENS.get(text.strip().strip(".,:").upper())


def _lines(words: list[dict], tol: float = LINE_TOL) -> list[list[dict]]:
    buckets: dict[int, list[dict]] = {}
    for word in words:
        buckets.setdefault(int(word["top"] / tol), []).append(word)
    merged: list[list[dict]] = []
    for key in sorted(buckets):
        line = buckets[key]
        if merged and abs(min(w["top"] for w in line) - min(w["top"] for w in merged[-1])) <= tol:
            merged[-1].extend(line)
        else:
            merged.append(line)
    return [sorted(line, key=lambda w: w["x0"]) for line in merged]


def _text_of(line: list[dict]) -> str:
    return re.sub(r"\s+", " ", " ".join(w["text"] for w in line)).strip()


def _strip_noise(words: list[dict]) -> list[dict]:
    """Drop page furniture, but only where it makes up a whole line."""
    return [w for line in _lines(words) if not NOISE_LINE_RE.match(_text_of(line)) for w in line]


def _titlecase(text: str) -> str:
    out = text.title()
    out = re.sub(r"'(\w)", lambda m: "'" + m[1].lower(), out)                 # Goat'S -> Goat's
    out = re.sub(r"\b(\d+)([A-Za-z]+)", lambda m: m[1] + m[2].lower(), out)   # 6Oz -> 6oz
    return out


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ,&-").replace("’", "'")


def _diet(name: str) -> tuple[bool | None, bool | None]:
    """(vegetarian, vegan) from the dish's tags.

    Only an explicit tag is evidence. An untagged dish is unknown, never assumed
    to contain meat, because the labelling is inconsistent between tiers.
    A "(V)" is evidence of *not* vegan, though: the labeller distinguishes the two.
    """
    tags = {t.upper() for t in TAG_RE.findall(name)}
    if "VE" in tags:
        return True, True
    if tags & {"V", "VEG"}:
        return True, False
    return None, None


def _make_dish(parts: list[str], kcal: int | None, per: str, **kwargs) -> Dish | None:
    name = _clean(" ".join(parts))
    vegetarian, vegan = _diet(name)
    name = _clean(TAG_RE.sub("", name))
    if len(name) < 3 or NOISE_LINE_RE.match(name):
        return None
    return Dish(
        dish=_titlecase(name),
        calories=kcal,
        per=per,
        vegetarian=vegetarian,
        vegan=vegan,
        **kwargs,
    )


def _dishes_in_cell(lines: list[list[dict]], **kwargs) -> list[Dish]:
    """Read a cell top to bottom: text accumulates until a kcal line closes a dish.

    A price applies to every dish above it that has none yet, which covers both
    Sprout layouts -- one price per cell, and one price per dish.
    """
    dishes: list[Dish] = []
    parts: list[str] = []
    for line in lines:
        # Deliberately not _clean()ed: a fragment often ends in "&" or "," that
        # joins it to the next line ("CRISP LEAF SALAD &" / "CHILLI LEMON SAUCE").
        text = _text_of(line).replace("’", "'")
        if not text:
            continue

        price = PRICE_RE.match(text.replace(" ", ""))
        if price:
            small, large = float(price[1]), float(price[2])
            for dish in dishes:
                if dish.price_small_gbp is None:
                    dish.price_small_gbp, dish.price_large_gbp = small, large
            continue

        kcal = KCAL_RE.search(text)
        if kcal:
            before = text[: kcal.start()].strip()
            if before:
                parts.append(before)
            dish = _make_dish(parts, int(kcal[1]), "100g" if "100" in kcal[2] else "portion", **kwargs)
            if dish:
                dishes.append(dish)
            parts = []
            continue

        parts.append(text)

    # Text left over with no kcal figure is still a real dish (a guest chef
    # takeover, for instance); keep it rather than silently dropping it.
    leftover = _make_dish(parts, None, "portion", **kwargs)
    if leftover:
        dishes.append(leftover)
    return dishes


# --------------------------------------------------------------------------
# reading a gridded page
# --------------------------------------------------------------------------

def _parse_pages(path: Path, canteen: str, monday: dt.date, *, day_per_cell: bool):
    """Yield (page number, column number, dish) for every dish in the file."""
    out: list[tuple[int, int, Dish]] = []

    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages):
            columns, rows = _grid(page)
            words = page.extract_words()
            day_words = [w for w in words if _day_index(w["text"]) is not None]
            content = _strip_noise([w for w in words if _day_index(w["text"]) is None])
            if not content:
                continue

            if not day_words:
                # A page with no days at all: Sprout's all-week garnish bar.
                for dish in _dishes_in_cell(_lines(content), date=None, canteen=canteen, section=""):
                    out.append((number, 0, dish))
                continue

            label_columns = {_band(w["x0"], columns) for w in day_words}
            content_columns = [
                c for c in range(len(columns)) if day_per_cell or c not in label_columns
            ]

            for top, bottom in rows:
                labels = [w for w in day_words if top <= w["top"] < bottom]
                if not labels:
                    continue
                for position, column in enumerate(content_columns):
                    if day_per_cell:
                        here = [w for w in labels if _band(w["x0"], columns) == column]
                        if not here:
                            continue
                        offset = _day_index(here[0]["text"])
                    else:
                        offset = _day_index(labels[0]["text"])

                    cell = [
                        w
                        for w in content
                        if _band(w["x0"], columns) == column
                        and top <= (w["top"] + w["bottom"]) / 2 < bottom
                    ]
                    if not cell:
                        continue
                    date = monday + dt.timedelta(days=offset)
                    for dish in _dishes_in_cell(
                        _lines(cell), date=date, canteen=canteen, section=""
                    ):
                        out.append((number, position, dish))
    return out


# --------------------------------------------------------------------------
# the two canteens
# --------------------------------------------------------------------------

def parse_main_event(path: Path, monday: dt.date) -> list[Dish]:
    """Sections come from the column: Main on the left, Side on the right."""
    dishes = []
    for _, column, dish in _parse_pages(path, MURRAYS, monday, day_per_cell=False):
        dish.section = (
            MAIN_EVENT_SECTIONS[column]
            if column < len(MAIN_EVENT_SECTIONS)
            else f"Column {column + 1}"
        )
        dishes.append(dish)
    return dishes


def parse_sprout(path: Path, monday: dt.date) -> list[Dish]:
    """Sections come from the price band, since the page order is not stable.

    Ranking the pages by their top price rather than matching exact figures means
    a price rise does not silently relabel the whole menu.
    """
    found = _parse_pages(path, SPROUT, monday, day_per_cell=True)

    top_price: dict[int, float] = {}
    for page, _, dish in found:
        if dish.price_large_gbp is not None:
            top_price[page] = max(top_price.get(page, 0.0), dish.price_large_gbp)

    ranked = sorted(top_price, key=lambda page: top_price[page], reverse=True)
    section_of = {
        page: SPROUT_TIERS[rank] if rank < len(SPROUT_TIERS) else f"Tier {rank + 1}"
        for rank, page in enumerate(ranked)
    }

    dishes = []
    for page, _, dish in found:
        dish.section = section_of.get(page, FLOURISHES)
        dishes.append(dish)
    return dishes


PARSERS = {"main-event": parse_main_event, "sprout": parse_sprout}


def parse(canteen: str, path: Path, monday: dt.date) -> list[Dish]:
    return PARSERS[canteen](path, monday)


def week_printed_in(path: Path) -> dt.date | None:
    """The week commencing date printed inside the PDF, if it prints one.

    The Murrays PDF stamps "31/08/26" on the page, which lets us confirm the file
    really is the week we asked for rather than trusting the file name.
    """
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:1]:
            match = PDF_DATE_RE.search(page.extract_text() or "")
            if match:
                day, month, year = (int(g) for g in match.groups())
                try:
                    return dt.date(2000 + year, month, day)
                except ValueError:
                    return None
    return None


def sort_key(dish: Dish) -> tuple:
    section = SECTION_ORDER.index(dish.section) if dish.section in SECTION_ORDER else 99
    return (dish.date or dt.date.max, dish.canteen, section, dish.dish)
