# Remote jobs data

A weekly, open dataset about the remote job market: how many remote job postings are open,
where they can hire, which professions and seniority levels they ask for, and what they pay
where pay is stated. Counted by [RemoteHunt](https://remotehunt.app) from its public index of remote job
postings, with the same rules as its [remote job market stats page](https://remotehunt.app/remote-job-market-stats).

**As of 2026-11-23** (UTC). Refreshed every Monday.

## Headline

- Open remote postings: **65,731**, -917 vs 2026-11-22
- Hire worldwide: **1,392** (2.1%), -22 vs 2026-11-22
- Hire in exactly one country: **33,095** (50.3%), -130 vs 2026-11-22
- Hire in several countries or a region: **8,209** (12.5%), -771 vs 2026-11-22
- United States only: **19,186** (29.2%), -29 vs 2026-11-22
- Do not say where they hire: **22,662** (34.5%), -367 vs 2026-11-22
- State pay: **21,588** (32.8%), -200 vs 2026-11-22

## Files

`latest/` holds the newest edition; `editions/<date>/` keeps every edition. Each folder has
`dataset.json` (every table plus definitions) and one CSV per table. An empty CSV cell means
"not measured", never zero.

| File | Rows | Columns |
|---|---|---|
| `summary.csv` | 9 | `metric`, `value`, `share_pct`, `previous_value`, `change`, `previous_as_of` |
| `geography.csv` | 4 | `scope`, `open`, `share_pct`, `previous_open`, `change_open` |
| `countries.csv` | 36 | `code`, `name`, `open`, `share_pct`, `named`, `single_country`, `with_stated_pay`, `median_pay_usd`, `previous_open`, `change_open` |
| `country_families.csv` | 416 | `country_code`, `country`, `family`, `open`, `previous_open`, `change_open` |
| `professions.csv` | 84 | `profession`, `open`, `share_pct`, `with_stated_pay`, `previous_open`, `change_open` |
| `seniority.csv` | 7 | `band`, `open`, `share_pct`, `with_stated_pay`, `pay_p25_usd`, `pay_median_usd`, `pay_p75_usd`, `previous_open`, `change_open` |
| `pay_by_role.csv` | 43 | `role`, `postings_with_pay`, `pay_p25_usd`, `pay_median_usd`, `pay_p75_usd`, `window_days` |
| `pay_by_role_seniority.csv` | 74 | `role`, `band`, `postings_with_pay`, `pay_p25_usd`, `pay_median_usd`, `pay_p75_usd`, `window_days` |
| `country_league.csv` | 36 | `rank`, `code`, `name`, `country_page`, `open`, `share_pct`, `only_country`, `with_others_or_region`, `anywhere`, `named`, `only_country_with_stated_pay`, `only_country_median_pay_usd`, `previous_open`, `change_open` |

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
- **country_league**: Every country with a RemoteHunt country page, plus the United States, ranked by open; country_page is its page on the site, empty when it has none. only_country: postings whose list holds that country and nothing else; with_others_or_region: postings that list it beside other countries or through a region; anywhere: postings that hire from any country. The three add up to open. share_pct is open out of open_postings in summary.csv.
- **only_country_median_pay_usd**: Median of the annual US-dollar midpoints of the only_country postings that state pay; empty under 20 such postings.

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
"Source: RemoteHunt remote jobs data, https://remotehunt.app" with a link to https://remotehunt.app. The builder script
(`scripts/build_public_dataset.py`) is released under the same licence.

## Editions

[2026-11-23](editions/2026-11-23/), [2026-11-22](editions/2026-11-22/)
