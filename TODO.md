# TODO

## Weekly scraper — diagnosed 2026-08-08: one real gap, one false alarm

Raised 2026-08-08 as "no data since 2026-07-22, not diagnosed". Diagnosed the same
day. **The scraper is not broken.** Of the two suspect runs, only 2026-07-29 lost
data; 2026-08-05 was correct behavior.

| ingest_date | matters | meetings | Cloud Run execution | verdict |
|---|---|---|---|---|
| 2026-07-22 | 49 | 3 | success, ~3 min | fine |
| 2026-07-29 | *(no partition)* | — | never started — "Resource readiness deadline exceeded" | **real gap** |
| 2026-08-05 | 0 | 0 | SUCCESS in ~74 s, wrote nothing | correct — recess |

### 2026-08-05 was not a failure — SF is in summer recess

The original note called this "the more dangerous one" on the theory that exit 0
with no output hides a silent outage. It doesn't here. The Board of Supervisors
holds a **summer recess**, and the last meeting on the *entire* 2026 Legistar
calendar is **7/28/2026**. There were no August meetings to scrape and no matters
created that week.

The Aug 5 logs say exactly this once you know what recess looks like:

```
[1/2] meetings → date window 2026-07-29..2026-08-05 → 0 of 0 meetings   (August calendar is empty)
[1b]  year pass → enumerated 134 calendar rows → 0 in window            (nothing dated after 7/28)
[2/2] matters  → slice 2026-07-29..2026-08-04 → 0 matters
```

The ~74 s runtime is the tell in the *other* direction: the run did all its normal
enumeration work (both meeting passes plus two matter slices) and simply found
nothing. A scrape that bailed early would not have reached `[2/2]`.

Reproduce against the live site — no Databricks, no GCP:

```bash
python -m scrape.legistar_meetings --year 2026 --from 2026-07-01 --to 2026-08-08 --raw-dir /tmp/y --date 2026-08-08
```

Expect 18 meetings, none later than 7/28. Recess ends when the calendar shows
September rows; the weekly job needs no change to pick them up.

### The real gap: 2026-07-29 never ran

The container never started, so no partition was created at all. That window is the
last active week before recess, and it is the only data actually missing:

- **41 matters** created 2026-07-22..2026-07-28
- **3 meetings** — Rules 7/27, Land Use and Transportation 7/27, Board of
  Supervisors 7/28

Backfill is a single re-run of the existing job with the window pinned; the
scrapers are idempotent and `collect()` de-dups by matter `ID=`, so overlap with
the 7/22 partition is harmless.

### Still worth fixing: nothing alerts on the scrape half

The 2026-07-29 miss went unnoticed for ten days because **the Cloud Run job has no
alerting at all** — `corn-off-the-cobb` has zero notification channels. The
Databricks side got `email_notifications.on_failure` on 2026-08-08; the scrape side
never got the equivalent. An execution that fails to start is the exact case that
needs it.

The originally proposed "fail loudly when a run produces zero files" is **not
safe as stated** — during recess every weekly run legitimately writes zero files,
so a bare zero-file alarm would fire every Wednesday until September and train
everyone to ignore it. If it gets built, the condition has to be *zero files
**and** the calendar had meetings in the window* — i.e. the scraper knows the
difference between "nothing happened" and "I failed to see what happened."

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