import pytest

from linkml_herd.update_check import ENV_OPT_OUT


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    """Keep CLI tests off the network; test_update_check.py opts back in where it needs to."""
    monkeypatch.setenv(ENV_OPT_OUT, "1")
