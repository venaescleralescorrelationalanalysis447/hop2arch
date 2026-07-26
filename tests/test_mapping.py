"""The mapping database and the matcher.

The matcher decides what a person's Windows program becomes on Arch, and it is
the one place where being *slightly* wrong is worse than admitting ignorance:
"Visual Studio Code -> keep a Windows VM" is a sentence that would send someone
to the wrong decision. So the four passes are pinned here in order, and the
shipped database is checked as data, not just as syntax.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_database

from hop.mapping import STRATEGIES, STRATEGY_WEIGHT, Database, DatabaseError

# Four rules, one per matching pass, deliberately overlapping. Every rule below
# would match the name "Thing"; which one wins is the whole subject of the first
# four tests.
PRECEDENCE_TOML = """
[[app]]
id = "by-winget"
name = "Winget rule"
strategy = "native"
winget = ["Vendor.Thing"]
pacman = ["thing-winget"]
notes = "matched by exact winget id."

[[app]]
id = "by-exe"
name = "Exe rule"
strategy = "native"
exe = ["thing.exe"]
pacman = ["thing-exe"]
notes = "matched by executable name."

[[app]]
id = "by-name"
name = "Name rule"
strategy = "native"
match = ["thing"]
pacman = ["thing-name"]
notes = "matched by substring of the display name."

[[app]]
id = "by-regex"
name = "Regex rule"
strategy = "native"
regex = "th.ng"
pacman = ["thing-regex"]
notes = "matched by regular expression."
"""


@pytest.fixture
def precedence_db(tmp_path: Path) -> Database:
    return Database.load(write_database(tmp_path / "db", PRECEDENCE_TOML))


def test_winget_id_beats_everything(precedence_db: Database) -> None:
    """An exact winget id is the only identifier a vendor actually controls."""
    result = precedence_db.match("Thing", winget_id="Vendor.Thing", executables=("thing.exe",))
    assert result is not None
    assert result.rule.id == "by-winget"
    assert result.method == "winget"
    assert result.matched_on == "Vendor.Thing"


def test_executable_beats_the_name(precedence_db: Database) -> None:
    """The name "Thing" would match by substring too; the exe is the better answer."""
    result = precedence_db.match("Thing", winget_id=None, executables=("thing.exe",))
    assert result is not None
    assert result.rule.id == "by-exe"
    assert result.method == "exe"


def test_name_substring_beats_the_regex(precedence_db: Database) -> None:
    """The regex th.ng matches "thing" as well. The cheaper, plainer pass wins."""
    result = precedence_db.match("Thing")
    assert result is not None
    assert result.rule.id == "by-name"
    assert result.method == "name"
    assert result.matched_on == "thing"


def test_regex_is_the_last_resort(precedence_db: Database) -> None:
    """No literal substring matches "th1ng", so the regex is what is left."""
    result = precedence_db.match("th1ng")
    assert result is not None
    assert result.rule.id == "by-regex"
    assert result.method == "regex"


def test_longest_substring_wins_in_the_real_database(db: Database) -> None:
    """Visual Studio Code is not Visual Studio, and the difference is a blocker.

    This is the case the longest-first ordering in ``Database._index`` was
    written for, so it is asserted against the shipped data rather than a
    fixture: the day someone adds a bare "visual studio" token, this fails.
    """
    code = db.match("Microsoft Visual Studio Code")
    assert code is not None
    assert code.rule.id == "vscode"
    assert code.rule.strategy == "native"
    assert code.matched_on == "visual studio code"

    ide = db.match("Microsoft Visual Studio Community 2022")
    assert ide is not None
    assert ide.rule.id == "visual-studio"
    assert ide.rule.strategy == "none"


def test_a_longer_token_wins_over_a_shorter_one(tmp_path: Path) -> None:
    """The same rule stated as a rule, not as one lucky pair of names."""
    toml = """
[[app]]
id = "short"
name = "Short token"
strategy = "native"
match = ["visual studio"]
pacman = ["short"]
notes = "the more general rule."

[[app]]
id = "long"
name = "Long token"
strategy = "native"
match = ["visual studio code"]
pacman = ["long"]
notes = "the more specific rule."
"""
    db = Database.load(write_database(tmp_path / "db", toml))
    result = db.match("Microsoft Visual Studio Code")
    assert result is not None
    assert result.rule.id == "long"
    # And the general rule still answers the general name.
    other = db.match("Microsoft Visual Studio 2022")
    assert other is not None
    assert other.rule.id == "short"


def test_ignore_rules_cover_names_that_also_match_an_app_rule(db: Database) -> None:
    """"Steamworks Common Redistributables" contains "steam" and is not Steam.

    ``Database`` answers both questions; the planner asks the ignore question
    first, which is checked in test_plan.py.
    """
    name = "Steamworks Common Redistributables"
    ignored = db.ignored(name)
    assert ignored is not None
    assert ignored.reason
    matched = db.match(name)
    assert matched is not None and matched.rule.id == "steam"


def test_shipped_database_lints_clean(db: Database) -> None:
    """The linter is what stops a bad pull request to packages.toml.

    If this fails, read the message: it names the rule id and the problem, and
    the fix belongs in hop/data/packages.toml, not here.
    """
    assert db.lint() == []


def test_web_and_none_entries_may_carry_packages(tmp_path: Path) -> None:
    """A service with no desktop client can still have a community wrapper.

    The linter used to complain about exactly this and the complaint was wrong:
    teams-for-linux and figma-linux-bin are real packages, and a blocker can
    have a partial stand-in worth naming. The report labels them as consolation
    prizes rather than answers. This test exists so the old rule cannot come
    back.
    """
    toml = """
[[app]]
id = "some-service"
name = "Some Service"
strategy = "web"
match = ["some service"]
aur = ["some-service-wrapper"]
notes = "no official desktop client; the browser is the product."

[[app]]
id = "some-blocker"
name = "Some Blocker"
strategy = "none"
match = ["some blocker"]
pacman = ["a-partial-standin"]
notes = "nothing here does the whole job; this covers part of it."
"""
    db = Database.load(write_database(tmp_path / "db", toml))
    assert db.lint() == []


def test_the_real_database_still_exercises_that_case(db: Database) -> None:
    """Guard against the test above becoming theoretical."""
    carriers = [r for r in db.apps if r.strategy in ("web", "none") and r.has_packages]
    assert carriers, "no web/none rule carries a package any more — is the test above still needed?"


def test_stats_add_up(db: Database) -> None:
    stats = db.stats()
    by_strategy = sum(v for k, v in stats.items() if k.startswith("strategy:"))
    by_confidence = sum(v for k, v in stats.items() if k.startswith("confidence:"))
    assert by_strategy == stats["apps"] == len(db.apps)
    assert by_confidence == stats["apps"]
    assert stats["ignore"] == len(db.ignores)
    assert stats["games"] == len(db.games)


def test_every_strategy_in_the_database_has_a_weight(db: Database) -> None:
    """A strategy the scorer has never heard of would silently count as zero."""
    for rule in db.apps:
        assert rule.strategy in STRATEGIES
        assert rule.strategy in STRATEGY_WEIGHT


def test_no_match_returns_none(db: Database) -> None:
    """Being unable to answer is a valid answer, and it must not be faked."""
    assert db.match("Frobnicator Deluxe 9000 by Nobody At All") is None


def test_search_finds_by_id_name_token_and_package(db: Database) -> None:
    assert any(r.id == "firefox" for r in db.search("firefox"))
    assert any(r.id == "vlc" for r in db.search("vlc"))


def test_game_status_by_appid_and_by_name(db: Database) -> None:
    by_id = db.game_status(appid=1085660)
    assert by_id is not None and by_id.status == "blocked"
    by_name = db.game_status(name="Counter-Strike 2")
    assert by_name is not None and by_name.status == "works"
    assert db.game_status(appid=0, name="A Game Nobody Has Heard Of") is None


def test_preferred_source_order_and_flatpak_flip(db: Database) -> None:
    rule = db.by_id("firefox")
    assert rule is not None
    assert rule.preferred() == ("pacman", "firefox")
    source, package = rule.preferred(prefer_flatpak=True)
    assert source == "flatpak"
    assert package.startswith("org.mozilla")


def test_a_bad_regex_is_reported_with_the_rule_id(tmp_path: Path) -> None:
    """A contributor's typo has to name itself, not surface as a re.error."""
    toml = """
[[app]]
id = "broken"
name = "Broken rule"
strategy = "native"
regex = "(unclosed"
pacman = ["broken"]
notes = "this rule does not compile."
"""
    with pytest.raises(DatabaseError, match="broken"):
        Database.load(write_database(tmp_path / "db", toml))


def test_a_bad_strategy_is_reported(tmp_path: Path) -> None:
    toml = """
[[app]]
id = "odd"
name = "Odd rule"
strategy = "teleport"
match = ["odd"]
notes = "not a strategy hop knows."
"""
    with pytest.raises(DatabaseError, match="teleport"):
        Database.load(write_database(tmp_path / "db", toml))


def test_a_missing_field_is_reported(tmp_path: Path) -> None:
    toml = """
[[app]]
id = "nameless"
strategy = "native"
pacman = ["nameless"]
notes = "no name field."
"""
    with pytest.raises(DatabaseError, match="name"):
        Database.load(write_database(tmp_path / "db", toml))


def test_a_missing_database_file_says_which_one(tmp_path: Path) -> None:
    with pytest.raises(DatabaseError, match="packages.toml"):
        Database.load(tmp_path / "nothing-here")
