"""Tell the user when PyPI has a newer linkml-herd than the one running.

The check runs in a background thread while the diagram renders, asks PyPI at
most once a day (the answer is cached under the user cache directory), and
never fails a run: no network, a slow PyPI or an unpublished package all just
mean no notice. Set LINKML_HERD_NO_UPDATE_CHECK=1 (or pass --no-update-check)
to turn it off.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE = "linkml-herd"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
ENV_OPT_OUT = "LINKML_HERD_NO_UPDATE_CHECK"
CACHE_TTL = 24 * 60 * 60
TIMEOUT = 2.0


def current_version() -> str | None:
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return None


def parse_version(v: str) -> tuple[int, ...] | None:
    """The release numbers of a plain version ("1.2.3" -> (1, 2, 3)); None for pre-releases etc."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", v.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / PACKAGE / "latest-version.json"


def _cached(path: Path, now: float) -> str | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if now - float(entry["checked"]) < CACHE_TTL and isinstance(entry["latest"], str):
            return entry["latest"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _fetch_latest() -> str | None:
    req = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        latest = json.load(resp)["info"]["version"]
    return latest if isinstance(latest, str) else None


def latest_version(now: float | None = None) -> str | None:
    """The newest release on PyPI, from the day-old cache when there is one."""
    now = time.time() if now is None else now
    path = cache_path()
    latest = _cached(path, now)
    if latest is not None:
        return latest or None
    try:
        latest = _fetch_latest()
    # Offline, timed out, not on PyPI yet (404), odd JSON: never break a run over it.
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        latest = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Cache misses too, so being offline doesn't cost a timeout on every run.
        path.write_text(json.dumps({"checked": now, "latest": latest or ""}), encoding="utf-8")
    except OSError:
        pass
    return latest


def notice(current: str, latest: str) -> str:
    return (
        f"linkml-herd {latest} is available (you have {current}); upgrade with "
        f"`uv tool upgrade {PACKAGE}` or `pip install -U {PACKAGE}`"
    )


class UpdateCheck:
    """Start looking for a newer release now; `report()` prints a notice if one turned up."""

    def __init__(self) -> None:
        self.current = current_version()
        self.latest: str | None = None
        self._thread: threading.Thread | None = None
        if self.current and not os.environ.get(ENV_OPT_OUT):
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        self.latest = latest_version()

    def report(self, wait: float = TIMEOUT) -> None:
        if self._thread is None:
            return
        self._thread.join(wait)
        if self.current and self.latest and is_newer(self.latest, self.current):
            print(notice(self.current, self.latest), file=sys.stderr)
