"""Reading a hopfile.

The scanner runs on a machine nobody on this project can see, under a
PowerShell version we did not choose, possibly without admin rights. Half of
what follows is about surviving that: a hopfile that is 60% filled in must still
plan, and the failures that are genuine must arrive as a sentence rather than a
traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hop.manifest import HopfileError, Manifest, SoftwareEntry, human_bytes

#: Every documented property, with the value it must return when the hopfile
#: says nothing at all. Listed rather than derived so that a property added
#: later without a default shows up as a failure here.
MINIMAL_DEFAULTS = {
    "generated_at": None,
    "generator": None,
    "hostname": "unknown-host",
    "username": "user",
    "locale": None,
    "timezone": None,
    "keyboard_layouts": [],
    "firmware": "unknown",
    "secure_boot": None,
    "chassis": "unknown",
    "memory_gb": None,
    "gpus": [],
    "gpu_vendors": [],
    "disks": [],
    "browsers": [],
    "wifi_profiles": [],
    "payload_dir": None,
    "payload_entries": [],
    "warnings": [],
    "steam_games": [],
    "user_folders": [],
    "onedrive": None,
    "user_data_bytes": 0,
}


def _property_names() -> list[str]:
    return [name for name, value in vars(Manifest).items() if isinstance(value, property)]


# --- the four ways a file can fail to be a hopfile -------------------------


def test_missing_version_names_the_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "hopfile.json"
    path.write_text(json.dumps({"software": []}), encoding="utf-8")
    with pytest.raises(HopfileError, match="hopfile_version"):
        Manifest.load(path)


def test_unsupported_version_says_what_hop_understands(tmp_path: Path) -> None:
    path = tmp_path / "hopfile.json"
    path.write_text(json.dumps({"hopfile_version": 99}), encoding="utf-8")
    with pytest.raises(HopfileError, match="99"):
        Manifest.load(path)


def test_a_json_array_is_not_a_hopfile(tmp_path: Path) -> None:
    path = tmp_path / "hopfile.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(HopfileError, match="JSON object"):
        Manifest.load(path)


def test_malformed_json_says_so_and_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "hopfile.json"
    path.write_text('{"hopfile_version": 1,', encoding="utf-8")
    with pytest.raises(HopfileError, match="not valid JSON"):
        Manifest.load(path)


def test_a_missing_file_is_not_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(HopfileError, match="no such hopfile"):
        Manifest.load(tmp_path / "not-here.json")


def test_a_utf8_bom_is_tolerated(tmp_path: Path) -> None:
    """PowerShell writes a BOM unless it is told not to, and often is not."""
    path = tmp_path / "hopfile.json"
    path.write_text(json.dumps({"hopfile_version": 1}), encoding="utf-8-sig")
    assert Manifest.load(path).hostname == "unknown-host"


# --- PowerShell's collapsed arrays ----------------------------------------


def test_a_single_software_entry_arriving_as_an_object() -> None:
    """ConvertTo-Json turns a one-element array into the element itself."""
    manifest = Manifest.from_dict(
        {"hopfile_version": 1, "software": {"name": "Mozilla Firefox", "version": "128.0"}}
    )
    assert len(manifest.software) == 1
    assert manifest.software[0].name == "Mozilla Firefox"


def test_a_single_source_arriving_as_a_string() -> None:
    manifest = Manifest.from_dict(
        {"hopfile_version": 1, "software": [{"name": "Thing", "sources": "registry"}]}
    )
    assert manifest.software[0].sources == ("registry",)


def test_collapsed_arrays_elsewhere_in_the_file() -> None:
    raw = {
        "hopfile_version": 1,
        "system": {"keyboard_layouts": "00000409", "gpus": {"vendor": "AMD"}},
        "disks": {"model": "one disk"},
        "warnings": "a single warning",
        "gaming": {"steam": {"games": {"appid": 730, "name": "Counter-Strike 2"}}},
        "payload": {"entries": {"kind": "ssh", "path": "ssh/id_ed25519"}},
    }
    manifest = Manifest.from_dict(raw)
    assert manifest.keyboard_layouts == ["00000409"]
    assert manifest.gpu_vendors == ["amd"]
    assert len(manifest.disks) == 1
    assert manifest.warnings == ["a single warning"]
    assert len(manifest.steam_games) == 1
    assert len(manifest.payload_entries) == 1


def test_entries_without_a_name_are_dropped() -> None:
    manifest = Manifest.from_dict(
        {"hopfile_version": 1, "software": [{"version": "1.0"}, {"name": "Real"}, "not a dict"]}
    )
    assert [e.name for e in manifest.software] == ["Real"]


# --- defaults --------------------------------------------------------------


def test_every_property_survives_an_almost_empty_hopfile() -> None:
    """A scan that read almost nothing still has to plan, not crash."""
    manifest = Manifest.from_dict({"hopfile_version": 1})
    for name in _property_names():
        getattr(manifest, name)  # must not raise


def test_documented_defaults() -> None:
    manifest = Manifest.from_dict({"hopfile_version": 1})
    for name, expected in MINIMAL_DEFAULTS.items():
        assert getattr(manifest, name) == expected, name


def test_the_defaults_table_covers_every_property() -> None:
    """A new property with no documented default is a hole in the test above."""
    assert sorted(MINIMAL_DEFAULTS) == sorted(_property_names())


def test_null_valued_sections_behave_like_absent_ones() -> None:
    """The scanner writes null for anything it could not read."""
    manifest = Manifest.from_dict(
        {"hopfile_version": 1, "system": None, "user": None, "disks": None, "payload": None}
    )
    assert manifest.hostname == "unknown-host"
    assert manifest.user_folders == []
    assert manifest.payload_entries == []


def test_dev_and_system_accessors() -> None:
    manifest = Manifest.from_dict(
        {"hopfile_version": 1, "dev": {"wsl": {"present": True}}, "system": {"memory_gb": 16}}
    )
    assert manifest.dev("wsl", "present") is True
    assert manifest.dev("wsl", "missing", default="fallback") == "fallback"
    assert manifest.system("memory_gb") == 16
    assert manifest.system("nothing", default=0) == 0


# --- SoftwareEntry ---------------------------------------------------------


def test_executables_are_guessed_from_the_install_location() -> None:
    backslashes = SoftwareEntry(name="VLC", install_location="C:\\Program Files\\VideoLAN\\VLC")
    forward = SoftwareEntry(name="VLC", install_location="C:/Program Files/VideoLAN/VLC")
    trailing = SoftwareEntry(name="VLC", install_location="C:\\Program Files\\VideoLAN\\VLC\\")
    assert backslashes.executables == ("vlc.exe",)
    assert forward.executables == ("vlc.exe",)
    assert trailing.executables == ("vlc.exe",)


def test_no_install_location_means_no_guess() -> None:
    assert SoftwareEntry(name="Thing").executables == ()
    assert SoftwareEntry(name="Thing", install_location="").executables == ()


def test_size_bytes_survives_junk() -> None:
    assert SoftwareEntry.from_dict({"name": "a", "size_bytes": "12"}).size_bytes == 12
    assert SoftwareEntry.from_dict({"name": "a", "size_bytes": "abc"}).size_bytes == 0
    assert SoftwareEntry.from_dict({"name": "a", "size_bytes": None}).size_bytes == 0
    assert SoftwareEntry.from_dict({"name": "a"}).size_bytes == 0


def test_key_folds_case_and_whitespace() -> None:
    a = SoftwareEntry(name="  Discord ", publisher="Discord Inc.")
    b = SoftwareEntry(name="discord", publisher="discord inc.")
    assert a.key == b.key


# --- user folders ----------------------------------------------------------


def test_user_folders_are_sorted_biggest_first_and_survive_junk() -> None:
    manifest = Manifest.from_dict(
        {
            "hopfile_version": 1,
            "user": {
                "folders": {
                    "Documents": {"size_bytes": 100, "files": 5},
                    "Videos": {"size_bytes": 900, "files": "many"},
                    "Music": {"size_bytes": ""},
                    "Pictures": {"size_bytes": None},
                    "Downloads": {"size_bytes": "abc", "files": 1},
                    "Broken": "not a record",
                }
            },
        }
    )
    folders = manifest.user_folders
    assert [f["name"] for f in folders] == [
        "Videos",
        "Documents",
        "Downloads",
        "Music",
        "Pictures",
    ]
    assert [f["size_bytes"] for f in folders] == [900, 100, 0, 0, 0]
    assert folders[0]["files"] == 0  # "many" is not a number and is not guessed at
    assert manifest.user_data_bytes == 1000


def test_onedrive_is_none_unless_it_is_present() -> None:
    absent = Manifest.from_dict({"hopfile_version": 1, "user": {"onedrive": {"present": False}}})
    present = Manifest.from_dict({"hopfile_version": 1, "user": {"onedrive": {"present": True}}})
    assert absent.onedrive is None
    assert present.onedrive is not None


# --- lint ------------------------------------------------------------------


def test_lint_reports_the_bitlocker_volume(example_manifest: Manifest) -> None:
    """The one warning that can cost somebody every file they own."""
    problems = example_manifest.lint()
    bitlocker = [p for p in problems if "BitLocker" in p]
    assert len(bitlocker) == 1
    assert "recovery key" in bitlocker[0]
    assert "C" in bitlocker[0]


def test_lint_is_quiet_about_a_complete_hopfile(example_manifest: Manifest) -> None:
    """Only BitLocker: the example is otherwise a fully scanned machine."""
    problems = [p for p in example_manifest.lint() if "BitLocker" not in p]
    assert problems == []


def test_lint_names_what_an_empty_hopfile_is_missing() -> None:
    problems = Manifest.from_dict({"hopfile_version": 1}).lint()
    joined = " ".join(problems)
    assert "software" in joined
    assert "timezone" in joined
    assert "locale" in joined
    assert "disks" in joined


def test_lint_notices_a_payload_index_that_points_nowhere() -> None:
    manifest = Manifest.from_dict({"hopfile_version": 1, "payload_dir": "hop-payload"})
    assert any("payload" in p for p in manifest.lint())


# --- human_bytes -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (None, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024**2, "1.0 MB"),
        (1024**3, "1.0 GB"),
        (1024**4, "1.0 TB"),
        (5 * 1024**4, "5.0 TB"),
    ],
)
def test_human_bytes(value: int | None, expected: str) -> None:
    assert human_bytes(value) == expected
