"""Installing Arch onto this machine, from inside the live environment.

``hop install`` is the far half of ``hop go``. The stick is in the machine, the
live system is up, and this is the command that erases the disk Windows is on.
It reads the drives that are present right now, shows the one it means to erase
and everything that is on it, requires that disk's device path to be typed by
hand, generates an ``archinstall`` configuration whose disk layout was computed
from that live reading, runs the installer, and puts the plan and the payload
into the new system so that ``hop land`` can finish the job after the first boot.

**Why a disk layout may be generated here and nowhere else.** ``hop/archinstall.py``
refuses to write a ``disk_config`` on principle, and it is right to: a hopfile is
a snapshot of a machine as it was, possibly weeks ago, and between the scan and
the install a drive can be added, a backup disk can be plugged in, and
``/dev/nvme0n1`` can mean something else entirely. Handing archinstall a
partitioning plan built from stale information is the one mistake this project
must never make. Nothing in this module reads the hopfile to decide what to
erase. The candidate list comes from ``lsblk``, run seconds earlier, on the
machine being installed; the layout is arithmetic over the size that reading
reported. A layout computed from live data cannot be stale, which is the whole
of the argument, and it is why the confirmation is a device path typed out in
full rather than a yes.

**What is irreversible, and where.** Everything up to the typed confirmation
changes nothing: reading disks, writing a configuration file into the live
system's own RAM disk, printing. After it, archinstall wipes the disk that was
named. There is no undo and no recovery step, and hop says so before it asks.

Every function that looks at hardware takes an injected runner, and the one
command that takes minutes — archinstall itself — goes through an injected
``execute``. That is not only for the tests: it is the only way code that erases
disks can be exercised anywhere other than on a machine it would erase.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import archinstall, usb
from .manifest import human_bytes
from .plan import Plan
from .report import render_summary

__all__ = [
    "STAGES",
    "InstallError",
    "InstallOptions",
    "Stage",
    "build_configuration",
    "build_disk_config",
    "choose_target",
    "describe_target",
    "run",
    "targets",
    "uefi_refusal",
    "windows_disks",
]

#: The stages, in order, with the heading each one prints. Same shape as
#: ``hop/go.py``: a caller that wants to describe the run without starting it
#: can read this.
STAGES: tuple[tuple[str, str], ...] = (
    ("plan", "the plan that came on the stick"),
    ("disks", "the disks that are here, and how this machine booted"),
    ("target", "the disk that will hold Arch"),
    ("config", "the answers archinstall will use"),
    ("install", "installing Arch"),
    ("land", "arranging for hop land after the first boot"),
)

#: Where archiso mounts the medium it booted from, and where ``hop go`` put
#: everything: the plan, the report, the payload, and a copy of hop itself.
MEDIUM_ROOT = Path("/run/archiso/bootmnt")

#: Where archinstall mounts the new system while it builds it.
TARGET_ROOT = Path("/mnt/archinstall")

#: How this machine says it booted. The directory exists only when the kernel
#: was started by UEFI firmware, which is the same question ``plan.system
#: ["firmware"]`` answers about the *old* machine — and, like the disk list,
#: this module prefers the live answer to the recorded one.
EFI_RUNTIME = Path("/sys/firmware/efi")

ARCHINSTALL = "archinstall"

PLAN_NAME = "hop-plan.json"
REPORT_NAME = "hop-report.md"
PAYLOAD_NAME = "payload"
PACKAGE_NAME = "hop"
POST_SCRIPT_NAME = "hop-post.sh"
CONFIG_NAME = "user_configuration.json"

#: Where ``hop go`` put the archinstall configuration on the medium. Named here
#: because ``hop/go.py`` names it there, and the two have to agree.
BAGGAGE_ARCHINSTALL = "archinstall"

#: Where the plan and the payload land inside the installed system, relative to
#: the home directory they land in.
LANDING_DIR = "hop"

#: The first partition starts here and the last one stops this far from the end:
#: one mebibyte of alignment at the front, and room at the back for the backup
#: GPT header, which is smaller than this and does not have to be measured
#: exactly to be left alone.
ALIGNMENT = 1024**2

#: The EFI system partition. Arch's own recommendation is 1 GiB, and the reason
#: is not decoration: the ESP holds the kernel and the initramfs on a
#: systemd-boot install, a fallback initramfs beside them, and often a second
#: kernel later. The 100 MB Windows leaves behind is the size people run out of
#: two months in, in the middle of an update.
EFI_BYTES = 1024**3

#: Below this, hop will not install: the base system, a desktop, and the
#: packages in a typical plan do not fit, and finding that out at 90% of the
#: package install is a bad way to spend an evening.
MINIMUM_TARGET_BYTES = 20 * 1024**3

#: Filesystems that mean Windows lived here. NTFS is the strong signal; the
#: labels are the weaker one, and are only consulted for drives that also have
#: an EFI system partition on them.
WINDOWS_FILESYSTEMS = ("ntfs", "ntfs3")
WINDOWS_LABELS = ("system reserved", "recovery", "winre", "windows", "os")

_WRAP = 74

#: Markers around the block hop appends to the user's shell profile, so that it
#: can be found again, written once, and deleted by anybody who reads it.
_PROFILE_START = "# >>> hop >>>"
_PROFILE_END = "# <<< hop <<<"


class InstallError(Exception):
    """``hop install`` cannot carry on. The message is the explanation."""


class _NotStarted(InstallError):
    """archinstall could not be launched at all, so it wrote nothing.

    Separate from every other install failure because the ending reads it. "The
    disk may have been partly written" is the right thing to say about a program
    that ran and stopped, and the wrong thing to say about one that was never
    there — a reader who believes it goes looking for a half-installed system on
    a machine whose Windows is still sitting where it was.
    """


class _Declined(InstallError):
    """The device path was not typed. A refusal by the user, not a failure."""

    def __init__(self, typed: str) -> None:
        super().__init__(
            "That is not the device path of the disk hop was about to erase"
            + (f" — you typed {typed!r}." if typed else ", and nothing was typed.")
            + " Nothing has been changed, and no disk in this machine has been touched. That is a "
            "complete answer: if you meant to stop, you have. If you meant to go on, run hop "
            "install again and type the path exactly as it is printed above."
        )


@dataclass
class InstallOptions:
    """Everything ``hop install`` needs that is not a fact about the machine.

    ``plan`` names the plan file; without it hop looks on the medium it booted
    from, which is where ``hop go`` put one. ``target`` names the disk to erase
    and exists for the machine hop cannot choose for — two Windows disks, or
    none it recognises. ``dry_run`` prints the whole run, including the
    configuration it would write and the command it would run, and installs
    nothing.
    """

    plan: Path | None = None
    target: str | None = None
    out_dir: Path = Path("hop-install")
    filesystem: str = "ext4"
    assume_yes: bool = False
    dry_run: bool = False


@dataclass
class Stage:
    """One stage of the run, and whether it finished."""

    name: str
    title: str
    done: bool = False


def run(
    options: InstallOptions,
    *,
    out: TextIO = sys.stdout,
    ask: Callable[[str], str] | None = None,
    runner: usb.Runner | None = None,
    execute: Callable[[Sequence[str]], int] | None = None,
    platform: str | None = None,
    efi_runtime: Path | None = None,
    target_root: Path | None = None,
) -> int:
    """Do the whole thing. Returns an exit code.

    ``0`` means Arch is installed. ``1`` means hop stopped because it was told
    to — the confirmation not typed, or Ctrl+C. ``2`` means hop refused, or
    could not finish.

    The keyword arguments after ``out`` are the seams. ``ask`` puts the one
    question, ``runner`` runs the commands hop reads an answer from, ``execute``
    runs archinstall, and ``platform``, ``efi_runtime`` and ``target_root`` say
    where the machine is to be found — which is how this can be exercised
    somewhere other than a machine it would erase.
    """
    return _Install(
        options,
        out=out,
        ask=ask,
        runner=runner,
        execute=execute,
        platform=platform,
        efi_runtime=efi_runtime,
        target_root=target_root,
    ).run()


# --- what is out there, and which of it is the target ----------------------


def targets(found: Sequence[usb.Drive]) -> list[usb.Drive]:
    """The drives hop would consider installing onto."""
    return [drive for drive in found if target_refusal(drive) is None]


def target_refusal(drive: usb.Drive) -> str | None:
    """Why hop will not install onto this drive, or ``None`` when it would.

    Everything the live system is running from is out, which covers the stick
    hop booted from: ``hop/usb.py`` marks a drive mounted under ``/run/archiso``
    as a system drive for exactly this reason, and it stays out however large it
    is. A 2 TB USB disk with an archiso on it is still the disk being read from.

    A drive with anything else mounted is out as well, and that is a separate
    rule rather than the same one. Somebody in a live environment who plugs in
    their backup disk and mounts it to copy one last file off has a drive that
    is not a system drive, is large enough, and may well have NTFS on it — every
    signal by which hop recognises the machine it is leaving. A filesystem that
    is mounted right now is a filesystem somebody is using right now, and that
    is reason enough not to offer it.
    """
    if drive.system:
        return "the live system is running from this one; hop will not erase it"
    if drive.mounted:
        return (
            "mounted at " + ", ".join(drive.mounted) + " — something is using this drive right "
            "now, so hop will not offer it. Unmount it first if it really is the one."
        )
    if drive.size_bytes < MINIMUM_TARGET_BYTES:
        return (
            f"{human_bytes(drive.size_bytes)}, and a base system, a desktop and the packages in "
            f"this plan need at least {human_bytes(MINIMUM_TARGET_BYTES)}"
        )
    return None


def windows_disks(found: Sequence[usb.Drive]) -> list[usb.Drive]:
    """The candidates that look like they hold the Windows this plan came from.

    Recognising Windows is what lets hop pick a disk without a menu: on the
    machine this was written for there is one, and it is the one being left
    behind. Where there is not exactly one, hop asks rather than guesses.
    """
    return [drive for drive in targets(found) if _looks_like_windows(drive)]


def choose_target(found: Sequence[usb.Drive], *, hint: str | None = None) -> usb.Drive:
    """The disk to erase, or a refusal that names every disk it could see.

    ``hint`` is ``--target``: an exact device path, checked against the drives
    that are here now rather than accepted on trust. Without it hop takes the
    single Windows disk if there is exactly one, and refuses otherwise — two
    disks with Windows on them is precisely the machine where guessing costs
    somebody the wrong operating system.
    """
    if hint:
        wanted = hint.strip()
        for drive in found:
            if drive.device_id == wanted:
                if drive.system:
                    raise InstallError(
                        f"{wanted} is the disk this live environment is running from — it is the "
                        "stick you booted, or it carries the root of the system reading this. hop "
                        "will not erase the ground it is standing on. Nothing has been changed."
                    )
                # --target is a name typed by somebody who means it, and it is
                # still checked against the same rules the automatic choice is:
                # naming a disk by hand is a way to say which one, not a way to
                # say the rules do not apply.
                refusal = target_refusal(drive)
                if refusal is not None:
                    raise InstallError(
                        f"hop will not install onto {wanted}: it is {refusal}. Nothing has been "
                        "changed.\n\n" + describe_target(drive, doomed=False)
                    )
                return drive
        raise InstallError(
            f"There is no disk called {wanted!r} in this machine. Device names are handed out in "
            "the order the kernel saw the hardware and they are not the names the same disks had "
            "on the machine that was scanned, which is why hop checks. What is actually here:\n\n"
            + _disk_list(found)
        )

    windows = windows_disks(found)
    if len(windows) == 1:
        return windows[0]

    if not windows:
        usable = targets(found)
        if not usable:
            raise InstallError(
                "hop cannot see a disk in this machine that it could install onto. Either the "
                "drive list came back empty — which usually means lsblk failed rather than that "
                "the machine has no disks — or everything here is smaller than "
                f"{human_bytes(MINIMUM_TARGET_BYTES)} or is the medium hop booted from. What it "
                "found:\n\n" + _disk_list(found)
            )
        raise InstallError(
            "hop cannot tell which of these disks held Windows, so it will not choose one to "
            "erase. Name it yourself, with the whole device path:\n\n"
            + _disk_list(found)
            + f"\n\n    hop install --target {usable[0].device_id}"
        )

    listed = "\n\n".join(describe_target(drive, doomed=False) for drive in windows)
    raise InstallError(
        "More than one disk in this machine has Windows on it, and hop will not choose between "
        f"them:\n\n{listed}\n\nOne of these is the machine you are leaving and one of them is "
        "something else, and only you know which. Name the one to erase with the whole device "
        f"path:\n\n    hop install --target {windows[0].device_id}"
    )


def describe_target(drive: usb.Drive, *, doomed: bool = True) -> str:
    """The disk and everything on it, as a block a person can check against.

    Not :attr:`hop.usb.Drive.describe`, which is one line and stops after three
    volumes. Everything on the disk that gets chosen is about to stop existing,
    so every partition on it is listed, named, and measured — the label
    somebody recognises is the one that stops them.

    ``doomed`` is false where the same block is used to list the disks that are
    merely present. Saying "all of which is lost" beside a disk hop is not going
    to touch is the kind of small lie that makes a reader stop believing the
    large truths in the same transcript.
    """
    lines = [f"  {drive.device_id}"]
    facts = [
        drive.model or "unnamed disk",
        human_bytes(drive.size_bytes),
        drive.bus or "unknown bus",
    ]
    if drive.serial:
        facts.append(f"serial {drive.serial}")
    lines.append("    " + "   ".join(facts))
    if drive.volumes:
        lines.append(
            "    what is on it now, all of which is lost:" if doomed else "    what is on it now:"
        )
        lines.extend(f"      {volume.describe}" for volume in drive.volumes)
    else:
        lines.append("    no filesystem hop can read on it")
    return "\n".join(lines)


# --- the layout ------------------------------------------------------------


def uefi_refusal(firmware: str | None) -> str | None:
    """Whether a system installed the way hop installs one would boot this machine.

    ``None`` when it would. The counterpart of :func:`hop.usb.firmware_refusal`,
    which asks the same question about the stick; this one asks it about the
    machine the stick was booted on, and takes its answer from that machine
    rather than from the plan.
    """
    if str(firmware or "").strip().upper() == "UEFI":
        return None
    return (
        f"This machine did not boot through UEFI (firmware reads as {firmware or 'unknown'}), and "
        "the layout hop writes is a GPT disk with an EFI system partition and systemd-boot on it, "
        "which a legacy BIOS cannot start. hop will not partition a disk for a system that will "
        "not then boot.\n\n"
        "If the machine really is UEFI, this usually means the stick was started from the "
        "firmware's legacy or CSM entry. Restart, open the boot menu, and pick the entry for the "
        "same stick that has UEFI in its name — the disk in this machine has not been touched. If "
        "the machine is genuinely BIOS-only, run archinstall by hand and choose GRUB with an MBR "
        "layout; the plan, the report and the payload are on the stick and 'hop land' will still "
        "finish the job afterwards."
    )


def build_disk_config(
    drive: usb.Drive, *, firmware: str, filesystem: str = "ext4"
) -> dict[str, Any]:
    """The partition layout for ``drive``, as archinstall's ``disk_config`` key.

    The whole device: a 1 GiB EFI system partition at the front and the root
    filesystem over everything else. Computed from ``drive.size_bytes`` as
    ``lsblk`` reported it a moment ago on this machine — see the module
    docstring for why that provenance is the entire justification for this
    function existing.

    There is no swap partition because the configuration asks for zram instead,
    which is what ``swap: true`` means to archinstall, and no data partition
    because there is nothing on this machine left to keep by the time it runs.
    """
    refusal = uefi_refusal(firmware)
    if refusal is not None:
        raise InstallError(refusal)
    if drive.size_bytes < MINIMUM_TARGET_BYTES:
        raise InstallError(
            f"{drive.device_id} is {human_bytes(drive.size_bytes)}, and hop wants at least "
            f"{human_bytes(MINIMUM_TARGET_BYTES)} for a base system, a desktop and the packages "
            "in this plan. Nothing has been changed."
        )

    root_start = ALIGNMENT + EFI_BYTES
    root_size = drive.size_bytes - root_start - ALIGNMENT
    return {
        "_comment": (
            "hop generated this layout on the machine being installed, from the disk list read "
            "at the moment of the install — never from the Windows scan, which describes a "
            "machine that may no longer be this one. Everything on "
            f"{drive.device_id} is erased when archinstall applies it. Flag spellings and the "
            "shape of 'size' move between archinstall releases; if the installer rejects a key, "
            "delete the whole disk_config and partition in its menus instead."
        ),
        "config_type": "manual_partitioning",
        "device_modifications": [
            {
                "device": drive.device_id,
                "wipe": True,
                "partitions": [
                    _partition(
                        mountpoint="/boot",
                        fs_type="fat32",
                        start=ALIGNMENT,
                        size=EFI_BYTES,
                        flags=["Boot", "ESP"],
                    ),
                    _partition(
                        mountpoint="/",
                        fs_type=filesystem,
                        start=root_start,
                        size=root_size,
                        flags=[],
                    ),
                ],
            }
        ],
    }


def build_configuration(
    plan: Plan, drive: usb.Drive, *, firmware: str, filesystem: str = "ext4"
) -> dict[str, Any]:
    """The archinstall answer file for this plan, with a disk layout in it.

    Everything except the layout comes from ``hop/archinstall.py`` unchanged.
    The note that module writes — that there is no ``disk_config`` and the
    installer will ask — is removed rather than left to contradict the file it
    is in.
    """
    config = archinstall.build_user_configuration(plan)
    block = config.get("_hop")
    notes = block.get("notes") if isinstance(block, dict) else None
    if isinstance(notes, list):
        kept = [note for note in notes if not str(note).startswith("No disk_config key")]
        kept.insert(
            0,
            f"disk_config was written by 'hop install' on the machine being installed. "
            f"{drive.device_id} ({drive.model or 'unnamed disk'}, "
            f"{human_bytes(drive.size_bytes)}) is wiped and repartitioned when this runs.",
        )
        block["notes"] = kept
    config["disk_config"] = build_disk_config(drive, firmware=firmware, filesystem=filesystem)
    return config


def _partition(
    *, mountpoint: str, fs_type: str, start: int, size: int, flags: list[str]
) -> dict[str, Any]:
    """One entry of ``device_modifications[].partitions``.

    Sizes are given in bytes rather than sectors on purpose: a sector is 512
    bytes on some of these disks and 4096 on others, and a layout that is right
    only on one of them is a layout that will one day be applied to the other.
    """
    return {
        "btrfs": [],
        "flags": flags,
        "fs_type": fs_type,
        "mount_options": [],
        "mountpoint": mountpoint,
        "obj_id": str(uuid.uuid4()),
        "size": {"sector_size": None, "unit": "B", "value": size},
        "start": {"sector_size": None, "unit": "B", "value": start},
        "status": "create",
        "type": "primary",
    }


# --- the run ---------------------------------------------------------------


class _Install:
    """The run, as one object, so each stage can see what the last one found."""

    def __init__(
        self,
        options: InstallOptions,
        *,
        out: TextIO,
        ask: Callable[[str], str] | None,
        runner: usb.Runner | None,
        execute: Callable[[Sequence[str]], int] | None,
        platform: str | None,
        efi_runtime: Path | None,
        target_root: Path | None,
    ) -> None:
        self.options = options
        self.out = out
        self.ask = ask
        self.runner = runner if runner is not None else _default_runner
        self.execute = execute if execute is not None else _stream
        self.platform = platform if platform is not None else _this_platform()
        self.efi_runtime = Path(efi_runtime) if efi_runtime is not None else EFI_RUNTIME
        self.target_root = Path(target_root) if target_root is not None else TARGET_ROOT
        self.stages = [Stage(name, title) for name, title in STAGES]
        self.out_dir = Path(options.out_dir)

        # What the ending reads to say what state the machine was left in.
        # Whether archinstall was started is the only honest way to answer "is
        # that disk half written": a stage that finished says nothing about a
        # command launched inside it that did not come back.
        self.started = False
        self.firmware = "unknown"
        # Whether the disk that was erased had a Windows on it. hop can be asked
        # to install onto a second disk, and on that machine the sentence
        # "Windows is gone from that disk" is simply untrue.
        self.erased_windows = False
        # Built in the plan stage rather than where it is written, so that a
        # plan hop cannot turn into a script is a refusal at stage one instead of
        # a refusal in stage four — which is after somebody has typed a device
        # path and is owed something better than an error about a locale.
        self.post_script = ""

    def run(self) -> int:
        try:
            self._refuse_windows()
            self._header()
            plan, hop_dir = self._plan()
            found = self._disks()
            drive = self._target(found)
            config_path = self._config(plan, drive)
            self._install(config_path)
            return self._land(plan, hop_dir)
        except _Declined as exc:
            # Not a failure: the machine is as it was, and being asked to stop
            # is the one outcome this command is built to make easy.
            return self._stopped(str(exc), code=1)
        except KeyboardInterrupt:
            self._say()
            return self._stopped("Stopped from the keyboard.", code=1)
        except (InstallError, usb.UsbError, ValueError, OSError) as exc:
            return self._stopped(str(exc), code=2)

    def _refuse_windows(self) -> None:
        """Before anything else, including the header.

        Somebody who typed this on the machine they have not wiped yet is owed
        one sentence, not a stage transcript ending in a complaint that there
        is no plan file where the Arch live environment keeps one.
        """
        if self.platform == usb.WINDOWS:
            raise InstallError(
                "hop install runs inside the Arch live environment, on the machine being "
                "installed, and this is Windows. If you have not booted the stick yet, that is "
                "the next thing to do; if you meant to build the stick, the verb is 'hop go'. "
                "Nothing has been changed."
            )

    def _header(self) -> None:
        self._say("hop install")
        self._say()
        self._wrapped(
            "This installs Arch onto a disk in this machine, and the disk it installs onto is "
            "erased. hop reads the disks that are here now — not the ones the scan found on the "
            "machine you left — shows you what is on the one it means to erase, and asks you to "
            "type that disk's device path in full before anything happens to it."
        )
        self._say()
        self._wrapped(
            "Until you type it, nothing on any disk in this machine has been changed and Ctrl+C "
            "leaves it that way."
        )
        self._say()

    # --- 1: the plan -------------------------------------------------------

    def _plan(self) -> tuple[Plan, Path | None]:
        self._begin("plan")

        path = self._plan_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise InstallError(
                f"{path} is not readable as JSON ({exc}). That file is what hop go wrote onto the "
                "stick; if it was edited by hand, the shortest way back is to copy it again from "
                "the stick. Nothing has been changed."
            ) from exc
        if not isinstance(raw, dict):
            raise InstallError(f"{path} does not contain a plan. Nothing has been changed.")
        plan = Plan.from_dict(raw)
        self.post_script = _post_script(plan, path)

        hop_dir = path.parent if path.parent.is_dir() else None
        self._say(f"  plan          {path}")
        host = plan.hopfile.get("hostname")
        if host:
            self._say(f"  scanned from  {host}")
        self._say()
        self._say(render_summary(plan))
        self._say()
        self._done("plan")
        return (plan, hop_dir)

    def _plan_path(self) -> Path:
        """The plan named on the command line, or the one that came on the stick."""
        if self.options.plan is not None:
            path = Path(self.options.plan)
            if not path.is_file():
                raise InstallError(f"There is no plan at {path}. Nothing has been changed.")
            return path

        candidates = [
            MEDIUM_ROOT / usb.HOP_DIR / PLAN_NAME,
            Path.cwd() / PLAN_NAME,
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        listed = "\n".join(f"    {candidate}" for candidate in candidates)
        raise InstallError(
            "hop install could not find a plan. It looked in:\n\n"
            f"{listed}\n\n"
            "The plan is the file hop go wrote onto the stick, beside this program. If the "
            "medium is mounted somewhere else, pass it: hop install --plan "
            "/path/to/hop-plan.json. Nothing has been changed."
        )

    # --- 2: the disks ------------------------------------------------------

    def _disks(self) -> list[usb.Drive]:
        self._begin("disks")

        found = usb.drives(runner=self.runner, platform=self.platform)
        for drive in found:
            self._say(describe_target(drive, doomed=False))
            if drive.system:
                self._say("    hop will not touch this one: the live system is running from it")
            self._say()

        # Asked here rather than where the layout is built, so that a machine
        # hop cannot install onto is refused before anybody is shown a disk and
        # asked to type its path. Nothing refusable should sit behind the one
        # irreversible question.
        self.firmware = self._firmware()
        self._say(f"  firmware      {self.firmware}, read from this machine, not from the plan")
        self._say()
        refusal = uefi_refusal(self.firmware)
        if refusal is not None:
            raise InstallError(refusal)

        self._done("disks")
        return found

    # --- 3: the disk that will hold Arch -----------------------------------

    def _target(self, found: Sequence[usb.Drive]) -> usb.Drive:
        self._begin("target")

        drive = choose_target(found, hint=self.options.target)
        self._say(describe_target(drive))
        self._say()
        self._wrapped(
            f"Everything on {drive.device_id} is erased: every partition, every file, the "
            "Windows installation and whatever was left in it. There is no undo, and no step "
            "after this one that puts any of it back. If anything on that disk has not been "
            "copied somewhere else, stop now — the machine is exactly as it was and Ctrl+C "
            "leaves it that way."
        )
        self._say()

        if self.options.assume_yes:
            self._wrapped(
                "assume_yes was set, so hop did not ask. Nobody typed that device path: this run "
                "was told to carry on before it started."
            )
            self._say()
        else:
            answer = self._ask_line(
                f"Type {drive.device_id} to erase it. Anything else stops here: "
            )
            if answer.strip() != drive.device_id:
                raise _Declined(answer.strip())
            self._say()

        drive = self._still_the_same_disk(drive)
        self.erased_windows = _looks_like_windows(drive)
        self._done("target")
        return drive

    def _still_the_same_disk(self, drive: usb.Drive) -> usb.Drive:
        """Read the disks again, and refuse if the path now means something else.

        The list this was chosen from was read before a person was shown a
        block of text and asked to type a device path, which is long enough to
        plug something in. ``/dev/sdb`` is a position in the order the kernel
        saw the hardware, not a name the disk carries: unplug one drive and
        attach another and the same three characters address a different
        object. ``hop/usb.py`` re-reads for the same reason before it formats a
        stick, and the disk being erased here is the larger of the two.

        This is also what keeps the module docstring's claim true. The layout is
        arithmetic over a size, and a size read before the question is exactly
        as stale as a size read on another machine last week if the disk behind
        the path has changed in between.
        """
        present = usb.drives(runner=self.runner, platform=self.platform)
        match = next((item for item in present if item.device_id == drive.device_id), None)
        if match is None:
            raise InstallError(
                f"{drive.device_id} is not in this machine any more. It was in the list hop read "
                "a moment ago and it is not in the one it reads now, so something has been "
                "unplugged or has gone away by itself. Nothing has been changed and no disk has "
                "been touched. Start hop install again, so that the path you type is the disk "
                "that is actually there."
            )
        if (
            match.serial != drive.serial
            or match.size_bytes != drive.size_bytes
            or match.model != drive.model
            or match.bus != drive.bus
        ):
            raise InstallError(
                f"{drive.device_id} does not name the disk it named a moment ago. It was "
                f"{drive.model or 'an unnamed disk'}, {human_bytes(drive.size_bytes)}, "
                f"{drive.bus} bus, serial {drive.serial or 'unknown'}; it is now "
                f"{match.model or 'an unnamed disk'}, {human_bytes(match.size_bytes)}, "
                f"{match.bus} bus, serial {match.serial or 'unknown'}. Device names are handed "
                "out in the order the kernel saw the hardware and they move when something is "
                "plugged in or pulled out. Nothing has been changed and no disk has been "
                "touched.\n\n" + describe_target(match, doomed=False)
            )
        refusal = target_refusal(match)
        if refusal is not None:
            raise InstallError(
                f"{drive.device_id} is no longer a disk hop will install onto: it is {refusal}. "
                "Nothing has been changed and no disk has been touched."
            )
        # The one read second is the one the layout is computed from, because it
        # is the one that describes this machine now.
        return match

    # --- 4: the configuration ----------------------------------------------

    def _config(self, plan: Plan, drive: usb.Drive) -> Path:
        self._begin("config")

        config = build_configuration(
            plan, drive, firmware=self.firmware, filesystem=self.options.filesystem
        )

        self.out_dir.mkdir(parents=True, exist_ok=True)
        config_path = self.out_dir / CONFIG_NAME
        _write(config_path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        self._say(f"  wrote         {config_path}")

        post_path = self.out_dir / POST_SCRIPT_NAME
        _write(post_path, self.post_script)
        self._say(f"  wrote         {post_path}")
        self._say()

        for line in _layout_lines(config):
            self._say(line)
        self._say()
        self._done("config")
        return config_path

    def _firmware(self) -> str:
        """How this machine booted, asked of this machine.

        ``plan.system["firmware"]`` records how the *scanned* machine booted,
        and this module does not use it for the same reason it does not use the
        recorded disk list: the answer that decides what gets written to a disk
        is taken from the machine the disk is in.
        """
        try:
            return "UEFI" if self.efi_runtime.is_dir() else "BIOS"
        except OSError:
            return "unknown"

    # --- 5: archinstall ----------------------------------------------------

    def _install(self, config_path: Path) -> None:
        self._begin("install")

        argv = [ARCHINSTALL, "--config", str(config_path)]
        self._say(f"  running       {' '.join(argv)}")
        self._say()
        self._wrapped(
            "archinstall opens with every answer in this plan already filled in, including the "
            "disk layout above. It asks for one thing hop does not carry: the password for your "
            "account. hop never stores a password, generates one, or writes a placeholder that "
            "looks like one, so that question is answered in front of you, in the installer, "
            "where you can see what you are typing."
        )
        self._say()
        # The same caveat the configuration file carries as a comment, said
        # here as well because this is the moment it matters and nobody reads
        # the file. archinstall's schema moves between releases and hop cannot
        # know which one is on this ISO.
        self._wrapped(
            "If archinstall rejects the disk layout — its configuration format changes between "
            "releases and hop cannot know which release is on this ISO — delete the disk_config "
            "key from the configuration and partition in archinstall's own menus, with the disk "
            "list on the screen in front of you. Everything else in the file still applies:"
        )
        self._say()
        self._say(f"    {config_path}")
        self._say()

        if self.options.dry_run:
            self._wrapped(
                "This is a dry run. archinstall has not been started and no disk has been "
                "touched. The configuration above is on disk and can be read; running the same "
                "command without --dry-run is what applies it."
            )
            self._say()
            self._done("install")
            return

        self.started = True
        try:
            code = self.execute(argv)
        except _NotStarted:
            # It never ran, so nothing it does was done. Put the flag back
            # before the ending reads it.
            self.started = False
            raise
        self._say()
        if code != 0:
            raise InstallError(
                f"archinstall exited {code}; its output is above. What state the disk is in "
                "depends on how far it got, and hop cannot tell that from an exit code — read "
                "the output, and if the install did not complete, run archinstall again with "
                f"the same configuration:\n\n    {ARCHINSTALL} --config {config_path}\n\n"
                "The plan and the payload are still on the stick, and 'hop land' will finish "
                "the job once a system boots."
            )
        self._done("install")

    # --- 6: the landing ----------------------------------------------------

    def _land(self, plan: Plan, hop_dir: Path | None) -> int:
        self._begin("land")

        if self.options.dry_run:
            self._wrapped(
                "Skipped, because nothing was installed. In a real run the plan, the report, the "
                "payload and a copy of hop are copied into the new system's home directory, and "
                "the first login runs 'hop land' to show what it would do."
            )
            self._say()
            self._done("land")
            return self._finished(landed=False)

        root = self.target_root
        if not root.is_dir():
            self._wrapped(
                f"The new system is not mounted at {root}, so hop could not put the plan into "
                "it. Nothing is lost: everything is still on the stick. Once the new system is "
                "up, mount the stick and run hop from it — the commands are printed at the end "
                "of this."
            )
            self._say()
            self._done("land")
            return self._finished(landed=False)

        home, user = self._home(plan, root)
        destination = home / LANDING_DIR
        carried = self._carry(hop_dir, destination)
        for name in carried:
            self._say(f"  copied        {name}")
        if not carried:
            self._wrapped(
                "hop found nothing on the stick to copy into the new system, which should not "
                "happen if this stick was built by hop go. The install itself is finished; the "
                "plan can be copied across by hand later."
            )
            self._say()
            self._done("land")
            return self._finished(landed=False)

        profile = self._arrange_first_login(home)
        if profile is not None:
            self._say(f"  wrote         {profile}")
        self._own(root, home, user)
        self._say()

        # Written out rather than built with pathlib: this is a path in the
        # system being installed, and pathlib would spell it the way the machine
        # hop is running on spells paths, which is not always the same thing.
        # What is named is what arrived — the list two lines above this one —
        # rather than what a stick built by hop go usually carries.
        self._wrapped(
            f"Everything listed above is in ~/{LANDING_DIR} in the new system."
            + (
                " The copy of hop that travelled on the stick is beside it, so it works there "
                "with no network."
                if PACKAGE_NAME in carried
                else ""
            )
        )
        self._say()
        # Said only where it was arranged. A transcript that describes a first
        # login which will not happen sends somebody to a shell prompt expecting
        # a summary, finding none, and concluding hop did nothing at all.
        if profile is not None:
            self._wrapped(
                "The first login runs 'hop land' and shows what it would do — it changes "
                "nothing until you run it again with --execute, and the block that starts it "
                "says how to delete itself."
            )
        else:
            self._wrapped(
                "hop could not write ~/.bash_profile in the new system, so nothing starts by "
                "itself after the first login. That is the only thing missing — everything else "
                "is in place — and the command to run by hand is at the end of this."
            )
        self._say()
        # The copy does not remove the original, and the original is on a FAT32
        # filesystem that nothing about this install has changed. Somebody who
        # carried private keys across should hear that once more, here, while
        # the stick is still in their hand.
        #
        # Gated on the payload having actually arrived, not on the plan listing
        # private entries. The plan describes what the scan found on the machine
        # being left; whether any of it is on this stick is a different question,
        # and "that payload includes files marked private" said over a payload
        # that never travelled is a sentence about nothing.
        if PAYLOAD_NAME in carried and _private_payload(plan):
            self._wrapped(
                "That payload includes files the scanner marked private — keys, Wi-Fi "
                "passwords. Copying them here did not take them off the stick, and the stick is "
                "FAT32, which has no permissions on it at all. Erase it once hop land has "
                "finished."
            )
            self._say()
        self._done("land")
        return self._finished(
            landed=True,
            first_login=profile is not None,
            post=POST_SCRIPT_NAME in carried,
        )

    def _home(self, plan: Plan, root: Path) -> tuple[Path, str | None]:
        """The home directory in the new system to put the plan into.

        The account archinstall was told to make, if it made one; root's home
        otherwise, which is where somebody who installed without a user account
        will be standing when they first log in.

        The name is checked against :data:`_USERNAME` before it becomes a path
        component. ``"../.."`` is a directory that exists on every machine, so
        an unchecked name here does not fail — it succeeds, somewhere else, and
        what it takes there is the payload.
        """
        user = str((plan.system or {}).get("username") or "").strip()
        if user and _USERNAME.fullmatch(user):
            candidate = root / "home" / user
            if candidate.is_dir():
                return (candidate, user)
        return (root / "root", None)

    def _carry(self, hop_dir: Path | None, destination: Path) -> list[str]:
        """Copy what came on the stick into the new system. Returns what landed."""
        if hop_dir is None or not hop_dir.is_dir():
            return []
        destination.mkdir(parents=True, exist_ok=True)
        carried: list[str] = []
        for name in (PLAN_NAME, REPORT_NAME, PAYLOAD_NAME, PACKAGE_NAME):
            source = hop_dir / name
            if not source.exists():
                continue
            target = destination / name
            try:
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copyfile(source, target)
            except OSError as exc:
                raise InstallError(
                    f"Arch is installed, but hop could not copy {source} into the new system "
                    f"({exc}). Everything is still on the stick: mount it after the first boot "
                    "and copy it across by hand."
                ) from exc
            carried.append(name)

        post = hop_dir / BAGGAGE_ARCHINSTALL / POST_SCRIPT_NAME
        if post.is_file():
            shutil.copyfile(post, destination / POST_SCRIPT_NAME)
            carried.append(POST_SCRIPT_NAME)
        return carried

    def _arrange_first_login(self, home: Path) -> Path | None:
        """Have the first login print what hop land would do. Returns the file.

        Not ``hop land --execute``. Landing installs packages and copies keys
        around, and a command that does that before anybody has typed anything
        is the opposite of everything else in this program. What runs is the dry
        run, which prints its transcript and changes nothing, and which prints
        the command that does.
        """
        profile = home / ".bash_profile"
        try:
            existing = profile.read_text(encoding="utf-8") if profile.is_file() else ""
        except OSError:
            return None
        if _PROFILE_START in existing:
            return profile

        block = f"""{_PROFILE_START}
# Added by hop install. Delete this block, or the whole file if you did not
# have one, and nothing here runs again.
if [ -f "$HOME/{LANDING_DIR}/{PLAN_NAME}" ] && [ ! -f "$HOME/{LANDING_DIR}/.landed" ]; then
    PYTHONPATH="$HOME/{LANDING_DIR}${{PYTHONPATH:+:$PYTHONPATH}}" \\
        python3 -m hop land "$HOME/{LANDING_DIR}/{PLAN_NAME}"
    echo
    echo "That was a dry run: nothing above has been done yet. To do it:"
    echo "    python3 -m hop land ~/{LANDING_DIR}/{PLAN_NAME} --execute"
    echo "Then: touch ~/{LANDING_DIR}/.landed  to stop this message."
fi
{_PROFILE_END}
"""
        body = existing
        if body and not body.endswith("\n"):
            body += "\n"
        try:
            _write(profile, body + block)
        except OSError:
            return None
        return profile

    def _own(self, root: Path, home: Path, user: str | None) -> None:
        """Give the copied files to the account that will read them.

        Everything hop writes here is written as root, from the live
        environment. Left that way, the first thing the new user meets is a
        directory in their home they cannot write to.
        """
        if user is None:
            return
        inside = "/" + str(home.relative_to(root)).replace("\\", "/")
        code, _, stderr = self.runner(
            ["arch-chroot", str(root), "chown", "-R", f"{user}:{user}", f"{inside}/{LANDING_DIR}"]
        )
        if code != 0:
            self._say(f"  note          {inside}/{LANDING_DIR} is still owned by root")
            self._wrapped(
                f"chown could not be run in the new system{_tail(stderr)}. After the first boot: "
                f"sudo chown -R {user}:{user} ~/{LANDING_DIR}"
            )

    # --- endings -----------------------------------------------------------

    def _finished(self, *, landed: bool, first_login: bool = False, post: bool = False) -> int:
        self._say(_rule("done"))
        self._say()
        if self.options.dry_run:
            self._wrapped(
                "Nothing was installed and no disk was touched: this was a dry run. The "
                "configuration hop would use is written out above, and the same command without "
                "--dry-run is what applies it."
            )
            self._say()
            return 0

        # "Windows is gone" only where there was a Windows to go. A second disk
        # installed onto leaves the first one exactly as it was, and telling
        # somebody their Windows is gone when it is still there is how a machine
        # gets wiped a second time, by hand, to finish a job already done.
        self._wrapped(
            "Arch is installed. Take the stick out and restart, and the machine boots into it."
            + (" Windows is gone from that disk." if self.erased_windows else "")
        )
        self._say()
        if landed:
            self._wrapped(
                "Log in as yourself. The plan is in your home directory"
                + (" and the first login shows what is left to do:" if first_login else ":")
            )
            self._say()
            if post:
                self._say(
                    f"    bash ~/{LANDING_DIR}/{POST_SCRIPT_NAME}     the AUR helper, flatpak, services"
                )
            self._say(f"    python3 -m hop land ~/{LANDING_DIR}/{PLAN_NAME} --execute")
        else:
            self._wrapped(
                "The plan and the payload are still on the stick, under /hop. After the first "
                "boot, plug it back in, mount it, and run:"
            )
            self._say()
            self._say(f"    python3 -m hop land /path/to/{PLAN_NAME}")
        self._say()
        return 0

    def _stopped(self, message: str, *, code: int) -> int:
        self._say()
        self._say(_rule("stopped"))
        self._say()
        self._paragraphs(message)
        self._say()
        for paragraph in self._aftermath():
            self._paragraphs(paragraph)
            self._say()
        return code

    def _aftermath(self) -> list[str]:
        """What state this machine was left in. Always said."""
        finished = {stage.name for stage in self.stages if stage.done}
        if "install" in finished:
            return [
                _fill(
                    "Arch is installed on this machine"
                    + (" and Windows is gone from that disk." if self.erased_windows else ".")
                    + " What failed was after the install, so this is not a reason to install "
                    "again: the plan and the payload are on the stick, under /hop, and 'hop "
                    "land' reads them from wherever they are."
                )
            ]
        if self.started:
            return [
                _fill(
                    "The disk may have been partly written: archinstall was started, and how far "
                    "it got is in the output above. Nothing else in this machine was touched. If "
                    "the install did not finish, running it again with the same configuration is "
                    "safe — it erases the same disk from the start."
                )
            ]
        return [_fill("No disk has been touched and nothing in this machine has been changed.")]

    # --- transcript --------------------------------------------------------

    def _begin(self, name: str) -> None:
        for index, stage in enumerate(self.stages, start=1):
            if stage.name == name:
                self._say(_rule(f"{index}/{len(self.stages)}  {stage.title}"))
                self._say()
                return
        raise InstallError(f"internal error: hop install has no stage called {name!r}")

    def _done(self, name: str) -> None:
        for stage in self.stages:
            if stage.name == name:
                stage.done = True
                return

    def _say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def _wrapped(self, text: str) -> None:
        # break_on_hyphens off. The hyphen in 'hop-report.md' or '--dry-run' is
        # not a place to break a line, and a filename split across two of them
        # is one nobody can copy, search for, or read back over the phone.
        for line in textwrap.wrap(text, _WRAP, break_on_hyphens=False):
            self._say(line)

    def _paragraphs(self, text: str) -> None:
        """A refusal, laid out: prose filled, blocks that are already laid out kept.

        The same rule ``hop/go.py`` states in ``_hang``. A block that arrives
        indented — a list of disks, a command to type — was laid out by whoever
        wrote it and filling it would turn a list into a paragraph. Everything
        else is one long sentence that a terminal would otherwise break
        wherever it happened to run out of columns, usually mid-path.
        """
        for index, block in enumerate(text.split("\n\n")):
            if index:
                self._say()
            if block.startswith((" ", "\t")):
                for line in block.splitlines():
                    self._say(line)
            else:
                self._wrapped(block)

    def _ask_line(self, question: str) -> str:
        """Put the question. A question nobody can hear is not a yes."""
        asker = self.ask if self.ask is not None else _prompt
        try:
            return asker(question)
        except (EOFError, OSError):
            self._say()
            self._wrapped(
                "There is nobody at the keyboard to answer that: stdin is closed. hop will not "
                "take silence for a device path. Run it from the live environment's own console."
            )
            return ""


# --- module-level helpers -------------------------------------------------


def _this_platform() -> str:
    return usb.WINDOWS if os.name == "nt" else usb.LINUX


def _post_script(plan: Plan, path: Path) -> str:
    """``hop-post.sh`` for this plan, built early so a bad plan refuses early.

    ``hop/archinstall.py`` checks every value it pastes into that script and
    raises rather than escaping. Building it here means a plan carrying
    something it will not write is refused in the first stage — before the disk
    list, before the device path is typed — rather than in the stage after the
    one irreversible question, where the only honest thing left to say is that
    hop stopped over a locale.
    """
    try:
        return archinstall.build_post_script(plan)
    except ValueError as exc:
        raise InstallError(
            f"{path} carries something hop will not write into a shell script: {exc} Nothing "
            "has been changed and no disk has been read. The plan is JSON and can be edited; "
            "a plan written by 'hop plan' does not reach this."
        ) from exc


#: An account name hop is willing to build a path out of. The same characters
#: ``hop/plan.py`` reduces a Windows account name to, and the check is here
#: because a plan is a JSON file: it arrives off a FAT32 stick, hop's own
#: documentation invites editing it, and ``--plan`` takes any file at all. What
#: it stops is not exotic — ``".."`` and ``"../../etc"`` are ordinary strings —
#: and what it stops them doing is steering where the payload lands, which is
#: where the private keys are.
_USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def _looks_like_windows(drive: usb.Drive) -> bool:
    """Whether this disk carries a Windows installation.

    An NTFS filesystem is the signal. The labels are a second opinion for the
    machine whose Windows partition is BitLockered — lsblk reports those as
    ``BitLocker`` rather than NTFS — and are only trusted on a disk that also
    has an EFI system partition, so that a plain NTFS-free data disk called
    "Recovery" does not get mistaken for the machine.
    """
    filesystems = {(volume.filesystem or "").lower() for volume in drive.volumes}
    if filesystems & set(WINDOWS_FILESYSTEMS):
        return True
    if "vfat" not in filesystems and "bitlocker" not in filesystems:
        return False
    labels = {(volume.label or "").strip().lower() for volume in drive.volumes}
    return bool(labels & set(WINDOWS_LABELS)) or "bitlocker" in filesystems


def _private_payload(plan: Plan) -> list[dict]:
    """The payload entries the scanner marked as private material.

    The same rule as ``hop/go.py``, which is where the reader was first told:
    ``mode`` of ``0600`` is what the scanner writes for a private key or an
    exported Wi-Fi password.
    """
    return [
        entry
        for entry in plan.payload
        if isinstance(entry, dict)
        and str(entry.get("mode") or "").strip().lstrip("0") in ("600", "400")
    ]


def _disk_list(found: Sequence[usb.Drive]) -> str:
    if not found:
        return "  nothing at all, which is not something lsblk normally says"
    blocks = []
    for drive in found:
        block = describe_target(drive, doomed=False)
        refusal = target_refusal(drive)
        if refusal is not None:
            block += f"\n      {refusal}"
        blocks.append(block)
    return "\n\n".join(blocks)


def _layout_lines(config: dict[str, Any]) -> list[str]:
    """The layout, as the lines the reader checks against the disk they typed."""
    disk = config.get("disk_config") or {}
    lines: list[str] = []
    for modification in disk.get("device_modifications") or []:
        device = modification.get("device", "the disk")
        lines.append(f"  layout        {device}, wiped, GPT")
        for partition in modification.get("partitions") or []:
            size = int((partition.get("size") or {}).get("value") or 0)
            lines.append(
                f"                {partition.get('mountpoint', '?'):<6} "
                f"{partition.get('fs_type', '?'):<6} {human_bytes(size)}"
            )
    return lines


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run one command. A list, never a shell, never ``check=True``.

    The same conventions as ``hop/usb.py`` and ``hop/go.py``: a program that is
    not installed comes back as 127 rather than an exception, and output is
    decoded with replacement rather than risking a decode error mid-run.
    """
    try:
        done = subprocess.run(argv, check=False, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        return (127, "", str(exc))
    return (done.returncode, done.stdout or "", done.stderr or "")


def _stream(argv: Sequence[str]) -> int:
    """Run archinstall with its output going straight to the terminal.

    The one command here that does not go through the runner, for the reason
    ``hop/go.py`` streams the scan: archinstall takes minutes, draws its own
    screen, and a captured run would show nothing until it was over. It is also
    the only command in this module that is allowed to be interactive — it asks
    for the account password.
    """
    try:
        return subprocess.run(list(argv), check=False).returncode
    except OSError as exc:
        raise _NotStarted(
            f"could not start {argv[0]}: {exc}. That program ships on the Arch install medium; "
            "if this is not that medium, install it with 'pacman -Sy archinstall'. No disk has "
            "been touched: nothing ran."
        ) from exc


def _write(path: Path, text: str) -> None:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    # Unix line endings: these files are read by bash and by archinstall on the
    # machine being installed, and bash will not run a script with carriage
    # returns in it.
    path.write_text(text, encoding="utf-8", newline="\n")


def _fill(text: str, width: int = _WRAP) -> str:
    return textwrap.fill(" ".join(text.split()), width, break_on_hyphens=False)


def _prompt(question: str) -> str:
    """The default ``ask``. Everything that calls it can be given another one."""
    return input(question)


def _rule(label: str) -> str:
    head = f"--- {label} "
    return head + "-" * max(3, _WRAP - len(head))


def _tail(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return f": {line.strip()}"
    return ""
