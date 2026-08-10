# Browser-in SF Legistar scraper image, for Cloud Run Jobs.

# Base = Microsoft's Playwright image: Chromium plus every system lib headless
# Chromium needs are prebaked, so we never run apt or `playwright install`.
# INVARIANT: the vX.Y.Z tag MUST match the pinned playwright version in requirements.txt
# so the bundled browser matches the client lib. Bump both together. (now: 1.61.0)
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

# playwright==1.61.0 exactly matches the base's preinstall, so pip installs only
# the rest (requests/bs4/lxml/pypdf/…) and leaves the browser-matched playwright alone.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scrape/ ./scrape/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
