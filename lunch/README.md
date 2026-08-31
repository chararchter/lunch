# lunch

Downloads the two weekly Murray's cafeteria menus from the Wellcome Genome
Campus site, reads them, and prints every option as one table.

```bash
uv run menu.py                       # the coming week
uv run menu.py --week 2026-08-17     # any date inside the week you want
uv run menu.py --date 2026-09-02     # one day
uv run menu.py --canteen Sprout
uv run menu.py --format json
uv run menu.py --out lunch.csv         # choose where the CSV goes
```

Every run saves the full week to `data/menu-<week>.csv` and says so. It is
written *before* `--date` / `--canteen` filtering, so a one-day lookup never
overwrites the week's file with a subset. `--no-save` skips it.

## Columns

| column | notes |
|---|---|
| `date` | `YYYY-MM-DD`. Empty means available all week (Sprout's garnish bar). |
| `canteen` | `Murrays` or `Sprout` |
| `section` | Main / Side, or Sprout's price tier |
| `dish` | |
| `calories` | per portion, or marked `/100g` for the garnish bar |
| `price_small_gbp` | plain number, always pounds. Empty for Murrays. |
| `price_large_gbp` | the larger portion |
| `vegetarian` | `yes` / `no` / **empty when the PDF does not say** |
| `vegan` | same |

Dates rather than weekday names, so there is never a question of which week a
row belongs to.

**The diet flags are deliberately nullable.** Only an explicit tag counts as
evidence. Murrays carried no tags at all until the w/c 31.08.26 redesign, and
Sprout's hot dish tier labels erratically — 0 of 8 dishes one week, 1 of 8 the
next — so a missing tag is not evidence that a dish contains meat. An explicit
`(V)` *is* evidence of not-vegan, since the labeller distinguishes it from `(VE)`.

**Murrays PDFs carry no prices**, so those price cells are empty.

## How it finds the PDFs

The menus index page is behind a WordPress login and every anonymous route to the
file list is closed — REST `media` and `pages` both 403, no directory listing, no
attachment sitemap — so the URLs are rebuilt from the week's Monday.

The naming is not stable, and the two canteens do not follow the same convention
as each other:

```
W.C.-17.08.26-MAIN-EVENT.pdf           up to w/c 17.08.26
W.C.-10.08.26-Main-Event.pdf           casing varies week to week
Main-Event-Weekly-31.8.26.pdf          from w/c 31.08.26, unpadded date
SPROUT-WEEKLY-MENU-W.C-31.8.26.pdf     Sprout renamed differently again
```

So each canteen has its own list of named templates in `download.py`, newest
first, tried against both date paddings and the neighbouring upload folders.
Whichever wins is remembered in `data/url-templates.json` and probed first next
week, so the steady state is a single request.

When it changes again, either add a template or just pass the link:

```bash
uv run menu.py --url main-event=https://.../whatever-it-is-now.pdf
```

PDFs cache under `data/pdfs/`; `--force` re-downloads.

At the weekend `--week this` means the week about to start — asking on Sunday is
asking about tomorrow, not about the five days that just finished.

## How it reads them

Neither PDF tags its content, and the text stream interleaves the columns. But
both *draw* their table as real line objects, and `parse.py` reads those rules to
get exact cell boundaries — more reliable than whitespace, since the gap between
two days varies week to week and is sometimes smaller than the line spacing
inside a single cell. Each cell is then read top to bottom, where a
`NNN kcal per portion` line closes off a dish.

| | grid | sections |
|---|---|---|
| **Murrays** (titled "Main Event" in the PDF) | one day per row | columns: Main / Side |
| **Sprout** | one day per cell | price tier (see below) |

**Sprout's tiers are not in a fixed page order** — pages 1 and 2 swapped between
w/c 17.08.26 and w/c 31.08.26. Tiers are identified by ranking each page's top
price (Hot dish > Salad & grains > Vegetable sides, and unpriced = Flourishes),
so neither a reshuffle nor a price rise silently relabels the menu.

Other quirks handled: dish names wrap mid-phrase and are rejoined across lines;
Sprout prices are per cell on some pages and per dish on others; Sprout does not
serve on Fridays; tags appear as `(VE)`, `(v)` and `( Ve)`; the source PDFs
contain typos (`442 kal per portion`, `193kcal`); and some days have no calorie
figure at all (a guest chef takeover), which is kept rather than dropped.

The Murrays PDF stamps its own week inside the page, which is checked against
the week requested — a filename is a guess, that stamp is not.

## Tests

```bash
uv run pytest
```

Mostly invariants that must hold for any week — every calorie figure becomes
exactly one dish, dates land inside the right working week, tiers rank by price,
diet flags are never guessed — plus golden values for both PDF generations. They
run against the PDF cache; populate it with `uv run download.py`.
