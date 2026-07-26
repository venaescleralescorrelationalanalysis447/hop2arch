"""The archinstall answer file and the post-install script.

One test in here matters more than the rest: ``disk_config`` is absent unless
somebody explicitly asks for it. hop knows what disks the *old* machine had,
which is not the same as knowing what is in the machine being installed on
today, and an unattended partitioning plan built from a stale snapshot is the
only mistake this project could make that cannot be undone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hop.archinstall import build_post_script, build_user_configuration, write_config
from hop.manifest import Manifest
from hop.mapping import Database
from hop.plan import Plan, Planner

BASH = shutil.which("bash")


def walk(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every (dotted key path, value) pair in a nested document."""
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            out.append((here, value))
            out += walk(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out += walk(value, f"{path}[{index}]")
    return out


def replan(example_manifest: Manifest, db: Database, **changes: Any) -> Plan:
    for key, value in changes.items():
        example_manifest.raw["system"][key] = value
    return Planner(example_manifest, db).build()


# --- the configuration -----------------------------------------------------


def test_the_config_is_json_and_carries_the_answers(example_plan: Plan) -> None:
    config = build_user_configuration(example_plan)
    text = json.dumps(config)  # must not raise: archinstall reads a file, not a dict
    assert json.loads(text) == config

    assert config["hostname"] == "nb-artem"
    assert config["timezone"] == "Europe/Moscow"
    assert config["locale_config"] == {"kb_layout": "us", "sys_enc": "UTF-8", "sys_lang": "ru_RU"}
    assert config["profile_config"]["profile"]["details"] == ["KDE Plasma"]
    assert config["profile_config"]["greeter"] == "sddm"
    assert config["users"][0]["username"] == "artem"
    assert config["users"][0]["sudo"] is True


def test_uefi_gets_systemd_boot_and_bios_gets_grub(
    example_manifest: Manifest, db: Database
) -> None:
    """systemd-boot does not exist on a legacy BIOS machine, and the config has
    to agree with the warning the plan already prints."""
    uefi = build_user_configuration(replan(example_manifest, db, firmware="UEFI"))
    assert uefi["bootloader"] == "Systemd-boot"

    bios = build_user_configuration(replan(example_manifest, db, firmware="BIOS"))
    assert bios["bootloader"] == "Grub"
    assert any("GRUB" in note for note in bios["_hop"]["notes"])


def test_no_password_anywhere(example_plan: Plan) -> None:
    """archinstall asks for the password itself, in the installer, where you can
    see what you are typing. hop does not store one, generate one, or leave a
    placeholder that looks like one."""
    config = build_user_configuration(example_plan)
    for path, value in walk(config):
        key = path.rsplit(".", 1)[-1].lower()
        assert "password" not in key, path
        assert "passwd" not in key, path
        if isinstance(value, str):
            assert not value.startswith("$6$"), path  # a crypt hash slipped in
    assert set(config["users"][0]) == {"username", "sudo", "groups"}


def test_disk_config_is_absent_by_default(example_plan: Plan) -> None:
    """This test exists because this is the one thing hop must never get wrong.

    With no disk_config key, archinstall stops and asks which disk to install
    to, with the real disk list on the real screen. A hopfile is a snapshot of a
    different machine taken possibly weeks earlier; between then and now a drive
    can be added and /dev/nvme0n1 can mean something else entirely. If this test
    ever fails, do not fix the test.
    """
    config = build_user_configuration(example_plan)
    assert "disk_config" not in config
    assert not any("disk" in path for path, _ in walk(config) if path != "_hop.notes")
    assert any("archinstall will ask you which disk" in note for note in config["_hop"]["notes"])


def test_an_explicit_disk_config_is_still_empty(example_plan: Plan) -> None:
    """The flag documents the shape of the key and does nothing else. As written
    the installer has nowhere to install and stops, which is intended."""
    config = build_user_configuration(example_plan, disk_config=True)
    assert config["disk_config"]["device_modifications"] == []
    assert config["disk_config"]["config_type"] == "manual_partitioning"
    assert "by hand" in config["disk_config"]["_comment"]


def test_multilib_is_enabled_for_a_machine_that_plays_games(example_plan: Plan) -> None:
    """Steam and the 32-bit halves of the driver stacks are multilib-only, and
    multilib has to be on before the install rather than after."""
    assert example_plan.target["gaming"] is True
    assert build_user_configuration(example_plan)["additional-repositories"] == ["multilib"]


def test_the_profile_packages_are_not_asked_for_twice(example_plan: Plan) -> None:
    """archinstall's Plasma profile installs plasma-meta and sddm itself."""
    config = build_user_configuration(example_plan)
    assert "plasma-meta" not in config["packages"]
    assert "sddm" not in config["packages"]
    assert "konsole" in config["packages"]
    assert set(config["packages"]) < set(example_plan.packages["pacman"])


def test_a_desktopless_plan_gets_a_minimal_profile(example_manifest: Manifest, db: Database) -> None:
    plan = Planner(example_manifest, db, desktop="none").build()
    config = build_user_configuration(plan)
    assert config["profile_config"]["profile"]["main"] == "Minimal"
    assert "greeter" not in config["profile_config"]


def test_the_graphics_choice_explains_itself(example_plan: Plan) -> None:
    config = build_user_configuration(example_plan)
    assert config["profile_config"]["gfx_driver"] == "Nvidia (proprietary)"
    assert any("NVIDIA" in note and "integrated" in note for note in config["_hop"]["notes"])


# --- the post-install script ----------------------------------------------


def test_the_script_refuses_to_run_as_root(example_plan: Plan) -> None:
    script = build_post_script(example_plan)
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    assert 'if [[ "$(id -u)" -eq 0 ]]; then' in script
    assert "makepkg refuses to build packages as root" in script


def test_the_script_says_who_installed_the_repository_packages(example_plan: Plan) -> None:
    """It installs the AUR and the flatpaks. Claiming the other 83 packages
    would leave anyone who installed Arch by hand believing they were done."""
    script = build_post_script(example_plan)
    assert "The repository packages came from archinstall, before this script ran." in script
    assert "hop land hop-plan.json --only packages" in script
    assert "hop land hop-plan.json --only payload" in script


def test_the_script_does_not_send_the_reader_round_the_loop_again(example_plan: Plan) -> None:
    """A full 'hop land' would walk packages, services and locale a second time."""
    script = build_post_script(example_plan)
    for line in script.splitlines():
        if "hop land hop-plan.json" in line and "--only" not in line:
            pytest.fail(f"hop-post.sh asks for a full landing: {line.strip()}")


def test_the_script_is_safe_to_run_twice(example_plan: Plan) -> None:
    script = build_post_script(example_plan)
    assert "--needed" in script  # pacman leaves what is already installed alone
    assert "grep -qxF" in script  # a locale is added to locale.gen only once
    assert "command -v paru" in script  # the helper is built only if it is missing


def test_the_flatpak_section_adds_flathub_idempotently(
    example_manifest: Manifest, db: Database
) -> None:
    plan = Planner(example_manifest, db, prefer_flatpak=True).build()
    assert plan.packages["flatpak"]
    script = build_post_script(plan)
    assert "remote-add --if-not-exists flathub" in script
    assert "flatpak install -y flathub" in script


def test_the_script_refuses_a_name_it_cannot_quote(example_plan: Plan) -> None:
    """Everything here came out of a JSON file written on another machine."""
    example_plan.packages["aur"] = ["innocent", "; rm -rf ~"]
    with pytest.raises(ValueError, match="not a usable package name"):
        build_post_script(example_plan)


def test_a_machine_named_in_another_alphabet_still_produces_a_script(
    example_manifest: Manifest, db: Database
) -> None:
    """Windows takes a Cyrillic computer name. Arch does not, and neither does
    bash — so the sanitising has to happen in the plan, not as a refusal here.
    """
    example_manifest.raw["system"]["hostname"] = "Ноутбук-Артёма"
    example_manifest.raw["user"]["name"] = "Артём"
    plan = Planner(example_manifest, db).build()
    script = build_post_script(plan)
    assert plan.system["hostname"].isascii()
    assert plan.system["username"].isascii()
    assert "Ноутбук" not in script
    assert "Артём" not in script


@pytest.mark.skipif(BASH is None, reason="no bash on PATH to check the syntax with")
def test_bash_accepts_the_generated_script(example_plan: Plan, tmp_path: Path) -> None:
    """bash -n parses without executing. Nothing in the script is ever run here."""
    config_path, script_path = write_config(example_plan, tmp_path)
    assert config_path.name == "user_configuration.json"
    done = subprocess.run(
        [str(BASH), "-n", script_path.as_posix()], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_write_config_writes_unix_line_endings(example_plan: Plan, tmp_path: Path) -> None:
    """The script is generated on the Windows machine being left behind, and
    bash will not read a script with carriage returns in it."""
    config_path, script_path = write_config(example_plan, tmp_path / "out")
    assert b"\r\n" not in script_path.read_bytes()
    assert b"\r\n" not in config_path.read_bytes()
    assert json.loads(config_path.read_text(encoding="utf-8"))["hostname"] == "nb-artem"
