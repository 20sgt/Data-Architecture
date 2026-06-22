# SF Legislation Analytics — Gold Layer Schema (ERD)

Documentation for the gold-layer star schema in [`schema.dbml`](schema.dbml). Render it at
[dbdiagram.io](https://dbdiagram.io/d) or in the
[dbdocs project](https://dbdocs.io/jacksoncdawson/Group-Project-ERD).
For open decisions and the meeting/legislation split, see [`../DISCUSSION.md`](../DISCUSSION.md).

> Milestone-2 snapshot is archived at [`schema_milestone2.dbml`](schema_milestone2.dbml) (do not edit).

---

## 1. What this is

This schema is the **gold (analytics) layer** of a medallion warehouse for San Francisco Board of
Supervisors legislative data. It answers questions like *"how did my supervisor vote on housing bills?"*,
*"what happened to file 250782?"*, and *"which committee controls this matter?"*.

It is **not** the scraper output. Scrapers land raw data in bronze; a transform builds this layer.

```
  SF Legistar (HTML)                  bronze                 silver                       gold
  ─────────────────                   ──────                 ──────                       ────
  Calendar.aspx          ──scrape──▶  raw rows,      ──▶  clean / conform /        ──▶  this star schema
  MeetingDetail.aspx                  every source        dedup on SOURCE               (surrogate keys,
  HistoryDetail.aspx                  id retained         natural keys                  SCD2, lookups)
  LegislationDetail.aspx
```

**The Legistar Web API is unusable for SF** (frozen ~2018–2020 snapshot, and corrupted — every
action/vote/event endpoint throws `Invalid object name 'dbo.tblPassedFlags'`). So *all* current data is
HTML-scraped. The two scrape efforts:

| Effort | Entry point | Primarily feeds |
|---|---|---|
| **scrape-by-meeting** (this branch) | `Calendar.aspx` → `MeetingDetail.aspx` → `HistoryDetail.aspx` | `dim_meeting`, `fact_matter_action`, `fact_vote`, meeting docs |
| **scrape-by-legislation** (teammate) | legislation search → `LegislationDetail.aspx` | `dim_matter`, sponsors / subjects / matter docs; backfills facts |

---

## 2. Conventions

- **`_sk` = surrogate key.** Generated in gold only. Scrapers never produce these.
- **`_id` = Legistar source id** (EventId, MatterId, PersonId, BodyId…). Carried verbatim through
  bronze/silver because the **fact dedup keys are built on source ids**, making the two crawlers idempotent.
- **SCD2 dimensions** (`dim_person`) use `effective_from` / `effective_to` / `is_current`.
- **Facts are append-then-dedup**, not SCD2. Dedup happens in silver on the natural keys below.
- **Raw labels are always preserved** (`action_text`) so normalization can be re-derived as mappings evolve.

### Fact natural keys (dedup contract)
| Fact | Natural key |
|---|---|
| `fact_matter_action` | `(matter_id, meeting_id, action_type_code)` |
| `fact_vote` | `(matter_id, meeting_id, person_id)` |

Both scrapers **must** upsert on these. `action_type_code` is the *normalized* code, so both must apply the
**same** raw-label→code mapping (`dim_action_type`) or dedup silently fails.

---

## 3. Tables

### Dimensions

**`dim_person`** — SCD2, one row per person-version. Real person identified by `person_id` (Legistar PersonId,
joinable from `PersonDetail.aspx?ID=`). Biographical fields (`party`, `gender`, `birth_date`) and
`district`/term come from a separate biographical/OfficeRecords source, not the meeting scrape.

**`dim_committee`** — flat dimension of bodies (committees + full Board). `committee_id` = Legistar BodyId;
`committee_type` ∈ {Full Board, Standing Committee, Special}; `is_active` flags historical committees.
The meeting scrape **joins** to this on body **name** (the calendar exposes the name, not the BodyId).

**`dim_matter`** — one row per legislative item. `matter_file` (e.g. `240123`) is the **only** key a meeting
agenda row exposes to join back here — *not* the numeric LegislationDetail URL id. Owned by scrape-by-legislation.

**`dim_subject`** — Legistar subject index (Housing, Transportation…), linked via `bridge_matter_subject`.

**`dim_document`** — documents for **matters** (attachments) **and meetings** (agenda/minutes/transcript).
`document_source` disambiguates the two; `document_id` is NULL for meeting docs (they have no MatterAttachmentId).
`body_text` holds extracted text (agenda/minutes PDFs and transcript VTT are all text-extractable).

**`dim_meeting`** — **owned by scrape-by-meeting.** Committee meetings and full Board sessions.
`meeting_id` = Legistar EventId. **`event_guid` is operationally mandatory** — every detail/document fetch
returns HTTP 410 without it and it can't be guessed, so it must be harvested from the calendar and persisted.
`video_clip_id` (Granicus clip id) is the only link from a meeting to its video/audio/transcript.

### Lookup

**`dim_action_type`** — canonical action codes + categories. Replaces the old inline CHECK enum, which couldn't
represent real Legistar labels (ADOPTED, APPROVED, CONTINUED, AMENDED, RECOMMENDED AS AMENDED, FINALLY PASSED…).
The single shared raw-label→code mapping lives here / in a shared module both scrapers import.

### Facts

**`fact_matter_action`** — *the* matter lifecycle table. Grain: one row per (matter, meeting, action).
`action_result` (Pass/Fail/NULL) distinguishes a failed amendment from a passed one. Also absorbs
committee/voice/unanimous outcomes that have no per-person breakdown. Meeting scrape is system-of-record.

**`fact_vote`** — per-person roll-call votes, **Board and committee** (`body_scope` distinguishes; downstream
filters `body_scope='board'` for "the Board vote"). Grain: one row per (matter, meeting, person).
`vote_value`: HTML emits `"No"` → normalize to `Nay` in silver. Meeting scrape is system-of-record.

**`fact_committee_membership`** — SCD2-shaped tenure on a committee. From OfficeRecords (biographical source).

### Bridges (M:M)

- **`bridge_matter_subject`** — matters ↔ subjects. ⚠️ not yet populatable (no per-matter subject source found — gap).
- **`bridge_matter_sponsor`** — matters ↔ sponsors; `sponsor_type` ∈ {Primary, Co}.
- **`bridge_matter_document`** — matters ↔ matter attachments.
- **`bridge_meeting_document`** — meetings ↔ agenda/minutes/transcript docs.

---

## 4. Source → schema map (scrape-by-meeting)

Crawl: `Calendar.aspx` (Year via cookie/GET; Telerik grid ~100-row page cap → slice per-body/per-month)
→ harvest `EventId` + `event_guid` + row fields → `MeetingDetail.aspx?ID=<EventId>&GUID=<guid>`
→ per voted item, `HistoryDetail.aspx` for the roll-call grid.

| Gold column | Source |
|---|---|
| `dim_meeting.meeting_id` | Calendar row link `MeetingDetail.aspx?ID=` |
| `dim_meeting.event_guid` | Calendar row link `&GUID=` |
| `dim_meeting.committee_sk` | join `dim_committee` on body name (Calendar col0 / MeetingDetail `hypName`) |
| `dim_meeting.meeting_date/time/location` | Calendar cols / MeetingDetail `lblDate/lblTime/lblLocation` |
| `dim_meeting.meeting_subtype` | split from the location string ("Regular/Special Meeting") |
| `dim_meeting.agenda_url/minutes_url` | `View.ashx?M=A` / `M=M` with `ID`+`GUID` |
| `dim_meeting.video_clip_id` | Calendar video link `window.open` clip id |
| `fact_matter_action.matter_sk` | join `dim_matter` on **matter_file** (MeetingDetail `gridMain` col0) |
| `fact_matter_action.action_type_code` | derive from `gridMain` Action / `HistoryDetail.lblAction` via `dim_action_type` |
| `fact_matter_action.action_result` | `gridMain` Result / `HistoryDetail.lblResult` |
| `fact_matter_action.action_text` | `HistoryDetail.lblActionText` (full motion text) |
| `fact_vote.person_sk` | `HistoryDetail gridVote` `hypPerson` → `PersonDetail.aspx?ID=<PersonId>` |
| `fact_vote.body_scope` | meeting body (`board` if "Board of Supervisors", else `committee`) |
| `fact_vote.vote_value` | `gridVote` Vote column ("Aye"/"No" → Aye/Nay) |
| `fact_vote.motion_text` | `HistoryDetail.lblActionText` |
| meeting docs (`dim_document` + `bridge_meeting_document`) | agenda/minutes `View.ashx`; transcript VTT `…/videos/<clip_id>/captions.vtt` |

---

## 5. Changelog vs milestone 2

- `dim_meeting`: **+ `event_guid`** (mandatory for refetch), `video_clip_id`, `meeting_subtype`,
  `agenda_status`, `minutes_status`.
- `fact_matter_action`: **+ `action_result`**; `action_type_code` now references `dim_action_type`
  (was an inline CHECK that couldn't hold real labels).
- `fact_vote`: **+ `body_scope`** — now captures committee per-person votes too (was Board-only).
- **+ `dim_action_type`** lookup.
- **+ `bridge_meeting_document`**; `dim_document` gains `document_source` and `document_id` is now nullable.

## 6. Known gaps / open items (see [`../DISCUSSION.md`](../DISCUSSION.md))

- Full historical backfill depth (v1 is a **pilot: last few months**).
- `bridge_matter_subject` has no known source — currently unpopulatable.
- `dim_matter` may be missing for current files (API stale) → meeting facts can orphan; needs a stub strategy.
- `vote_value` Excused/Absent casing unconfirmed against a live absentee meeting.
