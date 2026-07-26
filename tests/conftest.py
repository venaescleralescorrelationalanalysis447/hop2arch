"""Fixtures shared by the whole suite.

Three rules hold everywhere in ``tests/``. There is no network. Nothing runs
against the real machine — no pacman, no scanner, no installer. Nothing is
written outside ``tmp_path``.

Everything the tests read lives in the repository: the shipped mapping database
and the example hopfile. Both are found from ``__file__``, never from the
working directory, because the suite has to give the same answer whether it is
run from the checkout root, from inside ``tests/``, or by a CI runner with its
own idea of where it started.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# pytest puts the directory holding the test files on sys.path, which is
# tests/, not the checkout root. CI installs nothing — hop2arch has no
# dependencies and proving that is the point — so the package has to be
# importable from the source tree itself.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hop.manifest import Manifest  # noqa: E402
from hop.mapping import Database  # noqa: E402
from hop.plan import Plan, Planner  # noqa: E402


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The checkout root, resolved from this file rather than the cwd."""
    return _REPO_ROOT


@pytest.fixture(scope="session")
def data_dir(repo_root: Path) -> Path:
    return repo_root / "hop" / "data"


@pytest.fixture(scope="session")
def example_hopfile(repo_root: Path) -> Path:
    return repo_root / "examples" / "hopfile.example.json"


@pytest.fixture(scope="session")
def db(data_dir: Path) -> Database:
    """The database hop actually ships, loaded once.

    Session-scoped because parsing 88 kB of TOML for every test is a slow way to
    prove nothing. A :class:`~hop.mapping.Database` is only read by the tests, so
    sharing one is safe; anything that needs a database it can change builds its
    own with :func:`write_database`.
    """
    return Database.load(data_dir)


@pytest.fixture
def example_manifest(example_hopfile: Path) -> Manifest:
    """The synthetic machine in ``examples/``, freshly parsed for each test.

    Not shared: ``Manifest.raw`` is an ordinary mutable dict, and a test that
    edited it would silently change the answers every later test got.
    """
    return Manifest.load(example_hopfile)


@pytest.fixture
def example_plan(example_manifest: Manifest, db: Database) -> Plan:
    """The example hopfile planned with the defaults hop uses when asked nothing."""
    return Planner(example_manifest, db).build()


def write_database(directory: Path, packages_toml: str, anticheat_toml: str = "") -> Path:
    """Write a throwaway mapping database and return its directory.

    Used where a test needs a rule that does not exist in the shipped data — a
    deliberately broken regex, two rules fighting over one name. The real
    database is never edited by a test.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "packages.toml").write_text(packages_toml, encoding="utf-8")
    if anticheat_toml:
        (directory / "anticheat.toml").write_text(anticheat_toml, encoding="utf-8")
    return directory
