"""The planner.

The number this module produces is the number somebody makes the decision on,
so the weights behind it are pinned to the decimal place. The rest of the file
is about the machine rather than the software: the driver that follows from the
graphics card, the layout that follows from the Windows keyboard id, the
hostname that has to survive being handed to hostnamectl.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from conftest import write_database

from hop.manifest import Manifest
from hop.mapping import Database
from hop.plan import (
    DESKTOPS,
    GAMING_PACKAGES,
    KLID_TO_XKB,
    LAPTOP_PACKAGES,
    PLAN_VERSION,
    Plan,
    Planner,
    _sanitise_hostname,
    _sanitise_username,
    _to_posix_locale,
    _verdict,
    _xkb_layouts,
)

#: A valid Linux hostname: lowercase, ASCII, no leading or trailing hyphen.
#: systemd-hostnamed rejects anything else, and archinstall refuses to write it.
HOSTNAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")

#: What useradd's default NAME_REGEX accepts, near enough.
USERNAME_RE = re.compile(r"[a-z_][a-z0-9_-]*")


def manifest_with(*names: str, **sections: Any) -> Manifest:
    """A hopfile carrying nothing but the named programs, plus any sections given."""
    raw: dict[str, Any] = {"hopfile_version": 1, "software": [{"name": n} for n in names]}
    raw.update(sections)
    return Manifest.from_dict(raw)


# --- scoring ---------------------------------------------------------------


def test_hoppability_weights_are_pinned(db: Database) -> None:
    """One program per strategy, so the arithmetic is readable.

    native 1.0 + builtin 1.0 + alternative 0.75 + compat 0.5 + web 0.4 +
    none 0.0 = 3.65 over 6 = 60.83%.
    """
    manifest = manifest_with(
        "VLC media player",  # native
        "CCleaner",  # builtin
        "WinRAR 7.01",  # alternative
        "Battle.net",  # compat
        "Figma",  # web
        "Adobe Premiere Pro 2024",  # none
    )
    plan = Planner(manifest, db).build()
    assert plan.score["by_strategy"] == {
        "native": 1,
        "builtin": 1,
        "alternative": 1,
        "compat": 1,
        "web": 1,
        "none": 1,
    }
    assert plan.score["hoppability"] == 60.8
    assert plan.score["considered"] == 6
    assert plan.score["blockers"] == 1


def test_a_machine_of_things_that_come_across(db: Database) -> None:
    """(1.0 + 1.0 + 0.75) / 3 = 91.7%."""
    manifest = manifest_with("Mozilla Firefox (x64 ru)", "VLC media player", "WinRAR 7.01")
    plan = Planner(manifest, db).build()
    assert plan.score["hoppability"] == 91.7


def test_unknown_programs_count_as_zero(db: Database) -> None:
    """Deliberate. Being unable to answer is a real cost, and hiding it is a lie."""
    absent = "Frobnicator Deluxe 9000 by Nobody At All"
    assert db.match(absent) is None, "pick a name the database really has no opinion on"

    plan = Planner(manifest_with("VLC media player", absent), db).build()
    assert plan.score["hoppability"] == 50.0
    assert plan.score["considered"] == 2
    assert plan.score["matched"] == 1
    assert plan.score["unknown"] == 1


def test_ignored_entries_leave_the_denominator_alone(db: Database) -> None:
    """A Visual C++ redistributable is not a program the user chose to have."""
    plan = Planner(
        manifest_with(
            "VLC media player",
            "Frobnicator Deluxe 9000 by Nobody At All",
            "Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.40.33810",
        ),
        db,
    ).build()
    assert plan.score["hoppability"] == 50.0
    assert plan.score["considered"] == 2
    assert plan.score["ignored"] == 1
    assert [e["name"] for e in plan.ignored] == [
        "Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.40.33810"
    ]


def test_an_ignore_rule_wins_over_an_app_rule(db: Database) -> None:
    """"Steamworks Common Redistributables" contains "steam". It is not Steam."""
    plan = Planner(manifest_with("Steamworks Common Redistributables"), db).build()
    assert plan.items == []
    assert [e["name"] for e in plan.ignored] == ["Steamworks Common Redistributables"]


def test_an_empty_scan_scores_nothing_rather_than_everything(db: Database) -> None:
    """100% would be the most encouraging possible answer to a question hop never
    got to look at."""
    plan = Planner(manifest_with(), db).build()
    assert plan.score["hoppability"] is None
    assert "nothing to score" in plan.score["verdict"]


@pytest.mark.parametrize("percent", [100.0, 97.0, 90.0, 82.5, 75.0, 60.0, 55.0])
def test_the_verdict_never_says_nothing_lost_while_there_are_blockers(percent: float) -> None:
    """A blocker weighs zero, so a machine can sit at 97% with three of them.

    The report answers that with a section headed "No Linux path. Decide what
    you are doing about these before you wipe anything." The one-line verdict
    printed above it used to answer it with "nothing lost", and the one-line
    verdict is the one people read.
    """
    verdict = _verdict(percent, 3)
    assert "nothing lost" not in verdict
    assert "nothing on this machine is holding you back" not in verdict
    assert "blocker" in verdict, verdict


@pytest.mark.parametrize(("percent", "wanted"), [(97.0, "clean hop"), (80.0, "nothing lost")])
def test_a_machine_with_no_blockers_still_gets_the_good_news(percent: float, wanted: str) -> None:
    assert wanted in _verdict(percent, 0)


def test_a_verdict_only_sends_you_to_the_blockers_when_there_are_some() -> None:
    """A report with no Blockers section should not open by telling you to read it."""
    assert "read the blockers" not in _verdict(60.0, 0)
    assert "no blockers" in _verdict(60.0, 0)
    assert "read the blockers" in _verdict(60.0, 1)


def test_the_verdict_and_the_blockers_section_agree(db: Database, tmp_path: Path) -> None:
    """The same machine, end to end: one program with no Linux path and enough
    native ones to push the number over 90."""
    data = write_database(
        tmp_path / "db",
        """
[[app]]
id = "fine"
name = "Fine"
strategy = "native"
match = ["fine"]
pacman = ["fine"]
notes = "Same program, same name."

[[app]]
id = "stuck"
name = "Stuck"
strategy = "none"
match = ["stuck"]
notes = "No Linux build and no stand-in worth naming."
""",
    )
    names = [f"Fine {n}" for n in range(19)] + ["Stuck 1"]
    plan = Planner(manifest_with(*names), Database.load(data)).build()
    assert plan.score["hoppability"] == 95.0
    assert plan.score["blockers"] == 1
    assert "nothing lost" not in plan.score["verdict"]
    assert "1 blocker" in plan.score["verdict"]


def test_the_example_machine_reconciles(example_plan: Plan) -> None:
    """The headline number and the section counts have to be the same story."""
    score = example_plan.score
    assert sum(score["by_strategy"].values()) == score["matched"]
    assert score["matched"] + score["unknown"] == score["considered"]
    assert score["blockers"] == len(example_plan.blockers)
    assert score["by_strategy"].get("none", 0) == score["blockers"]


def test_duplicate_software_entries_are_counted_once(db: Database) -> None:
    plan = Planner(manifest_with("VLC media player", "VLC media player"), db).build()
    assert len(plan.items) == 1


# --- packages --------------------------------------------------------------


def test_a_package_in_both_lists_survives_only_in_pacman(tmp_path: Path) -> None:
    """Building from the AUR something the repos already have is wasted hours."""
    toml = """
[[app]]
id = "alpha"
name = "Alpha"
strategy = "native"
match = ["alpha"]
pacman = ["shared"]
notes = "in the official repositories."

[[app]]
id = "beta"
name = "Beta"
strategy = "native"
match = ["beta"]
aur = ["shared"]
notes = "the same name, from the AUR."
"""
    db = Database.load(write_database(tmp_path / "db", toml))
    plan = Planner(manifest_with("Alpha", "Beta"), db).build()
    assert "shared" in plan.packages["pacman"]
    assert "shared" not in plan.packages["aur"]


def test_no_item_names_a_package_the_plan_does_not_install(example_plan: Plan) -> None:
    """The report prints items; everything else installs the buckets.

    Conflict resolution drops nvidia in favour of nvidia-dkms. If it dropped the
    package without rewriting the item that asked for it, the report would tell
    the reader to install one driver while every list that installs anything
    installed the other — the same document, two answers, about graphics.
    """
    for item in example_plan.items:
        if not item.package or not item.install_source:
            continue
        bucket = example_plan.packages.get(item.install_source, [])
        # item.packages is the whole menu the rule offers and is deliberately
        # wider than what gets installed; item.package is the one hop chose, and
        # that one has to be real.
        assert item.package in bucket, f"{item.source} points at {item.package}, which is not installed"


def test_conflicting_packages_are_resolved_and_explained(example_plan: Plan) -> None:
    """pacman refuses a transaction, not a package: one bad pair costs the batch."""
    pacman = example_plan.packages["pacman"]
    assert "nvidia-dkms" in pacman and "nvidia" not in pacman
    assert "7zip" in pacman and "p7zip" not in pacman
    assert "chosen over" in example_plan.package_reasons["nvidia-dkms"]
    assert "chosen over" in example_plan.package_reasons["7zip"]


def test_every_package_carries_a_reason(example_plan: Plan) -> None:
    """The report shows not only what, but why. A package with no reason reads
    as something hop slipped in."""
    for package in example_plan.packages["pacman"]:
        assert example_plan.package_reasons.get(package), package


def test_one_wifi_backend_not_two(example_plan: Plan) -> None:
    """NetworkManager talks to one supplicant. The other is a package the reader
    cannot account for."""
    assert "wpa_supplicant" in example_plan.packages["pacman"]
    assert "iwd" not in example_plan.packages["pacman"]


@pytest.mark.parametrize(
    ("vendors", "expected", "unexpected"),
    [
        (["nvidia"], ["nvidia-dkms", "nvidia-utils"], ["vulkan-radeon"]),
        (["amd"], ["mesa", "vulkan-radeon"], ["nvidia-utils"]),
        (["intel"], ["mesa", "vulkan-intel"], ["vulkan-radeon"]),
        (["intel", "nvidia"], ["mesa", "vulkan-intel", "nvidia-dkms"], ["vulkan-radeon"]),
        ([], ["mesa"], ["nvidia-utils", "vulkan-radeon"]),
    ],
)
def test_gpu_vendors_choose_the_driver_stack(
    db: Database, vendors: list[str], expected: list[str], unexpected: list[str]
) -> None:
    manifest = manifest_with(system={"gpus": [{"vendor": v} for v in vendors]})
    plan = Planner(manifest, db).build()
    for package in expected:
        assert package in plan.packages["pacman"], package
    for package in unexpected:
        assert package not in plan.packages["pacman"], package


def test_a_laptop_gets_the_laptop_packages(db: Database) -> None:
    laptop = Planner(manifest_with(system={"chassis": "laptop"}), db).build()
    desktop = Planner(manifest_with(system={"chassis": "desktop"}), db).build()
    for package in LAPTOP_PACKAGES:
        assert package in laptop.packages["pacman"], package
        assert package not in desktop.packages["pacman"], package


def test_no_gaming_suppresses_the_stack_even_with_steam_installed(
    example_manifest: Manifest, db: Database
) -> None:
    """Steam itself still comes across — it is installed software, and the flag
    is about the extras hop would otherwise add on its own."""
    plan = Planner(example_manifest, db, include_gaming=False).build()
    assert plan.target["gaming"] is False
    for package in ("gamemode", "lib32-gamemode", "winetricks"):
        assert package not in plan.packages["pacman"], package
    assert "steam" in plan.packages["pacman"]


def test_gaming_is_detected_from_the_installed_launchers(db: Database) -> None:
    plan = Planner(manifest_with("Battle.net"), db).build()
    assert plan.target["gaming"] is True
    for package in GAMING_PACKAGES:
        assert package in plan.packages["pacman"], package


def test_prefer_flatpak_flips_the_source(db: Database) -> None:
    manifest = manifest_with("Mozilla Firefox (x64 ru)")
    repos = Planner(manifest, db).build().items[0]
    flatpaks = Planner(manifest, db, prefer_flatpak=True).build()
    assert (repos.install_source, repos.package) == ("pacman", "firefox")
    assert flatpaks.items[0].install_source == "flatpak"
    assert flatpaks.items[0].package.startswith("org.mozilla")
    assert flatpaks.items[0].package in flatpaks.packages["flatpak"]
    assert "firefox" not in flatpaks.packages["pacman"]


@pytest.mark.parametrize("desktop", sorted(DESKTOPS))
def test_every_desktop_plans(db: Database, desktop: str) -> None:
    plan = Planner(manifest_with(), db, desktop=desktop).build()
    assert plan.target["desktop"] == desktop
    assert plan.target["display_manager"] == DESKTOPS[desktop]["display_manager"]
    for package in DESKTOPS[desktop]["pacman"]:
        assert package in plan.packages["pacman"], package


def test_an_unknown_desktop_is_refused(db: Database) -> None:
    with pytest.raises(ValueError, match="unknown desktop"):
        Planner(manifest_with(), db, desktop="fluxbox")


# --- locale, layouts, names ------------------------------------------------


@pytest.mark.parametrize(
    ("windows", "posix"),
    [
        ("ru-RU", "ru_RU.UTF-8"),
        ("en-US", "en_US.UTF-8"),
        ("en", "en_EN.UTF-8"),
        ("sr-Latn-RS", "sr_RS.UTF-8"),
        ("pt-BR", "pt_BR.UTF-8"),
        ("en_GB", "en_GB.UTF-8"),
        (None, "en_US.UTF-8"),
        ("", "en_US.UTF-8"),
    ],
)
def test_locale_conversion(windows: str | None, posix: str) -> None:
    assert _to_posix_locale(windows) == posix


def test_english_is_always_generated_as_well(db: Database) -> None:
    """An error message in English is the one you can search for."""
    plan = Planner(manifest_with(system={"locale": "ru-RU"}), db).build()
    assert plan.system["locale"] == "ru_RU.UTF-8"
    assert plan.system["locales"] == ["en_US.UTF-8", "ru_RU.UTF-8"]


def test_klid_layouts_map_and_unknown_ones_are_dropped() -> None:
    assert _xkb_layouts(["00000409", "00000419"]) == ["us", "ru"]
    assert _xkb_layouts(["409", "419"]) == ["us", "ru"]  # zero-padded on the way in
    assert _xkb_layouts(["ffffffff"]) == []
    assert _xkb_layouts(["00000409", "00000409"]) == ["us"]
    assert _xkb_layouts([]) == []
    assert _xkb_layouts(["00000437"]) == ["ge"]


def test_every_klid_in_the_table_is_reachable() -> None:
    """A KLID is eight hex digits, and the lookup zero-pads to eight before it
    asks. An entry of any other length is a row that can never be matched — the
    Georgian one sat there being seven digits long, so a Georgian keyboard came
    out as 'us' with nothing to say it had been dropped."""
    for klid, layout in KLID_TO_XKB.items():
        assert len(klid) == 8, f"{klid} ({layout}) is {len(klid)} characters, so nothing can match it"
        assert klid == klid.lower(), f"{klid} is upper case; the lookup lower-cases before it asks"
        int(klid, 16)
        assert _xkb_layouts([klid]) == [layout]


def test_layouts_fall_back_to_us(db: Database) -> None:
    """A keyboard hop cannot name is still a keyboard someone has to type on."""
    plan = Planner(manifest_with(system={"keyboard_layouts": ["ffffffff"]}), db).build()
    assert plan.system["x11_layouts"] == ["us"]
    assert plan.system["keymap"] == "us"


@pytest.mark.parametrize(
    ("windows", "expected"),
    [
        ("NB-ARTEM", "nb-artem"),
        ("My Home PC", "my-home-pc"),
        ("NB_ARTEM", "nb-artem"),
        ("DESKTOP-4F2A1B", "desktop-4f2a1b"),
        ("", "arch"),
        ("---", "arch"),
        ("x" * 80, "x" * 63),
    ],
)
def test_hostname_sanitising(windows: str, expected: str) -> None:
    assert _sanitise_hostname(windows) == expected


def test_a_non_ascii_hostname_becomes_a_usable_one() -> None:
    """Windows accepts a Cyrillic computer name. hostnamectl does not.

    The name has to be reduced to something the new machine will take, because
    everything downstream — hostnamectl, /etc/hosts, the generated post-install
    script — is entitled to refuse anything else, and refusing it *there* means
    the failure lands on the user long after the decision was made.
    """
    for name in ("Артём-ПК", "ノートパソコン", "Ноутбук-Артёма-2"):
        cleaned = _sanitise_hostname(name)
        assert cleaned.isascii(), cleaned
        assert HOSTNAME_RE.fullmatch(cleaned), cleaned
        assert len(cleaned) <= 63


@pytest.mark.parametrize(
    ("windows", "expected"),
    [
        ("artem", "artem"),
        ("ARTEM", "artem"),
        ("John.Doe Jr", "johndoejr"),
        ("2cool", "u2cool"),
        ("-weird", "weird"),
        ("", "user"),
        ("!!!", "user"),
        ("Артём", "user"),
    ],
)
def test_username_sanitising(windows: str, expected: str) -> None:
    assert _sanitise_username(windows) == expected


def test_sanitised_names_are_valid_on_the_other_side(db: Database) -> None:
    plan = Planner(
        manifest_with(system={"hostname": "Ноутбук Артёма"}, user={"name": "Артём"}), db
    ).build()
    assert HOSTNAME_RE.fullmatch(plan.system["hostname"])
    assert USERNAME_RE.fullmatch(plan.system["username"])


def test_an_explicit_hostname_wins(example_manifest: Manifest, db: Database) -> None:
    plan = Planner(example_manifest, db, hostname="workstation").build()
    assert plan.system["hostname"] == "workstation"


# --- the rest of the document ---------------------------------------------


def test_the_example_system_block(example_plan: Plan) -> None:
    system = example_plan.system
    assert system["hostname"] == "nb-artem"
    assert system["username"] == "artem"
    assert system["locale"] == "ru_RU.UTF-8"
    assert system["timezone"] == "Europe/Moscow"
    assert system["x11_layouts"] == ["us", "ru"]
    assert system["firmware"] == "UEFI"
    assert system["secure_boot"] is True
    assert system["gpu_vendors"] == ["intel", "nvidia"]


def test_warnings_lead_with_the_ones_that_cost_data(example_plan: Plan) -> None:
    joined = "\n".join(example_plan.warnings)
    assert "BitLocker" in joined
    assert "Secure Boot" in joined
    assert "WSL" in joined
    assert "nvidia-dkms" in joined


def test_games_are_resolved_against_the_anticheat_snapshot(example_plan: Plan) -> None:
    games = example_plan.games
    assert games["total"] == 9
    assert games["counts"]["blocked"] == 2
    assert games["counts"]["unknown"] == 1
    # Worst news first: blocked, then broken, then unknown, then what plays.
    statuses = [t["status"] for t in games["titles"]]
    assert statuses == sorted(statuses, key=["blocked", "broken", "unknown", "works"].index)


def test_user_data_is_summarised(example_plan: Plan) -> None:
    data = example_plan.data
    assert [f["name"] for f in data["folders"]][0] == "Videos"
    assert data["total_bytes"] == sum(f["size_bytes"] for f in data["folders"])
    assert data["steam_bytes"] > 0
    assert data["onedrive"]["present"] is True


def test_payload_entries_are_carried_through(example_plan: Plan) -> None:
    assert len(example_plan.payload) == 13
    assert example_plan.payload[0]["path"] == "ssh/id_ed25519"


# --- serialisation ---------------------------------------------------------


def test_round_trip_preserves_every_field(example_plan: Plan) -> None:
    """A plan is meant to be read, edited by hand and handed back to hop land."""
    raw = example_plan.to_dict()
    assert raw["plan_version"] == PLAN_VERSION
    assert raw["generated_at"].endswith("Z")

    restored = Plan.from_dict(raw)
    assert restored.items == example_plan.items
    assert restored.hopfile == example_plan.hopfile
    assert restored.target == example_plan.target
    assert restored.system == example_plan.system
    assert restored.packages == example_plan.packages
    assert restored.package_reasons == example_plan.package_reasons
    assert restored.unknown == example_plan.unknown
    assert restored.ignored == example_plan.ignored
    assert restored.games == example_plan.games
    assert restored.payload == example_plan.payload
    assert restored.data == example_plan.data
    assert restored.warnings == example_plan.warnings
    assert restored.score == example_plan.score

    again = restored.to_dict()
    again.pop("generated_at")
    raw.pop("generated_at")
    assert again == raw


def test_a_hand_edited_item_is_a_sentence_not_a_traceback(example_plan: Plan) -> None:
    """Editing a plan is supported — it is JSON so that it can be edited — so a
    deleted line and a mistyped key are ordinary events, not corruption."""
    raw = example_plan.to_dict()
    del raw["items"][0]["version"]
    with pytest.raises(ValueError, match="is missing 'version'") as excinfo:
        Plan.from_dict(raw)
    assert raw["items"][0]["source"] in str(excinfo.value), "say which item, there are dozens"
    assert "delete the whole item" in str(excinfo.value), "say what to do instead"


def test_an_unrecognised_item_key_lists_the_ones_that_exist(example_plan: Plan) -> None:
    raw = example_plan.to_dict()
    raw["items"][0]["notez"] = "a typo for notes"
    with pytest.raises(ValueError, match="'notez'") as excinfo:
        Plan.from_dict(raw)
    assert "notes" in str(excinfo.value)


def test_an_item_that_is_not_an_object(example_plan: Plan) -> None:
    raw = example_plan.to_dict()
    raw["items"][1] = "just a name"
    with pytest.raises(ValueError, match="item 2 of the plan is a str"):
        Plan.from_dict(raw)


def test_a_plan_with_no_items_at_all_loads() -> None:
    plan = Plan.from_dict({"plan_version": PLAN_VERSION})
    assert plan.items == []
    assert plan.packages == {}


@pytest.mark.parametrize(
    "key", ["hopfile", "target", "system", "packages", "package_reasons", "games", "data", "score"]
)
def test_a_section_blanked_out_with_null_loads_as_an_empty_one(key: str) -> None:
    """Emptying a section with null is as ordinary a hand-edit as deleting the
    key, and both have to mean the same thing. Left as None it arrives in hop
    land as an AttributeError, which is a traceback in front of somebody on a
    machine that has no desktop yet."""
    plan = Plan.from_dict({"plan_version": PLAN_VERSION, key: None})
    assert getattr(plan, key) == {}


@pytest.mark.parametrize("key", ["items", "unknown", "ignored", "payload", "warnings"])
def test_a_null_list_section_loads_as_an_empty_list(key: str) -> None:
    plan = Plan.from_dict({"plan_version": PLAN_VERSION, key: None})
    assert getattr(plan, key) == []


def test_a_section_of_the_wrong_shape_is_a_sentence() -> None:
    with pytest.raises(ValueError, match="'packages'") as excinfo:
        Plan.from_dict({"plan_version": PLAN_VERSION, "packages": ["firefox", "vlc"]})
    message = str(excinfo.value)
    assert "hop needs an object there" in message
    assert "set it to null to empty it" in message


def test_a_plan_from_another_version_is_refused() -> None:
    with pytest.raises(ValueError, match="plan version"):
        Plan.from_dict({"plan_version": PLAN_VERSION + 1})


def test_a_document_that_is_not_a_plan_is_refused() -> None:
    with pytest.raises(ValueError, match="plan version"):
        Plan.from_dict({"hopfile_version": 1})
