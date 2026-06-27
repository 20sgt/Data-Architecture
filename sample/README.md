# `sample/` — committed bronze sample

A small, **git-tracked** slice of the bronze layer — one week of SF Legistar data — kept in the repo
for two reasons:

1. **Final verification** that the scrapers produce the agreed bronze contract end-to-end.
2. **Quick reference for the silver layer** (Lynn): the exact JSON shape silver consumes, without
   running a scrape.

> This mirrors what the weekly job will write to GCS (`raw/` locally, which is **gitignored** — only
> this curated `sample/` is committed). Window: **2026-06-15 … 2026-06-21**, scraped 2026-06-26.

## Layout
```
sample/
  meetings/ingest_date=2026-06-26/<EventId>.json      # one file per meeting  (scrape-by-meeting)
  matters/ingest_date=2026-06-26/<file_number>.json   # one file per matter   (scrape-by-legislation)
```
`ingest_date=` is the partition key (= scrape date). The window above is the date range of the
meetings/matters themselves.

## Bronze contract

**Meeting** (`meetings/.../<EventId>.json`) — owns `dim_meeting`, documents, and the meeting↔fact map:
```
meeting_id, event_guid, body_name, meeting_date, meeting_time, location, meeting_subtype,
agenda_status, minutes_status, agenda_url, minutes_url, video_clip_id,
documents[]   {document_source, document_title, document_url, body_text}
agenda_items[]{item_seq, matter_file, matter_url, agenda_number, matter_name, matter_type,
               matter_status, title, action_raw, action_result, history_id, history_url}
```
The meeting slice does **not** fetch HistoryDetail — no `action_text`/`votes` here (those are the
legislation slice's). `action_raw`/`action_result` come free from the MeetingDetail grid and are
populated only for items the meeting actually acted on (null otherwise / on Draft minutes).

**Matter** (`matters/.../<file_number>.json`) — sole producer of the facts:
```
file_number, detail_url, name, title, type, status, introduced, on_agenda, final_action,
enactment_date, enactment_number, in_control, full_text,
sponsors[]        (list[str])
related_files[]   (list[str])
attachments[]     {name, url}
actions[]         {date, body, action, result, history_id, history_url,
                   votes[]{person_id, person_name, vote_value}}
```
Pure raw — no derived fields. `status` is the raw label; the `passed | in_works | other` lifecycle is
a silver derivation (rules: `bucket()` in `scrape/legistar_scrape.py`).

## Join keys
- **meeting ↔ matter action:** `history_id` (the MatterHistory id both slices carry) → fills
  `fact.*.meeting_sk` by exact join. Single-producer: legislation produces every fact row.
- **matter ↔ person:** `person_id` (the real Legistar PersonId on each vote).
- **agenda discovery feed:** `agenda_items[].matter_url` is the LegislationDetail URL the legislation
  scraper hits directly, so its coverage is a superset of the meeting slice (any year).

## Regenerate
```bash
python -m scrape.legistar_meetings --from 2026-06-15 --to 2026-06-21 \
    --raw-dir sample/meetings --date 2026-06-26
playwright install chromium   # one-time, for the legislation enumeration
python -m scrape.legistar_scrape --from 2026-06-15 --to 2026-06-21 \
    --agenda-bronze sample/meetings/ingest_date=2026-06-26 \
    --raw-dir sample/matters/ingest_date=2026-06-26
```
