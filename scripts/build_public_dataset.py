"""Build one weekly edition of the public remote-jobs dataset (CSV + JSON + README).

The dataset lives in its own public repository (``useagnt/remote-jobs-data``),
whose GitHub Actions workflow runs a copy of this file every week. It reads
ONLY public, anonymous RemoteHunt endpoints, so it needs no secret:

* ``/public/market-stats``      headline counts, top professions, 10 countries
* ``/public/country-hubs``      open postings per published country page
* ``/public/hiring-lists``      open postings per country x profession family
* ``/public/salary/roles`` and ``/public/salary/{role}``  pay by role and seniority
* ``/public/market-breakdown``  seniority, single-country and every profession
                                (optional: a 404 drops its tables, and the
                                edition says so)
* ``/public/country-league``    every country page plus the United States, ranked:
                                only that country / with others or a region /
                                anywhere, country-only pay (optional, like the
                                breakdown; the monthly stats edition copies it)

    python build_public_dataset.py --out .                 # fetch, write an edition into the repo at .
    python build_public_dataset.py --out . --dry-run       # fetch, print the summary, write nothing
    python build_public_dataset.py --out . --save-payloads raw/   # also keep what was read
    python build_public_dataset.py --out . --from-dir raw/        # no request: build from saved payloads

Rules (runbook: ``docs/runbooks/public-dataset.md`` in the main repository):

* Aggregates only, and only the fields listed in this file: no posting, no
  employer, no account figure, no name of a site the postings come from. The
  salary endpoint's employer count and its raw low / high are NOT copied.
* Nothing unmeasured is written as 0 (invariant #58): a missing figure is an
  empty CSV cell / ``null``; a headline figure the payload lacks stops the run.
* One request per endpoint and role, no retry loop, a pause between requests
  (the anonymous limits are 30-60 an hour per endpoint).
* Week-over-week change is computed only against the newest EARLIER edition in
  ``editions/``; the first edition has none.
* An edition is ``editions/<as_of>/`` plus a copy in ``latest/``; re-running on
  the same day replaces that day's folder.
* ``LICENSE`` is rewritten every run with the CC BY 4.0 legal code alone
  (``CC_BY_4_0_LEGALCODE``), so GitHub names the licence; our attribution line
  is in the README.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_BASE = "https://remotehunt.app/api/public"
SITE = "https://remotehunt.app"
STATS_PAGE = f"{SITE}/remote-job-market-stats"
REPO_URL = "https://github.com/useagnt/remote-jobs-data"
TIMEOUT_S = 90
PAUSE_S = 2.0
USER_AGENT = "remote-jobs-data-builder (+https://github.com/useagnt/remote-jobs-data)"
# A country x family row thinner than this is left out (its postings stay in
# the country's own total).
MIN_ROW = 20

TABLE_COLUMNS: dict[str, list[str]] = {
    "summary": ["metric", "value", "share_pct", "previous_value", "change", "previous_as_of"],
    "geography": ["scope", "open", "share_pct", "previous_open", "change_open"],
    "countries": ["code", "name", "open", "share_pct", "named", "single_country",
                  "with_stated_pay", "median_pay_usd", "previous_open", "change_open"],
    "country_families": ["country_code", "country", "family", "open", "previous_open",
                         "change_open"],
    "professions": ["profession", "open", "share_pct", "with_stated_pay", "previous_open",
                    "change_open"],
    "seniority": ["band", "open", "share_pct", "with_stated_pay", "pay_p25_usd",
                  "pay_median_usd", "pay_p75_usd", "previous_open", "change_open"],
    "pay_by_role": ["role", "postings_with_pay", "pay_p25_usd", "pay_median_usd",
                    "pay_p75_usd", "window_days"],
    "pay_by_role_seniority": ["role", "band", "postings_with_pay", "pay_p25_usd",
                              "pay_median_usd", "pay_p75_usd", "window_days"],
    "country_league": ["rank", "code", "name", "country_page", "open", "share_pct",
                       "only_country", "with_others_or_region", "anywhere", "named",
                       "only_country_with_stated_pay", "only_country_median_pay_usd",
                       "previous_open", "change_open"],
}

# A country-only pay median needs this many postings that state pay (the
# salary pages' floor); the endpoint already leaves it empty below.
LEAGUE_PAY_MIN = 20

DEFINITIONS: dict[str, str] = {
    "open_postings": "Open remote job postings in RemoteHunt's public index on the counting day: "
                     "live, remote, and listed on the site's public job pages.",
    "open (country)": "Postings that can hire in the country: they say they hire worldwide, or "
                      "their list of countries or regions includes it.",
    "named (country)": "Postings whose list names the country itself, counted inside open.",
    "single_country": "Postings whose list holds exactly one country.",
    "multi_country": "Postings that list two or more countries, or a region.",
    "hire_worldwide": "Postings that say they hire anywhere.",
    "hiring_location_not_stated": "Postings that state no readable hiring location.",
    "us_only": "Postings whose list holds the United States and nothing else.",
    "europe_only": "Postings whose list holds only European countries.",
    "with_stated_pay": "Postings that state a salary or a salary range.",
    "pay": "Annual US dollars, the midpoint of the stated range. Pay by role and by role x "
           "seniority covers postings published in the last 90 days that state a full range; "
           "a figure needs at least 20 such postings, else it is left empty.",
    "profession": "The posting's profession as RemoteHunt files it; professions with fewer than "
                  "20 open postings are left out of the table, not counted as 0.",
    "family": "A group of professions (Engineering, Sales, ...) as on the site's country pages.",
    "seniority": "Junior (intern, junior, entry), Mid-level, Senior, Staff & Principal (lead, "
                 "staff, principal), Management (manager, director, VP, C-level), Other, Not stated.",
    "change": "Value minus the value in the previous edition (normally one week earlier); empty "
              "when either value is not measured.",
    "country_league": "Every country with a RemoteHunt country page, plus the United States, ranked "
                      "by open; country_page is its page on the site, empty when it has none. "
                      "only_country: postings whose list holds that country and nothing "
                      "else; with_others_or_region: postings that list it beside other countries or "
                      "through a region; anywhere: postings that hire from any country. The three "
                      "add up to open. share_pct is open out of open_postings in summary.csv.",
    "only_country_median_pay_usd": "Median of the annual US-dollar midpoints of the only_country "
                                   "postings that state pay; empty under 20 such postings.",
}


class DatasetError(Exception):
    """A reason not to write the edition; the message is printed as is."""


# ── reading ───────────────────────────────────────────────────────────────


def _get(base: str, path: str, optional: bool = False) -> Optional[dict[str, Any]]:
    request = urllib.request.Request(
        f"{base}/{path}", headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 — fixed https base
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if optional and exc.code == 404:
            return None
        raise DatasetError(f"GET {path} answered {exc.code}; nothing written.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DatasetError(f"GET {path} failed ({exc}); nothing written.") from exc
    finally:
        time.sleep(PAUSE_S)


def fetch_payloads(base: str) -> dict[str, Any]:
    payloads: dict[str, Any] = {
        "market": _get(base, "market-stats"),
        "hubs": _get(base, "country-hubs"),
        "hiring_lists": _get(base, "hiring-lists"),
        "salary_roles": _get(base, "salary/roles"),
        "breakdown": _get(base, "market-breakdown", optional=True),
        "league": _get(base, "country-league", optional=True),
        "salary": {},
    }
    for entry in (payloads["salary_roles"] or {}).get("roles", []):
        role = str(entry["role"])
        payloads["salary"][role] = _get(base, f"salary/{role}")
    return payloads


def load_payloads(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "payloads.json").read_text(encoding="utf-8"))


# ── building ──────────────────────────────────────────────────────────────


def _share(part: Optional[int], whole: int) -> Optional[float]:
    if part is None or not whole:
        return None
    return round(100 * part / whole, 1)


def _change(now: Optional[int], before: Optional[int]) -> Optional[int]:
    if now is None or before is None:
        return None
    return now - before


def _number(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatasetError(f"the market-stats payload has no measured {key!r} (got {value!r}); "
                           "nothing written.")
    return value


def _role_name(slug: str) -> str:
    return slug.replace("-", " ").capitalize()


def _place_name(slug: str) -> str:
    return " ".join(word.capitalize() for word in slug.split("-"))


def _previous_rows(previous: Optional[dict[str, Any]], table: str, key: str,
                   value: str) -> dict[str, Any]:
    if not previous:
        return {}
    return {row[key]: row.get(value) for row in previous.get("tables", {}).get(table, [])}


def league_table(league: Optional[dict[str, Any]], total: int,
                 previous: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The country league (``/public/country-league``, 2026-11-23): every
    country page plus the United States, ranked by ``open``. ``share_pct`` is
    out of the edition's own ``open_postings``, the figure summary.csv prints,
    so the table and the headline share one denominator."""
    if not league:
        return []
    prev = _previous_rows(previous, "country_league", "code", "open")
    rows = []
    for row in league.get("rows", []):
        open_, only, anywhere = int(row["open"]), int(row["only"]), int(row["anywhere"])
        others = open_ - only - anywhere
        if others < 0:
            raise DatasetError(f"league {row['code']}: open {open_} is less than only {only} "
                               f"+ anywhere {anywhere}; nothing written.")
        stated = row.get("only_pay_stated")
        median = row.get("only_pay_median")
        rows.append({
            "code": row["code"], "name": _place_name(row["slug"]),
            "country_page": f"{SITE}/remote-jobs/{row['slug']}" if row.get("page") else None,
            "open": open_,
            "share_pct": _share(open_, total), "only_country": only,
            "with_others_or_region": others, "anywhere": anywhere, "named": int(row["named"]),
            "only_country_with_stated_pay": None if stated is None else int(stated),
            "only_country_median_pay_usd": (
                int(median) if median is not None and (stated or 0) >= LEAGUE_PAY_MIN else None
            ),
            "previous_open": prev.get(row["code"]),
            "change_open": _change(open_, prev.get(row["code"])),
        })
    rows.sort(key=lambda r: (-r["open"], r["code"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def build_edition(payloads: dict[str, Any], previous: Optional[dict[str, Any]]) -> dict[str, Any]:
    """One edition: every table as a list of rows, plus definitions and notes."""
    market = payloads.get("market") or {}
    total = _number(market, "total")
    if total <= 0:
        raise DatasetError("the market-stats payload counts no postings; nothing written.")
    as_of = str(market.get("as_of") or "")
    if len(as_of) != 10:
        raise DatasetError(f"the market-stats payload has no counting day (got {as_of!r}).")
    breakdown = payloads.get("breakdown")
    notes: list[str] = []
    if breakdown is None:
        notes.append("Seniority, single-country and full profession tables are not published "
                     "in this edition (their public reader was not live yet); professions are "
                     "the top 8.")
    prev_as_of = previous.get("as_of") if previous else None

    # Summary
    prev_summary = _previous_rows(previous, "summary", "metric", "value")
    head: list[tuple[str, Optional[int]]] = [
        ("open_postings", total),
        ("hire_worldwide", _number(market, "worldwide")),
        ("hiring_location_not_stated", _number(market, "unknown")),
        ("us_only", _number(market, "us_only")),
        ("europe_only", _number(market, "europe_only")),
        ("with_stated_pay", _number(market, "with_salary")),
        ("profession_not_labelled", _number(market, "unlabelled_professions")),
    ]
    if breakdown:
        geo = breakdown["geography"]
        head[2:2] = [("single_country", int(geo["single_country"])),
                     ("multi_country", int(geo["multi_country"]))]
    summary = [
        {"metric": metric, "value": value,
         "share_pct": None if metric == "open_postings" else _share(value, total),
         "previous_value": prev_summary.get(metric),
         "change": _change(value, prev_summary.get(metric)),
         "previous_as_of": prev_as_of if metric in prev_summary else None}
        for metric, value in head
    ]

    # Geography scope
    geography: list[dict[str, Any]] = []
    if breakdown:
        prev_geo = _previous_rows(previous, "geography", "scope", "open")
        geo = breakdown["geography"]
        for scope, key in (("worldwide", "worldwide"), ("single_country", "single_country"),
                           ("multi_country_or_region", "multi_country"),
                           ("not_stated", "unknown")):
            value = int(geo[key])
            geography.append({"scope": scope, "open": value, "share_pct": _share(value, total),
                              "previous_open": prev_geo.get(scope),
                              "change_open": _change(value, prev_geo.get(scope))})

    # Countries: the 10 market-stats rows (with pay) plus every published country page.
    single_by = {row["code"]: int(row["count"])
                 for row in (breakdown or {}).get("single_country_top", [])}
    countries: dict[str, dict[str, Any]] = {}
    for row in market.get("by_country", []):
        countries[row["code"]] = {
            "code": row["code"], "name": row["name"], "open": int(row["open"]),
            "named": int(row["named"]), "with_stated_pay": row.get("with_salary"),
            "median_pay_usd": row.get("median_annual_usd"),
        }
    for row in (payloads.get("hubs") or {}).get("countries", []):
        if row["code"] not in countries:
            countries[row["code"]] = {
                "code": row["code"], "name": _place_name(row["slug"]), "open": int(row["open"]),
                "named": None, "with_stated_pay": None, "median_pay_usd": None,
            }
    prev_countries = _previous_rows(previous, "countries", "code", "open")
    country_rows = []
    for entry in sorted(countries.values(), key=lambda c: (-c["open"], c["code"])):
        country_rows.append({
            **entry,
            "share_pct": _share(entry["open"], total),
            "single_country": single_by.get(entry["code"]) if breakdown else None,
            "previous_open": prev_countries.get(entry["code"]),
            "change_open": _change(entry["open"], prev_countries.get(entry["code"])),
        })

    # Country x profession family
    code_of = {row["slug"]: row["code"] for row in (payloads.get("hubs") or {}).get("countries", [])}
    lists = payloads.get("hiring_lists") or {}
    family_name = {f["slug"]: f["name"] for f in lists.get("families", [])}
    prev_cf = {
        (r["country_code"], r["family"]): r.get("open")
        for r in (previous or {}).get("tables", {}).get("country_families", [])
    }
    country_families = []
    for row in lists.get("lists", []):
        place, family, open_ = row["place"], row["family"], int(row["open"])
        if not family or open_ < MIN_ROW:
            continue
        if place == "anywhere":
            code, name = "", "Anywhere (hires worldwide)"
        elif place in code_of:
            code, name = code_of[place], _place_name(place)
        else:
            continue
        fam = family_name.get(family, _place_name(family))
        before = prev_cf.get((code, fam))
        country_families.append({"country_code": code, "country": name, "family": fam,
                                 "open": open_, "previous_open": before,
                                 "change_open": _change(open_, before)})
    country_families.sort(key=lambda r: (r["country"], -r["open"], r["family"]))

    # Professions
    prev_prof = _previous_rows(previous, "professions", "profession", "open")
    if breakdown:
        source_rows = [(p["name"], int(p["open"]), p.get("with_salary"))
                       for p in breakdown["by_profession"]]
    else:
        source_rows = [(p["name"], int(p["count"]), None) for p in market.get("top_professions", [])]
    professions = [
        {"profession": name, "open": n, "share_pct": _share(n, total),
         "with_stated_pay": with_pay, "previous_open": prev_prof.get(name),
         "change_open": _change(n, prev_prof.get(name))}
        for name, n, with_pay in source_rows
    ]

    # Seniority
    seniority = []
    if breakdown:
        prev_sen = _previous_rows(previous, "seniority", "band", "open")
        for row in breakdown["by_seniority"]:
            n = int(row["open"])
            seniority.append({
                "band": row["band"], "open": n, "share_pct": _share(n, total),
                "with_stated_pay": row.get("with_salary"),
                "pay_p25_usd": row.get("pay_p25_usd"), "pay_median_usd": row.get("pay_median_usd"),
                "pay_p75_usd": row.get("pay_p75_usd"),
                "previous_open": prev_sen.get(row["band"]),
                "change_open": _change(n, prev_sen.get(row["band"])),
            })

    # Pay by role, and by role x seniority (only the fields below are copied).
    pay_by_role, pay_by_band = [], []
    for slug, data in sorted((payloads.get("salary") or {}).items()):
        if not data or not data.get("qualifies") or data.get("median") is None:
            continue
        role, window = _role_name(slug), data.get("window_days")
        pay_by_role.append({"role": role, "postings_with_pay": data.get("sample_size"),
                            "pay_p25_usd": data.get("p25"), "pay_median_usd": data.get("median"),
                            "pay_p75_usd": data.get("p75"), "window_days": window})
        for band in data.get("by_seniority", []):
            pay_by_band.append({"role": role, "band": band["band"],
                                "postings_with_pay": band.get("count"),
                                "pay_p25_usd": band.get("p25"),
                                "pay_median_usd": band.get("median"),
                                "pay_p75_usd": band.get("p75"), "window_days": window})
    pay_by_role.sort(key=lambda r: (-(r["postings_with_pay"] or 0), r["role"]))

    league = league_table(payloads.get("league"), total, previous)
    if payloads.get("league") is None:
        notes.append("The country league table is not published in this edition (its public "
                     "reader was not live yet).")

    tables = {
        "summary": summary, "geography": geography, "countries": country_rows,
        "country_families": country_families, "professions": professions,
        "seniority": seniority, "pay_by_role": pay_by_role, "pay_by_role_seniority": pay_by_band,
        "country_league": league,
    }
    for name, rows in tables.items():
        tables[name] = [{col: row.get(col) for col in TABLE_COLUMNS[name]} for row in rows]
    return {
        "dataset": "remote-jobs-data",
        "as_of": as_of,
        "previous_as_of": prev_as_of,
        "publisher": "RemoteHunt",
        "homepage": SITE,
        "license": "CC BY 4.0",
        "attribution": f"RemoteHunt remote jobs data ({SITE})",
        "definitions": DEFINITIONS,
        "notes": notes,
        "tables": tables,
    }


# ── writing ───────────────────────────────────────────────────────────────


def _fmt(value: Optional[int]) -> str:
    return "n/a" if value is None else f"{value:,}"


def headline(edition: dict[str, Any]) -> list[str]:
    head = {row["metric"]: row for row in edition["tables"]["summary"]}

    def line(metric: str, words: str) -> Optional[str]:
        row = head.get(metric)
        if not row:
            return None
        share = f" ({row['share_pct']}%)" if row["share_pct"] is not None else ""
        change = f", {row['change']:+,} vs {row['previous_as_of']}" if row["change"] is not None else ""
        return f"- {words}: **{_fmt(row['value'])}**{share}{change}"

    lines = [
        line("open_postings", "Open remote postings"),
        line("hire_worldwide", "Hire worldwide"),
        line("single_country", "Hire in exactly one country"),
        line("multi_country", "Hire in several countries or a region"),
        line("us_only", "United States only"),
        line("hiring_location_not_stated", "Do not say where they hire"),
        line("with_stated_pay", "State pay"),
    ]
    return [entry for entry in lines if entry]


def readme(edition: dict[str, Any], history: list[str]) -> str:
    tables = edition["tables"]
    files = "\n".join(
        f"| `{name}.csv` | {len(rows):,} | {', '.join(f'`{c}`' for c in TABLE_COLUMNS[name])} |"
        for name, rows in tables.items() if rows
    )
    definitions = "\n".join(f"- **{key}**: {text}" for key, text in DEFINITIONS.items())
    notes = "".join(f"\n> {note}\n" for note in edition["notes"])
    past = ", ".join(f"[{day}](editions/{day}/)" for day in history) or "none yet"
    return f"""# Remote jobs data

A weekly, open dataset about the remote job market: how many remote job postings are open,
where they can hire, which professions and seniority levels they ask for, and what they pay
where pay is stated. Counted by [RemoteHunt]({SITE}) from its public index of remote job
postings, with the same rules as its [remote job market stats page]({STATS_PAGE}).

**As of {edition['as_of']}** (UTC). Refreshed every Monday.
{notes}
## Headline

{chr(10).join(headline(edition))}

## Files

`latest/` holds the newest edition; `editions/<date>/` keeps every edition. Each folder has
`dataset.json` (every table plus definitions) and one CSV per table. An empty CSV cell means
"not measured", never zero.

| File | Rows | Columns |
|---|---|---|
{files}

## Definitions

{definitions}

## Method

- One reading of RemoteHunt's public, anonymous endpoints on the counting day. A posting's
  hiring geography, profession and seniority are read from the posting by RemoteHunt's
  pipeline, the same reading its job pages use.
- Aggregates only. No individual posting, no employer, and no figure about RemoteHunt's users
  is published. Rows thinner than 20 postings are left out.
- Week-over-week change compares with the previous edition in `editions/`.
- The builder is `scripts/build_public_dataset.py`; the workflow is
  `.github/workflows/weekly.yml`.

## Licence

The data (everything under `latest/` and `editions/`, and the figures in this README) is
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the full legal code
is in `LICENSE`. Use it for anything, including commercially, with attribution:
"Source: RemoteHunt remote jobs data, {SITE}" with a link to {SITE}. The builder script
(`scripts/build_public_dataset.py`) is released under the same licence.

## Editions

{past}
"""


# The Creative Commons Attribution 4.0 International legal code, word for word
# as GitHub's licence detector knows it (choosealicense.com's cc-by-4.0.txt),
# so the repository's licence reads CC-BY-4.0 and not NOASSERTION, which a
# LICENSE of our own words got (2026-11-23). Nothing of ours goes in LICENSE:
# the attribution line lives in the README.
CC_BY_4_0_LEGALCODE = """Attribution 4.0 International

=======================================================================

Creative Commons Corporation ("Creative Commons") is not a law firm and
does not provide legal services or legal advice. Distribution of
Creative Commons public licenses does not create a lawyer-client or
other relationship. Creative Commons makes its licenses and related
information available on an "as-is" basis. Creative Commons gives no
warranties regarding its licenses, any material licensed under their
terms and conditions, or any related information. Creative Commons
disclaims all liability for damages resulting from their use to the
fullest extent possible.

Using Creative Commons Public Licenses

Creative Commons public licenses provide a standard set of terms and
conditions that creators and other rights holders may use to share
original works of authorship and other material subject to copyright
and certain other rights specified in the public license below. The
following considerations are for informational purposes only, are not
exhaustive, and do not form part of our licenses.

     Considerations for licensors: Our public licenses are
     intended for use by those authorized to give the public
     permission to use material in ways otherwise restricted by
     copyright and certain other rights. Our licenses are
     irrevocable. Licensors should read and understand the terms
     and conditions of the license they choose before applying it.
     Licensors should also secure all rights necessary before
     applying our licenses so that the public can reuse the
     material as expected. Licensors should clearly mark any
     material not subject to the license. This includes other CC-
     licensed material, or material used under an exception or
     limitation to copyright. More considerations for licensors:
     wiki.creativecommons.org/Considerations_for_licensors

     Considerations for the public: By using one of our public
     licenses, a licensor grants the public permission to use the
     licensed material under specified terms and conditions. If
     the licensor's permission is not necessary for any reason--for
     example, because of any applicable exception or limitation to
     copyright--then that use is not regulated by the license. Our
     licenses grant only permissions under copyright and certain
     other rights that a licensor has authority to grant. Use of
     the licensed material may still be restricted for other
     reasons, including because others have copyright or other
     rights in the material. A licensor may make special requests,
     such as asking that all changes be marked or described.
     Although not required by our licenses, you are encouraged to
     respect those requests where reasonable. More considerations
     for the public:
     wiki.creativecommons.org/Considerations_for_licensees

=======================================================================

Creative Commons Attribution 4.0 International Public License

By exercising the Licensed Rights (defined below), You accept and agree
to be bound by the terms and conditions of this Creative Commons
Attribution 4.0 International Public License ("Public License"). To the
extent this Public License may be interpreted as a contract, You are
granted the Licensed Rights in consideration of Your acceptance of
these terms and conditions, and the Licensor grants You such rights in
consideration of benefits the Licensor receives from making the
Licensed Material available under these terms and conditions.


Section 1 -- Definitions.

  a. Adapted Material means material subject to Copyright and Similar
     Rights that is derived from or based upon the Licensed Material
     and in which the Licensed Material is translated, altered,
     arranged, transformed, or otherwise modified in a manner requiring
     permission under the Copyright and Similar Rights held by the
     Licensor. For purposes of this Public License, where the Licensed
     Material is a musical work, performance, or sound recording,
     Adapted Material is always produced where the Licensed Material is
     synched in timed relation with a moving image.

  b. Adapter's License means the license You apply to Your Copyright
     and Similar Rights in Your contributions to Adapted Material in
     accordance with the terms and conditions of this Public License.

  c. Copyright and Similar Rights means copyright and/or similar rights
     closely related to copyright including, without limitation,
     performance, broadcast, sound recording, and Sui Generis Database
     Rights, without regard to how the rights are labeled or
     categorized. For purposes of this Public License, the rights
     specified in Section 2(b)(1)-(2) are not Copyright and Similar
     Rights.

  d. Effective Technological Measures means those measures that, in the
     absence of proper authority, may not be circumvented under laws
     fulfilling obligations under Article 11 of the WIPO Copyright
     Treaty adopted on December 20, 1996, and/or similar international
     agreements.

  e. Exceptions and Limitations means fair use, fair dealing, and/or
     any other exception or limitation to Copyright and Similar Rights
     that applies to Your use of the Licensed Material.

  f. Licensed Material means the artistic or literary work, database,
     or other material to which the Licensor applied this Public
     License.

  g. Licensed Rights means the rights granted to You subject to the
     terms and conditions of this Public License, which are limited to
     all Copyright and Similar Rights that apply to Your use of the
     Licensed Material and that the Licensor has authority to license.

  h. Licensor means the individual(s) or entity(ies) granting rights
     under this Public License.

  i. Share means to provide material to the public by any means or
     process that requires permission under the Licensed Rights, such
     as reproduction, public display, public performance, distribution,
     dissemination, communication, or importation, and to make material
     available to the public including in ways that members of the
     public may access the material from a place and at a time
     individually chosen by them.

  j. Sui Generis Database Rights means rights other than copyright
     resulting from Directive 96/9/EC of the European Parliament and of
     the Council of 11 March 1996 on the legal protection of databases,
     as amended and/or succeeded, as well as other essentially
     equivalent rights anywhere in the world.

  k. You means the individual or entity exercising the Licensed Rights
     under this Public License. Your has a corresponding meaning.


Section 2 -- Scope.

  a. License grant.

       1. Subject to the terms and conditions of this Public License,
          the Licensor hereby grants You a worldwide, royalty-free,
          non-sublicensable, non-exclusive, irrevocable license to
          exercise the Licensed Rights in the Licensed Material to:

            a. reproduce and Share the Licensed Material, in whole or
               in part; and

            b. produce, reproduce, and Share Adapted Material.

       2. Exceptions and Limitations. For the avoidance of doubt, where
          Exceptions and Limitations apply to Your use, this Public
          License does not apply, and You do not need to comply with
          its terms and conditions.

       3. Term. The term of this Public License is specified in Section
          6(a).

       4. Media and formats; technical modifications allowed. The
          Licensor authorizes You to exercise the Licensed Rights in
          all media and formats whether now known or hereafter created,
          and to make technical modifications necessary to do so. The
          Licensor waives and/or agrees not to assert any right or
          authority to forbid You from making technical modifications
          necessary to exercise the Licensed Rights, including
          technical modifications necessary to circumvent Effective
          Technological Measures. For purposes of this Public License,
          simply making modifications authorized by this Section 2(a)
          (4) never produces Adapted Material.

       5. Downstream recipients.

            a. Offer from the Licensor -- Licensed Material. Every
               recipient of the Licensed Material automatically
               receives an offer from the Licensor to exercise the
               Licensed Rights under the terms and conditions of this
               Public License.

            b. No downstream restrictions. You may not offer or impose
               any additional or different terms or conditions on, or
               apply any Effective Technological Measures to, the
               Licensed Material if doing so restricts exercise of the
               Licensed Rights by any recipient of the Licensed
               Material.

       6. No endorsement. Nothing in this Public License constitutes or
          may be construed as permission to assert or imply that You
          are, or that Your use of the Licensed Material is, connected
          with, or sponsored, endorsed, or granted official status by,
          the Licensor or others designated to receive attribution as
          provided in Section 3(a)(1)(A)(i).

  b. Other rights.

       1. Moral rights, such as the right of integrity, are not
          licensed under this Public License, nor are publicity,
          privacy, and/or other similar personality rights; however, to
          the extent possible, the Licensor waives and/or agrees not to
          assert any such rights held by the Licensor to the limited
          extent necessary to allow You to exercise the Licensed
          Rights, but not otherwise.

       2. Patent and trademark rights are not licensed under this
          Public License.

       3. To the extent possible, the Licensor waives any right to
          collect royalties from You for the exercise of the Licensed
          Rights, whether directly or through a collecting society
          under any voluntary or waivable statutory or compulsory
          licensing scheme. In all other cases the Licensor expressly
          reserves any right to collect such royalties.


Section 3 -- License Conditions.

Your exercise of the Licensed Rights is expressly made subject to the
following conditions.

  a. Attribution.

       1. If You Share the Licensed Material (including in modified
          form), You must:

            a. retain the following if it is supplied by the Licensor
               with the Licensed Material:

                 i. identification of the creator(s) of the Licensed
                    Material and any others designated to receive
                    attribution, in any reasonable manner requested by
                    the Licensor (including by pseudonym if
                    designated);

                ii. a copyright notice;

               iii. a notice that refers to this Public License;

                iv. a notice that refers to the disclaimer of
                    warranties;

                 v. a URI or hyperlink to the Licensed Material to the
                    extent reasonably practicable;

            b. indicate if You modified the Licensed Material and
               retain an indication of any previous modifications; and

            c. indicate the Licensed Material is licensed under this
               Public License, and include the text of, or the URI or
               hyperlink to, this Public License.

       2. You may satisfy the conditions in Section 3(a)(1) in any
          reasonable manner based on the medium, means, and context in
          which You Share the Licensed Material. For example, it may be
          reasonable to satisfy the conditions by providing a URI or
          hyperlink to a resource that includes the required
          information.

       3. If requested by the Licensor, You must remove any of the
          information required by Section 3(a)(1)(A) to the extent
          reasonably practicable.

       4. If You Share Adapted Material You produce, the Adapter's
          License You apply must not prevent recipients of the Adapted
          Material from complying with this Public License.


Section 4 -- Sui Generis Database Rights.

Where the Licensed Rights include Sui Generis Database Rights that
apply to Your use of the Licensed Material:

  a. for the avoidance of doubt, Section 2(a)(1) grants You the right
     to extract, reuse, reproduce, and Share all or a substantial
     portion of the contents of the database;

  b. if You include all or a substantial portion of the database
     contents in a database in which You have Sui Generis Database
     Rights, then the database in which You have Sui Generis Database
     Rights (but not its individual contents) is Adapted Material; and

  c. You must comply with the conditions in Section 3(a) if You Share
     all or a substantial portion of the contents of the database.

For the avoidance of doubt, this Section 4 supplements and does not
replace Your obligations under this Public License where the Licensed
Rights include other Copyright and Similar Rights.


Section 5 -- Disclaimer of Warranties and Limitation of Liability.

  a. UNLESS OTHERWISE SEPARATELY UNDERTAKEN BY THE LICENSOR, TO THE
     EXTENT POSSIBLE, THE LICENSOR OFFERS THE LICENSED MATERIAL AS-IS
     AND AS-AVAILABLE, AND MAKES NO REPRESENTATIONS OR WARRANTIES OF
     ANY KIND CONCERNING THE LICENSED MATERIAL, WHETHER EXPRESS,
     IMPLIED, STATUTORY, OR OTHER. THIS INCLUDES, WITHOUT LIMITATION,
     WARRANTIES OF TITLE, MERCHANTABILITY, FITNESS FOR A PARTICULAR
     PURPOSE, NON-INFRINGEMENT, ABSENCE OF LATENT OR OTHER DEFECTS,
     ACCURACY, OR THE PRESENCE OR ABSENCE OF ERRORS, WHETHER OR NOT
     KNOWN OR DISCOVERABLE. WHERE DISCLAIMERS OF WARRANTIES ARE NOT
     ALLOWED IN FULL OR IN PART, THIS DISCLAIMER MAY NOT APPLY TO YOU.

  b. TO THE EXTENT POSSIBLE, IN NO EVENT WILL THE LICENSOR BE LIABLE
     TO YOU ON ANY LEGAL THEORY (INCLUDING, WITHOUT LIMITATION,
     NEGLIGENCE) OR OTHERWISE FOR ANY DIRECT, SPECIAL, INDIRECT,
     INCIDENTAL, CONSEQUENTIAL, PUNITIVE, EXEMPLARY, OR OTHER LOSSES,
     COSTS, EXPENSES, OR DAMAGES ARISING OUT OF THIS PUBLIC LICENSE OR
     USE OF THE LICENSED MATERIAL, EVEN IF THE LICENSOR HAS BEEN
     ADVISED OF THE POSSIBILITY OF SUCH LOSSES, COSTS, EXPENSES, OR
     DAMAGES. WHERE A LIMITATION OF LIABILITY IS NOT ALLOWED IN FULL OR
     IN PART, THIS LIMITATION MAY NOT APPLY TO YOU.

  c. The disclaimer of warranties and limitation of liability provided
     above shall be interpreted in a manner that, to the extent
     possible, most closely approximates an absolute disclaimer and
     waiver of all liability.


Section 6 -- Term and Termination.

  a. This Public License applies for the term of the Copyright and
     Similar Rights licensed here. However, if You fail to comply with
     this Public License, then Your rights under this Public License
     terminate automatically.

  b. Where Your right to use the Licensed Material has terminated under
     Section 6(a), it reinstates:

       1. automatically as of the date the violation is cured, provided
          it is cured within 30 days of Your discovery of the
          violation; or

       2. upon express reinstatement by the Licensor.

     For the avoidance of doubt, this Section 6(b) does not affect any
     right the Licensor may have to seek remedies for Your violations
     of this Public License.

  c. For the avoidance of doubt, the Licensor may also offer the
     Licensed Material under separate terms or conditions or stop
     distributing the Licensed Material at any time; however, doing so
     will not terminate this Public License.

  d. Sections 1, 5, 6, 7, and 8 survive termination of this Public
     License.


Section 7 -- Other Terms and Conditions.

  a. The Licensor shall not be bound by any additional or different
     terms or conditions communicated by You unless expressly agreed.

  b. Any arrangements, understandings, or agreements regarding the
     Licensed Material not stated herein are separate from and
     independent of the terms and conditions of this Public License.


Section 8 -- Interpretation.

  a. For the avoidance of doubt, this Public License does not, and
     shall not be interpreted to, reduce, limit, restrict, or impose
     conditions on any use of the Licensed Material that could lawfully
     be made without permission under this Public License.

  b. To the extent possible, if any provision of this Public License is
     deemed unenforceable, it shall be automatically reformed to the
     minimum extent necessary to make it enforceable. If the provision
     cannot be reformed, it shall be severed from this Public License
     without affecting the enforceability of the remaining terms and
     conditions.

  c. No term or condition of this Public License will be waived and no
     failure to comply consented to unless expressly agreed to by the
     Licensor.

  d. Nothing in this Public License constitutes or may be interpreted
     as a limitation upon, or waiver of, any privileges and immunities
     that apply to the Licensor or You, including from the legal
     processes of any jurisdiction or authority.


=======================================================================

Creative Commons is not a party to its public licenses.
Notwithstanding, Creative Commons may elect to apply one of its public
licenses to material it publishes and in those instances will be
considered the “Licensor.” The text of the Creative Commons public
licenses is dedicated to the public domain under the CC0 Public Domain
Dedication. Except for the limited purpose of indicating that material
is shared under a Creative Commons public license or as otherwise
permitted by the Creative Commons policies published at
creativecommons.org/policies, Creative Commons does not authorize the
use of the trademark "Creative Commons" or any other trademark or logo
of Creative Commons without its prior written consent including,
without limitation, in connection with any unauthorized modifications
to any of its public licenses or any other arrangements,
understandings, or agreements concerning use of licensed material. For
the avoidance of doubt, this paragraph does not form part of the public
licenses.

Creative Commons may be contacted at creativecommons.org.
"""


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else v for k, v in row.items()})


def write_edition(edition: dict[str, Any], out: Path) -> Path:
    folder = out / "editions" / edition["as_of"]
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    (folder / "dataset.json").write_text(
        json.dumps(edition, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    for name, rows in edition["tables"].items():
        if rows:
            write_csv(folder / f"{name}.csv", TABLE_COLUMNS[name], rows)
    latest = out / "latest"
    if latest.exists():
        shutil.rmtree(latest)
    shutil.copytree(folder, latest)
    history = sorted((p.name for p in (out / "editions").iterdir() if p.is_dir()), reverse=True)
    (out / "README.md").write_text(readme(edition, history), encoding="utf-8", newline="\n")
    (out / "LICENSE").write_text(CC_BY_4_0_LEGALCODE, encoding="utf-8", newline="\n")
    return folder


def previous_edition(out: Path, as_of: str) -> Optional[dict[str, Any]]:
    """The newest edition counted before ``as_of``."""
    root = out / "editions"
    if not root.exists():
        return None
    earlier = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name < as_of)
    if not earlier:
        return None
    return json.loads((root / earlier[-1] / "dataset.json").read_text(encoding="utf-8"))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="the dataset repository's root")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--from-dir", type=Path, help="build from a saved payloads.json")
    parser.add_argument("--save-payloads", type=Path, help="also save what was read here")
    parser.add_argument("--dry-run", action="store_true", help="print the headline, write nothing")
    args = parser.parse_args(argv)
    try:
        payloads = load_payloads(args.from_dir) if args.from_dir else fetch_payloads(args.base_url)
        if args.save_payloads:
            args.save_payloads.mkdir(parents=True, exist_ok=True)
            (args.save_payloads / "payloads.json").write_text(
                json.dumps(payloads, indent=1), encoding="utf-8", newline="\n")
        as_of = str((payloads.get("market") or {}).get("as_of") or "")
        edition = build_edition(payloads, previous_edition(args.out, as_of))
    except DatasetError as exc:
        print(f"Not written: {exc}", file=sys.stderr)
        return 1
    print(f"Edition {edition['as_of']} (previous: {edition['previous_as_of'] or 'none'})")
    for line in headline(edition):
        print(line)
    for note in edition["notes"]:
        print(f"Note: {note}")
    if args.dry_run:
        return 0
    folder = write_edition(edition, args.out)
    print(f"Wrote {folder} and latest/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
