# Remote jobs data

A weekly, open dataset about the remote job market: how many remote job postings are open,
where they can hire, which professions and seniority levels they ask for, and what they pay
where pay is stated. Counted by [RemoteHunt](https://remotehunt.app) from its public index of remote job
postings, with the same rules as its [remote job market stats page](https://remotehunt.app/remote-job-market-stats).

**As of 2026-11-22** (UTC). Refreshed every Monday.

## Headline

- Open remote postings: **66,648**
- Hire worldwide: **1,414** (2.1%)
- Hire in exactly one country: **33,225** (49.9%)
- Hire in several countries or a region: **8,980** (13.5%)
- United States only: **19,215** (28.8%)
- Do not say where they hire: **23,029** (34.6%)
- State pay: **21,788** (32.7%)

## Files

`latest/` holds the newest edition; `editions/<date>/` keeps every edition. Each folder has
`dataset.json` (every table plus definitions) and one CSV per table. An empty CSV cell means
"not measured", never zero.

| File | Rows | Columns |
|---|---|---|
| `summary.csv` | 9 | `metric`, `value`, `share_pct`, `previous_value`, `change`, `previous_as_of` |
| `geography.csv` | 4 | `scope`, `open`, `share_pct`, `previous_open`, `change_open` |
| `countries.csv` | 35 | `code`, `name`, `open`, `share_pct`, `named`, `single_country`, `with_stated_pay`, `median_pay_usd`, `previous_open`, `change_open` |
| `country_families.csv` | 430 | `country_code`, `country`, `family`, `open`, `previous_open`, `change_open` |
| `professions.csv` | 85 | `profession`, `open`, `share_pct`, `with_stated_pay`, `previous_open`, `change_open` |
| `seniority.csv` | 7 | `band`, `open`, `share_pct`, `with_stated_pay`, `pay_p25_usd`, `pay_median_usd`, `pay_p75_usd`, `previous_open`, `change_open` |
| `pay_by_role.csv` | 43 | `role`, `postings_with_pay`, `pay_p25_usd`, `pay_median_usd`, `pay_p75_usd`, `window_days` |
| `pay_by_role_seniority.csv` | 74 | `role`, `band`, `postings_with_pay`, `pay_p25_usd`, `pay_median_usd`, `pay_p75_usd`, `window_days` |

## Definitions

- **open_postings**: Open remote job postings in RemoteHunt's public index on the counting day: live, remote, and listed on the site's public job pages.
- **open (country)**: Postings that can hire in the country: they say they hire worldwide, or their list of countries or regions includes it.
- **named (country)**: Postings whose list names the country itself, counted inside open.
- **single_country**: Postings whose list holds exactly one country.
- **multi_country**: Postings that list two or more countries, or a region.
- **hire_worldwide**: Postings that say they hire anywhere.
- **hiring_location_not_stated**: Postings that state no readable hiring location.
- **us_only**: Postings whose list holds the United States and nothing else.
- **europe_only**: Postings whose list holds only European countries.
- **with_stated_pay**: Postings that state a salary or a salary range.
- **pay**: Annual US dollars, the midpoint of the stated range. Pay by role and by role x seniority covers postings published in the last 90 days that state a full range; a figure needs at least 20 such postings, else it is left empty.
- **profession**: The posting's profession as RemoteHunt files it; professions with fewer than 20 open postings are left out of the table, not counted as 0.
- **family**: A group of professions (Engineering, Sales, ...) as on the site's country pages.
- **seniority**: Junior (intern, junior, entry), Mid-level, Senior, Staff & Principal (lead, staff, principal), Management (manager, director, VP, C-level), Other, Not stated.
- **change**: Value minus the value in the previous edition (normally one week earlier); empty when either value is not measured.

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
commercially, with attribution: "Source: RemoteHunt remote jobs data, https://remotehunt.app" with a link to
https://remotehunt.app.

## Editions

[2026-11-22](editions/2026-11-22/)
