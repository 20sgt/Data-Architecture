# TODO — reduce scraper redundancy

Both scrapers independently fetch & parse the same `HistoryDetail` pages and emit the same actions +
votes, so there are two divergent parsers, double network fetches, and a cross-slice dedup layer that
exists only because of this overlap (and where passes 2–3's bugs lived). Two improvements:

## 1. Shared `HistoryDetail` parse module  (low-risk, do whenever this is next touched)
Extract one `HistoryDetail` fetch+parse helper (action + per-person roll-call) into a shared module —
like `scrape/action_types.py` is the shared label→code map — and have BOTH scrapers import it.
- Removes the duplicated, divergent parsers (`legistar_meetings.parse_history` vs
  `legistar_scrape.parse_votes`).
- Fixes the legislation parser's known bug (value-whitelist silently drops unknown vote literals; no
  PersonId; no `normalize_action`/`normalize_vote`).
- Gives the legislation slice real `PersonId` + normalized codes for free.
- Pure refactor — no schema or ownership change.

## 2. Single-producer facts via `history_id`  (bigger; needs a team/ERD decision)
Stop having both slices produce `fact_matter_action` / `fact_vote` and dedup them. Instead:
- **Legislation** owns the facts (it already walks every matter's full history → best coverage).
- **Meeting** owns `dim_meeting` and emits just a `history_id → meeting_sk` map — it can read
  `history_id` straight from `MeetingDetail` and so **skip HistoryDetail fetches entirely**.
- Transform fills `fact.meeting_sk` by an exact join on `history_id`.
- Removes double-fetching AND the whole dedup/heuristic layer in `transform_gold.build_facts`.
- ⚠️ Reverses DISCUSSION Q4/D4 ("meeting is system-of-record for facts") and makes fact coverage
  depend on the legislation crawl — so it's a Jack + Lynn decision, not a unilateral change.
