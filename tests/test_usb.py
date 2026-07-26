"""Choosing a drive, and refusing to erase the wrong one.

Nothing in this file touches a disk. Every function that looks at hardware takes
an injected runner, so the tests feed canned ``Get-Disk`` and ``lsblk`` output
and read back what hop decided; the fake runner records every command it was
asked for, which is how the refusal tests prove that a refusal happened *before*
anything ran rather than after. The one test that copies files copies them into
``tmp_path``.

The canned JSON is shaped the way Windows PowerShell 5.1 really writes it,
including the two things that catch people out: a single-element array comes
back as a bare object, and a size arrives as a float.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from hop import usb
from hop.usb import Drive, UsbError, Volume

STICK_ID = r"\\.\PHYSICALDRIVE2"
SYSTEM_ID = r"\\.\PHYSICALDRIVE0"
BACKUP_ID = r"\\.\PHYSICALDRIVE3"
DATA_ID = r"\\.\PHYSICALDRIVE1"

STICK_BYTES = 30_765_219_840


def windows_payload(**overrides: object) -> dict:
    """A machine with four disks: the system, a data disk, a stick, a backup drive."""
    payload: dict = {
        "systemroot": "C:\\Windows",
        "disks": [
            {"number": 0, "model": "Samsung SSD 980 1TB", "serial": "S5GXNF0R", "size": 1.0e12,
             "bus": "NVMe", "boot": True, "system": True},
            {"number": 1, "model": "WDC WD20EZRZ", "serial": "WD-WCC4M0", "size": 2.0e12,
             "bus": "SATA", "boot": False, "system": False},
            {"number": 2, "model": "SanDisk Ultra USB 3.0", "serial": "4C530001", "size": float(STICK_BYTES),
             "bus": "USB", "boot": False, "system": False},
            {"number": 3, "model": "Seagate Backup Plus", "serial": "NA8H2QLK", "size": 2.0e12,
             "bus": "USB", "boot": False, "system": False},
        ],
        "media": [
            {"number": 0, "media": "Fixed hard disk media"},
            {"number": 1, "media": "Fixed hard disk media"},
            {"number": 2, "media": "Removable Media"},
            {"number": 3, "media": "External hard disk media"},
        ],
        "partitions": [
            {"number": 0, "letter": "", "size": 1.04e8},
            {"number": 0, "letter": "C", "size": 9.9e11},
            {"number": 1, "letter": "E", "size": 2.0e12},
            {"number": 2, "letter": "F", "size": 3.07e10},
            {"number": 3, "letter": "G", "size": 2.0e12},
        ],
        "volumes": [
            {"letter": "C", "label": "", "fs": "NTFS", "size": 9.9e11},
            {"letter": "E", "label": "Data", "fs": "NTFS", "size": 2.0e12},
            {"letter": "F", "label": "KINGSTON", "fs": "FAT32", "size": 3.07e10},
            {"letter": "G", "label": "Backup", "fs": "NTFS", "size": 2.0e12},
        ],
        "windows": ["C"],
    }
    payload.update(overrides)
    return payload


class FakeRunner:
    """Canned answers, and a record of everything hop asked for.

    Answers are ``(needle, answer)`` pairs matched by substring against the whole
    command, which is enough to tell an enumeration from a format from a flush
    and keeps each canned answer readable next to the test that needs it. An
    answer is a dict to be returned as JSON, or a ``(returncode, out, err)``
    triple for the failures. The shape is ``hop.usb.Runner``, which is also
    ``hop.iso.Runner``.
    """

    def __init__(self, answers: Sequence[tuple[str, object]] = (), *, tmp_path: Path | None = None) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []
        self.tmp_path = tmp_path

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        self.calls.append(list(argv))
        # 'mkdir -p' is the one command in the Linux path whose real effect the
        # copy afterwards depends on, so the fake performs it for real inside
        # tmp_path rather than pretending.
        if argv[:2] == ["mkdir", "-p"] and self.tmp_path is not None:
            Path(argv[2]).mkdir(parents=True, exist_ok=True)
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


def windows_runner(payload: dict | None = None, **extra: object) -> FakeRunner:
    answers: list[tuple[str, object]] = [("Get-Disk | Select-Object", payload or windows_payload())]
    answers += [(needle, answer) for needle, answer in extra.items()]
    return FakeRunner(answers)


def stick() -> Drive:
    """The drive hop would offer, as the enumeration produces it."""
    found = usb.drives(runner=windows_runner(), platform="windows")
    return next(drive for drive in found if drive.device_id == STICK_ID)


# --- enumeration ------------------------------------------------------------


def test_windows_enumeration_reads_json_not_a_table() -> None:
    runner = windows_runner()
    found = usb.drives(runner=runner, platform="windows")

    assert [drive.device_id for drive in found] == [SYSTEM_ID, DATA_ID, STICK_ID, BACKUP_ID]
    assert "ConvertTo-Json" in runner.commands
    assert "Format-Table" not in runner.commands

    disk = {drive.device_id: drive for drive in found}
    assert disk[STICK_ID].model == "SanDisk Ultra USB 3.0"
    assert disk[STICK_ID].size_bytes == STICK_BYTES
    assert disk[STICK_ID].bus == "USB"
    assert disk[STICK_ID].removable and not disk[STICK_ID].system
    assert disk[STICK_ID].volumes == (Volume("F", "KINGSTON", "FAT32", 30_700_000_000),)


def test_the_system_disk_is_known_by_three_separate_signals() -> None:
    """IsSystem, the disk holding %SystemRoot%, and a Windows directory on it."""
    payload = windows_payload()
    for disk in payload["disks"]:
        disk["boot"] = disk["system"] = False
    found = {drive.device_id: drive for drive in usb.drives(runner=windows_runner(payload), platform="windows")}
    assert found[SYSTEM_ID].system, "C: still holds %SystemRoot%"

    payload = windows_payload(windows=["C", "F"])
    found = {drive.device_id: drive for drive in usb.drives(runner=windows_runner(payload), platform="windows")}
    assert found[STICK_ID].system, "a Windows directory on the stick is enough to refuse it"


@pytest.mark.parametrize(
    "overrides",
    [
        {"partitions": []},
        {"volumes": [], "windows": []},
        {"systemroot": ""},
    ],
    ids=["no partition table", "no volumes", "no %SystemRoot%"],
)
def test_cannot_tell_means_system(overrides: dict) -> None:
    """The refusal is allowed to be wrong. It is not allowed to be wrong that way."""
    found = usb.drives(runner=windows_runner(windows_payload(**overrides)), platform="windows")
    assert all(drive.system for drive in found)


def test_a_disk_record_missing_its_flags_is_a_system_disk() -> None:
    payload = windows_payload()
    payload["disks"][2].pop("system")
    found = {drive.device_id: drive for drive in usb.drives(runner=windows_runner(payload), platform="windows")}
    assert found[STICK_ID].system


def test_one_disk_comes_back_as_an_object_not_an_array() -> None:
    """ConvertTo-Json unwraps single-element arrays, and one-disk laptops exist."""
    payload = windows_payload()
    payload["disks"] = payload["disks"][2]
    payload["media"] = payload["media"][2]
    payload["partitions"] = payload["partitions"][3]
    payload["volumes"] = payload["volumes"][2]
    payload["windows"] = "C"
    found = usb.drives(runner=windows_runner(payload), platform="windows")
    assert [drive.device_id for drive in found] == [STICK_ID]
    assert found[0].volumes[0].label == "KINGSTON"


def test_describe_is_one_line_with_what_is_written_on_the_drive() -> None:
    line = stick().describe
    assert "\n" not in line
    for part in (STICK_ID, "SanDisk Ultra USB 3.0", "28.7 GB", "USB", "removable", "KINGSTON"):
        assert part in line


def test_lsblk_parsing() -> None:
    payload = {
        "blockdevices": [
            {"name": "sda", "path": "/dev/sda", "model": "Samsung SSD", "serial": "S1", "size": 512110190592,
             "tran": "sata", "rm": False, "mountpoint": None, "label": None, "fstype": None,
             "children": [
                 {"name": "sda1", "path": "/dev/sda1", "size": 536870912, "mountpoint": "/boot",
                  "label": "ESP", "fstype": "vfat"},
                 {"name": "sda2", "path": "/dev/sda2", "size": 511573319680, "mountpoint": "/",
                  "label": None, "fstype": "ext4"},
             ]},
            {"name": "sdb", "path": "/dev/sdb", "model": "Ultra USB 3.0", "serial": "4C530001",
             "size": STICK_BYTES, "tran": "usb", "rm": True, "mountpoint": None,
             "children": [
                 {"name": "sdb1", "path": "/dev/sdb1", "size": 30_700_000_000, "mountpoint": None,
                  "label": "KINGSTON", "fstype": "vfat"},
             ]},
            {"name": "loop0", "path": "/dev/loop0", "size": 1000, "mountpoint": "/run/archiso/sfs"},
            {"name": "sr0", "path": "/dev/sr0", "size": 0, "mountpoint": None},
        ]
    }
    runner = FakeRunner([("lsblk", payload)])
    found = usb.drives(runner=runner, platform="linux")

    assert [drive.device_id for drive in found] == ["/dev/sda", "/dev/sdb"], "loop and cdrom are not drives"
    assert runner.calls[0][:4] == ["lsblk", "-J", "-b", "-o"]
    assert found[0].system and not found[0].removable
    assert found[1].removable and not found[1].system
    assert found[1].bus == "USB"
    assert found[1].volumes == (Volume(None, "KINGSTON", "vfat", 30_700_000_000),)


def test_a_mount_point_below_the_partitions_still_counts_as_mounted() -> None:
    """LUKS inside a partition, LVM inside that, the filesystem inside that.

    lsblk nests as deep as the stack does. Reading only the disk and its direct
    children saw no mount point on a machine whose root is encrypted, which made
    the drive carrying the running system look like a drive with nothing on it —
    ``system`` false, ``mounted`` empty — and ``hop install`` will offer to erase
    a drive on both of those.
    """
    payload = {
        "blockdevices": [
            {"name": "sda", "path": "/dev/sda", "model": "Samsung SSD", "serial": "S1",
             "size": 512110190592, "tran": "usb", "rm": True, "mountpoints": [None], "children": [
                 {"name": "sda1", "path": "/dev/sda1", "size": 536870912, "fstype": "vfat",
                  "mountpoints": [None]},
                 {"name": "sda2", "path": "/dev/sda2", "size": 511573319680,
                  "fstype": "crypto_LUKS", "mountpoints": [None], "children": [
                      {"name": "dm-0", "path": "/dev/mapper/vg", "size": 511573319680,
                       "fstype": "LVM2_member", "mountpoints": [None], "children": [
                           {"name": "dm-1", "path": "/dev/mapper/vg-root", "size": 4e11,
                            "fstype": "ext4", "mountpoints": ["/"]},
                           {"name": "dm-2", "path": "/dev/mapper/vg-home", "size": 1e11,
                            "fstype": "ext4", "mountpoints": ["/home"]},
                       ]},
                  ]},
             ]},
        ]
    }
    drive = usb.drives(runner=FakeRunner([("lsblk", payload)]), platform="linux")[0]

    assert drive.mounted == ("/", "/home")
    assert drive.system, "the running root is on it, three levels down"
    assert usb.refuse_reason(drive) is not None


def test_a_label_in_another_alphabet_survives_the_trip_to_the_description() -> None:
    """The line somebody matches against the drive in their hand, in their own language."""
    payload = {
        "blockdevices": [
            {"name": "sdb", "path": "/dev/sdb", "model": "Ultra USB 3.0", "serial": "4C53",
             "size": STICK_BYTES, "tran": "usb", "rm": True, "mountpoint": None, "children": [
                 {"name": "sdb1", "path": "/dev/sdb1", "size": 30_700_000_000, "mountpoint": None,
                  "label": "Восстановление", "fstype": "ntfs"},
             ]},
            {"name": "sdc", "path": "/dev/sdc", "model": "SD card reader", "serial": None,
             "size": 0, "tran": "usb", "rm": True, "mountpoint": None, "children": []},
        ]
    }
    found = usb.drives(runner=FakeRunner([("lsblk", payload)]), platform="linux")

    assert found[0].volumes[0].label == "Восстановление"
    assert "Восстановление" in found[0].describe

    # An empty card reader: no partitions at all, and a refusal that says so
    # rather than a description with a blank where the filesystem should be.
    assert found[1].volumes == ()
    assert "no filesystem on it" in found[1].describe
    assert "size of 0 bytes" in usb.refuse_reason(found[1])


def test_the_live_medium_you_booted_from_is_never_a_candidate() -> None:
    payload = {
        "blockdevices": [
            {"name": "sdb", "path": "/dev/sdb", "size": STICK_BYTES, "tran": "usb", "rm": True,
             "mountpoint": None, "children": [{"name": "sdb1", "path": "/dev/sdb1", "size": STICK_BYTES,
                           "mountpoint": "/run/archiso/bootmnt", "fstype": "vfat", "label": "ARCH_202601"}]},
        ]
    }
    found = usb.drives(runner=FakeRunner([("lsblk", payload)]), platform="linux")
    assert found[0].system
    assert usb.refuse_reason(found[0])


def test_enumeration_failure_says_so_rather_than_returning_nothing() -> None:
    runner = FakeRunner([("Get-Disk", (1, "", "Access is denied."))])
    with pytest.raises(UsbError) as caught:
        usb.drives(runner=runner, platform="windows")
    assert "Access is denied." in str(caught.value)


# --- the refusal matrix -----------------------------------------------------


def test_candidates_are_only_the_drives_that_may_be_erased() -> None:
    runner = windows_runner()
    assert [drive.device_id for drive in usb.candidates(runner=runner, platform="windows")] == [STICK_ID]


def test_every_refusal_names_the_drive_and_the_rule() -> None:
    found = usb.drives(runner=windows_runner(), platform="windows")
    reasons = {drive.device_id: usb.refuse_reason(drive) for drive in found}

    assert "operating system" in reasons[SYSTEM_ID]
    assert SYSTEM_ID in reasons[SYSTEM_ID]

    assert "fixed drive" in reasons[DATA_ID] and "SATA" in reasons[DATA_ID]
    assert DATA_ID in reasons[DATA_ID]

    assert "larger than" in reasons[BACKUP_ID] and "backup" in reasons[BACKUP_ID]
    assert "Seagate Backup Plus" in reasons[BACKUP_ID]

    assert reasons[STICK_ID] is None


def test_a_large_removable_drive_needs_the_second_word() -> None:
    found = usb.drives(runner=windows_runner(), platform="windows")
    backup = next(drive for drive in found if drive.device_id == BACKUP_ID)
    assert usb.refuse_reason(backup) is not None
    assert usb.refuse_reason(backup, allow_large=True) is None
    ids = [d.device_id for d in usb.candidates(runner=windows_runner(), platform="windows", allow_large=True)]
    assert ids == [STICK_ID, BACKUP_ID]


def test_a_drive_of_no_measurable_size_is_refused() -> None:
    empty = Drive("/dev/sdz", 9, "USB card reader", None, 0, "USB", True, False)
    assert "size of 0 bytes" in usb.refuse_reason(empty)


@pytest.mark.parametrize(
    ("drive", "expected"),
    [
        (Drive(SYSTEM_ID, 0, "Samsung", "S1", 10**12, "NVMe", False, True), "operating system"),
        (Drive(DATA_ID, 1, "WDC", "W1", 10**12, "SATA", False, False), "fixed drive"),
        (Drive(BACKUP_ID, 3, "Seagate", "N1", 2 * 10**12, "USB", True, False), "larger than"),
    ],
)
def test_prepare_refuses_before_it_runs_anything(drive: Drive, expected: str) -> None:
    runner = windows_runner()
    with pytest.raises(UsbError) as caught:
        usb.prepare(drive, label="ARCH_202601", confirm_device_id=drive.device_id,
                    runner=runner, platform="windows")
    assert expected in str(caught.value)
    assert runner.calls == [], "a refusal that runs a command first is not a refusal"


def test_the_confirmation_has_to_be_the_device_id() -> None:
    runner = windows_runner()
    for wrong in ("yes", "y", "true", "PHYSICALDRIVE2", r"\\.\PHYSICALDRIVE3", ""):
        with pytest.raises(UsbError) as caught:
            usb.prepare(stick(), label="ARCH_202601", confirm_device_id=wrong,
                        runner=runner, platform="windows")
        message = str(caught.value)
        assert STICK_ID in message and repr(wrong) in message
    assert runner.calls == []


def test_write_medium_checks_the_confirmation_before_it_reads_the_iso(tmp_path: Path) -> None:
    runner = windows_runner()
    with pytest.raises(UsbError) as caught:
        usb.write_medium(stick(), tmp_path / "nothing-here", {}, label="ARCH_202601",
                         confirm_device_id="yes", runner=runner, platform="windows")
    assert "not the same drive" in str(caught.value)
    assert runner.calls == []


def test_a_drive_that_has_gone_away_is_not_erased() -> None:
    drive = stick()
    payload = windows_payload()
    payload["disks"] = [disk for disk in payload["disks"] if disk["number"] != 2]
    with pytest.raises(UsbError) as caught:
        usb.prepare(drive, label="ARCH_202601", confirm_device_id=STICK_ID,
                    runner=windows_runner(payload), platform="windows")
    assert "not there any more" in str(caught.value)


def test_a_disk_number_that_now_belongs_to_another_drive_is_not_erased() -> None:
    """The reason the confirmation is a device id and the check runs twice."""
    drive = stick()
    payload = windows_payload()
    payload["disks"][2] = {"number": 2, "model": "Seagate Backup Plus", "serial": "NA8H2QLK",
                           "size": 2.0e12, "bus": "USB", "boot": False, "system": False}
    with pytest.raises(UsbError) as caught:
        usb.prepare(drive, label="ARCH_202601", confirm_device_id=STICK_ID,
                    runner=windows_runner(payload), platform="windows")
    message = str(caught.value)
    assert "no longer a drive hop will erase" in message or "not the drive hop was told" in message
    assert "Nothing has been erased" in message


def test_a_swapped_drive_of_the_same_size_is_caught_by_its_serial() -> None:
    drive = stick()
    payload = windows_payload()
    payload["disks"][2]["serial"] = "SOMETHINGELSE"
    with pytest.raises(UsbError) as caught:
        usb.prepare(drive, label="ARCH_202601", confirm_device_id=STICK_ID,
                    runner=windows_runner(payload), platform="windows")
    assert "not the drive hop was told to erase" in str(caught.value)


def test_the_label_has_to_be_one_the_kernel_can_find_the_filesystem_by() -> None:
    for bad in ("", "arch linux 2026", "ARCH_2026'; DEL", "TOO_LONG_A_LABEL"):
        with pytest.raises(UsbError) as caught:
            usb.prepare(stick(), label=bad, confirm_device_id=STICK_ID,
                        runner=windows_runner(), platform="windows")
        assert "volume label" in str(caught.value)


# --- what the format actually says ------------------------------------------


def test_every_command_names_the_disk_and_none_of_them_takes_a_wildcard() -> None:
    runner = windows_runner(**{"Clear-Disk": {"letter": "H"}})
    letter = usb.prepare(stick(), label="ARCH_202601", confirm_device_id=STICK_ID,
                         runner=runner, platform="windows")
    assert letter == "H:"

    script = next(call[-1] for call in runner.calls if "Clear-Disk" in call[-1])
    for raw in script.splitlines():
        line = raw.strip()
        if line.startswith("$"):  # '$disk = Get-Disk ...' is still a Get-Disk
            line = line.partition("=")[2].strip()
        if line.startswith(("Clear-Disk", "Initialize-Disk", "Get-Disk")):
            assert "-Number 2" in line, raw
        if line.startswith(("New-Partition", "Get-Partition")):
            assert "-DiskNumber 2" in line, raw
    assert "*" not in script
    assert '"' not in script, "the script is written without double quotes on purpose"
    assert "ARCH_202601" in script
    assert "IsSystem" in script and "SerialNumber" in script, "it re-checks the disk before erasing it"


def test_an_ordinary_stick_is_partitioned_by_maximum_size_not_by_its_own_size() -> None:
    """The number hop has is the size of the disk, and no partition is that big.

    ``Get-Disk``'s ``Size`` counts the partition table and the megabyte Windows
    aligns the first partition past, so a ``-Size`` of exactly that never fits
    the largest free extent — and ``New-Partition`` does not clamp, it fails.
    It fails after ``Clear-Disk``, which means every stick under the FAT32
    ceiling — which is every stick anybody uses for this — would be erased and
    then left unformatted.
    """
    script = usb._windows_prepare_script(stick(), "ARCH_202601")
    assert "-UseMaximumSize" in script
    assert f"-Size {STICK_BYTES}" not in script
    assert "-Size" not in script, "no size at all is named for a drive that fits"


@pytest.mark.parametrize(
    "size",
    [
        128 * 1024**3,
        usb.WINDOWS_FAT32_MAX_PARTITION + 1,
        usb.WINDOWS_FAT32_MAX_PARTITION + 1024**2,
    ],
    ids=["a 128 GB stick", "one byte over the ceiling", "a megabyte over the ceiling"],
)
def test_a_stick_larger_than_windows_will_format_gets_a_smaller_partition(size: int) -> None:
    """And the named size fits inside the disk even at the edge of the ceiling.

    A drive a hair over the ceiling has less free extent than the ceiling once
    the alignment is taken off the front, so asking for the ceiling exactly
    would fail there in the same way asking for the whole disk fails everywhere.
    """
    big = Drive(STICK_ID, 2, "SanDisk Extreme", "4C530001", size, "USB", True, False)
    script = usb._windows_prepare_script(big, "ARCH_202601")
    named = int(re.search(r"-Size (\d+)", script).group(1))
    assert named <= usb.WINDOWS_FAT32_MAX_PARTITION
    assert named < size - 1024**2, "it has to fit past the alignment at the front of the disk"


def test_a_serial_that_cannot_be_written_into_a_script_is_dropped_not_escaped() -> None:
    odd = Drive(STICK_ID, 2, "SanDisk", "4C53'; Clear-Disk -Number 0 #", STICK_BYTES, "USB", True, False)
    script = usb._windows_prepare_script(odd, "ARCH_202601")
    assert "Clear-Disk -Number 0" not in script
    assert "reported no serial number" in script


def test_the_linux_format_names_the_device_every_time(tmp_path: Path) -> None:
    payload = {
        "blockdevices": [
            {"name": "sdb", "path": "/dev/sdb", "model": "Ultra", "serial": "4C53", "size": STICK_BYTES,
             "tran": "usb", "rm": True, "mountpoint": None, "children": []},
        ]
    }
    runner = FakeRunner([("lsblk", payload)], tmp_path=tmp_path)
    drive = usb.drives(runner=runner, platform="linux")[0]
    mount = usb.prepare(drive, label="ARCH_202601", confirm_device_id="/dev/sdb",
                        runner=runner, platform="linux", mount_root=tmp_path)

    assert mount.endswith("/ARCH_202601")
    assert Path(mount) == tmp_path / "ARCH_202601"
    ran = [call for call in runner.calls if call[0] != "lsblk"]
    assert [call[0] for call in ran] == ["sgdisk", "sgdisk", "udevadm", "mkfs.vfat", "mkdir", "mount"]
    for call in ran:
        if call[0] in ("sgdisk", "mkfs.vfat", "mount"):
            assert any(part.startswith("/dev/sdb") for part in call), call
    assert ["mkfs.vfat", "-F", "32", "-n", "ARCH_202601", "/dev/sdb1"] in ran


def test_a_failing_format_command_is_a_sentence_not_a_traceback(tmp_path: Path) -> None:
    payload = {"blockdevices": [{"name": "sdb", "path": "/dev/sdb", "size": STICK_BYTES,
                                 "tran": "usb", "rm": True, "mountpoint": None, "children": []}]}
    runner = FakeRunner(
        [("lsblk", payload), ("sgdisk", (2, "", "Problem opening /dev/sdb"))],
        tmp_path=tmp_path,
    )
    drive = usb.drives(runner=runner, platform="linux")[0]
    with pytest.raises(UsbError) as caught:
        usb.prepare(drive, label="ARCH_202601", confirm_device_id="/dev/sdb",
                    runner=runner, platform="linux", mount_root=tmp_path)
    assert "Problem opening /dev/sdb" in str(caught.value)
    assert "needs root" in str(caught.value)


# --- the copy ---------------------------------------------------------------


def make_iso(root: Path, *, boot_file: bool = True) -> Path:
    """The parts of an extracted archiso the medium code actually looks at."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "arch" / "x86_64").mkdir(parents=True)
    (root / "arch" / "x86_64" / "airootfs.sfs").write_bytes(b"squashfs" * 64)
    (root / "arch" / "boot" / "x86_64").mkdir(parents=True)
    (root / "arch" / "boot" / "x86_64" / "vmlinuz-linux").write_bytes(b"kernel")
    (root / "loader" / "entries").mkdir(parents=True)
    (root / "loader" / "loader.conf").write_text(
        "timeout 15\ndefault 01-archiso-x86_64-linux.conf\n", encoding="utf-8"
    )
    (root / "loader" / "entries" / "01-archiso-x86_64-linux.conf").write_text(
        "title   Arch Linux install medium (x86_64, UEFI)\n"
        "linux   /arch/boot/x86_64/vmlinuz-linux\n"
        "initrd  /arch/boot/x86_64/initramfs-linux.img\n"
        "options archisobasedir=arch archisolabel=ARCH_202601\n",
        encoding="utf-8",
    )
    (root / "loader" / "entries" / "02-archiso-x86_64-speech.conf").write_text(
        "title   Arch Linux install medium (x86_64, UEFI, speech)\n"
        "options archisobasedir=arch archisolabel=ARCH_202601 accessibility=on\n",
        encoding="utf-8",
    )
    if boot_file:
        (root / "EFI" / "BOOT").mkdir(parents=True)
        (root / "EFI" / "BOOT" / "BOOTx64.EFI").write_bytes(b"MZ" * 32)
    return root


def linux_stick(size: int = STICK_BYTES) -> tuple[Drive, dict]:
    # 'mountpoint': None is not decoration. lsblk is asked for that column by
    # name and always answers with it; a listing without it is one hop refuses
    # to read anything into. See test_a_listing_with_no_mount_points_at_all.
    payload = {"blockdevices": [{"name": "sdb", "path": "/dev/sdb", "model": "Ultra", "serial": "4C53",
                                 "size": size, "tran": "usb", "rm": True, "mountpoint": None, "children": []}]}
    drive = Drive("/dev/sdb", 0, "Ultra", "4C53", size, "USB", True, False)
    return drive, payload


def test_write_medium_copies_the_installer_and_the_baggage(tmp_path: Path) -> None:
    iso = make_iso(tmp_path / "iso")
    payload_dir = tmp_path / "hop-payload"
    (payload_dir / "ssh").mkdir(parents=True)
    (payload_dir / "ssh" / "id_ed25519").write_text("key", encoding="utf-8")
    plan = tmp_path / "hop-plan.json"
    plan.write_text(json.dumps({"plan": 1}), encoding="utf-8")

    drive, listing = linux_stick()
    runner = FakeRunner([("lsblk", listing)], tmp_path=tmp_path)
    medium = usb.write_medium(
        drive, iso, {"hop-plan.json": plan, "hop-payload": payload_dir},
        label="ARCH_202601", confirm_device_id="/dev/sdb", runner=runner,
        platform="linux", mount_root=tmp_path / "media",
    )

    assert (medium / "EFI" / "BOOT" / "BOOTx64.EFI").read_bytes() == b"MZ" * 32
    assert (medium / "arch" / "x86_64" / "airootfs.sfs").stat().st_size == 8 * 64
    assert (medium / "hop" / "hop-plan.json").read_text(encoding="utf-8") == '{"plan": 1}'
    assert (medium / "hop" / "hop-payload" / "ssh" / "id_ed25519").read_text(encoding="utf-8") == "key"
    assert ["sync"] in runner.calls, "the copy is not finished until it is out of the cache"


def test_a_short_copy_is_caught_and_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The classic bad stick: it takes the write, and keeps half of it."""
    iso = make_iso(tmp_path / "iso")
    drive, listing = linux_stick()
    runner = FakeRunner([("lsblk", listing)], tmp_path=tmp_path)

    real_copy = usb.shutil.copyfile

    def truncating_copy(source: str, destination: str) -> None:
        real_copy(source, destination)
        if str(destination).endswith("airootfs.sfs"):
            Path(destination).write_bytes(b"half")

    monkeypatch.setattr(usb.shutil, "copyfile", truncating_copy)
    with pytest.raises(UsbError) as caught:
        usb.write_medium(drive, iso, {}, label="ARCH_202601", confirm_device_id="/dev/sdb",
                         runner=runner, platform="linux", mount_root=tmp_path / "media")
    message = str(caught.value)
    assert "airootfs.sfs" in message
    assert "Do not boot from this stick" in message


def test_a_file_fat32_cannot_hold_stops_it_before_the_drive_is_erased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso = make_iso(tmp_path / "iso")
    monkeypatch.setattr(usb, "FAT32_MAX_FILE", 8)
    drive, listing = linux_stick()
    runner = FakeRunner([("lsblk", listing)], tmp_path=tmp_path)

    with pytest.raises(UsbError) as caught:
        usb.write_medium(drive, iso, {}, label="ARCH_202601", confirm_device_id="/dev/sdb",
                         runner=runner, platform="linux", mount_root=tmp_path / "media")
    assert "4 GB or more" in str(caught.value)
    assert "Nothing has been erased" in str(caught.value)
    assert not any(call[0] in ("sgdisk", "mkfs.vfat") for call in runner.calls)


def test_a_stick_too_small_for_what_it_has_to_carry(tmp_path: Path) -> None:
    iso = make_iso(tmp_path / "iso")
    drive, listing = linux_stick(size=1024)
    runner = FakeRunner([("lsblk", listing)], tmp_path=tmp_path)
    with pytest.raises(UsbError) as caught:
        usb.write_medium(drive, iso, {}, label="ARCH_202601", confirm_device_id="/dev/sdb",
                         runner=runner, platform="linux", mount_root=tmp_path / "media")
    assert "does not leave" in str(caught.value)
    assert not any(call[0] == "sgdisk" for call in runner.calls)


def test_the_windows_32gb_ceiling_is_counted_as_the_usable_size(tmp_path: Path) -> None:
    """A 2 TB stick with allow_large still only offers 32 GB of FAT32 on Windows."""
    big = Drive(STICK_ID, 2, "SanDisk", "4C530001", 2 * 1024**4, "USB", True, False)
    assert usb._usable_bytes(big, "windows") == usb.WINDOWS_FAT32_MAX_PARTITION
    assert usb._usable_bytes(big, "linux") == 2 * 1024**4


def test_an_iso_without_the_uefi_boot_file_is_refused(tmp_path: Path) -> None:
    iso = make_iso(tmp_path / "iso", boot_file=False)
    drive, listing = linux_stick()
    runner = FakeRunner([("lsblk", listing)], tmp_path=tmp_path)
    with pytest.raises(UsbError) as caught:
        usb.write_medium(drive, iso, {}, label="ARCH_202601", confirm_device_id="/dev/sdb",
                         runner=runner, platform="linux", mount_root=tmp_path / "media")
    assert usb.UEFI_BOOT_FILE in str(caught.value)
    assert not any(call[0] == "sgdisk" for call in runner.calls)


def test_baggage_cannot_be_addressed_outside_the_hop_directory(tmp_path: Path) -> None:
    iso = make_iso(tmp_path / "iso")
    victim = tmp_path / "elsewhere.txt"
    victim.write_text("mine", encoding="utf-8")
    drive, listing = linux_stick()
    for relative in ("../../elsewhere.txt", "/loader/loader.conf", "C:/Windows/x"):
        runner = FakeRunner([("lsblk", listing)], tmp_path=tmp_path)
        with pytest.raises(UsbError) as caught:
            usb.write_medium(drive, iso, {relative: victim}, label="ARCH_202601",
                             confirm_device_id="/dev/sdb", runner=runner, platform="linux",
                             mount_root=tmp_path / "media")
        assert "not a path inside" in str(caught.value)
        assert not any(call[0] == "sgdisk" for call in runner.calls)


def test_baggage_that_is_not_there_is_reported_before_anything_is_erased(tmp_path: Path) -> None:
    iso = make_iso(tmp_path / "iso")
    drive, listing = linux_stick()
    runner = FakeRunner([("lsblk", listing)], tmp_path=tmp_path)
    with pytest.raises(UsbError) as caught:
        usb.write_medium(drive, iso, {"hop-plan.json": tmp_path / "gone.json"},
                         label="ARCH_202601", confirm_device_id="/dev/sdb", runner=runner,
                         platform="linux", mount_root=tmp_path / "media")
    assert "is not there" in str(caught.value)
    assert not any(call[0] == "sgdisk" for call in runner.calls)


# --- ejecting ---------------------------------------------------------------


def test_eject_flushes_then_lets_go_on_windows() -> None:
    runner = windows_runner(**{"Write-VolumeCache": {"flushed": True}})
    usb.eject("E:", runner=runner, platform="windows")
    assert ["mountvol", "E:", "/P"] in runner.calls


def test_a_flush_that_cannot_be_confirmed_is_not_reported_as_finished() -> None:
    runner = windows_runner(**{"Write-VolumeCache": {"flushed": False}})
    with pytest.raises(UsbError) as caught:
        usb.eject("E:", runner=runner, platform="windows")
    assert "Safely Remove Hardware" in str(caught.value)
    assert ["mountvol", "E:", "/P"] not in runner.calls


# --- the boot menu ----------------------------------------------------------


def test_add_autostart_edits_a_copy_and_leaves_the_originals_alone(tmp_path: Path) -> None:
    medium = make_iso(tmp_path / "medium")
    original = (medium / "loader" / "entries" / "01-archiso-x86_64-linux.conf").read_text(encoding="utf-8")

    changed = usb.add_autostart(medium, script_relative="hop/bootstrap.sh")

    entry = medium / "loader" / "entries" / usb.HOP_ENTRY_NAME
    loader = medium / "loader" / "loader.conf"
    assert changed == [entry, loader]

    text = entry.read_text(encoding="utf-8")
    assert "script=/hop/bootstrap.sh" in text
    assert "archisolabel=ARCH_202601" in text, "the original kernel parameters survive"
    assert "hop" in text.splitlines()[0].lower(), "the boot menu says which entry is which"
    assert "\r" not in text

    assert (medium / "loader" / "entries" / "01-archiso-x86_64-linux.conf").read_text(
        encoding="utf-8"
    ) == original, "a user who does not want hop can still boot a plain Arch installer"

    conf = loader.read_text(encoding="utf-8")
    assert f"default {usb.HOP_ENTRY_NAME}" in conf
    assert "timeout 15" in conf


def test_the_menu_is_never_left_hidden(tmp_path: Path) -> None:
    medium = make_iso(tmp_path / "medium")
    (medium / "loader" / "loader.conf").write_text("timeout 0\ndefault 01-archiso-x86_64-linux.conf\n",
                                                   encoding="utf-8")
    usb.add_autostart(medium, script_relative="hop/bootstrap.sh")
    assert "timeout 10" in (medium / "loader" / "loader.conf").read_text(encoding="utf-8")


def test_the_plain_entry_is_the_one_that_gets_automated(tmp_path: Path) -> None:
    """Not the speech-synthesiser variant, which somebody chose for a reason."""
    medium = make_iso(tmp_path / "medium")
    (medium / "loader" / "loader.conf").write_text("timeout 15\n", encoding="utf-8")
    usb.add_autostart(medium, script_relative="hop/bootstrap.sh")
    text = (medium / "loader" / "entries" / usb.HOP_ENTRY_NAME).read_text(encoding="utf-8")
    assert "accessibility=on" not in text


def test_add_autostart_is_safe_to_run_twice(tmp_path: Path) -> None:
    medium = make_iso(tmp_path / "medium")
    usb.add_autostart(medium, script_relative="hop/bootstrap.sh")
    first = (medium / "loader" / "entries" / usb.HOP_ENTRY_NAME).read_text(encoding="utf-8")
    usb.add_autostart(medium, script_relative="hop/bootstrap.sh")
    second = (medium / "loader" / "entries" / usb.HOP_ENTRY_NAME).read_text(encoding="utf-8")
    assert first == second
    assert second.count("script=/hop/bootstrap.sh") == 1


def test_a_medium_with_no_loader_entries_says_how_to_start_hop_by_hand(tmp_path: Path) -> None:
    (tmp_path / "medium").mkdir()
    with pytest.raises(UsbError) as caught:
        usb.add_autostart(tmp_path / "medium", script_relative="hop/bootstrap.sh")
    assert "bootstrap.sh" in str(caught.value)


def test_an_entry_with_no_options_line_is_not_an_archiso_entry(tmp_path: Path) -> None:
    medium = tmp_path / "medium"
    (medium / "loader" / "entries").mkdir(parents=True)
    (medium / "loader" / "entries" / "windows.conf").write_text("title Windows\n", encoding="utf-8")
    with pytest.raises(UsbError) as caught:
        usb.add_autostart(medium, script_relative="hop/bootstrap.sh")
    assert "no 'options' line" in str(caught.value)


# --- the bootstrap and the firmware -----------------------------------------


def test_the_bootstrap_says_how_to_run_it_by_hand() -> None:
    script = usb.bootstrap_script(label="ARCH_202601")
    assert script.startswith("#!/usr/bin/env bash")
    assert usb.BOOTSTRAP_NAME in script, "the command to run it by hand is printed"
    assert "python3 -m hop install" in script
    assert "Ctrl+C" in script
    assert "ARCH_202601" in script


@pytest.mark.parametrize(
    ("firmware", "expected"),
    [("UEFI", None), ("BIOS", "syslinux"), ("unknown", "msinfo32"), (None, "msinfo32")],
)
def test_a_stick_that_will_not_boot_is_refused_rather_than_built(
    firmware: str | None, expected: str | None
) -> None:
    reason = usb.firmware_refusal(firmware)
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


def test_nothing_in_this_module_shells_out() -> None:
    """subprocess is called with a list, never a string and never through a shell."""
    source = Path(usb.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source


# --- what "cannot tell" has to mean -----------------------------------------


def test_a_listing_with_no_mount_points_at_all_is_not_an_answer() -> None:
    """On Linux the mount points are the only thing that says "this one is in use".

    lsblk is asked for the column by name, and a running Linux always has
    something mounted, so a listing that carries the field nowhere is a tool hop
    has misread rather than a machine with nothing mounted. Read the other way
    round it marked every drive as free to erase — including the stick the live
    environment is being read from, which is the one drive that must never be
    offered.
    """
    told = {
        "blockdevices": [
            {"name": "sda", "path": "/dev/sda", "size": STICK_BYTES, "tran": "usb", "rm": True,
             "mountpoint": None, "children": [
                 {"name": "sda1", "path": "/dev/sda1", "size": STICK_BYTES, "fstype": "vfat",
                  "label": "ARCH_202607", "mountpoint": "/run/archiso/bootmnt"}]},
        ]
    }
    # The same machine, described by an lsblk that did not report the column.
    blind = {
        "blockdevices": [
            {"name": "sda", "path": "/dev/sda", "size": STICK_BYTES, "tran": "usb", "rm": True,
             "children": [
                 {"name": "sda1", "path": "/dev/sda1", "size": STICK_BYTES, "fstype": "vfat",
                  "label": "ARCH_202607"}]},
        ]
    }
    assert usb.drives(runner=FakeRunner([("lsblk", told)]), platform="linux")[0].system

    found = usb.drives(runner=FakeRunner([("lsblk", blind)]), platform="linux")
    assert found[0].system, "an unanswered question about the live medium is not a no"
    assert usb.refuse_reason(found[0]) is not None


def test_a_mount_point_reported_the_new_way_is_still_a_mount_point() -> None:
    """util-linux moved to a MOUNTPOINTS list; both spellings are read."""
    payload = {
        "blockdevices": [
            {"name": "sda", "path": "/dev/sda", "size": STICK_BYTES, "tran": "usb", "rm": True,
             "mountpoints": [None], "children": [
                 {"name": "sda1", "path": "/dev/sda1", "size": STICK_BYTES, "fstype": "vfat",
                  "mountpoints": ["/run/archiso/bootmnt"]}]},
        ]
    }
    found = usb.drives(runner=FakeRunner([("lsblk", payload)]), platform="linux")
    assert found[0].system
    assert found[0].mounted == ("/run/archiso/bootmnt",)


def test_a_serial_less_stick_swapped_for_another_of_the_same_size_is_caught() -> None:
    """Plenty of cheap sticks report no serial, and two of them are the same size.

    With the serial unknown on both, an identity check that compares only the
    serial and the size compares nothing at all, and the disk number — which is
    a position in a list that moves when hardware is unplugged — is enough to
    reach the format.
    """
    payload = windows_payload()
    payload["disks"][2]["serial"] = ""
    drive = next(
        item for item in usb.drives(runner=windows_runner(payload), platform="windows")
        if item.device_id == STICK_ID
    )
    assert drive.serial is None

    swapped = windows_payload()
    swapped["disks"][2] = {"number": 2, "model": "Kingston DataTraveler", "serial": "",
                           "size": float(STICK_BYTES), "bus": "USB", "boot": False, "system": False}
    runner = windows_runner(swapped)
    with pytest.raises(UsbError) as caught:
        usb.prepare(drive, label="ARCH_202601", confirm_device_id=STICK_ID,
                    runner=runner, platform="windows")
    message = str(caught.value)
    assert "not the drive hop was told to erase" in message
    assert "Kingston DataTraveler" in message and "SanDisk Ultra USB 3.0" in message
    assert "Nothing has been erased" in message
    assert not any("Clear-Disk" in call[-1] for call in runner.calls)


def test_a_bus_that_is_not_a_bus_never_reaches_the_format_script() -> None:
    """The last value pasted into the script that was not being checked.

    A serial hop cannot write into a script is dropped, because drives without
    one are ordinary. The bus is one of the two checks left standing when that
    happens, so it is refused rather than dropped.
    """
    odd = Drive(STICK_ID, 2, "SanDisk", "4C530001", STICK_BYTES,
                "USB'; Clear-Disk -Number 0 -RemoveData -Confirm:$false; '", True, False)
    with pytest.raises(UsbError) as caught:
        usb._windows_prepare_script(odd, "ARCH_202601")
    assert "bus type" in str(caught.value)
    assert "format script" in str(caught.value), "the bus never goes near the bootstrap script"


def test_a_refusal_from_the_format_script_arrives_before_on_erase() -> None:
    """on_erase is where the caller stops being able to say "nothing was lost".

    ``Get-Disk`` reports bus types with spaces in them — 'Fibre Channel', 'File
    Backed Virtual', 'Storage Spaces' — and hop drops rather than escapes a
    value it cannot paste into a script. That refusal used to arrive after the
    callback, so ``hop go`` printed "The stick was erased before this failed.
    Everything that was on it is gone" about a drive no command had touched.
    """
    payload = windows_payload()
    payload["disks"][2]["bus"] = "Fibre Channel"
    payload["media"][2]["media"] = "Removable Media"
    runner = windows_runner(payload)
    drive = next(
        item
        for item in usb.drives(runner=runner, platform="windows")
        if item.device_id == STICK_ID
    )
    assert drive.removable and not drive.system, "the drive is one hop would otherwise erase"

    erased: list[bool] = []
    with pytest.raises(UsbError) as caught:
        usb.prepare(drive, label="ARCH_202601", confirm_device_id=STICK_ID,
                    runner=runner, platform="windows", on_erase=lambda: erased.append(True))

    assert "bus type" in str(caught.value)
    assert erased == [], "nothing may be reported as erased before a command has run"
    assert not any("Clear-Disk" in call[-1] for call in runner.calls)


def test_a_mount_that_is_not_a_drive_letter_is_not_pasted_into_a_script() -> None:
    """The mount is the one value reaching a script that hop did not write.

    It comes back from the format as whatever Windows named the new volume, and
    ``eject`` takes it from the caller. Whatever arrives, one letter is what
    reaches the script.
    """
    runner = FakeRunner([("Write-VolumeCache", {"flushed": True})])
    usb.eject("E: ; Clear-Disk -Number 0 -RemoveData", runner=runner, platform="windows")
    assert "Clear-Disk" not in runner.commands
    assert "-DriveLetter E\n" in runner.calls[0][-1]
    assert runner.calls[1] == ["mountvol", "E:", "/P"]

    # And a mount with no volume in it at all is a refusal, not a guess.
    bare = FakeRunner()
    with pytest.raises(UsbError) as caught:
        usb.eject("; Clear-Disk -Number 0", runner=bare, platform="windows")
    assert "not a drive letter" in str(caught.value)
    assert bare.calls == []

    # The shapes prepare() really returns still work, and a longer path is
    # reduced to the volume it sits on.
    assert usb._drive_letter("E:") == usb._drive_letter("E:\\") == "E"
    assert usb._drive_letter(r"E:\hop\iso-contents") == "E"


def test_on_erase_fires_once_after_the_last_check_and_before_the_first_command(
    tmp_path: Path,
) -> None:
    """The line a caller needs to know which side of it a failure happened on."""
    drive, listing = linux_stick()
    runner = FakeRunner([("lsblk", listing)], tmp_path=tmp_path)
    crossings: list[int] = []
    usb.prepare(drive, label="ARCH_202601", confirm_device_id="/dev/sdb", runner=runner,
                platform="linux", mount_root=tmp_path / "media",
                on_erase=lambda: crossings.append(len(runner.calls)))
    assert len(crossings) == 1
    ran = [call[0] for call in runner.calls[: crossings[0]]]
    assert ran == ["lsblk"], "it fired after the re-read and before anything destructive"

    # And a run that never gets past the checks never fires it.
    crossings.clear()
    with pytest.raises(UsbError):
        usb.prepare(drive, label="ARCH_202601", confirm_device_id="not the id", runner=runner,
                    platform="linux", mount_root=tmp_path / "media",
                    on_erase=lambda: crossings.append(0))
    assert crossings == []
