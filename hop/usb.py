"""Choosing a removable drive, and turning it into a bootable Arch installer.

This is the module that erases something somebody owns, so it is the one that
argues with its caller. Three things follow from that, and they are the whole
design:

* **Nothing here takes a boolean for consent.** ``prepare`` and ``write_medium``
  want ``confirm_device_id`` to be the exact device id of the drive they are
  being asked to erase. A "yes" can be typed by accident and a flag can be
  threaded through three functions and lose its meaning on the way; a device id
  says which drive, and it says it in the same words the caller would have to
  print to a person. What it does not do is prove a person was asked: a caller
  holding a :class:`Drive` can satisfy it from that object, and ``hop go``
  does. It is a check against a value going astray between functions, not a
  consent token, and the thing that actually stands between ``hop go`` and the
  wrong disk is further down this list — the drive rules, the re-read in
  :func:`_guard`, and the third check inside the elevated script itself.
* **Every refusal is a sentence.** Somebody about to lose a disk is owed an
  explanation naming the drive and the rule it broke, not an exception type.
  :func:`refuse_reason` exists so a caller can list every drive it found and say
  why each one was not offered.
* **Every command names the disk explicitly.** No cmdlet here defaults to "the
  current disk", takes a wildcard, or accepts a disk number without first
  checking that the disk still carries the serial number and the size hop was
  told to expect. Disk numbers are assigned in the order Windows saw the
  hardware, and they move when a drive is unplugged and plugged back in.

The medium is a FAT32 filesystem with the ISO's files copied onto it, not a raw
image written to the block device. That is the manual method the Arch wiki
documents, and it is a deliberate choice: a raw write needs ``\\\\.\\PhysicalDrive``
access and a wrong drive number there destroys the wrong disk with no filesystem
layer in between. Copying files means ordinary file APIs do all the work, the
same filesystem has room for the hopfile and the payload next to the installer,
and UEFI boots it through ``/EFI/BOOT/BOOTx64.EFI`` with no bootloader to
install. Two costs come with that choice and neither is hidden:

* the stick is **UEFI-only**. Without a syslinux install there is nothing for a
  legacy BIOS machine to boot. :func:`firmware_refusal` turns
  ``plan.system["firmware"]`` into the refusal to show rather than handing
  somebody a stick that does not boot;
* FAT32 cannot hold a file of 4 GB or more. :func:`write_medium` checks the
  whole tree for that before it formats anything, not halfway through the copy.

Nothing in this module inspects hardware without going through an injected
runner. That is not only so the tests can feed it canned output — it is the only
way this code can be tested at all, because the alternative is running it
against somebody's disks.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import human_bytes

__all__ = [
    "BOOTSTRAP_NAME",
    "FAT32_MAX_FILE",
    "HOP_DIR",
    "LARGE_DRIVE_BYTES",
    "Drive",
    "Runner",
    "UsbError",
    "Volume",
    "add_autostart",
    "bootstrap_script",
    "candidates",
    "drives",
    "eject",
    "firmware_refusal",
    "prepare",
    "refuse_reason",
    "write_medium",
]

#: Above this, hop wants a second word from the caller. A 2 TB USB disk is
#: removable, and it is far more likely to be somebody's only backup of the
#: photographs they are about to stop being able to boot into than it is their
#: install stick. The threshold is in binary gigabytes, which puts a drive sold
#: as "512 GB" (about 477 GiB of real capacity) just under it and a 1 TB drive
#: well over: the sizes people actually buy fall on the right sides of the line.
LARGE_DRIVE_BYTES = 512 * 1024**3

#: FAT32 stores a file length in 32 bits, so 4 GiB minus one byte is the largest
#: file that can exist on the medium. archiso's squashfs is the file that will
#: cross this line first if it ever does.
FAT32_MAX_FILE = 4 * 1024**3 - 1

#: Windows' own formatter refuses to make a FAT32 filesystem larger than 32 GB,
#: so on a bigger stick hop makes a partition of this size and leaves the rest of
#: the drive unpartitioned. That space is not lost — it is reclaimable with one
#: pass of the disk manager later — and the alternative is Format-Volume failing
#: after Clear-Disk has already emptied the drive. mkfs.vfat has no such limit,
#: so the Linux path uses the whole disk.
WINDOWS_FAT32_MAX_PARTITION = 32_000_000_000

#: Taken off the explicit ``-Size`` on a drive too big for one FAT32 partition.
#: ``Get-Disk``'s ``Size`` is the whole disk, and a partition can never be that:
#: the partition table sits at the front and Windows aligns the first partition
#: at one mebibyte, so the largest free extent is always smaller than the disk.
#: ``New-Partition -Size`` does not clamp — it fails outright when the size does
#: not fit — and it fails *after* ``Clear-Disk`` has emptied the drive, which
#: makes this arithmetic the difference between a stick and an erased stick.
#: Where the whole drive is wanted the script says ``-UseMaximumSize`` and does
#: no arithmetic at all; this only covers the ceiling case, where a number has
#: to be named and the band between the ceiling and the ceiling plus alignment
#: would otherwise be unformattable.
WINDOWS_PARTITION_SLACK = 16 * 1024**2

#: Slack on top of the bytes actually being copied: the partition table, the FAT
#: itself, and the difference between a file's length and the clusters it takes
#: up. Generous rather than exact, because the failure it prevents happens with
#: the drive already erased.
HEADROOM_BYTES = 128 * 1024**2

#: Where the baggage goes on the medium: one directory, so that everything hop
#: put on the stick can be told apart from everything archiso put there.
HOP_DIR = "hop"

BOOTSTRAP_NAME = "bootstrap.sh"

#: Where the Linux path mounts the medium it has just made. Overridable for the
#: same reason ``Lander`` takes a ``home``: the alternative is a test that mounts
#: a real filesystem on the machine running the suite.
MOUNT_ROOT = "/run/media/hop"

#: The boot menu entry hop adds. The archiso entries it was copied from are left
#: exactly as they are, so the same stick still boots a plain Arch installer.
HOP_ENTRY_NAME = "hop.conf"
HOP_ENTRY_TITLE = "Arch Linux (hop — starts the guided install)"

#: The file UEFI actually boots off a removable FAT32 filesystem. If the
#: extracted ISO does not contain it, the copy will produce a stick that the
#: firmware skips silently, and "it just boots Windows again" is a miserable
#: thing to debug.
UEFI_BOOT_FILE = "efi/boot/bootx64.efi"

#: A FAT32 volume label: eleven characters, and hop keeps to the set archiso
#: itself uses. The label is not cosmetic. The kernel command line carries
#: ``archisolabel=<label>``, and that is how the live system finds the
#: filesystem holding its own root: get it wrong and the boot ends at an
#: emergency shell complaining it cannot find the squashfs.
_LABEL = re.compile(r"[A-Z0-9_-]{1,11}")

#: Values that may be pasted into a generated PowerShell script. Same reasoning
#: as ``hop/archinstall.py``: everything that reaches there arrives from
#: somewhere else, and a serial number is not a place to find out that quoting
#: was harder than it looked. Anything outside this set is dropped rather than
#: escaped — see :func:`_windows_prepare_script`.
_PLAIN = re.compile(r"[A-Za-z0-9._-]{1,64}")

#: Mount points that mean "this drive is running the machine you are sitting at".
#: ``/run/archiso`` is on the list because the live environment's own boot medium
#: is removable, is a USB stick, and is the one drive that must never be erased
#: while it is being read from.
_SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/efi")

WINDOWS = "windows"
LINUX = "linux"

#: Never a shell, never a string, and no double quote anywhere in the script —
#: see :func:`_powershell`.
_POWERSHELL = (
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
)


class UsbError(Exception):
    """The drive cannot safely be used. Raised before anything has been changed."""


#: Runs an argv list and returns ``(returncode, stdout, stderr)``. The same shape
#: ``hop/iso.py`` takes, so that ``hop go`` can hand one runner to both. Every
#: function in this module that looks at hardware takes one, and the default is
#: the only implementation in hop that talks to the real machine.
Runner = Callable[[list[str]], tuple[int, str, str]]


@dataclass(frozen=True)
class _Result:
    """What one command said, with names on it."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class Volume:
    """One filesystem on a drive, as the operating system describes it.

    ``letter`` is a Windows idea and is ``None`` everywhere else; a Linux volume
    is identified by its label and its filesystem here, and by its mount point
    only inasmuch as the mount point decides whether the drive it sits on counts
    as :attr:`Drive.system`. A partition with no filesystem on it still appears,
    with ``filesystem`` and ``label`` as ``None``, because "this drive has three
    partitions on it" is something the reader should be able to see.
    """

    letter: str | None
    label: str | None
    filesystem: str | None
    size_bytes: int

    @property
    def describe(self) -> str:
        """The volume in a few words, for the line the reader matches against."""
        head = f"{self.letter}:" if self.letter else (self.label or "unnamed")
        inner = [part for part in (self.label if self.letter else None, self.filesystem) if part]
        inner.append(human_bytes(self.size_bytes))
        return f"{head} ({', '.join(inner)})"


@dataclass(frozen=True)
class Drive:
    """One physical drive, as the operating system describes it right now.

    ``device_id`` is the identity everything else in this module is keyed on:
    ``\\\\.\\PHYSICALDRIVE2`` on Windows, ``/dev/sdb`` on Linux. ``number`` is the
    disk number the Windows commands take; on Linux it is only the position in
    the enumeration and means nothing to any command.

    ``system`` is true when the drive carries an operating system — the running
    one, or a Windows installation found on any of its volumes — **and whenever
    hop could not work out the answer**. The two mistakes are not the same size:
    refusing a drive that would have been fine costs somebody a minute, and
    erasing the drive with their operating system on it costs them the machine.

    ``mounted`` is every place a filesystem on this drive is currently reachable
    from: mount points on Linux, drive letters on Windows. A drive with anything
    in it is a drive something is reading from right now, which is a different
    question from whether it holds an operating system and is asked separately
    by ``hop/install.py``.
    """

    device_id: str
    number: int
    model: str
    serial: str | None
    size_bytes: int
    bus: str
    removable: bool
    system: bool
    volumes: tuple[Volume, ...] = ()
    mounted: tuple[str, ...] = ()

    @property
    def describe(self) -> str:
        """One line a human can match against the thing in their hand."""
        bits = [
            self.device_id,
            self.model or "unnamed drive",
            human_bytes(self.size_bytes),
            self.bus or "unknown bus",
            "removable" if self.removable else "fixed",
        ]
        if self.serial:
            bits.append(f"serial {self.serial}")
        if self.volumes:
            shown = ", ".join(volume.describe for volume in self.volumes[:3])
            if len(self.volumes) > 3:
                shown += f", and {len(self.volumes) - 3} more"
            bits.append(shown)
        else:
            bits.append("no filesystem on it")
        return "  ".join(bits)


# --- what is out there ----------------------------------------------------


def drives(*, runner: Runner | None = None, platform: str | None = None) -> list[Drive]:
    """Every drive this machine can see, including the ones hop would refuse.

    The refusals are the point of returning everything: a caller that lists only
    the usable drives leaves somebody staring at a menu their stick is not in,
    with no way to find out why. Pair this with :func:`refuse_reason`.
    """
    system = _platform(platform)
    if system == WINDOWS:
        payload = _json(_run(runner, _powershell(_WINDOWS_ENUMERATE)), "the drive list")
        return _windows_drives(payload)
    payload = _json(_run(runner, list(_LSBLK)), "the drive list")
    return _linux_drives(payload)


def candidates(
    *,
    runner: Runner | None = None,
    platform: str | None = None,
    allow_large: bool = False,
) -> list[Drive]:
    """Only the drives that may safely be erased, in the order they were found."""
    return [
        drive
        for drive in drives(runner=runner, platform=platform)
        if refuse_reason(drive, allow_large=allow_large) is None
    ]


def refuse_reason(drive: Drive, *, allow_large: bool = False) -> str | None:
    """Why hop will not erase this drive, or ``None`` when it would.

    The order is worst first: a caller that shows one line per drive shows the
    most alarming true thing about it.
    """
    name = _name(drive)
    if drive.system:
        volumes = ", ".join(volume.describe for volume in drive.volumes) or "no readable volumes"
        return (
            f"{name} carries an operating system — the one running now, or a Windows "
            f"installation hop found on it ({volumes}). hop never erases it. If hop has this "
            "wrong, it is because it could not tell, and it treats not being able to tell as a "
            "yes: the cost of being wrong the other way is the machine you are sitting at."
        )
    if not drive.removable:
        return (
            f"{name} is a fixed drive on the {drive.bus} bus, not a removable one. hop only "
            "erases removable drives, so that a mistyped disk number cannot reach the disks "
            "that stay in the machine."
        )
    if drive.size_bytes <= 0:
        return (
            f"{name} reports a size of {drive.size_bytes} bytes, which usually means the drive "
            "is a card reader with no card in it, or that the disk has gone away since it was "
            "listed. hop will not act on a drive it cannot measure."
        )
    if drive.size_bytes > LARGE_DRIVE_BYTES and not allow_large:
        return (
            f"{name} is {human_bytes(drive.size_bytes)}, which is larger than the "
            f"{human_bytes(LARGE_DRIVE_BYTES)} hop will erase without being told twice. A drive "
            "this size is removable, but it is far more likely to be a backup disk than an "
            "install stick — and the backup is the thing you will want on the evening this goes "
            "wrong. If it really is the stick, pass allow_large=True."
        )
    return None


def firmware_refusal(firmware: str | None) -> str | None:
    """Whether a stick made this way can boot the machine the plan describes.

    ``plan.system["firmware"]`` records ``"UEFI"``, ``"BIOS"`` or ``"unknown"``.
    Copying the ISO's files onto a FAT32 filesystem produces a medium with no
    legacy boot sector on it at all: UEFI finds ``/EFI/BOOT/BOOTx64.EFI`` by
    itself, and a BIOS machine finds nothing and moves on to the next boot
    device. Returns ``None`` when the medium will boot, and otherwise the
    paragraph to put in front of the reader instead of a stick that does not.
    """
    value = str(firmware or "unknown").strip()
    if value.upper() == "UEFI":
        return None
    if value.upper() == "BIOS":
        return (
            "This machine was scanned in legacy BIOS mode, and the installer stick hop builds is "
            "UEFI-only: it is the ISO's files copied onto a FAT32 filesystem, which has no boot "
            "sector for a BIOS to run. The stick would be skipped at boot and you would end up "
            "back in Windows wondering what happened, so hop stops here instead.\n\n"
            "Two ways forward. If the machine is old enough to be BIOS-only, write the ISO to a "
            "stick with a tool that installs syslinux — Rufus in 'MBR / BIOS' mode does it — and "
            "carry on with 'hop install' by hand from the live environment. If the machine is "
            "really UEFI and was booted in legacy or CSM mode, change that in the firmware "
            "settings first: it is also the mode Arch should be installed in, so this is worth "
            "fixing rather than working around."
        )
    return (
        f"hop could not tell whether this machine boots through UEFI or a legacy BIOS "
        f"(firmware was recorded as {value!r}). The stick it builds is UEFI-only — the ISO's "
        "files on a FAT32 filesystem, with no boot sector — so on a BIOS machine it would be "
        "skipped at boot without a word. hop will not hand you a stick it cannot say will boot.\n\n"
        "You can check in Windows: run msinfo32 and read the 'BIOS Mode' line. If it says UEFI, "
        "re-run the scan so the hopfile records it and this refusal goes away."
    )


# --- erasing and writing --------------------------------------------------


def prepare(
    drive: Drive,
    *,
    label: str,
    confirm_device_id: str,
    runner: Runner | None = None,
    platform: str | None = None,
    allow_large: bool = False,
    mount_root: str | Path | None = None,
    on_erase: Callable[[], None] | None = None,
) -> str:
    """Erase ``drive`` and put one FAT32 filesystem on it. Returns where it landed.

    ``"E:"`` on Windows, ``"/run/media/hop/<label>"`` on Linux. Everything on the
    drive is gone when this returns; there is no undo and no recovery step, and
    the checks that run first are the only thing between the caller and that.

    ``label`` becomes the FAT32 volume label and has to match the ISO's own
    label, because the kernel command line finds the live filesystem by it.

    ``mount_root`` is where the Linux path mounts what it made; Windows gives
    the volume a drive letter and there is nothing to choose. Needs
    Administrator on Windows and root on Linux, and both refusals arrive as a
    :class:`UsbError` naming what was missing rather than as an access-denied
    error from halfway through.

    ``on_erase`` is called once, after every check has passed and immediately
    before the first command that destroys anything. It exists so that a caller
    which fails later can say which side of that line it got to: "was the stick
    erased before this went wrong" is the question somebody asks about a stick
    they are holding, and guessing at the answer from an exception type gets it
    wrong in both directions.
    """
    system = _platform(platform)
    volume_label = _label(label)
    _guard(drive, confirm_device_id, runner=runner, platform=system, allow_large=allow_large)

    # Built before the callback, not after it. ``_windows_prepare_script`` and
    # ``_powershell`` can both still refuse — a bus name hop will not paste into
    # a script is a refusal, and ``Get-Disk`` has bus names with spaces in them —
    # and a refusal that arrives after ``on_erase`` makes the caller tell
    # somebody their stick was erased when nothing was run at all. That is the
    # same class of lie as the opposite one, and it is told to somebody holding
    # the stick in question.
    argv = _powershell(_windows_prepare_script(drive, volume_label)) if system == WINDOWS else []

    if on_erase is not None:
        on_erase()

    if system == WINDOWS:
        result = _run(runner, argv)
        payload = _json(result, f"the format of {drive.device_id}")
        letter = str(payload.get("letter") or "").strip().rstrip(":")
        if not letter:
            raise UsbError(
                f"{_name(drive)} was formatted, but Windows did not give the new volume a drive "
                "letter, so hop has nowhere to copy to. Open Disk Management, assign the volume "
                "a letter, and run this again — the drive is already erased and formatted, so "
                "nothing is lost by repeating it."
            )
        return f"{letter}:"

    mount_point = f"{str(mount_root or MOUNT_ROOT).rstrip('/')}/{volume_label}"
    partition = _partition_path(drive.device_id)
    for argv in (
        # --zap-all clears both the GPT and the MBR that may be shadowing it; a
        # stick that has been written with a hybrid ISO image before has both,
        # and leaving half of one behind is how firmware ends up booting a
        # partition table that no longer describes anything.
        ["sgdisk", "--zap-all", drive.device_id],
        ["sgdisk", "--new=1:0:0", "--typecode=1:ef00", drive.device_id],
        # The partition node does not appear the instant sgdisk returns, and
        # mkfs.vfat on a path that is not there yet is a confusing failure.
        ["udevadm", "settle"],
        ["mkfs.vfat", "-F", "32", "-n", volume_label, partition],
        ["mkdir", "-p", mount_point],
        ["mount", partition, mount_point],
    ):
        _checked(runner, argv, drive)
    return mount_point


def write_medium(
    drive: Drive,
    iso_dir: Path,
    baggage: Mapping[str, Path],
    *,
    label: str,
    confirm_device_id: str,
    runner: Runner | None = None,
    platform: str | None = None,
    allow_large: bool = False,
    mount_root: str | Path | None = None,
    on_erase: Callable[[], None] | None = None,
) -> Path:
    """Erase ``drive`` and build the installer on it. Returns the medium's root.

    ``iso_dir`` is the *extracted* contents of the Arch ISO — the ``arch/``,
    ``EFI/`` and ``loader/`` directories, copied as ordinary files. ``baggage``
    maps a destination path, relative to a ``hop/`` directory on the medium, to
    the file or directory to put there: the hopfile, the plan, the payload.

    The order matters and is the whole of the safety story. Everything that can
    refuse refuses before the drive is touched: the identity check, the drive
    rules, the 4 GB file limit, and whether what is being copied fits. Only then
    is anything erased. After the copy, every file's size is checked against its
    source, because a stick that truncates silently is the classic bad-USB
    failure and what it produces is a boot that hangs with nothing on screen to
    explain it.

    The volume is flushed but deliberately not dismounted: :func:`add_autostart`
    still has to write to it. Call :func:`eject` when everything is done, and
    tell the reader to wait for it before pulling the stick out.

    ``on_erase`` is passed through to :func:`prepare`; see it for why a caller
    wants to know exactly where the line was crossed.
    """
    system = _platform(platform)
    volume_label = _label(label)
    source = Path(iso_dir)

    # Order is deliberate: the drive rules first, so that a caller who passed the
    # wrong drive hears about the drive rather than about a file size.
    _guard(drive, confirm_device_id, runner=runner, platform=system, allow_large=allow_large)

    iso_files = _inventory(source, "the extracted ISO")
    if not iso_files:
        raise UsbError(
            f"{source} has no files in it, so there is no installer to copy. If the ISO was "
            "meant to be extracted there, the extraction produced nothing and the drive has "
            "not been touched."
        )
    if not any(entry.relative.lower() == UEFI_BOOT_FILE for entry in iso_files):
        raise UsbError(
            f"{source} does not contain {UEFI_BOOT_FILE}, which is the file the firmware boots "
            "off a removable FAT32 filesystem. Copying it would produce a stick that UEFI skips "
            "without a word, so hop stops before erasing anything. Check that the ISO was "
            "extracted whole."
        )

    baggage_files = _baggage_inventory(baggage)

    total = sum(entry.size for entry in iso_files) + sum(entry.size for entry, _ in baggage_files)
    usable = _usable_bytes(drive, system)
    if usable < total + HEADROOM_BYTES:
        raise UsbError(
            f"{_name(drive)} has {human_bytes(usable)} usable and the installer plus the "
            f"baggage comes to {human_bytes(total)}, which does not leave the "
            f"{human_bytes(HEADROOM_BYTES)} of slack a FAT32 filesystem needs for itself. "
            "Nothing has been erased. Use a larger stick, or run the scan again without the "
            "payload if the payload is what is filling it."
            + (
                f" (The drive is {human_bytes(drive.size_bytes)}, but Windows will not make a "
                f"FAT32 filesystem larger than {human_bytes(WINDOWS_FAT32_MAX_PARTITION)}, so "
                "that is all of it hop can use.)"
                if usable < drive.size_bytes
                else ""
            )
        )

    mount = prepare(
        drive,
        label=volume_label,
        confirm_device_id=confirm_device_id,
        runner=runner,
        platform=system,
        allow_large=allow_large,
        mount_root=mount_root,
        on_erase=on_erase,
    )
    medium = _medium_path(mount)

    _copy_tree(source, medium, iso_files)
    _verify(medium, iso_files, "the installer")

    hop_root = medium / HOP_DIR
    copied: list[_Entry] = []
    for entry, origin in baggage_files:
        destination = hop_root / entry.relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(origin, destination)
        copied.append(entry)
    _verify(hop_root, copied, "the files hop is carrying")

    _flush(runner, system, mount)
    return medium


def eject(mount: str, *, runner: Runner | None = None, platform: str | None = None) -> None:
    """Flush the medium and ask the system to let go of it.

    Call this once, after the last thing that writes to the stick. On Windows it
    flushes the volume's write cache and then takes the drive letter away, which
    is what "safely remove hardware" does and what makes it safe to unplug. On
    Linux it syncs and unmounts.

    A failure here is a :class:`UsbError` because the alternative — telling
    somebody a stick is finished when the last megabyte may still be in a cache —
    produces a boot that stops with a corrupt-filesystem message an hour later,
    on a machine that no longer has Windows on it to look the message up with.
    """
    system = _platform(platform)
    _flush(runner, system, mount)
    if system == WINDOWS:
        letter = _drive_letter(mount)
        # mountvol /P dismounts the volume and removes its mount point, which is
        # the documented way to do this without a GUI. It is a plain program with
        # plain arguments, so it does not need a shell or a PowerShell script.
        result = _run(runner, ["mountvol", f"{letter}:", "/P"])
        if result.returncode != 0:
            raise UsbError(
                f"Everything was copied and checked, but {letter}: could not be dismounted "
                f"({_tail(result)}). The data is on the stick. Use 'Safely Remove Hardware' in "
                "the notification area before you unplug it, or leave it in until the machine "
                "reboots."
            )
        return
    result = _run(runner, ["umount", mount])
    if result.returncode != 0:
        raise UsbError(
            f"Everything was copied and checked, but {mount} could not be unmounted "
            f"({_tail(result)}). The data is on the stick. Run 'sync' and unmount it by hand "
            "before you pull it out."
        )


# --- making the live environment start hop by itself ----------------------


def add_autostart(medium: Path, *, script_relative: str) -> list[Path]:
    """Add a boot menu entry that runs a script once the live system is up.

    archiso reads a ``script=`` kernel parameter and runs what it points at after
    the live environment has started. This copies the default systemd-boot entry
    on the medium to a new one with that parameter added, points ``loader.conf``
    at the copy, and returns the files it changed.

    **The original entries are left exactly as they were.** A stick built by hop
    still boots a plain Arch installer from the same menu, and the entry hop adds
    says so in its title, so somebody who does not want to be automated can pick
    the other line. The menu timeout is raised if it was zero or missing, because
    a hidden menu is not a choice.

    ``script=`` is an archiso feature rather than a kernel one. If a future
    archiso stops honouring it, nothing here breaks except the automation: the
    stick still boots, the files are all still on it, and the script can be run
    by hand from the live shell. The bootstrap script prints that command itself
    every time it starts, so the way out is on screen at the moment it is needed
    rather than in a document on a machine that no longer exists.
    """
    root = Path(medium)
    entries_dir = root / "loader" / "entries"
    if not entries_dir.is_dir():
        raise UsbError(
            f"{entries_dir} is not there, so this medium has no systemd-boot entries to edit. "
            "Either the ISO was not copied whole, or archiso has changed how it boots. The "
            "stick itself is untouched by this call; if it boots, you can still run the hop "
            f"script by hand from the live shell: /run/archiso/bootmnt/{HOP_DIR}/{BOOTSTRAP_NAME}"
        )

    parameter = "script=/" + str(script_relative).replace("\\", "/").lstrip("/")
    loader_conf = root / "loader" / "loader.conf"
    source = _default_entry(entries_dir, loader_conf)
    body = _read_text(source)

    lines: list[str] = []
    patched = False
    for line in body.splitlines():
        if line.strip().lower().startswith("title"):
            lines.append(f"title   {HOP_ENTRY_TITLE}")
            continue
        if line.strip().lower().startswith("options"):
            if parameter in line:
                lines.append(line)
            else:
                lines.append(line.rstrip() + " " + parameter)
            patched = True
            continue
        lines.append(line)

    if not patched:
        raise UsbError(
            f"{source.name} has no 'options' line, so it is not an archiso boot entry and there "
            "is nothing for hop to add a script= parameter to. Nothing on the medium has been "
            "changed."
        )
    if not any(line.lower().startswith("title") for line in lines):
        lines.insert(0, f"title   {HOP_ENTRY_TITLE}")

    entry = entries_dir / HOP_ENTRY_NAME
    _write_text(entry, "\n".join(lines) + "\n")
    changed = [entry]

    if loader_conf.exists():
        _write_text(loader_conf, _patch_loader_conf(_read_text(loader_conf)))
        changed.append(loader_conf)
    else:
        _write_text(loader_conf, f"default {HOP_ENTRY_NAME}\ntimeout 15\n")
        changed.append(loader_conf)
    return changed


def bootstrap_script(*, label: str, command: Sequence[str] = ("install",)) -> str:
    """The script the live environment runs, as text. Write it into the baggage.

    It is deliberately the smallest thing that works: say where it is and how to
    run it by hand, put the copy of hop that is on the stick on the path, and
    hand over. Anything cleverer than this runs before the person watching has
    any way to interrupt it.
    """
    label = _label(label)
    argv = " ".join(_plain(str(part), "hop argument") for part in command)
    return f"""#!/usr/bin/env bash
#
# hop bootstrap — runs inside the Arch live environment.
#
# The boot entry hop added to this stick passes 'script=' to the kernel, and
# archiso runs this file once the live system is up. If that ever stops working
# the stick still boots a normal Arch installer and this script still runs when
# you start it yourself; the command is printed below every time.

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"

cat <<EOF

  hop — installing Arch onto this machine.

  This came off the stick labelled {label}. Nothing on any disk has been
  changed yet: hop shows you what it found and asks you to type the device
  path of the disk to erase, by hand, before anything happens.

  If you are reading this after starting it yourself, that is the supported
  way to run it. The command is:

      $here/{BOOTSTRAP_NAME}

  Press Ctrl+C now to stop and get a normal live shell.

EOF

# The copy of hop that travelled on the stick, rather than one installed in the
# live image: the live image has no hop in it, and there may be no network here.
if [ -d "$here/hop" ]; then
  export PYTHONPATH="$here${{PYTHONPATH:+:$PYTHONPATH}}"
fi

exec python3 -m hop {argv}
"""


# --- refusals, in one place -----------------------------------------------


def _guard(
    drive: Drive,
    confirm_device_id: str,
    *,
    runner: Runner | None,
    platform: str,
    allow_large: bool,
) -> None:
    """Every reason not to erase this drive, checked before anything is done."""
    if confirm_device_id != drive.device_id:
        raise UsbError(
            f"hop was asked to erase {drive.device_id} but the confirmation names "
            f"{confirm_device_id!r}, and those are not the same drive. This is not a formality: "
            "the confirmation has to be the device id itself, so that it cannot be a yes typed "
            "by reflex or a flag that picked up the wrong meaning on its way through three "
            f"functions. Pass confirm_device_id={drive.device_id!r} if that is really the drive.\n\n"
            f"  {drive.describe}"
        )

    reason = refuse_reason(drive, allow_large=allow_large)
    if reason is not None:
        raise UsbError(reason)

    # Read the hardware again rather than trusting the Drive that was passed in.
    # It was produced by an earlier enumeration, and between then and now the
    # reader has been asked a question and has had time to answer it — which is
    # time enough to plug something in.
    present = drives(runner=runner, platform=platform)
    match = next((item for item in present if item.device_id == drive.device_id), None)
    if match is None:
        raise UsbError(
            f"{_name(drive)} is not there any more. It was in the list hop was working from, "
            "and it is not in the list the machine gives now — the drive has been unplugged, or "
            "it has gone away by itself. Nothing has been erased. Plug it back in and start "
            "again, so that the drive you confirm is the drive that is actually connected."
        )
    fresh = refuse_reason(match, allow_large=allow_large)
    if fresh is not None:
        raise UsbError(
            f"{drive.device_id} is no longer a drive hop will erase. {fresh}\n\n"
            "Nothing has been erased."
        )
    # Every fact the enumeration carries about which physical object this is, not
    # only the serial: plenty of cheap sticks report no serial number at all, and
    # on those the check used to fall back to the size alone — which two sticks
    # of the same capacity share. Model and bus cost nothing to compare and are
    # the difference between catching a swap and not. Where the serial really is
    # unknown this is still weaker than hop would like, and there is no honest
    # way to make it stronger from a drive that will not say who it is; what
    # protects the disks that matter is that they are neither removable nor
    # non-system, and that is checked separately and does not depend on this.
    if (
        match.serial != drive.serial
        or match.size_bytes != drive.size_bytes
        or match.model != drive.model
        or match.bus != drive.bus
    ):
        raise UsbError(
            f"{drive.device_id} is not the drive hop was told to erase any more. It was "
            f"{drive.model or 'an unnamed drive'}, {human_bytes(drive.size_bytes)}, "
            f"{drive.bus} bus, serial {drive.serial or 'unknown'}; it is now "
            f"{match.model or 'an unnamed drive'}, {human_bytes(match.size_bytes)}, "
            f"{match.bus} bus, serial {match.serial or 'unknown'}. Disk numbers are handed out "
            "in the order the machine saw the hardware and they move when something is "
            "unplugged, which is exactly the mistake this check exists to catch. Nothing has "
            "been erased. List the drives again and confirm the one you mean."
        )


# --- enumeration: Windows -------------------------------------------------

#: Read through PowerShell and parsed from JSON, never scraped from the
#: formatted table output: that output is localised, column-aligned, and
#: truncated on narrow consoles, and a drive model cut off at 20 characters is
#: not something to pick a disk to erase from.
#:
#: Every enum-valued property is cast to a string inside the script. Windows
#: PowerShell serialises a BusType as the number behind it otherwise, and "7" is
#: not a bus a reader can check against the thing in their hand.
_WINDOWS_ENUMERATE = """
$ErrorActionPreference = 'Stop'
$disks = @(Get-Disk | Select-Object @{n='number';e={[int]$_.Number}}, @{n='model';e={[string]$_.FriendlyName}}, @{n='serial';e={[string]$_.SerialNumber}}, @{n='size';e={[double]$_.Size}}, @{n='bus';e={[string]$_.BusType}}, @{n='boot';e={[bool]$_.IsBoot}}, @{n='system';e={[bool]$_.IsSystem}})
try {
  $media = @(Get-CimInstance -ClassName Win32_DiskDrive | Select-Object @{n='number';e={[int]$_.Index}}, @{n='media';e={[string]$_.MediaType}})
} catch { $media = @() }
try {
  $partitions = @(Get-Partition | Select-Object @{n='number';e={[int]$_.DiskNumber}}, @{n='letter';e={[string]$_.DriveLetter}}, @{n='size';e={[double]$_.Size}})
} catch { $partitions = @() }
try {
  $volumes = @(Get-Volume | Select-Object @{n='letter';e={[string]$_.DriveLetter}}, @{n='label';e={[string]$_.FileSystemLabel}}, @{n='fs';e={[string]$_.FileSystem}}, @{n='size';e={[double]$_.Size}})
  $windows = @($volumes | Where-Object { $_.letter } | Where-Object { Test-Path -LiteralPath ($_.letter + ':\\Windows') -ErrorAction SilentlyContinue } | ForEach-Object { [string]$_.letter })
} catch { $volumes = @(); $windows = @() }
[pscustomobject]@{
  systemroot = [string]$env:SystemRoot
  disks = $disks
  media = $media
  partitions = $partitions
  volumes = $volumes
  windows = $windows
} | ConvertTo-Json -Depth 5 -Compress
"""


def _windows_drives(payload: Any) -> list[Drive]:
    if not isinstance(payload, dict):
        raise UsbError("the drive list came back in a shape hop does not understand")

    disks = _as_list(payload.get("disks"))
    partitions = _as_list(payload.get("partitions"))
    listed_volumes = _as_list(payload.get("volumes"))
    media = {
        _int(item.get("number"), -1): str(item.get("media") or "")
        for item in _as_list(payload.get("media"))
    }
    volumes = {
        letter: item for item in listed_volumes if (letter := _letter(item.get("letter")))
    }
    windows_letters = {
        letter for letter in (_letter(item) for item in _as_strings(payload.get("windows"))) if letter
    }
    system_letter = _letter(str(payload.get("systemroot") or "")[:1])

    by_disk: dict[int, list[dict]] = {}
    for item in partitions:
        by_disk.setdefault(_int(item.get("number"), -1), []).append(item)

    # If the partitions or the volumes did not come back there is no way to say
    # which disk holds C:, or which one has a Windows on it, and "cannot tell"
    # means "system" everywhere in this module. Same for a %SystemRoot% that did
    # not arrive. An empty list is the signal the script sends when one of those
    # queries failed, and no real machine has no volumes on it.
    blind = not partitions or not listed_volumes or system_letter is None

    out: list[Drive] = []
    for disk in disks:
        number = _int(disk.get("number"), -1)
        parts = by_disk.get(number, [])
        letters = {letter for letter in (_letter(p.get("letter")) for p in parts) if letter}

        knows_flags = "system" in disk and "boot" in disk
        system = (
            blind
            or not knows_flags
            or bool(disk.get("system"))
            or bool(disk.get("boot"))
            or (system_letter in letters)
            or bool(letters & windows_letters)
        )

        bus = str(disk.get("bus") or "").strip() or "unknown"
        media_type = media.get(number, "").lower()
        removable = bus.upper() == "USB" or "removable" in media_type or "external" in media_type

        drive_volumes: list[Volume] = []
        for part in parts:
            letter = _letter(part.get("letter"))
            volume = volumes.get(letter) if letter else None
            drive_volumes.append(
                Volume(
                    letter=letter,
                    label=_text(volume.get("label")) if volume else None,
                    filesystem=_text(volume.get("fs")) if volume else None,
                    size_bytes=_int((volume or part).get("size"), 0),
                )
            )

        out.append(
            Drive(
                # Built from the disk number rather than taken from Get-Disk's
                # Path, which is a device instance path nobody can match against
                # a drive: this is the spelling Win32_DiskDrive.DeviceID uses and
                # the one a person can be asked to type back.
                device_id=rf"\\.\PHYSICALDRIVE{number}",
                number=number,
                model=str(disk.get("model") or "").strip(),
                serial=_text(disk.get("serial")),
                size_bytes=_int(disk.get("size"), 0),
                bus=bus,
                removable=removable,
                system=system,
                volumes=tuple(drive_volumes),
                mounted=tuple(f"{letter}:" for letter in sorted(letters)),
            )
        )
    return sorted(out, key=lambda item: item.number)


def _windows_prepare_script(drive: Drive, label: str) -> str:
    """The format, as a script that checks the disk is still the right one.

    Every cmdlet here is given ``-DiskNumber``/``-Number`` explicitly; none of
    them is allowed to fall back to a pipeline, a wildcard or "the disk we were
    just talking about". Before any of them runs, the script re-reads the disk
    and compares the bus, the size and — when the serial number is a value that
    can be written into a script without quoting games — the serial. A disk
    number is a position in a list that changes when hardware is plugged in, and
    this is the check that turns a stale number into a refusal rather than into
    somebody else's disk.
    """
    number = int(drive.number)
    size = int(drive.size_bytes)
    serial = drive.serial if drive.serial and _PLAIN.fullmatch(drive.serial) else ""
    # The serial may be dropped, because plenty of drives have none and the size
    # and bus checks still run without it. The bus may not: it is one of the two
    # checks left, it arrives from the same place the serial does, and a bus name
    # that is not a plain word means hop has misread the enumeration rather than
    # that the drive is shy. Pasting it into the script unchecked is a single
    # quote away from putting another Clear-Disk in there.
    bus = _plain(drive.bus, "bus type", "the format script")

    serial_check = (
        f"""
if ($null -eq $disk.SerialNumber -or $disk.SerialNumber.Trim() -ne '{serial}') {{
  throw ('disk {number} has serial ' + $disk.SerialNumber + ', not {serial}; refusing to erase it')
}}"""
        if serial
        else """
# The drive reported no serial number, or one with characters that do not belong
# in a script. The size and bus checks above still have to pass.
"""
    )

    # -UseMaximumSize wherever the whole drive is wanted. Naming a number there
    # is how a stick gets erased and then not formatted: the number hop has is
    # the size of the disk, and no partition on a disk is ever the size of the
    # disk. Only the drive that is too big for one FAT32 filesystem needs a
    # number, and that one gets slack for the same reason.
    if size > WINDOWS_FAT32_MAX_PARTITION:
        extent = f"-Size {WINDOWS_FAT32_MAX_PARTITION - WINDOWS_PARTITION_SLACK}"
    else:
        extent = "-UseMaximumSize"

    return f"""
$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
  throw 'formatting a drive needs an elevated PowerShell; nothing has been changed'
}}
$disk = Get-Disk -Number {number}
if ($null -eq $disk) {{ throw 'there is no disk number {number} on this machine' }}
if ($disk.IsSystem -or $disk.IsBoot) {{ throw 'disk {number} carries the running system; refusing' }}
if ($disk.BusType -ne '{bus}') {{
  throw ('disk {number} is on the ' + [string]$disk.BusType + ' bus, not {bus}; refusing to erase it')
}}
if ([double]$disk.Size -ne {size}) {{
  throw ('disk {number} is ' + $disk.Size + ' bytes, not {size}; refusing to erase it')
}}{serial_check}
# A disk that has never been partitioned is already empty and Clear-Disk refuses
# it; that is not a reason to stop.
if ($disk.PartitionStyle -ne 'RAW') {{
  Clear-Disk -Number {number} -RemoveData -RemoveOEM -Confirm:$false
}}
$disk = Get-Disk -Number {number}
if ($disk.PartitionStyle -eq 'RAW') {{
  Initialize-Disk -Number {number} -PartitionStyle MBR
  $disk = Get-Disk -Number {number}
}}
# MBR rather than GPT, and the partition marked active where the layout has the
# idea at all: some firmware only looks for a boot file on a removable drive laid
# out the way removable drives have always been laid out.
if ($disk.PartitionStyle -eq 'MBR') {{
  $partition = New-Partition -DiskNumber {number} {extent} -IsActive -AssignDriveLetter
}} else {{
  $partition = New-Partition -DiskNumber {number} {extent} -AssignDriveLetter
}}
# Windows takes a moment to surface the new volume, and the drive letter is not
# always on the object New-Partition hands back. Ask the disk again.
Start-Sleep -Seconds 2
$partition = Get-Partition -DiskNumber {number} -PartitionNumber $partition.PartitionNumber
$letter = [string]$partition.DriveLetter
if (-not $letter) {{ throw 'the new partition on disk {number} was given no drive letter' }}
Format-Volume -DriveLetter $letter -FileSystem FAT32 -NewFileSystemLabel '{label}' -Confirm:$false | Out-Null
[pscustomobject]@{{ letter = $letter }} | ConvertTo-Json -Compress
"""


# --- enumeration: Linux ---------------------------------------------------

_LSBLK = (
    "lsblk",
    "-J",
    "-b",
    "-o",
    "NAME,PATH,MODEL,SERIAL,SIZE,TRAN,RM,MOUNTPOINT,LABEL,FSTYPE",
)

#: Names that are not drives anybody wants to erase: loop mounts, the optical
#: drive, RAM disks, device-mapper and md devices. lsblk reports them alongside
#: the real thing.
_NOT_A_DRIVE = ("loop", "sr", "ram", "zram", "dm-", "md")


def _linux_drives(payload: Any) -> list[Drive]:
    if not isinstance(payload, dict):
        raise UsbError("the drive list came back in a shape hop does not understand")

    nodes = _as_list(payload.get("blockdevices"))

    # On Linux the only thing that says "this drive is running the machine you
    # are sitting at" is where its filesystems are mounted, so a listing that
    # does not carry mount points has not answered that question — it has failed
    # to. lsblk is asked for the column by name and a running Linux always has
    # something mounted, so an answer with the field absent everywhere means the
    # tool is not the one hop thinks it is talking to. Left untreated that
    # marked every drive as free to erase, including the stick the live system
    # is being read from; "cannot tell" has to mean the same thing here as it
    # does in ``_windows_drives``, which is refuse.
    told = _mounts_reported(nodes)

    out: list[Drive] = []
    for index, node in enumerate(nodes):
        name = str(node.get("name") or "")
        if not name or name.startswith(_NOT_A_DRIVE):
            continue
        children = _as_list(node.get("children"))
        mounts = _mounts_of(node)
        system = not told or any(
            mount in _SYSTEM_MOUNTS or mount.startswith("/run/archiso") for mount in mounts
        )
        transport = str(node.get("tran") or "").strip()
        volumes = tuple(
            Volume(
                letter=None,
                label=_text(child.get("label")),
                filesystem=_text(child.get("fstype")),
                size_bytes=_int(child.get("size"), 0),
            )
            for child in (children or ([node] if node.get("fstype") else []))
        )
        out.append(
            Drive(
                device_id=str(node.get("path") or f"/dev/{name}"),
                # Only a position in the list. Nothing on Linux takes a disk
                # number; every command in this module takes the path.
                number=index,
                model=str(node.get("model") or "").strip(),
                serial=_text(node.get("serial")),
                size_bytes=_int(node.get("size"), 0),
                bus=transport.upper() or "unknown",
                removable=_truthy(node.get("rm")) or transport.lower() == "usb",
                system=system,
                volumes=volumes,
                mounted=tuple(mounts),
            )
        )
    return out


def _descend(nodes: Sequence[dict]) -> list[dict]:
    """Every node in an lsblk tree, at any depth, parents before children.

    lsblk nests as deep as the stack does, and the stacks people run are not
    shallow: a partition holding a LUKS container holding an LVM volume group
    puts the filesystem that is actually mounted three levels below the disk.
    Reading only the disk and its partitions there sees no mount point at all
    and reports the drive carrying the running root as free to erase.
    """
    out: list[dict] = []
    stack = [node for node in nodes if isinstance(node, dict)]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(_as_list(node.get("children")))
    return out


def _mounts_reported(nodes: Sequence[dict]) -> bool:
    """Did this lsblk answer carry mount points at all?

    The key being present and null is an answer — nothing is mounted there. The
    key being absent from every node in the listing is not: it means the column
    hop asked for is not in the output it got back.
    """
    return any(
        "mountpoint" in item or "mountpoints" in item for item in _descend(nodes)
    )


def _mounts_of(node: dict) -> list[str]:
    """Every place a filesystem on this drive is currently reachable from.

    ``mountpoints`` (plural, a list) is what recent util-linux prefers and
    ``mountpoint`` is what older versions write; both are read, because which
    one arrives depends on a version of lsblk hop does not choose. The whole
    subtree is read, not just the partitions — see :func:`_descend`.
    """
    found: list[str] = []
    for item in _descend([node]):
        single = item.get("mountpoint")
        if single:
            found.append(str(single))
        listed = item.get("mountpoints")
        if isinstance(listed, list):
            found.extend(str(entry) for entry in listed if entry)
    return sorted(set(found))


def _partition_path(device: str) -> str:
    """``/dev/sdb`` -> ``/dev/sdb1``; ``/dev/nvme0n1`` -> ``/dev/nvme0n1p1``."""
    return f"{device}p1" if device[-1:].isdigit() else f"{device}1"


# --- copying --------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """One file that is going to be copied, and the size it must arrive at."""

    relative: str
    size: int


def _inventory(root: Path, what: str) -> list[_Entry]:
    """Every regular file under ``root``, checked against what FAT32 can hold.

    The check happens here, before anything is erased, rather than when the copy
    reaches the offending file — which would be after the drive was emptied.
    """
    if not root.is_dir():
        raise UsbError(f"{root} is not a directory, so there is nothing to copy from it ({what})")

    out: list[_Entry] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise UsbError(
                f"{path} is not a regular file, and FAT32 cannot hold anything else. Nothing "
                "has been erased. This usually means the ISO was extracted with a tool that "
                "kept symbolic links; extract it again with one that does not."
            )
        size = path.stat().st_size
        if size > FAT32_MAX_FILE:
            raise UsbError(
                f"{path} is {human_bytes(size)}, and FAT32 cannot hold a file of 4 GB or more. "
                "Nothing has been erased. If this is archiso's squashfs, the release has "
                "outgrown the method hop uses to build a stick and hop needs fixing rather than "
                "working around — please open an issue with the ISO's date on it."
            )
        out.append(_Entry(path.relative_to(root).as_posix(), size))
    return out


def _baggage_inventory(baggage: Mapping[str, Path]) -> list[tuple[_Entry, Path]]:
    """The baggage flattened into files, with the destination each one lands at."""
    out: list[tuple[_Entry, Path]] = []
    for relative, origin in baggage.items():
        destination = str(relative).replace("\\", "/").strip()
        # The same reasoning as hop/land.py's payload check, one step earlier: a
        # path with a .. in it, a leading slash or a drive letter is not a place
        # inside the medium's hop directory, and quietly reinterpreting it as one
        # would put a file somewhere the caller did not ask for. The mapping is
        # built from a plan, and a plan is a file like any other.
        if (
            not destination
            or destination.startswith("/")
            or ".." in destination.split("/")
            or destination[1:2] == ":"
        ):
            raise UsbError(
                f"{relative!r} is not a path inside the medium's hop directory, so hop will not "
                "copy anything to it. Nothing has been erased."
            )
        destination = destination.rstrip("/")
        source = Path(origin)
        if source.is_dir():
            for entry in _inventory(source, f"the baggage at {destination}"):
                out.append(
                    (_Entry(f"{destination}/{entry.relative}", entry.size), source / entry.relative)
                )
        elif source.is_file():
            size = source.stat().st_size
            if size > FAT32_MAX_FILE:
                raise UsbError(
                    f"{source} is {human_bytes(size)} and FAT32 cannot hold a file of 4 GB or "
                    "more. Nothing has been erased."
                )
            out.append((_Entry(destination, size), source))
        else:
            raise UsbError(
                f"{source} is not there, so hop cannot carry it to the new machine. Nothing has "
                "been erased."
            )
    return out


def _copy_tree(source: Path, destination: Path, entries: Sequence[_Entry]) -> None:
    for entry in entries:
        target = destination / entry.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(source / entry.relative, target)


def _copy_file(source: Path, destination: Path) -> None:
    try:
        # copyfile rather than copy2: FAT32 has nowhere to put a mode, an owner
        # or a nanosecond timestamp, and asking it to take them turns a working
        # copy into a warning nobody reads.
        shutil.copyfile(source, destination)
    except OSError as exc:
        raise UsbError(
            f"could not write {destination}: {exc}. The drive has already been formatted, so "
            "there is nothing on it worth keeping — sort out what stopped the copy and run the "
            "same command again."
        ) from exc


def _verify(root: Path, entries: Sequence[_Entry], what: str) -> None:
    """Check every copied file's size against its source.

    A stick that accepts writes and silently truncates them is the classic bad
    USB failure, and what it produces is a machine that boots to a blinking
    cursor with nothing on screen to explain it. Reading the sizes back costs a
    second and turns that into a sentence.
    """
    wrong: list[str] = []
    for entry in entries:
        target = root / entry.relative
        try:
            landed = target.stat().st_size
        except OSError:
            wrong.append(f"{entry.relative} — did not arrive at all")
            continue
        if landed != entry.size:
            wrong.append(
                f"{entry.relative} — {human_bytes(landed)} on the stick, "
                f"{human_bytes(entry.size)} at the source"
            )
    if not wrong:
        return
    listed = "\n".join(f"  {line}" for line in wrong[:10])
    if len(wrong) > 10:
        listed += f"\n  and {len(wrong) - 10} more"
    raise UsbError(
        f"{len(wrong)} of the {len(entries)} files in {what} did not arrive on the stick at the "
        f"size they left at:\n{listed}\n\n"
        "Do not boot from this stick. A short copy is usually a failing stick or a bad USB port, "
        "not bad luck; try the other port first, then a different stick."
    )


def _flush(runner: Runner | None, platform: str, mount: str) -> None:
    """Get the bytes out of the cache and onto the medium."""
    if platform == WINDOWS:
        # The one value in this module that reaches a script without having been
        # written by hop: it comes back from the format as whatever Windows named
        # the new volume, and ``eject`` takes it from the caller. A drive letter
        # is one letter; anything else is not a mount point hop can flush.
        letter = _drive_letter(mount)
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "if (Get-Command Write-VolumeCache -ErrorAction SilentlyContinue) {\n"
            f"  Write-VolumeCache -DriveLetter {letter}\n"
            "  [pscustomobject]@{ flushed = $true } | ConvertTo-Json -Compress\n"
            "} else {\n"
            "  [pscustomobject]@{ flushed = $false } | ConvertTo-Json -Compress\n"
            "}\n"
        )
        payload = _json(_run(runner, _powershell(script)), f"the flush of {letter}:")
        if not payload.get("flushed"):
            raise UsbError(
                f"Everything was copied and checked, but this Windows has no Write-VolumeCache, "
                f"so hop cannot confirm that {letter}: has been written out rather than left in "
                "a cache. Use 'Safely Remove Hardware' in the notification area and wait for it "
                "to say the drive can be removed before you unplug it."
            )
        return
    result = _run(runner, ["sync"])
    if result.returncode != 0:
        raise UsbError(
            f"Everything was copied and checked, but 'sync' failed ({_tail(result)}), so hop "
            "cannot say the data has reached the stick. Do not unplug it yet."
        )


# --- the boot menu --------------------------------------------------------


def _default_entry(entries_dir: Path, loader_conf: Path) -> Path:
    """The entry to copy: what loader.conf points at, or the plainest archiso one."""
    if loader_conf.exists():
        for line in _read_text(loader_conf).splitlines():
            key, _, value = line.strip().partition(" ")
            if key.lower() == "default" and value.strip():
                named = entries_dir / value.strip()
                if named.exists() and named.name != HOP_ENTRY_NAME:
                    return named

    found = sorted(p for p in entries_dir.glob("*.conf") if p.name != HOP_ENTRY_NAME)
    if not found:
        raise UsbError(
            f"{entries_dir} has no boot entries in it, so there is nothing for hop to copy. The "
            "ISO was probably not extracted whole."
        )
    # archiso ships variants next to the ordinary entry — one that copies itself
    # to RAM, one with a speech synthesiser. The ordinary one is the one to
    # automate; the others stay in the menu untouched for whoever needs them.
    ranked = sorted(
        found,
        key=lambda path: (
            any(word in path.name.lower() for word in ("ram", "speech", "accessibility")),
            path.name,
        ),
    )
    return ranked[0]


def _patch_loader_conf(text: str) -> str:
    """Point the menu at hop's entry, and make sure the menu is visible.

    A timeout of zero hides the menu, and a hidden menu would take away the
    choice the other entries exist to offer.
    """
    lines: list[str] = []
    seen_default = False
    seen_timeout = False
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        lowered = key.lower()
        if lowered == "default":
            lines.append(f"default {HOP_ENTRY_NAME}")
            seen_default = True
        elif lowered == "timeout":
            seen_timeout = True
            try:
                timeout = int(value.strip())
            except ValueError:
                timeout = 0
            lines.append(f"timeout {max(timeout, 10)}")
        else:
            lines.append(line)
    if not seen_default:
        lines.insert(0, f"default {HOP_ENTRY_NAME}")
    if not seen_timeout:
        lines.append("timeout 15")
    return "\n".join(lines) + "\n"


# --- small internals ------------------------------------------------------


def _platform(name: str | None) -> str:
    if name is None:
        return WINDOWS if os.name == "nt" else LINUX
    value = str(name).strip().lower()
    if value in (WINDOWS, "nt", "win32"):
        return WINDOWS
    if value in (LINUX, "posix"):
        return LINUX
    raise UsbError(f"hop knows how to enumerate drives on Windows and on Linux, not on {name!r}")


def _run(runner: Runner | None, argv: Sequence[str]) -> _Result:
    code, out, err = (runner or _subprocess_runner)(list(argv))
    return _Result(tuple(argv), code, out or "", err or "")


def _subprocess_runner(argv: list[str]) -> tuple[int, str, str]:
    """The only thing in this module that talks to the real machine.

    A program that is not there comes back as 127 rather than an exception, and
    output is decoded with replacement: a Windows console in a Russian locale
    hands back bytes that are not UTF-8, and losing a character from a message
    is better than a traceback in the middle of a format. Same conventions as
    ``hop/iso.py``, because the same runner is expected to serve both.
    """
    try:
        done = subprocess.run(argv, check=False, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        return (127, "", str(exc))
    return (done.returncode, done.stdout or "", done.stderr or "")


def _powershell(script: str) -> list[str]:
    """A PowerShell invocation as argv, never a shell and never a string.

    The scripts in this module are written without a single double quote in them,
    and this refuses to run one that has any. The reason is unglamorous: the
    script arrives at powershell.exe through the Windows command line, where the
    double quote is the one character whose meaning is decided twice, and a
    module that erases drives is not the place to find out that the quoting was
    subtler than it looked.
    """
    if '"' in script:
        raise UsbError(
            "internal error: a PowerShell script in hop/usb.py contains a double quote. "
            "Nothing has been run. Rewrite the script with single quotes."
        )
    return [*_POWERSHELL, script]


def _checked(runner: Runner | None, argv: Sequence[str], drive: Drive) -> _Result:
    result = _run(runner, argv)
    if result.returncode != 0:
        raise UsbError(
            f"{' '.join(argv)} exited {result.returncode} while preparing {_name(drive)}"
            f"{_tail(result, prefix=': ')}\n\n"
            "The drive is in whatever state that command left it in — most likely erased and "
            "unformatted, which is recoverable by running the same command again once whatever "
            "stopped it is sorted out. On Linux this all needs root."
        )
    return result


def _json(result: _Result, what: str) -> Any:
    if result.returncode != 0:
        raise UsbError(f"could not read {what}: the command exited {result.returncode}{_tail(result, prefix=' — ')}")
    text = (result.stdout or "").strip()
    if not text:
        raise UsbError(f"could not read {what}: the command printed nothing at all")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise UsbError(f"could not read {what}: its output is not the JSON hop expected ({exc})") from exc


def _as_list(value: Any) -> list[dict]:
    """Windows PowerShell writes a one-element array as a bare object. Cope.

    ``ConvertTo-Json`` on a list of one drive produces an object rather than an
    array of one, and a machine with a single disk in it is not an unusual
    machine. Treating that as "no disks" would refuse everything on exactly the
    laptops hop is for.
    """
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _as_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)] if value else []


def _int(value: Any, default: int = 0) -> int:
    """Sizes arrive as ints, as floats via ConvertTo-Json, or as strings."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _letter(value: Any) -> str | None:
    text = str(value or "").strip().rstrip(":").upper()
    return text[:1] if text[:1].isalpha() else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _name(drive: Drive) -> str:
    return f"{drive.device_id} ({drive.model or 'unnamed drive'}, {human_bytes(drive.size_bytes)})"


def _tail(result: _Result, prefix: str = "") -> str:
    for line in (result.stderr or result.stdout or "").splitlines():
        if line.strip():
            return prefix + line.strip()
    return ""


def _usable_bytes(drive: Drive, platform: str) -> int:
    """How much of the drive a FAT32 filesystem on it can hold, near enough.

    The partition table and the alignment in front of the first partition are
    not counted here, and do not need to be: :data:`HEADROOM_BYTES`, which the
    caller adds on top of this, is several times larger than either.
    """
    if platform == WINDOWS:
        return min(drive.size_bytes, WINDOWS_FAT32_MAX_PARTITION)
    return drive.size_bytes


def _medium_path(mount: str) -> Path:
    """``E:`` -> ``E:\\``. A bare drive letter means "wherever that drive is now"."""
    if len(mount) == 2 and mount[1] == ":":
        return Path(mount + "\\")
    return Path(mount)


def _label(label: str) -> str:
    text = str(label or "").strip().upper()
    if not _LABEL.fullmatch(text):
        raise UsbError(
            f"{label!r} is not a FAT32 volume label hop will use. It has to be one to eleven "
            "characters from A-Z, 0-9, underscore and hyphen, and it has to be the label the ISO "
            "expects — the kernel command line finds the live filesystem by that name, so a stick "
            "labelled anything else boots to an emergency shell."
        )
    return text


def _drive_letter(mount: str) -> str:
    """The volume to flush, as the one letter the cmdlets take.

    ``E:`` and ``E:\\`` are what :func:`prepare` returns; a longer path is
    reduced to the volume it sits on, which is the same thing the caller means.
    Whatever comes out is one letter or a refusal — this value is pasted into a
    PowerShell script, and it is the only one in this module that hop did not
    write itself.
    """
    text = str(mount or "").strip()
    bare = text.rstrip(":\\/")
    # ntpath rather than os.path so that the answer does not depend on which
    # machine the tests are running on.
    letter = bare if len(bare) == 1 else ntpath.splitdrive(text)[0].rstrip(":\\/")
    if len(letter) != 1 or not ("A" <= letter.upper() <= "Z"):
        raise UsbError(
            f"{mount!r} is not a drive letter, so hop does not know what to flush or dismount. "
            "The data may still be in a cache: use 'Safely Remove Hardware' in the notification "
            "area before you unplug the stick."
        )
    return letter.upper()


def _plain(value: str, kind: str, into: str = "the bootstrap script") -> str:
    """Check a value before it is written into a generated script.

    The same check, for the same reason, as ``hop/archinstall.py::_safe``.
    ``into`` names the file, because a message that names the wrong one sends
    whoever reads it looking in a place the value never went.
    """
    if not _PLAIN.fullmatch(value):
        raise UsbError(f"refusing to write {value!r} into {into}: not a usable {kind}")
    return value


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise UsbError(f"could not read {path}: {exc}") from exc


def _write_text(path: Path, body: str) -> None:
    try:
        # Unix line endings always: systemd-boot reads these files, and a shell
        # script with carriage returns in it is a file bash refuses in a way that
        # names the wrong problem.
        path.write_text(body, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise UsbError(f"could not write {path}: {exc}") from exc
