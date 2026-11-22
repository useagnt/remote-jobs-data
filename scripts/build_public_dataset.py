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
}

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

    tables = {
        "summary": summary, "geography": geography, "countries": country_rows,
        "country_families": country_families, "professions": professions,
        "seniority": seniority, "pay_by_role": pay_by_role, "pay_by_role_seniority": pay_by_band,
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

[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Use it for anything, including
commercially, with attribution: "Source: RemoteHunt remote jobs data, {SITE}" with a link to
{SITE}.

## Editions

{past}
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
