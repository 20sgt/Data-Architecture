"""Offline tests of entrypoint.sh orchestration.

A fake `python` on PATH records each module invocation to a log, so the script's
branching (including the month-boundary guard) runs with zero network/browser.
Env overrides (WINDOW_FROM/INGEST_DATE/NOW_MONTH) mean the GNU-date defaults never
execute, so this also runs on macOS — and pinning NOW_MONTH keeps these tests from
changing their answer depending on what month you run them in.

The guard turns on the year pass whenever the window falls outside the month the
scrape RUNS IN, because `--current-month` fetches that month's calendar and no
other. So every case below has to say where "now" is, not just where the window is.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FAKE_PYTHON = '#!/usr/bin/env bash\necho "$@" >> "$CALL_LOG"\n'


def run_entrypoint(tmp_path, window_from, ingest_date, now_month):
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "python"
    shim.write_text(FAKE_PYTHON)
    shim.chmod(0o755)
    call_log = tmp_path / "calls.txt"
    call_log.touch()

    result = subprocess.run(
        ["bash", str(ROOT / "entrypoint.sh")],
        env={
            "PATH": f"{shim_dir}:/usr/bin:/bin",
            "CALL_LOG": str(call_log),
            "WINDOW_FROM": window_from,
            "INGEST_DATE": ingest_date,
            "NOW_MONTH": now_month,
            "RAW_ROOT": str(tmp_path / "raw"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return call_log.read_text().splitlines()


def test_window_inside_current_month_runs_two_passes(tmp_path):
    # The ordinary weekly run: --current-month already covers it, no browser needed.
    calls = run_entrypoint(tmp_path, "2026-07-08", "2026-07-15", now_month="2026-07")
    assert len(calls) == 2
    assert "legistar_meetings --current-month" in calls[0]
    assert "legistar_scrape" in calls[1]
    assert not any("--year" in c for c in calls)


def test_month_boundary_adds_year_pass(tmp_path):
    calls = run_entrypoint(tmp_path, "2026-06-24", "2026-07-01", now_month="2026-07")
    assert len(calls) == 3
    assert "legistar_meetings --current-month" in calls[0]
    assert "--year 2026" in calls[1] and "--from 2026-06-24" in calls[1]
    assert "legistar_scrape" in calls[2]  # matters always last, after the full feed


def test_backfill_of_a_past_month_adds_year_pass(tmp_path):
    # Regression: re-running the 2026-07-22 window in August scraped August's
    # calendar (empty — recess), then skipped the year pass because both ends of
    # the window agreed it was July. 41 matters landed, 0 meetings, exit 0. The
    # empty meetings pass also starved the agenda feed the matter slice consumes.
    calls = run_entrypoint(tmp_path, "2026-07-22", "2026-07-29", now_month="2026-08")
    assert len(calls) == 3
    assert "--year 2026" in calls[1] and "--from 2026-07-22" in calls[1]


def test_year_boundary_year_pass_uses_prior_year(tmp_path):
    calls = run_entrypoint(tmp_path, "2026-12-28", "2027-01-03", now_month="2027-01")
    assert len(calls) == 3
    assert "--year 2026" in calls[1]  # FROM's year, i.e. December's side
