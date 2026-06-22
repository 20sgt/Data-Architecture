# Data-Architecture

A big obstacle for people to get involved with their local politics is the accessibility of information from local government meetings. To understand how policies are moving and what issues are being addressed or passed on, people need a way to access the information on the topics they are passionate about.

## Resources

Documentation/Slides links are not accessible publicly. Must be logged in with associated account.

- [GitHub Repository](https://github.com/20sgt/Data-Architecture)
- [Documentation](https://docs.google.com/document/d/1y_hW02plKa5s89caJt-dc-Sf21EAM0dQNJ3ot7Cwv6s/edit?usp=sharing)
- [Slides](https://docs.google.com/presentation/d/1v0ImK7iBYsuHg1ciyDIfsG_P-4kD4YRl8uSPw1vbQmQ/edit?usp=sharing)

## Architecture

- [ERD](https://dbdocs.io/jacksoncdawson/Group-Project-ERD?view=relationships)

## Pipelines

Medallion ELT (bronze raw scrape → silver staging → gold star schema), split into two scrape efforts
that share the gold layer. On the `integration` branch both run into **one** warehouse with a unified
gold builder and joint fact merge — see [`docs/integration.md`](docs/integration.md) (one-command run:
`python warehouse/run_local.py`).

- **scrape-by-meeting** — `Calendar.aspx → MeetingDetail.aspx → HistoryDetail.aspx`. Builds
  `dim_meeting`, meeting documents, and (at the cross-slice merge) the per-meeting facts. See
  [`docs/meeting_pipeline_design.md`](docs/meeting_pipeline_design.md). Code: [`scrape/legistar_meetings.py`](scrape/legistar_meetings.py),
  [`warehouse/`](warehouse). Quick start:
  ```bash
  pip install -r requirements.txt
  python -m scrape.legistar_meetings --current-month --raw-dir raw/meetings
  python warehouse/run_local.py --meeting-raw raw/meetings/ingest_date=$(date +%F) --date $(date +%F)
  python warehouse/smoke_test_meetings.py   # offline end-to-end check
  ```
- **scrape-by-legislation** — legislation search → `LegislationDetail.aspx`. Builds `dim_matter`,
  sponsors/subjects/matter docs. See [`docs/pipeline_design.md`](docs/pipeline_design.md).

The shared raw-label → `dim_action_type` mapping ([`scrape/action_types.py`](scrape/action_types.py))
is the cross-slice contract both scrapers import so the fact dedup keys stay consistent.