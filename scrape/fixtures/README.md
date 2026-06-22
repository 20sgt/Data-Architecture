# Test fixtures — verbatim SF Legistar captures

Saved 2026-06-21 from live `sfgov.legistar.com` to let `warehouse/smoke_test_meetings.py`
parse real HTML offline (no network in CI). Do not edit — they are ground truth.

| File | Page | Why it's here |
|---|---|---|
| `calendar.html` | `Calendar.aspx` (This Month) | Calendar grid: subtype suffix, Granicus clip id, Video/Audio/Transcript columns |
| `meeting_committee_1422963.html` | Land Use & Transportation Cmte, 6/15, minutes **Final** | Acted agenda items (RECOMMENDED / CONTINUED) with HistoryDetail links |
| `meeting_board_1423292.html` | Board of Supervisors, 6/16, minutes **Draft** | Gating case: 50 items, **0** actions/votes posted yet |
| `history_committee_36969551.html` | HistoryDetail (file 260422) | Committee roll-call: 3× Aye, PersonId links |
| `history_board_36861771.html` | HistoryDetail (file 260300) | Board roll-call: FINALLY PASSED, 10× Aye / 1× Excused |
