"""Keep pytest temporary files isolated between users and test runs."""

from pathlib import Path
from tempfile import TemporaryDirectory


def pytest_configure(config):
    if config.option.basetemp is not None:
        return

    # Windows users and sandbox processes may share TEMP but have different ACLs.
    # Avoid pytest's shared pytest-of-<username> directory in that case.
    directory = TemporaryDirectory(
        prefix="bjm-pytest-", ignore_cleanup_errors=True
    )
    config.option.basetemp = Path(directory.name) / "tests"
    config.add_cleanup(directory.cleanup)
