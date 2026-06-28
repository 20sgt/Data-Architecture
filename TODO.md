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