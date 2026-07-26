"""Choosing the disk to erase, and refusing to erase the wrong one.

Nothing here partitions anything, and no disk on the machine running the suite
is enumerated. The drive list is canned ``lsblk`` JSON fed through the injected
runner, archinstall is an injected ``execute`` that records the argv it was
handed and installs nothing, the firmware answer is a directory in ``tmp_path``
that either exists or does not, and the system being installed is another
directory in ``tmp_path``. The confirmation is answered by a scripted ``ask``.

The two tests that matter most are
``test_typing_the_device_path_is_what_starts_the_install`` and
``test_anything_other_than_the_device_path_stops_before_archinstall``: the first
proves the layout handed to archinstall was computed from the disk that is
actually here, and the second proves that a yes typed by reflex does not reach
it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest

from hop import install, usb
from hop.install import InstallError, InstallOptions
from hop.plan import Plan

TARGET_ID = "/dev/nvme0n1"
MEDIUM_ID = "/dev/sda"
TARGET_BYTES = 1_000_204_886_016


def windows_disk(**overrides: object) -> dict:
    """The disk Windows is on, as lsblk describes it: ESP, MSR, C:, recovery.

    The recovery partition is labelled in Russian because that is what the
    machines this was written for answer with, and a label the reader
    recognises is the thing that stops them typing the wrong path.
    """
    node: dict = {
        "name": "nvme0n1",
        "path": TARGET_ID,
        "model": "Samsung SSD 980 1TB",
        "serial": "S5GXNF0R",
        "size": TARGET_BYTES,
        "tran": "nvme",
        "rm": False,
        "mountpoint": None,
        "children": [
            {"name": "nvme0n1p1", "path": f"{TARGET_ID}p1", "size": 104_857_600,
             "label": "SYSTEM", "fstype": "vfat", "mountpoint": None},
            {"name": "nvme0n1p2", "path": f"{TARGET_ID}p2", "size": 16_777_216,
             "label": None, "fstype": None, "mountpoint": None},
            {"name": "nvme0n1p3", "path": f"{TARGET_ID}p3", "size": 998_000_000_000,
             "label": "Windows", "fstype": "ntfs", "mountpoint": None},
            {"name": "nvme0n1p4", "path": f"{TARGET_ID}p4", "size": 1_000_000_000,
             "label": "Восстановление",
             "fstype": "ntfs", "mountpoint": None},
        ],
    }
    node.update(overrides)
    return node


def live_medium(**overrides: object) -> dict:
    """The stick hop is running from, mounted where archiso mounts it."""
    node: dict = {
        "name": "sda",
        "path": MEDIUM_ID,
        "model": "SanDisk Ultra USB 3.0",
        "serial": "4C530001",
        "size": 30_765_219_840,
        "tran": "usb",
        "rm": True,
        "mountpoint": None,
        "children": [
            {"name": "sda1", "path": f"{MEDIUM_ID}1", "size": 30_700_000_000,
             "label": "ARCH_202607", "fstype": "vfat", "mountpoint": "/run/archiso/bootmnt"},
        ],
    }
    node.update(overrides)
    return node


def listing(*nodes: dict) -> dict:
    return {"blockdevices": list(nodes) or [windows_disk(), live_medium()]}


class FakeRunner:
    """Canned answers, matched by substring, and a record of every call.

    The same shape as ``hop.usb.Runner``, because that is what ``hop install``
    hands to ``usb.drives``.
    """

    def __init__(self, answers: Sequence[tuple[str, object]] = ()) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        self.calls.append(list(argv))
        for needle, answer in self.answers:
            if needle in command:
                if isinstance(answer, tuple):
                    return answer
                if isinstance(answer, (dict, list)):
                    return (0, json.dumps(answer), "")
                return (0, str(answer), "")
        return (0, "", "")

    @property
    def commands(self) -> str:
        return "\n".join(" ".join(call) for call in self.calls)


def runner_for(payload: dict | None = None, **extra: object) -> FakeRunner:
    answers: list[tuple[str, object]] = [("lsblk", payload if payload is not None else listing())]
    answers += [(needle, answer) for needle, answer in extra.items()]
    return FakeRunner(answers)


def drives_from(payload: dict) -> list[usb.Drive]:
    """The drive list as hop install sees it, parsed by the real usb.py."""
    return usb.drives(runner=runner_for(payload), platform="linux")


def drive(device_id: str, payload: dict | None = None) -> usb.Drive:
    found = drives_from(payload if payload is not None else listing())
    return next(item for item in found if item.device_id == device_id)


def flat(text: str) -> str:
    """The transcript with its line breaks taken out.

    Everything hop prints is wrapped to 74 columns, so a sentence worth
    asserting on is usually split across two lines.
    """
    return " ".join(text.split())


# --- which disk ------------------------------------------------------------


def test_one_windows_disk_is_the_target_and_nobody_is_shown_a_menu() -> None:
    chosen = install.choose_target(drives_from(listing()))
    assert chosen.device_id == TARGET_ID


def test_two_windows_disks_are_named_rather_than_chosen_between() -> None:
    second = windows_disk(name="sdb", path="/dev/sdb", model="WDC WD20EZRZ", serial="WD-WCC4M0")
    with pytest.raises(InstallError) as caught:
        install.choose_target(drives_from(listing(windows_disk(), second, live_medium())))
    message = str(caught.value)
    assert TARGET_ID in message and "/dev/sdb" in message
    assert "--target" in message, "the way out is printed, not left to be guessed"


def test_no_windows_disk_anywhere_is_a_refusal_that_lists_what_is_there() -> None:
    blank = windows_disk(children=[
        {"name": "nvme0n1p1", "path": f"{TARGET_ID}p1", "size": TARGET_BYTES,
         "label": "scratch", "fstype": "ext4", "mountpoint": None},
    ])
    with pytest.raises(InstallError) as caught:
        install.choose_target(drives_from(listing(blank, live_medium())))
    message = str(caught.value)
    assert "cannot tell which of these disks held Windows" in message
    assert TARGET_ID in message
    assert f"--target {TARGET_ID}" in message


def test_no_disk_worth_installing_onto_says_so_rather_than_offering_the_stick() -> None:
    with pytest.raises(InstallError) as caught:
        install.choose_target(drives_from(listing(live_medium())))
    assert "could install onto" in str(caught.value)
    assert "running from" in str(caught.value)


def test_the_medium_hop_booted_from_is_no_candidate_however_large_it_is() -> None:
    """A 2 TB USB disk with an archiso on it is still the disk being read from."""
    big = live_medium(size=2_000_398_934_016, model="Samsung T7", children=[
        {"name": "sda1", "path": f"{MEDIUM_ID}1", "size": 30_700_000_000, "label": "ARCH_202607",
         "fstype": "vfat", "mountpoint": "/run/archiso/bootmnt"},
        {"name": "sda2", "path": f"{MEDIUM_ID}2", "size": 1_900_000_000_000, "label": "Backup",
         "fstype": "ntfs", "mountpoint": None},
    ])
    found = drives_from(listing(windows_disk(), big))

    assert [item.device_id for item in install.targets(found)] == [TARGET_ID]
    assert [item.device_id for item in install.windows_disks(found)] == [TARGET_ID]
    assert install.choose_target(found).device_id == TARGET_ID


def test_a_target_that_is_not_in_this_machine_is_rejected() -> None:
    with pytest.raises(InstallError) as caught:
        install.choose_target(drives_from(listing()), hint="/dev/nvme9n1")
    message = str(caught.value)
    assert "/dev/nvme9n1" in message
    assert TARGET_ID in message, "what is actually here is printed beside what was asked for"


def test_the_disk_hop_is_running_from_is_refused_even_when_it_is_named() -> None:
    with pytest.raises(InstallError) as caught:
        install.choose_target(drives_from(listing()), hint=MEDIUM_ID)
    assert "running from" in str(caught.value)


def test_a_disk_too_small_to_hold_arch_is_not_installed_onto() -> None:
    small = windows_disk(name="sdc", path="/dev/sdc", size=8 * 1024**3, children=[
        {"name": "sdc1", "path": "/dev/sdc1", "size": 8 * 1024**3, "label": "OLD",
         "fstype": "ntfs", "mountpoint": None},
    ])
    found = drives_from(listing(small, live_medium()))
    assert install.targets(found) == []
    with pytest.raises(InstallError) as caught:
        install.choose_target(found, hint="/dev/sdc")
    assert "at least" in str(caught.value)


def test_a_disk_mounted_through_luks_and_lvm_is_not_offered() -> None:
    """Somebody in the live environment with their old disk open, copying a file.

    The mount point that says the disk is in use is three levels below the disk
    in lsblk's tree: partition, LUKS container, logical volume. Reading only the
    partitions reported no mount point at all, so hop offered to erase a
    filesystem somebody had open, having printed "what is on it now, all of
    which is lost" over a list that did not mention it.
    """
    open_disk = windows_disk(name="sdb", path="/dev/sdb", model="Crucial MX500", children=[
        {"name": "sdb1", "path": "/dev/sdb1", "size": 536_870_912, "fstype": "vfat",
         "mountpoints": [None]},
        {"name": "sdb2", "path": "/dev/sdb2", "size": 998_000_000_000, "fstype": "crypto_LUKS",
         "mountpoints": [None], "children": [
             {"name": "dm-0", "path": "/dev/mapper/old", "size": 998_000_000_000,
              "fstype": "ext4", "label": "old", "mountpoints": ["/mnt/old"]},
         ]},
    ])
    found = drives_from(listing(windows_disk(), open_disk, live_medium()))
    opened = next(item for item in found if item.device_id == "/dev/sdb")

    assert opened.mounted == ("/mnt/old",)
    assert "mounted at /mnt/old" in (install.target_refusal(opened) or "")
    assert [item.device_id for item in install.targets(found)] == [TARGET_ID]
    with pytest.raises(InstallError) as caught:
        install.choose_target(found, hint="/dev/sdb")
    assert "mounted at /mnt/old" in str(caught.value)


def test_a_bitlockered_windows_disk_is_still_recognised() -> None:
    """lsblk reports an encrypted C: as BitLocker, not as NTFS."""
    locked = windows_disk(children=[
        {"name": "nvme0n1p1", "path": f"{TARGET_ID}p1", "size": 104_857_600, "label": "SYSTEM",
         "fstype": "vfat", "mountpoint": None},
        {"name": "nvme0n1p3", "path": f"{TARGET_ID}p3", "size": 998_000_000_000, "label": None,
         "fstype": "BitLocker", "mountpoint": None},
    ])
    found = drives_from(listing(locked, live_medium()))
    assert [item.device_id for item in install.windows_disks(found)] == [TARGET_ID]


def test_the_description_names_every_partition_not_just_the_first_three() -> None:
    block = install.describe_target(drive(TARGET_ID))
    assert TARGET_ID in block
    assert "Samsung SSD 980 1TB" in block
    assert "931.5 GB" in block
    assert "serial S5GXNF0R" in block
    for label in ("SYSTEM", "Windows", "Восстановление"):
        assert label in block
    assert "all of which is lost" in block


def test_a_disk_that_is_only_being_listed_is_not_described_as_lost() -> None:
    """The listing of what is present is not a list of what is being erased."""
    listed = install.describe_target(drive(MEDIUM_ID), doomed=False)
    assert "what is on it now:" in listed
    assert "lost" not in listed


# --- the layout ------------------------------------------------------------


def test_the_layout_covers_the_whole_disk_and_puts_the_esp_first() -> None:
    config = install.build_disk_config(drive(TARGET_ID), firmware="UEFI")
    modification = config["device_modifications"][0]

    assert config["config_type"] == "manual_partitioning"
    assert modification["device"] == TARGET_ID
    assert modification["wipe"] is True

    efi, root = modification["partitions"]
    assert efi["fs_type"] == "fat32"
    assert efi["mountpoint"] == "/boot"
    assert {flag.lower() for flag in efi["flags"]} == {"boot", "esp"}
    assert efi["start"]["value"] == install.ALIGNMENT
    assert efi["size"]["value"] == install.EFI_BYTES

    assert root["fs_type"] == "ext4"
    assert root["mountpoint"] == "/"
    assert root["start"]["value"] == install.ALIGNMENT + install.EFI_BYTES
    end = root["start"]["value"] + root["size"]["value"]
    assert TARGET_BYTES - end == install.ALIGNMENT, "everything but the backup GPT header"


def test_the_layout_is_arithmetic_over_the_size_this_machine_just_reported() -> None:
    """The whole argument for generating one here: it cannot be stale."""
    smaller = windows_disk(size=512_110_190_592)
    first = install.build_disk_config(drive(TARGET_ID), firmware="UEFI")
    second = install.build_disk_config(
        drive(TARGET_ID, listing(smaller, live_medium())), firmware="UEFI"
    )
    root = first["device_modifications"][0]["partitions"][1]["size"]["value"]
    other = second["device_modifications"][0]["partitions"][1]["size"]["value"]
    assert root - other == TARGET_BYTES - 512_110_190_592


@pytest.mark.parametrize("firmware", ["BIOS", "unknown", ""])
def test_a_machine_that_did_not_boot_through_uefi_gets_no_layout(firmware: str) -> None:
    with pytest.raises(InstallError) as caught:
        install.build_disk_config(drive(TARGET_ID), firmware=firmware)
    message = str(caught.value)
    assert "GPT" in message and "systemd-boot" in message
    assert "has not been touched" in message
    assert "CSM" in message, "the usual cause is named, because it is usually the cause"


def test_a_disk_too_small_is_refused_by_the_layout_as_well() -> None:
    small = usb.Drive("/dev/sdc", 0, "tiny", "T1", 8 * 1024**3, "USB", True, False)
    with pytest.raises(InstallError) as caught:
        install.build_disk_config(small, firmware="UEFI")
    assert "at least" in str(caught.value)


def test_the_configuration_stops_saying_archinstall_will_ask(example_plan: Plan) -> None:
    config = install.build_configuration(example_plan, drive(TARGET_ID), firmware="UEFI")
    notes = config["_hop"]["notes"]
    assert not any(str(note).startswith("No disk_config key") for note in notes)
    assert TARGET_ID in notes[0]
    assert "wiped and repartitioned" in notes[0]
    # Everything hop/archinstall.py decides is left as it decided it.
    assert config["bootloader"] == "Systemd-boot"
    assert config["disk_config"]["device_modifications"][0]["device"] == TARGET_ID
    assert all("password" not in key for user in config["users"] for key in user)


# --- the run ---------------------------------------------------------------


@pytest.fixture
def medium(tmp_path: Path, example_plan: Plan) -> Path:
    """The hop directory as hop go leaves it on the stick."""
    root = tmp_path / "bootmnt" / usb.HOP_DIR
    (root / "payload" / "ssh").mkdir(parents=True)
    (root / "payload" / "ssh" / "id_ed25519.pub").write_text("key\n", encoding="utf-8")
    (root / "hop" / "data").mkdir(parents=True)
    (root / "hop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "hop" / "data" / "packages.toml").write_text("", encoding="utf-8")
    (root / "archinstall").mkdir(parents=True)
    (root / "archinstall" / "hop-post.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "hop-plan.json").write_text(
        json.dumps(example_plan.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    (root / "hop-report.md").write_text("# report\n", encoding="utf-8")
    return root


class Execution:
    """Every command that would have installed something, and none of them run."""

    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.argv: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.argv.append(list(argv))
        return self.code


def options(tmp_path: Path, medium: Path, **overrides: object) -> InstallOptions:
    settings: dict = {"plan": medium / "hop-plan.json", "out_dir": tmp_path / "out"}
    settings.update(overrides)
    return InstallOptions(**settings)


def install_run(
    opts: InstallOptions,
    *,
    runner: FakeRunner | None = None,
    execute: Execution | None = None,
    answers: Sequence[str] = (TARGET_ID,),
    firmware: str = "UEFI",
    target_root: Path | None = None,
    tmp_path: Path,
) -> tuple[int, str, list[str]]:
    """Run it with scripted answers. Returns (code, transcript, questions asked)."""
    out = StringIO()
    asked: list[str] = []
    replies = list(answers)

    def ask(question: str) -> str:
        asked.append(question)
        return replies.pop(0) if replies else ""

    efi = tmp_path / "sys" / "firmware" / "efi"
    if firmware == "UEFI":
        efi.mkdir(parents=True, exist_ok=True)

    code = install.run(
        opts,
        out=out,
        ask=ask,
        runner=runner if runner is not None else runner_for(),
        execute=execute if execute is not None else Execution(),
        platform="linux",
        efi_runtime=efi,
        target_root=target_root if target_root is not None else tmp_path / "mnt",
    )
    return (code, out.getvalue(), asked)


def new_system(tmp_path: Path, plan: Plan) -> tuple[Path, str]:
    """The mounted system archinstall would have left behind, and its user."""
    root = tmp_path / "mnt"
    user = str(plan.system["username"])
    (root / "home" / user).mkdir(parents=True)
    (root / "root").mkdir(parents=True)
    return (root, user)


def test_typing_the_device_path_is_what_starts_the_install(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    root, user = new_system(tmp_path, example_plan)
    runner = runner_for()
    execute = Execution()
    code, transcript, asked = install_run(
        options(tmp_path, medium), runner=runner, execute=execute,
        target_root=root, tmp_path=tmp_path,
    )

    assert code == 0, transcript
    assert asked == [f"Type {TARGET_ID} to erase it. Anything else stops here: "]

    # archinstall was handed a configuration file, and only that.
    config_path = tmp_path / "out" / "user_configuration.json"
    assert execute.argv == [["archinstall", "--config", str(config_path)]]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    modification = config["disk_config"]["device_modifications"][0]
    assert modification["device"] == TARGET_ID
    assert modification["wipe"] is True
    assert (tmp_path / "out" / "hop-post.sh").is_file()

    # The plan travelled into the system that was just installed.
    landing = root / "home" / user / "hop"
    assert json.loads((landing / "hop-plan.json").read_text(encoding="utf-8"))["plan_version"] == 1
    assert (landing / "hop-report.md").is_file()
    assert (landing / "payload" / "ssh" / "id_ed25519.pub").is_file()
    assert (landing / "hop" / "data" / "packages.toml").is_file()
    assert (landing / "hop-post.sh").is_file()

    # Paths in the system being installed are spelled the way that system
    # spells them, whatever machine hop is running on.
    assert "~/hop" in transcript and "~\\hop" not in transcript

    # And the account that will read them owns them.
    assert ["arch-chroot", str(root), "chown", "-R", f"{user}:{user}", f"/home/{user}/hop"] in runner.calls


def test_anything_other_than_the_device_path_stops_before_archinstall(
    tmp_path: Path, medium: Path
) -> None:
    for answer in ("yes", "y", "", "nvme0n1", "/dev/nvme0n2", "да"):
        execute = Execution()
        code, transcript, _ = install_run(
            options(tmp_path, medium), execute=execute, answers=[answer], tmp_path=tmp_path
        )
        assert code == 1, answer
        assert execute.argv == [], f"{answer!r} reached archinstall"
        assert "No disk has been touched" in flat(transcript)
        assert not (tmp_path / "out" / "user_configuration.json").exists()


def test_the_path_typed_with_spaces_around_it_is_still_the_path(
    tmp_path: Path, medium: Path
) -> None:
    execute = Execution()
    code, _, _ = install_run(
        options(tmp_path, medium), execute=execute, answers=[f"  {TARGET_ID}  "], tmp_path=tmp_path
    )
    assert code == 0
    assert execute.argv


def test_the_question_asks_for_the_path_and_shows_what_is_on_the_disk(
    tmp_path: Path, medium: Path
) -> None:
    _, transcript, asked = install_run(
        options(tmp_path, medium), answers=["no"], tmp_path=tmp_path
    )
    assert TARGET_ID in asked[0]
    assert "yes" not in asked[0].lower(), "a yes is what gets typed by reflex"

    for fact in ("Samsung SSD 980 1TB", "931.5 GB", "SYSTEM", "Windows", "Восстановление"):
        assert fact in transcript
    assert "There is no undo" in flat(transcript)


def test_a_closed_stdin_is_not_a_device_path(tmp_path: Path, medium: Path) -> None:
    out = StringIO()
    execute = Execution()
    efi = tmp_path / "sys" / "firmware" / "efi"
    efi.mkdir(parents=True)

    def ask(_question: str) -> str:
        raise EOFError

    code = install.run(
        options(tmp_path, medium), out=out, ask=ask, runner=runner_for(), execute=execute,
        platform="linux", efi_runtime=efi, target_root=tmp_path / "mnt",
    )
    assert code == 1
    assert execute.argv == []
    assert "will not take silence" in flat(out.getvalue())


def test_assume_yes_says_in_the_transcript_that_nobody_was_asked(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    root, _ = new_system(tmp_path, example_plan)
    execute = Execution()
    code, transcript, asked = install_run(
        options(tmp_path, medium, assume_yes=True), execute=execute, answers=(),
        target_root=root, tmp_path=tmp_path,
    )
    assert code == 0
    assert asked == [], "assume_yes does not ask a question and throw the answer away"
    assert execute.argv
    assert "Nobody typed that device path" in flat(transcript)


def test_a_dry_run_writes_a_configuration_and_starts_nothing(
    tmp_path: Path, medium: Path
) -> None:
    execute = Execution()
    code, transcript, _ = install_run(
        options(tmp_path, medium, dry_run=True), execute=execute, tmp_path=tmp_path
    )
    assert code == 0
    assert execute.argv == []
    assert (tmp_path / "out" / "user_configuration.json").is_file()
    assert "no disk has been touched" in flat(transcript).lower()


def test_a_bios_machine_is_refused_before_a_configuration_exists(
    tmp_path: Path, medium: Path
) -> None:
    execute = Execution()
    code, transcript, asked = install_run(
        options(tmp_path, medium), execute=execute, firmware="BIOS", tmp_path=tmp_path
    )
    assert code == 2
    assert execute.argv == []
    assert asked == [], "nothing refusable is left standing behind the one question"
    assert not (tmp_path / "out" / "user_configuration.json").exists()
    assert "did not boot through UEFI" in flat(transcript)
    assert "No disk has been touched" in flat(transcript), (
        "the ending has to say what happened, and nothing happened"
    )


def test_a_refusal_is_wrapped_like_everything_else_hop_says(
    tmp_path: Path, medium: Path
) -> None:
    """The last thing somebody reads is not the one thing printed as one line."""
    _, transcript, _ = install_run(
        options(tmp_path, medium), firmware="BIOS", tmp_path=tmp_path
    )
    ending = transcript[transcript.index("--- stopped") :]
    too_long = [line for line in ending.splitlines() if len(line) > 78]
    assert too_long == [], too_long


def test_the_firmware_answer_comes_from_this_machine_not_from_the_plan(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """The plan says UEFI. This machine booted otherwise, and this machine wins."""
    assert example_plan.system["firmware"] == "UEFI"
    code, transcript, _ = install_run(
        options(tmp_path, medium), firmware="BIOS", tmp_path=tmp_path
    )
    assert code == 2
    assert "firmware      BIOS" in transcript


def test_an_archinstall_that_fails_says_what_state_the_disk_is_in(
    tmp_path: Path, medium: Path
) -> None:
    code, transcript, _ = install_run(
        options(tmp_path, medium), execute=Execution(code=1), tmp_path=tmp_path
    )
    assert code == 2
    flattened = flat(transcript)
    assert "archinstall exited 1" in flattened
    assert "may have been partly written" in flattened
    assert "archinstall --config" in transcript, "the command to resume is printed"


def test_windows_is_the_wrong_side_of_this_and_says_so(tmp_path: Path, medium: Path) -> None:
    out = StringIO()
    execute = Execution()
    code = install.run(
        options(tmp_path, medium), out=out, ask=lambda _q: TARGET_ID, runner=runner_for(),
        execute=execute, platform="windows", efi_runtime=tmp_path / "none",
        target_root=tmp_path / "mnt",
    )
    assert code == 2
    assert execute.argv == []
    assert "'hop go'" in flat(out.getvalue())


def test_a_plan_that_is_not_there_says_where_it_looked(tmp_path: Path, medium: Path) -> None:
    code, transcript, _ = install_run(
        options(tmp_path, medium, plan=tmp_path / "gone.json"), tmp_path=tmp_path
    )
    assert code == 2
    assert "There is no plan at" in transcript


def test_a_plan_from_a_newer_hop_is_refused_rather_than_half_read(
    tmp_path: Path, medium: Path
) -> None:
    (medium / "hop-plan.json").write_text(json.dumps({"plan_version": 99}), encoding="utf-8")
    code, transcript, _ = install_run(options(tmp_path, medium), tmp_path=tmp_path)
    assert code == 2
    assert "plan version 99" in transcript


# --- the landing -----------------------------------------------------------


def test_the_stick_is_only_said_to_hold_secrets_where_a_payload_travelled(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """The plan describes the machine that was scanned, not this stick.

    A plan listing private files says the scan found them on the old machine.
    Whether any of them are on the stick in the reader's hand is a different
    question, and the sentence about erasing that stick is only worth anything
    where the answer is yes.
    """
    root, _ = new_system(tmp_path, example_plan)
    assert install._private_payload(example_plan), "the example plan lists private files"

    shutil.rmtree(medium / "payload")
    _, transcript, _ = install_run(
        options(tmp_path, medium), target_root=root, tmp_path=tmp_path
    )
    assert "marked private" not in flat(transcript)

    (medium / "payload" / "ssh").mkdir(parents=True)
    (medium / "payload" / "ssh" / "id_ed25519").write_text("k\n", encoding="utf-8")
    _, transcript, _ = install_run(
        options(tmp_path, medium), target_root=root, tmp_path=tmp_path
    )
    assert "marked private" in flat(transcript)
    assert "Erase it once hop land has finished" in flat(transcript)


def test_the_first_login_shows_the_landing_rather_than_performing_it(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """A command that installs packages before anybody typed anything is the
    one thing this program is written not to be."""
    root, user = new_system(tmp_path, example_plan)
    install_run(options(tmp_path, medium), target_root=root, tmp_path=tmp_path)

    profile = (root / "home" / user / ".bash_profile").read_text(encoding="utf-8")
    # The lines that run it, as opposed to the ones that print what to type.
    started = [
        line
        for line in profile.splitlines()
        if "hop land" in line and not line.strip().startswith("echo")
    ]
    assert started, profile
    assert all("--execute" not in line for line in started)
    assert "--execute" in profile, "and the command that does it is printed"
    assert "Delete this block" in profile
    assert "\r" not in profile


def test_the_first_login_block_is_written_once(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    root, user = new_system(tmp_path, example_plan)
    profile = root / "home" / user / ".bash_profile"
    profile.write_text("# mine\nexport EDITOR=vi\n", encoding="utf-8")

    for _ in range(2):
        install_run(options(tmp_path, medium), target_root=root, tmp_path=tmp_path)

    text = profile.read_text(encoding="utf-8")
    assert text.count(install._PROFILE_START) == 1
    assert "export EDITOR=vi" in text, "what was already in the file is still in it"


def test_a_first_login_that_could_not_be_arranged_is_not_described_as_arranged(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """The write can fail, and the ending used to describe it as done anyway.

    Somebody who believes the transcript logs in, sees a plain shell, and
    concludes hop did nothing — the plan and the payload are in front of them
    and the sentence that would say so has been replaced by one that is wrong.
    """
    root, user = new_system(tmp_path, example_plan)
    # A directory where the profile goes: reading it raises, writing it raises.
    (root / "home" / user / ".bash_profile").mkdir()

    code, transcript, _ = install_run(
        options(tmp_path, medium), target_root=root, tmp_path=tmp_path
    )
    flattened = flat(transcript)

    assert code == 0, "the install itself finished"
    assert "The first login runs 'hop land'" not in flattened
    assert "the first login shows what is left to do" not in flattened
    assert "could not write ~/.bash_profile" in flattened
    assert "nothing starts by itself after the first login" in flattened
    assert "python3 -m hop land ~/hop/hop-plan.json --execute" in flattened


def test_the_ending_only_names_a_post_script_that_travelled(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """hop go writes hop-post.sh into archinstall/ on the stick, and may not have.

    A run against a stick without one used to end by telling the reader to run a
    file that is not there, in the paragraph they will follow most literally.
    """
    (medium / "archinstall" / "hop-post.sh").unlink()
    root, _ = new_system(tmp_path, example_plan)
    code, transcript, _ = install_run(
        options(tmp_path, medium), target_root=root, tmp_path=tmp_path
    )
    assert code == 0
    assert "~/hop/hop-post.sh" not in flat(transcript)
    # And nothing anywhere in the transcript is broken across a hyphen, which
    # would make a filename unsearchable and a flag untypeable.
    assert "hop-\n" not in transcript

    # And where it did travel, it is named with the interpreter that runs it:
    # copyfile carries no mode bit onto the new system.
    (medium / "archinstall" / "hop-post.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    _, transcript, _ = install_run(
        options(tmp_path, medium), target_root=root, tmp_path=tmp_path
    )
    assert "bash ~/hop/hop-post.sh" in flat(transcript)


def test_windows_is_only_said_to_be_gone_where_there_was_one(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """--target names a second disk, and Windows stays exactly where it was.

    Telling somebody their Windows is gone when it is still on the other disk is
    how a machine gets wiped a second time by hand to finish a job already done.
    """
    spare = windows_disk(name="sdb", path="/dev/sdb", model="WDC WD10EZEX", serial="WD-1",
                         size=1_000_204_886_016, children=[
        {"name": "sdb1", "path": "/dev/sdb1", "size": 1_000_204_886_016, "label": "scratch",
         "fstype": "ext4", "mountpoint": None},
    ])
    runner = runner_for(listing(windows_disk(), spare, live_medium()))
    root, _ = new_system(tmp_path, example_plan)
    code, transcript, _ = install_run(
        options(tmp_path, medium, target="/dev/sdb"), runner=runner,
        answers=["/dev/sdb"], target_root=root, tmp_path=tmp_path,
    )
    flattened = flat(transcript)

    assert code == 0, transcript
    assert "Arch is installed" in flattened
    assert "Windows is gone" not in flattened


def test_archinstall_that_never_started_is_not_a_half_written_disk(
    tmp_path: Path, medium: Path
) -> None:
    """The ending reads a flag set the instant before the command is launched.

    A command that could not be launched wrote nothing, and "the disk may have
    been partly written" sends the reader looking for a half-installed system on
    a machine whose Windows is still sitting where it was.
    """
    # The production executor turns "the program is not there" into its own
    # exception. subprocess raises before it spawns anything, so the name below
    # is never run — and it could not be, because there is nothing to run.
    with pytest.raises(install._NotStarted) as caught:
        install._stream(["hop-there-is-no-such-program", "--config", "x"])
    assert "could not start" in str(caught.value)
    assert "nothing ran" in str(caught.value)

    def never_starts(argv: Sequence[str]) -> int:
        raise install._NotStarted("could not start archinstall: nothing ran.")

    code, transcript, _ = install_run(
        options(tmp_path, medium), execute=never_starts, tmp_path=tmp_path
    )
    flattened = flat(transcript)

    assert code == 2
    assert "could not start archinstall" in flattened
    assert "may have been partly written" not in flattened
    assert "No disk has been touched" in flattened


def test_a_plan_hop_cannot_turn_into_a_script_is_refused_before_the_question(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """Nothing refusable is allowed to sit behind the one irreversible question.

    hop/archinstall.py checks every value it pastes into hop-post.sh and raises
    rather than escaping. That refusal used to arrive in the configuration
    stage, which is after the device path has been typed.
    """
    raw = example_plan.to_dict()
    raw["system"]["locale"] = "ru_RU.UTF-8; rm -rf /"
    (medium / "hop-plan.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    execute = Execution()
    code, transcript, asked = install_run(
        options(tmp_path, medium), execute=execute, tmp_path=tmp_path
    )

    assert code == 2
    assert asked == [], "nobody was asked to type a device path"
    assert execute.argv == []
    assert "will not write into a shell script" in flat(transcript)
    assert "No disk has been touched" in flat(transcript)


def test_the_account_name_in_the_plan_cannot_steer_where_the_payload_lands(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """A plan is a JSON file off a FAT32 stick, and hop's docs invite editing it.

    ``".."`` is a directory that exists on every machine, so an unchecked name
    here does not fail — it succeeds, outside the home directory hop names in
    the transcript, and what it takes there is the payload.
    """
    raw = example_plan.to_dict()
    raw["system"]["username"] = ".."
    (medium / "hop-plan.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    root = tmp_path / "mnt"
    (root / "home").mkdir(parents=True)
    (root / "root").mkdir(parents=True)
    runner = runner_for()
    code, transcript, _ = install_run(
        options(tmp_path, medium), runner=runner, target_root=root, tmp_path=tmp_path
    )

    assert code == 0
    assert not (root / "home" / "hop").exists(), "it did not land beside the home directories"
    assert not (root / ".bash_profile").exists()
    assert (root / "root" / "hop" / "hop-plan.json").is_file(), "it fell back to root's home"
    assert "chown" not in runner.commands


def test_a_system_with_no_user_account_lands_in_root_home(
    tmp_path: Path, medium: Path
) -> None:
    root = tmp_path / "mnt"
    (root / "root").mkdir(parents=True)
    runner = runner_for()
    code, _, _ = install_run(
        options(tmp_path, medium), runner=runner, target_root=root, tmp_path=tmp_path
    )
    assert code == 0
    assert (root / "root" / "hop" / "hop-plan.json").is_file()
    assert "chown" not in runner.commands, "there is no account to give them to yet"


def test_an_unmounted_target_is_not_a_failed_install(tmp_path: Path, medium: Path) -> None:
    code, transcript, _ = install_run(
        options(tmp_path, medium), target_root=tmp_path / "nowhere", tmp_path=tmp_path
    )
    assert code == 0
    flattened = flat(transcript)
    assert "Arch is installed" in flattened
    assert "everything is still on the stick" in flattened


def test_a_chown_that_fails_is_reported_and_not_fatal(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    root, user = new_system(tmp_path, example_plan)
    runner = runner_for(**{"arch-chroot": (1, "", "chroot: cannot execute chown")})
    code, transcript, _ = install_run(
        options(tmp_path, medium), runner=runner, target_root=root, tmp_path=tmp_path
    )
    assert code == 0
    assert f"sudo chown -R {user}:{user}" in flat(transcript)


# --- what this module is not allowed to do ---------------------------------


def test_the_only_commands_it_runs_are_reading_ones(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    root, _ = new_system(tmp_path, example_plan)
    runner = runner_for()
    install_run(options(tmp_path, medium), runner=runner, target_root=root, tmp_path=tmp_path)

    # lsblk twice: once to choose the disk, once after the path was typed, to
    # check that the path still means the same disk. See
    # test_a_disk_that_changed_under_the_path_between_the_question_and_the_install.
    assert [call[0] for call in runner.calls] == ["lsblk", "lsblk", "arch-chroot"]
    for forbidden in ("sgdisk", "parted", "mkfs", "wipefs", "dd", "shutdown", "reboot"):
        assert forbidden not in runner.commands


def test_nothing_in_this_module_shells_out() -> None:
    """subprocess is called with a list, never a string and never through a shell."""
    source = Path(install.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source


# --- the disk under the path -----------------------------------------------


class ChangingRunner:
    """An lsblk that answers differently the second time it is asked.

    Which is what a machine does when something is plugged in or pulled out
    while somebody is reading a paragraph and typing a device path.
    """

    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[0] != "lsblk":
            return (0, "", "")
        payload = self.payloads[min(len(self.payloads) - 1, sum(
            1 for call in self.calls if call[0] == "lsblk") - 1)]
        return (0, json.dumps(payload), "")


def test_a_disk_that_changed_under_the_path_is_not_the_disk_that_gets_erased(
    tmp_path: Path, medium: Path
) -> None:
    """The path is read twice: once to choose, once after it has been typed.

    ``/dev/sdb`` is a position in the order the kernel saw the hardware, not a
    name a disk carries. Between the list hop printed and the path somebody
    typed there is a pause long enough to unplug one drive and attach another,
    and the second one answers to the same three characters.
    """
    was = windows_disk(name="sdb", path="/dev/sdb", model="Samsung SSD 980 1TB",
                       serial="S5GXNF0R", size=TARGET_BYTES)
    now = windows_disk(name="sdb", path="/dev/sdb", model="Seagate Backup Plus",
                       serial="NA8H2QLK", size=2_000_398_934_016)
    runner = ChangingRunner(listing(was, live_medium()), listing(now, live_medium()))
    execute = Execution()

    code, transcript, _ = install_run(
        options(tmp_path, medium), runner=runner, execute=execute,
        answers=["/dev/sdb"], tmp_path=tmp_path,
    )
    assert code == 2
    assert execute.argv == [], "the layout was built from a disk that is no longer there"
    flattened = flat(transcript)
    assert "does not name the disk it named a moment ago" in flattened
    assert "Seagate Backup Plus" in flattened and "Samsung SSD 980 1TB" in flattened
    assert "no disk has been touched" in flattened
    assert not (tmp_path / "out" / "user_configuration.json").exists()


def test_a_disk_that_went_away_after_the_path_was_typed_stops_the_install(
    tmp_path: Path, medium: Path
) -> None:
    runner = ChangingRunner(listing(), listing(live_medium()))
    execute = Execution()
    code, transcript, _ = install_run(
        options(tmp_path, medium), runner=runner, execute=execute, tmp_path=tmp_path
    )
    assert code == 2
    assert execute.argv == []
    assert "not in this machine any more" in flat(transcript)


def test_the_layout_is_built_from_the_reading_taken_after_the_question(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """A size read before the question is as stale as one read last week.

    The disk is the same disk — same model, same serial, same bus — and lsblk
    has simply reported it more precisely the second time. The arithmetic has to
    follow the later reading, because that is the one describing the disk that
    is about to be partitioned.
    """
    root, _ = new_system(tmp_path, example_plan)
    runner = ChangingRunner(listing(), listing())
    execute = Execution()
    code, _, _ = install_run(
        options(tmp_path, medium), runner=runner, execute=execute,
        target_root=root, tmp_path=tmp_path,
    )
    assert code == 0
    config = json.loads((tmp_path / "out" / "user_configuration.json").read_text(encoding="utf-8"))
    partitions = config["disk_config"]["device_modifications"][0]["partitions"]
    root_partition = next(p for p in partitions if p["mountpoint"] == "/")
    assert (
        root_partition["start"]["value"] + root_partition["size"]["value"]
        == TARGET_BYTES - install.ALIGNMENT
    )


def test_the_booted_medium_is_still_refused_when_lsblk_says_nothing_about_mounts(
    tmp_path: Path, medium: Path
) -> None:
    """The one drive that must never be chosen, described by a blinder lsblk.

    Everything that keeps the live medium out of the candidate list on Linux
    rests on where its filesystem is mounted. A listing that does not carry
    mount points has not said the medium is free — it has failed to say
    anything, and hop treats the two differently.
    """
    blind = {
        "blockdevices": [
            {"name": "sda", "path": MEDIUM_ID, "model": "SanDisk Ultra USB 3.0",
             "serial": "4C530001", "size": 64 * 1024**3, "tran": "usb", "rm": True,
             "children": [{"name": "sda1", "path": f"{MEDIUM_ID}1", "size": 64 * 1024**3,
                           "label": "ARCH_202607", "fstype": "vfat"}]},
        ]
    }
    found = drives_from(blind)
    assert install.targets(found) == []
    with pytest.raises(InstallError) as caught:
        install.choose_target(found, hint=MEDIUM_ID)
    assert "running from" in str(caught.value)

    execute = Execution()
    code, transcript, _ = install_run(
        options(tmp_path, medium, target=MEDIUM_ID), runner=runner_for(blind),
        execute=execute, tmp_path=tmp_path,
    )
    assert code == 2
    assert execute.argv == []
    assert "will not erase the ground it is standing on" in flat(transcript)


def test_a_drive_somebody_is_using_right_now_is_not_offered(
    tmp_path: Path, medium: Path
) -> None:
    """Mounting the backup disk to save one last file is what people do.

    It is not a system drive, it is large enough, and it has NTFS on it — every
    signal by which hop recognises the machine being left behind. A filesystem
    that is mounted is one somebody is reading from, and that is a separate
    reason not to offer it.
    """
    backup = windows_disk(name="sdb", path="/dev/sdb", model="Seagate Backup Plus",
                          serial="NA8H2QLK", size=2_000_398_934_016, children=[
        {"name": "sdb1", "path": "/dev/sdb1", "size": 2_000_000_000_000, "label": "Backup",
         "fstype": "ntfs", "mountpoint": "/mnt/backup"},
    ])
    found = drives_from(listing(backup, live_medium()))
    assert install.targets(found) == []
    assert install.windows_disks(found) == []
    assert "/mnt/backup" in install.target_refusal(found[0])

    execute = Execution()
    code, transcript, _ = install_run(
        options(tmp_path, medium, target="/dev/sdb"),
        runner=runner_for(listing(backup, live_medium())), execute=execute, tmp_path=tmp_path,
    )
    assert code == 2
    assert execute.argv == []
    assert "mounted at /mnt/backup" in flat(transcript)


def test_the_stick_keeps_the_private_files_and_is_said_so_at_the_end(
    tmp_path: Path, medium: Path, example_plan: Plan
) -> None:
    """Copying the payload into the new system did not take it off the stick."""
    root, _ = new_system(tmp_path, example_plan)
    assert install._private_payload(example_plan), "the example carries keys and a Wi-Fi password"
    _, transcript, _ = install_run(
        options(tmp_path, medium), target_root=root, tmp_path=tmp_path
    )
    flattened = flat(transcript)
    assert "did not take them off the stick" in flattened
    assert "no permissions" in flattened
