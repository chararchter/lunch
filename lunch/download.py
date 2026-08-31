"""Download the weekly Murray's cafeteria menu PDFs (Wellcome Genome Campus).

The menus index page is behind a WordPress login, and every anonymous route to
the file list is closed (REST media and pages both 403, no directory listing, no
attachment sitemap), so the URLs have to be reconstructed from the week's Monday.

The catch is that the naming is not stable. Two schemes have been seen, and the
canteens do not follow the same one as each other::

    W.C.-17.08.26-MAIN-EVENT.pdf            up to w/c 17.08.26
    W.C.-10.08.26-Main-Event.pdf            casing varies week to week
    Main-Event-Weekly-31.8.26.pdf           from w/c 31.08.26, unpadded date
    SPROUT-WEEKLY-MENU-W.C-31.8.26.pdf      Sprout renamed differently again

So each canteen gets its own list of named templates, newest first, and whichever
one wins is remembered in `data/url-templates.json` and tried first next week.
When the naming changes again the templates below are the only thing to update --
or pass the URL directly, which is always the escape hatch.
"""

from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

BASE_URL = "https://campuslife.wellcomegenomecampus.com"
UPLOADS_URL = f"{BASE_URL}/wp-content/uploads"
MENUS_PAGE_URL = f"{BASE_URL}/catering-info-murrays-menus-2450/"

USER_AGENT = "lunch-menu-fetcher/1.0 (personal use)"
TIMEOUT = 30
PROBE_TIMEOUT = 8
PROBE_WORKERS = 8

CANTEENS = ("main-event", "sprout")

# File name templates, most recently seen first. "{date}" is filled in with each
# of DATE_FORMATS in turn.
TEMPLATES: dict[str, tuple[str, ...]] = {
    "main-event": (
        "Main-Event-Weekly-{date}",
        "W.C.-{date}-MAIN-EVENT",
        "W.C.-{date}-Main-Event",
        "W.C.-{date}-main-event",
    ),
    "sprout": (
        "SPROUT-WEEKLY-MENU-W.C-{date}",
        "W.C.-{date}-SPROUT",
        "W.C.-{date}-Sprout",
        "W.C.-{date}-sprout",
    ),
}

# Both zero-padded and bare have been used ("17.08.26" then "31.8.26").
DATE_FORMATS: dict[str, str] = {
    "dd.mm.yy": "{d:02d}.{m:02d}.{y:02d}",
    "d.m.yy": "{d}.{m}.{y:02d}",
}

DEFAULT_CACHE_DIR = Path(__file__).parent / "data" / "pdfs"
LEARNED_PATH = Path(__file__).parent / "data" / "url-templates.json"


class MenuNotFound(LookupError):
    """No PDF could be located for the requested canteen and week."""


@dataclass(frozen=True)
class Candidate:
    url: str
    template: str
    date_format: str


@dataclass(frozen=True)
class MenuPDF:
    """A downloaded menu PDF on disk."""

    canteen: str
    week_commencing: dt.date
    url: str
    path: Path


# --------------------------------------------------------------------------
# weeks
# --------------------------------------------------------------------------

def week_commencing(day: dt.date) -> dt.date:
    """The Monday of the week containing `day`."""
    return day - dt.timedelta(days=day.weekday())


def current_week(today: dt.date | None = None) -> dt.date:
    """The week the menu is wanted for.

    On a weekday that is this week. At the weekend the working week is over, so
    it rolls forward -- asking on Sunday means asking about tomorrow, not about
    the five days that have just finished.
    """
    today = today or dt.date.today()
    if today.weekday() >= 5:  # Saturday or Sunday
        return week_commencing(today + dt.timedelta(days=7 - today.weekday()))
    return week_commencing(today)


def _upload_months(monday: dt.date) -> list[tuple[int, int]]:
    """Upload folders worth trying, most likely first.

    WordPress files land in the folder of their upload date, usually the same
    month as the Monday, but a menu published early can land in the previous one.
    """
    sunday = monday + dt.timedelta(days=6)
    previous = monday.replace(day=1) - dt.timedelta(days=1)
    months: list[tuple[int, int]] = []
    for day in (monday, sunday, previous):
        if (day.year, day.month) not in months:
            months.append((day.year, day.month))
    return months


# --------------------------------------------------------------------------
# what worked last time
# --------------------------------------------------------------------------

def _load_learned() -> dict[str, dict[str, str]]:
    try:
        return json.loads(LEARNED_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _remember(canteen: str, candidate: Candidate) -> None:
    """Record the winning template so next week starts with one request."""
    learned = _load_learned()
    learned[canteen] = {"template": candidate.template, "date_format": candidate.date_format}
    try:
        LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEARNED_PATH.write_text(json.dumps(learned, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass  # a cache we cannot write is not worth failing over


# --------------------------------------------------------------------------
# candidate URLs
# --------------------------------------------------------------------------

def candidate_urls(canteen: str, monday: dt.date) -> list[Candidate]:
    """Every URL worth probing, best guess first."""
    try:
        templates = TEMPLATES[canteen]
    except KeyError:
        raise ValueError(f"unknown canteen {canteen!r}; expected one of {sorted(TEMPLATES)}") from None

    candidates: list[Candidate] = []
    for year, month in _upload_months(monday):
        for template in templates:
            for name, pattern in DATE_FORMATS.items():
                date = pattern.format(d=monday.day, m=monday.month, y=monday.year % 100)
                stem = template.format(date=date)
                candidates.append(
                    Candidate(f"{UPLOADS_URL}/{year}/{month:02d}/{stem}.pdf", template, name)
                )

    # Whatever worked last week is the best guess for this one.
    learned = _load_learned().get(canteen)
    if learned:
        candidates.sort(
            key=lambda c: (
                c.template != learned.get("template"),
                c.date_format != learned.get("date_format"),
            )
        )
    return candidates


def _exists(url: str) -> bool:
    """True if `url` serves an actual PDF.

    A missing upload 302s to the login page, so redirects are not followed: a hit
    is a plain 200 with a PDF content type.
    """
    try:
        response = requests.head(
            url,
            timeout=PROBE_TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException:
        return False
    return response.status_code == 200 and "pdf" in response.headers.get("Content-Type", "").lower()


def resolve(canteen: str, monday: dt.date) -> Candidate:
    """Find the live PDF for a week, or raise `MenuNotFound`.

    The front runner is probed on its own, so the usual case -- the naming is
    what it was last week -- costs a single request. Only if it misses are the
    rest probed, in parallel chunks, still preferring the better guess over the
    faster answer.
    """
    candidates = candidate_urls(canteen, monday)
    if _exists(candidates[0].url):
        return candidates[0]

    rest = candidates[1:]
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        for start in range(0, len(rest), PROBE_WORKERS):
            chunk = rest[start : start + PROBE_WORKERS]
            for candidate, found in zip(chunk, pool.map(_exists, [c.url for c in chunk])):
                if found:
                    return candidate

    raise MenuNotFound(
        f"no {canteen} PDF for week commencing {monday:%Y-%m-%d}. Tried "
        f"{len(candidates)} name variants. Either it is not published yet, or the "
        f"naming changed again — open {MENUS_PAGE_URL}, copy the link, and pass it "
        f"with --url {canteen}=<url>."
    )


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def download(
    canteen: str,
    monday: dt.date,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    *,
    url: str | None = None,
    force: bool = False,
) -> MenuPDF:
    """Download one canteen's PDF for a week, caching it under `cache_dir`."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{monday:%Y-%m-%d}-{canteen}.pdf"
    if path.exists() and not force:
        return MenuPDF(canteen, monday, url or "cached", path)

    candidate = None
    if url is None:
        candidate = resolve(canteen, monday)
        url = candidate.url

    response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise MenuNotFound(f"{url} did not return a PDF ({len(response.content)} bytes of something else)")

    path.write_bytes(response.content)
    if candidate is not None:
        _remember(canteen, candidate)
    return MenuPDF(canteen, monday, url, path)


def download_week(
    monday: dt.date | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    *,
    urls: dict[str, str] | None = None,
    force: bool = False,
) -> tuple[list[MenuPDF], list[str]]:
    """Download both canteens for a week.

    Returns what was found plus the problems, so one missing canteen still gets
    you the other one's menu.
    """
    monday = monday or current_week()
    urls = urls or {}
    found: list[MenuPDF] = []
    problems: list[str] = []
    for canteen in CANTEENS:
        try:
            found.append(
                download(canteen, monday, cache_dir, url=urls.get(canteen), force=force)
            )
        except (MenuNotFound, requests.RequestException) as exc:
            problems.append(str(exc))
    return found, problems


if __name__ == "__main__":
    week = current_week()
    pdfs, issues = download_week(week)
    for pdf in pdfs:
        print(f"{pdf.canteen:12} {pdf.path.name}  <- {pdf.url}")
    for issue in issues:
        print(f"warning: {issue}")
