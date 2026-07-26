"""Anonymising a hopfile.

Two properties matter here and they pull against each other. The names have to
be gone — the account, the machine, the e-mail, the SSIDs — or somebody's issue
thread tells strangers who they are. The software, the sizes and the hardware
have to survive untouched, or the file is no use to the maintainer it was
posted for. Every test below is one side or the other of that line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hop.manifest import Manifest
from hop.mapping import Database
from hop.plan import Planner
from hop.scrub import SCRUB_VERSION, scrub

#: Everything in the example hopfile that identifies a person or a machine.
SECRETS = (
    "artem",
    "NB-ARTEM",
    "nb-artem",
    "Artem Sokolov",
    "artem.sokolov@example.com",
    "Артём",
    "Артём Соколов",
    "MGTS_GPON_1F4C",
    "eduroam",
)


@pytest.fixture
def raw_example(example_hopfile: Path) -> dict[str, Any]:
    return json.loads(example_hopfile.read_text(encoding="utf-8-sig"))


def dumped(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False)


# --- what must not survive -------------------------------------------------


def test_no_identifying_string_survives_anywhere(raw_example: dict[str, Any]) -> None:
    """Searched over the whole serialised document, keys included, case-blind.

    The named rules cover the format as documented; this covers the format as it
    arrives, including the fields nobody anticipated.
    """
    clean, _report = scrub(raw_example)
    text = dumped(clean).lower()
    for secret in SECRETS:
        assert secret.lower() not in text, secret


def test_the_public_key_body_is_gone_but_the_type_is_kept(raw_example: dict[str, Any]) -> None:
    """Which algorithm the machine was on is a real answer to a real question.
    The base64 and the user@host comment are not."""
    clean, _report = scrub(raw_example)
    key = clean["dev"]["ssh_keys"][0]["public_key"]
    assert key.startswith("ssh-ed25519 <redacted:")
    assert "AAAAC3Nza" not in dumped(clean)


def test_hardware_identifiers_are_deleted_not_replaced(raw_example: dict[str, Any]) -> None:
    """A stand-in for a MAC address would invite somebody to treat it as one."""
    raw_example["system"]["board_serial"] = "PF3X9K2L"
    raw_example["network"]["mac"] = "3c:52:82:11:aa:bb"
    clean, report = scrub(raw_example)
    assert "board_serial" not in clean["system"]
    assert "mac" not in clean["network"]
    assert "PF3X9K2L" not in dumped(clean)
    assert "system.board_serial" in report.removed


def test_the_payload_index_is_dropped(raw_example: dict[str, Any]) -> None:
    """The payload holds the real SSH keys and the real Wi-Fi passwords. It has
    no business in a bug report, and nor does the list of what is in it."""
    clean, report = scrub(raw_example)
    assert clean["payload"] is None
    assert clean["payload_dir"] is None
    text = dumped(clean)
    assert "restore_to" not in text
    assert "~/.ssh/id_ed25519" not in text
    assert "payload" in report.removed
    assert any("hop-payload" in note for note in report.notes)


def test_paths_lose_the_account_and_keep_the_program(raw_example: dict[str, Any]) -> None:
    """C:\\Users\\artem\\AppData\\Roaming\\Spotify still has to say Spotify —
    the directory name is how the matcher guesses the executable."""
    clean, _report = scrub(raw_example)
    locations = [e.get("install_location") or "" for e in clean["software"]]
    spotify = [p for p in locations if p.endswith("Spotify")]
    assert spotify, "the Spotify install location lost its own name"
    assert "artem" not in spotify[0].lower()
    assert spotify[0].startswith("C:\\Users\\")


# --- what must survive -----------------------------------------------------


def test_software_versions_and_publishers_are_untouched(raw_example: dict[str, Any]) -> None:
    clean, _report = scrub(raw_example)
    before = [(e["name"], e.get("version"), e.get("publisher")) for e in raw_example["software"]]
    after = [(e["name"], e.get("version"), e.get("publisher")) for e in clean["software"]]
    assert after == before


def test_disks_games_and_folder_sizes_are_untouched(raw_example: dict[str, Any]) -> None:
    clean, _report = scrub(raw_example)
    assert [d["size_bytes"] for d in clean["disks"]] == [
        d["size_bytes"] for d in raw_example["disks"]
    ]
    assert clean["gaming"]["steam"]["games"] == raw_example["gaming"]["steam"]["games"]
    assert [f["size_bytes"] for f in clean["user"]["folders"].values()] == [
        f["size_bytes"] for f in raw_example["user"]["folders"].values()
    ]
    assert clean["system"]["locale"] == "ru-RU"
    assert clean["system"]["timezone"] == raw_example["system"]["timezone"]


def test_the_original_document_is_not_modified(raw_example: dict[str, Any]) -> None:
    before = dumped(raw_example)
    scrub(raw_example)
    assert dumped(raw_example) == before


def test_the_warnings_survive_and_gain_one(raw_example: dict[str, Any]) -> None:
    clean, _report = scrub(raw_example)
    assert clean["warnings"][: len(raw_example["warnings"])] == raw_example["warnings"]
    assert "anonymised" in clean["warnings"][-1]
    assert clean["scrubbed"] is True
    assert clean["scrub_version"] == SCRUB_VERSION


# --- reproducibility -------------------------------------------------------


def test_scrubbing_twice_gives_byte_identical_json(raw_example: dict[str, Any]) -> None:
    """Stand-ins come from a hash, not a random number, so a file can be
    re-scrubbed and diffed against the copy already in the issue."""
    first, _ = scrub(raw_example)
    second, _ = scrub(raw_example)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_the_same_name_becomes_the_same_standin(raw_example: dict[str, Any]) -> None:
    clean, report = scrub(raw_example)
    standin = report.replacements["artem"]
    assert clean["user"]["name"] == standin
    assert clean["user"]["profile_path"] == f"C:\\Users\\{standin}"


def test_a_salt_changes_the_standins_and_reproduces_them(raw_example: dict[str, Any]) -> None:
    plain, _ = scrub(raw_example)
    salted, _ = scrub(raw_example, salt="private")
    again, _ = scrub(raw_example, salt="private")
    assert dumped(salted) != dumped(plain)
    assert dumped(salted) == dumped(again)


def test_scrub_refuses_something_that_is_not_a_hopfile() -> None:
    with pytest.raises(TypeError, match="json.load"):
        scrub("not a document")  # type: ignore[arg-type]


# --- still a working hopfile ----------------------------------------------


def test_a_scrubbed_hopfile_plans_to_the_same_packages(
    raw_example: dict[str, Any], db: Database
) -> None:
    """The whole point. A scrubbed file that plans differently from the original
    is worse than useless: the maintainer would be debugging a different machine.
    """
    original = Planner(Manifest.from_dict(raw_example), db).build()
    clean, _report = scrub(raw_example)
    scrubbed = Planner(Manifest.from_dict(clean), db).build()

    assert scrubbed.packages == original.packages
    assert scrubbed.package_reasons == original.package_reasons
    assert scrubbed.items == original.items
    assert scrubbed.unknown == original.unknown
    assert scrubbed.score == original.score
    # The two documents differ where they should: the payload is gone, and the
    # scrubber leaves a line saying the file was anonymised.
    assert scrubbed.payload == []
    assert len(scrubbed.warnings) == len(original.warnings) + 1


def test_a_hopfile_with_no_names_at_all_is_flagged() -> None:
    """Nothing to search for is unusual enough to say so out loud."""
    _clean, report = scrub({"hopfile_version": 1})
    assert any("neither an account name nor a hostname" in note for note in report.notes)


def test_a_common_account_name_is_only_replaced_as_a_whole_word() -> None:
    """An account called "User" must not rewrite C:\\Users\\ in every path."""
    doc = {
        "hopfile_version": 1,
        "user": {"name": "User", "profile_path": "C:\\Users\\User"},
        "software": [{"name": "Thing", "install_location": "C:\\Users\\User\\AppData\\Thing"}],
    }
    clean, report = scrub(doc)
    assert "\\Users\\" in clean["software"][0]["install_location"]
    assert clean["software"][0]["install_location"].endswith("\\AppData\\Thing")
    assert any("word on its own" in note for note in report.notes)
