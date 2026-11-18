"""Smoke test: the package is installed and importable."""

import take2


def test_package_exposes_version() -> None:
    assert isinstance(take2.__version__, str)
    assert take2.__version__
