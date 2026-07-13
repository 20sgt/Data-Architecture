"""Offline tests of entrypoint.sh orchestration.

A fake `python` on PATH records each module invocation to a log, so the script's
branching (including the month-boundary guard) runs with zero network/browser.
Env overrides (WINDOW_FROM/INGEST_DATE) mean the GNU-date defaults never execute,
so this also runs on macOS.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FAKE_PYTHON = '#!/usr/bin/env bash\necho "$@" >> "$CALL_LOG"\n'


def run_entrypoint(tmp_path, window_from, ingest_date):
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
            "RAW_ROOT": str(tmp_path / "raw"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return call_log.read_text().splitlines()


def test_same_month_window_runs_two_passes(tmp_path):
    calls = run_entrypoint(tmp_path, "2026-07-08", "2026-07-15")
    assert len(calls) == 2
    assert "legistar_meetings --current-month" in calls[0]
    assert "legistar_scrape" in calls[1]
    assert not any("--year" in c for c in calls)


def test_month_boundary_adds_year_pass(tmp_path):
    calls = run_entrypoint(tmp_path, "2026-06-24", "2026-07-01")
    assert len(calls) == 3
    assert "legistar_meetings --current-month" in calls[0]
    assert "--year 2026" in calls[1] and "--from 2026-06-24" in calls[1]
    assert "legistar_scrape" in calls[2]  # matters always last, after the full feed


def test_year_boundary_year_pass_uses_prior_year(tmp_path):
    calls = run_entrypoint(tmp_path, "2026-12-28", "2027-01-03")
    assert len(calls) == 3
    assert "--year 2026" in calls[1]  # FROM's year, i.e. December's side
