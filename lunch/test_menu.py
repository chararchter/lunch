"""Tests for the menu pipeline.

The PDFs change every week -- and have twice changed shape mid-stream -- so most
of these are invariants that must hold for *any* week rather than golden values
for one of them. They run against whatever is in the PDF cache; populate it with
`uv run download.py`.
"""

from __future__ import annotations

import datetime as dt
import re

import pdfplumber
import pytest

import download
import parse

CACHED = sorted(download.DEFAULT_CACHE_DIR.glob("*.pdf"))

# Every dish is priced per portion or per 100g, so counting those markers gives a
# dish count that does not depend on the parser's own regex.
MARKER_RE = re.compile(r"per\s*(?:portion|100\s*g)", re.I)


def _canteen_of(path):
    return "sprout" if path.stem.endswith("sprout") else "main-event"


def _monday_of(path):
    return dt.date.fromisoformat(path.stem[:10])


def _dishes(path):
    return parse.parse(_canteen_of(path), path, _monday_of(path))


def _markers(path) -> int:
    with pdfplumber.open(path) as pdf:
        return sum(
            len(MARKER_RE.findall(re.sub(r"\s+", " ", page.extract_text() or "")))
            for page in pdf.pages
        )


# --------------------------------------------------------------------------
# weeks
# --------------------------------------------------------------------------

def test_week_commencing_snaps_to_monday():
    assert download.week_commencing(dt.date(2026, 8, 20)) == dt.date(2026, 8, 17)
    assert download.week_commencing(dt.date(2026, 8, 17)) == dt.date(2026, 8, 17)


def test_weekend_rolls_forward_to_the_coming_week():
    """Asking on Sunday means asking about tomorrow, not the week just finished."""
    assert download.current_week(dt.date(2026, 8, 30)) == dt.date(2026, 8, 31)  # Sunday
    assert download.current_week(dt.date(2026, 8, 29)) == dt.date(2026, 8, 31)  # Saturday
    assert download.current_week(dt.date(2026, 8, 28)) == dt.date(2026, 8, 24)  # Friday
    assert download.current_week(dt.date(2026, 8, 31)) == dt.date(2026, 8, 31)  # Monday


# --------------------------------------------------------------------------
# URL templates
# --------------------------------------------------------------------------

def test_canteens_do_not_share_a_naming_convention():
    """The whole point of per-canteen templates: Sprout was renamed differently."""
    monday = dt.date(2026, 8, 31)
    main = {c.url for c in download.candidate_urls("main-event", monday)}
    sprout = {c.url for c in download.candidate_urls("sprout", monday)}
    assert any(u.endswith("Main-Event-Weekly-31.8.26.pdf") for u in main)
    assert any(u.endswith("SPROUT-WEEKLY-MENU-W.C-31.8.26.pdf") for u in sprout)


def test_both_date_paddings_are_tried():
    """"17.08.26" became "31.8.26" without warning."""
    urls = {c.url for c in download.candidate_urls("main-event", dt.date(2026, 8, 3))}
    assert any(u.endswith("W.C.-03.08.26-MAIN-EVENT.pdf") for u in urls)
    assert any(u.endswith("Main-Event-Weekly-3.8.26.pdf") for u in urls)


def test_older_naming_still_reachable():
    urls = {c.url for c in download.candidate_urls("main-event", dt.date(2026, 8, 10))}
    assert any(u.endswith("W.C.-10.08.26-Main-Event.pdf") for u in urls), "casing seen in the wild"
    assert any("/2026/07/" in u for u in urls), "can land in the previous month's folder"


def test_learned_template_is_probed_first(monkeypatch):
    monkeypatch.setattr(
        download,
        "_load_learned",
        lambda: {"main-event": {"template": "W.C.-{date}-Main-Event", "date_format": "dd.mm.yy"}},
    )
    first = download.candidate_urls("main-event", dt.date(2026, 8, 10))[0]
    assert first.url.endswith("W.C.-10.08.26-Main-Event.pdf")


def test_candidates_are_unique_and_unknown_canteen_rejected():
    urls = [c.url for c in download.candidate_urls("sprout", dt.date(2026, 8, 31))]
    assert len(urls) == len(set(urls)), "no wasted duplicate probes"
    with pytest.raises(ValueError):
        download.candidate_urls("canteen-that-does-not-exist", dt.date(2026, 8, 31))


# --------------------------------------------------------------------------
# parsing invariants, over every cached week
# --------------------------------------------------------------------------

@pytest.mark.skipif(not CACHED, reason="no PDFs cached; run `uv run download.py`")
@pytest.mark.parametrize("path", CACHED, ids=lambda p: p.stem)
class TestEveryCachedWeek:
    def test_every_dish_in_the_pdf_is_parsed(self, path):
        with_kcal = [d for d in _dishes(path) if d.calories is not None]
        assert len(with_kcal) == _markers(path)

    def test_dates_land_on_the_right_working_week(self, path):
        monday = _monday_of(path)
        for dish in _dishes(path):
            if dish.date is None:
                continue  # available all week
            assert monday <= dish.date <= monday + dt.timedelta(days=4), dish
            assert dish.date.weekday() < 5, dish

    def test_dishes_are_named_and_sectioned(self, path):
        for dish in _dishes(path):
            assert len(dish.dish) > 2, dish
            assert dish.section in parse.SECTION_ORDER, dish
            assert len(dish.dish) < 160, dish  # a cell that bled would be huge

    def test_prices_are_plain_numbers(self, path):
        for dish in _dishes(path):
            for price in (dish.price_small_gbp, dish.price_large_gbp):
                assert price is None or isinstance(price, float), dish
            if dish.price_small_gbp is not None:
                assert dish.price_large_gbp >= dish.price_small_gbp, dish

    def test_diet_flags_are_true_or_unknown_never_guessed(self, path):
        """A missing tag means unknown; only an explicit "(V)" proves not-vegan."""
        for dish in _dishes(path):
            assert dish.vegetarian in (True, None), dish
            if dish.vegan is False:
                assert dish.vegetarian is True, dish
            if dish.vegan is True:
                assert dish.vegetarian is True, dish

    def test_sprout_dishes_are_priced(self, path):
        if _canteen_of(path) != "sprout":
            pytest.skip("Murrays PDFs carry no prices")
        for dish in _dishes(path):
            if dish.section == parse.FLOURISHES:
                continue  # the garnish bar is free
            assert dish.price_small_gbp and dish.price_large_gbp, dish

    def test_sprout_tiers_are_identified_by_price_not_page_order(self, path):
        """Pages 1 and 2 swapped between weeks; the price band must decide."""
        if _canteen_of(path) != "sprout":
            pytest.skip("Murrays has no price tiers")
        by_section: dict[str, set] = {}
        for dish in _dishes(path):
            by_section.setdefault(dish.section, set()).add(dish.price_large_gbp)
        assert by_section[parse.FLOURISHES] == {None}
        tiers = [t for t in parse.SPROUT_TIERS if t in by_section]
        tops = [max(by_section[t]) for t in tiers]
        assert tops == sorted(tops, reverse=True), "tiers must rank by price"


# --------------------------------------------------------------------------
# the two PDF generations, by golden value
# --------------------------------------------------------------------------

OLD = download.DEFAULT_CACHE_DIR / "2026-08-17-sprout.pdf"
NEW = download.DEFAULT_CACHE_DIR / "2026-08-31-sprout.pdf"
NEW_MAIN = download.DEFAULT_CACHE_DIR / "2026-08-31-main-event.pdf"
OLD_MAIN = download.DEFAULT_CACHE_DIR / "2026-08-17-main-event.pdf"


@pytest.mark.skipif(not (OLD.exists() and NEW.exists()), reason="both weeks not cached")
def test_hot_dish_tier_found_in_both_page_orders():
    """It is page 1 in the old file and page 2 in the new one."""
    for path in (OLD, NEW):
        hot = [d for d in _dishes(path) if d.section == "Hot dish"]
        assert len(hot) == 8
        assert {d.price_large_gbp for d in hot} == {4.20}


@pytest.mark.skipif(not NEW.exists(), reason="w/c 31.08.26 not cached")
def test_sprout_week_of_31_august():
    dishes = _dishes(NEW)
    assert len(dishes) == 28

    # Four days of three tiers, two dishes each; Sprout does not serve Fridays.
    for offset in range(4):
        day = dt.date(2026, 8, 31) + dt.timedelta(days=offset)
        assert len([d for d in dishes if d.date == day]) == 6
    assert not [d for d in dishes if d.date == dt.date(2026, 9, 4)]

    flourishes = [d for d in dishes if d.section == parse.FLOURISHES]
    assert len(flourishes) == 4
    assert all(d.date is None and d.per == "100g" for d in flourishes)

    # "( Ve)" -- with a space -- must be read as a tag, not left in the name.
    squash = next(d for d in dishes if d.dish.startswith("Super Bean Orzo"))
    assert (squash.vegan, squash.vegetarian) == (True, True)
    assert "Ve" not in squash.dish.replace("Vegetable", "")


@pytest.mark.skipif(not (NEW_MAIN.exists() and OLD_MAIN.exists()), reason="not cached")
def test_murrays_labelling_appeared_with_the_redesign():
    """The old format tagged nothing; the redesign tags some sides."""
    assert all(d.vegetarian is None for d in _dishes(OLD_MAIN))
    assert any(d.vegetarian is True for d in _dishes(NEW_MAIN))


@pytest.mark.skipif(not OLD_MAIN.exists(), reason="w/c 17.08.26 not cached")
def test_murrays_week_of_17_august():
    dishes = _dishes(OLD_MAIN)
    assert len(dishes) == 25

    chicken = next(d for d in dishes if d.dish.startswith("Peri Peri Chicken"))
    assert chicken.calories == 570
    assert chicken.date == dt.date(2026, 8, 17)
    assert chicken.dish.endswith("Chilli Lemon Sauce"), "line fragments must join up"
    assert chicken.price_small_gbp is None, "Murrays PDFs carry no prices"

    # Friday's second main sits far below the first; it must not get lost.
    friday = [d for d in dishes if d.date == dt.date(2026, 8, 21) and d.section == "Main"]
    assert len(friday) == 2 and any("Tofu" in d.dish for d in friday)

    assert any(d.dish.startswith("6oz Beef Burger") for d in dishes)  # not "6Oz"


@pytest.mark.skipif(not CACHED, reason="no PDFs cached")
@pytest.mark.parametrize("path", CACHED, ids=lambda p: p.stem)
def test_counter_is_reported_as_murrays(path):
    """The PDF titles itself "MAIN EVENT"; we report the counter's real name."""
    names = {d.canteen for d in _dishes(path)}
    assert names <= set(parse.CANTEEN_NAMES), names
    if _canteen_of(path) == "main-event":
        assert names == {"Murrays"}


@pytest.mark.skipif(
    not (download.DEFAULT_CACHE_DIR / "2026-08-10-main-event.pdf").exists(),
    reason="w/c 10.08.26 not cached",
)
def test_dish_without_a_calorie_figure_is_kept():
    """That week Tuesday was a guest chef takeover, listed with no kcal."""
    path = download.DEFAULT_CACHE_DIR / "2026-08-10-main-event.pdf"
    takeover = next(d for d in _dishes(path) if "Guest Chef" in d.dish)
    assert takeover.date == dt.date(2026, 8, 11) and takeover.calories is None


@pytest.mark.skipif(not NEW_MAIN.exists(), reason="w/c 31.08.26 not cached")
def test_pdf_stamps_its_own_week():
    """Used to catch a file name that points at the wrong week."""
    assert parse.week_printed_in(NEW_MAIN) == dt.date(2026, 8, 31)
