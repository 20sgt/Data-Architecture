"""Offline deploy invariants — string checks tying files that must move together."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_playwright_pin_matches_docker_base():
    """The base image's bundled chromium must match the pip-installed client."""
    req = (ROOT / "requirements.txt").read_text()
    pin = re.search(r"^playwright==(\S+)", req, re.M)
    assert pin, "requirements.txt must pin playwright== (loose bounds drift past the base image)"

    docker = (ROOT / "Dockerfile").read_text()
    base = re.search(r"^FROM mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-", docker, re.M)
    assert base, "Dockerfile FROM must be a pinned mcr.microsoft.com/playwright/python:vX.Y.Z tag"

    assert pin.group(1) == base.group(1), (
        f"playwright=={pin.group(1)} but Dockerfile base is v{base.group(1)} — bump both together"
    )
