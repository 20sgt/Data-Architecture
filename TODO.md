# TODO

## Open-matter re-scrape (incremental status refresh)

The pilot relies on the weekly File-Created window plus the
agenda discovery feed for coverage.

**Problem.** A weekly run scrapes (a) matters *created* in the last window and
(b) every matter on that week's scraped agendas. It does **not** re-check a
matter that changed status *off-agenda*, or that was created in an earlier
window and moved later without reappearing on a scraped agenda (e.g. Mayor
approval, clerk referral, a committee continuance). Such a matter's
`status`/`lifecycle` in gold goes stale — the "weekly change" use case misses it.

**Fix.** Each weekly run, also re-scrape the *open set*: every matter whose
`lifecycle = 'in_works'` (not a terminal status). SF carries a few hundred open
matters at a time — fine at 1 req/s.

**Plumbing needed.**

- A source for the open set. Cleanest: read `detail_url`s from the warehouse
(`dim_matter WHERE is_current AND lifecycle = 'in_works'`). Re-scraping by URL
needs **no browser** — `scrape_matter()` is plain `requests`.
- A scraper entrypoint that takes a list of matter URLs/file numbers, e.g.
`--files-from open_set.txt`. `collect()` already de-dups by matter `ID=`, so
overlap with the window/agenda feed is harmless.

**Dependency.** Requires a gold→scraper feedback path (the warehouse must expose
the open set). Cross-team — coordinate with the DB/silver owner. Until then,
agenda-feed coverage is the pilot's approximation.

## Representative profiles (Legistar web API — NOT a PersonDetail scrape)

Today `dim_person` is **identity-only** — `person_id` + `full_name`, captured as a
byproduct of roll-call votes (`scrape/history_detail.py`) and sponsor names
(`databricks/gold_merge_databricks.py`). The biographical columns the schema
declares (`district`, `party`, `gender`, `birth_date`, `supervisor_term_start/end`)
are unpopulated, `dim_person` is a flat distinct list (no SCD2 versioning), and
`fact_committee_membership` is empty.

**Problem.** The "who is my representative / what do they work on" use case wants
district, party, term, and committee seats. None of that is on the meeting or
legislation pages.

**The HTML pass originally planned here is a dead end — verified 2026-07-10:**

- `People.aspx` lists **current members only** by default (11 rows); the
"View: Current / Past / All" dropdown is a webforms postback (browser required).
- Worse, `PersonDetail.aspx` returns **HTTP 410 Gone for former members**, even
with the correct `ID` + `GUID` (verified: Tom Ammiano, PersonId 3). An HTML pass
can never enrich the historical members the backfill introduces.

**Fix — use the Legistar web API instead (enabled for `sfgov`; plain JSON, no
browser):**

- `GET webapi.legistar.com/v1/sfgov/persons` — every member ever (first page is
1990s-era supervisors), each with `PersonId` + `PersonGuid`. CAUTION:
`PersonActiveFlag` is not maintained (Ammiano shows `1`) — never use it to
distinguish current from past.
- `GET webapi.legistar.com/v1/sfgov/officerecords?$filter=OfficeRecordPersonId eq <id>`
— the full membership history back to **1995**: body name, title
(Supervisor / Chair / President), start/end dates. This is
`fact_committee_membership` (position ← Title, effective_from/to ← Start/End)
plus `supervisor_term_start/end` (from the Board of Supervisors rows), for
historical and current members alike.
- Current seats carry far-future end dates (e.g. 2035) — normalize to open/NULL.
- Paginate with `$top`/`$skip`; route requests through the `scrape/fetch.py`
throttle like everything else.

**Coverage reality (checked page + API, current + former members):**
`district`, `party`, `gender`, `birth_date` are populated **nowhere in Legistar**
— not on a sitting member's PersonDetail, not in the API person record. Fillable
from Legistar: full name, contact, term dates, committee seats. District/party
need a non-Legistar source (sf.gov roster) or stay NULL.

**Schema is ready.** `dim_person` and `fact_committee_membership` columns already
exist in `erd/schema.dbml`; this pass fills what Legistar can fill. `dim_person`
becomes the SCD2 owner once profile attributes can change over time.

**Plumbing needed.**

- A new module/entrypoint (e.g. `scrape/legistar_people.py` — plain `requests`
against the two API endpoints) + a silver loader + gold merge into `dim_person` /
`fact_committee_membership`.
- `PersonId` is the join key — already present on `fact_vote`, so existing people
light up immediately; the pass also adds members who never cast a recorded vote.

**Dependency.** None cross-team — fully additive, and order-independent with the
historical backfill: it enriches all backfilled members retroactively on the
exact `PersonId` join. Single-producer-clean: the People slice is the sole
producer of `dim_person` profile attributes and `fact_committee_membership`.