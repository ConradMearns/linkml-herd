import json
from pathlib import Path

import pytest

from linkml_herd import update_check
from linkml_herd.cli import main
from linkml_herd.update_check import ENV_OPT_OUT, UpdateCheck, is_newer, latest_version

EXAMPLE = str(Path(__file__).parent.parent / "examples" / "ecommerce.linkml.yaml")


@pytest.fixture
def pypi(monkeypatch, tmp_path):
    """Opt back in to the check, with PyPI replaced by a counter and the cache in tmp_path."""
    monkeypatch.delenv(ENV_OPT_OUT)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(update_check, "current_version", lambda: "0.1.0")
    calls = []

    def fetch():
        calls.append(1)
        if isinstance(pypi.latest, Exception):
            raise pypi.latest
        return pypi.latest

    pypi.latest, pypi.calls = "0.2.0", calls
    monkeypatch.setattr(update_check, "_fetch_latest", fetch)
    return pypi


@pytest.mark.parametrize(
    ("latest", "current", "newer"),
    [
        ("0.2.0", "0.1.0", True),
        ("0.10.0", "0.9.3", True),
        ("1.0", "0.9.9", True),
        ("0.1.0", "0.1", False),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("0.2.0rc1", "0.1.0", False),  # pre-releases never nag
        ("0.2.0", "0.2.0.dev3", False),  # nor do odd local versions
    ],
)
def test_is_newer(latest, current, newer):
    assert is_newer(latest, current) is newer


def test_pypi_is_asked_at_most_once_a_day(pypi):
    assert latest_version(now=1000) == "0.2.0"
    pypi.latest = "0.3.0"
    assert latest_version(now=1000 + 3600) == "0.2.0"
    assert len(pypi.calls) == 1
    assert latest_version(now=1000 + 25 * 3600) == "0.3.0"


def test_failures_are_quiet_and_cached(pypi):
    pypi.latest = OSError("offline")
    assert latest_version(now=1000) is None
    assert latest_version(now=1001) is None
    assert len(pypi.calls) == 1  # no timeout on every run while offline


def test_corrupt_cache_is_ignored(pypi, tmp_path):
    cache = tmp_path / "cache" / "linkml-herd" / "latest-version.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json")
    assert latest_version() == "0.2.0"
    assert json.loads(cache.read_text())["latest"] == "0.2.0"


def test_cli_mentions_a_newer_release(pypi, tmp_path, capsys):
    main([EXAMPLE, "-o", str(tmp_path / "out.html")])
    err = capsys.readouterr().err
    assert "linkml-herd 0.2.0 is available (you have 0.1.0)" in err


def test_cli_is_quiet_when_up_to_date(pypi, tmp_path, capsys):
    pypi.latest = "0.1.0"
    main([EXAMPLE, "-o", str(tmp_path / "out.html")])
    assert "available" not in capsys.readouterr().err


def test_check_can_be_turned_off(pypi, tmp_path, monkeypatch, capsys):
    main([EXAMPLE, "-o", str(tmp_path / "a.html"), "--no-update-check"])
    monkeypatch.setenv(ENV_OPT_OUT, "1")
    UpdateCheck().report()
    main([EXAMPLE, "-o", str(tmp_path / "b.html")])
    assert "available" not in capsys.readouterr().err
    assert not pypi.calls
