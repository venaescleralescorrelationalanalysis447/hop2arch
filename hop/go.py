"""One command, from a running Windows machine to an installer that starts itself.

``hop go`` is the verb that does the irreversible thing. It scans the machine,
plans the move, shows what will be lost, asks once, fetches and checks the Arch
image, erases a USB stick and builds the installer on it, arranges for the live
environment to run ``hop install`` by itself, and reboots. Everything before the
single confirmation is reversible; everything after it, on that stick, is not.

**Where the disk decision is made, and why that is allowed.** Nothing here
partitions anything. The stick is the only thing this module erases, and the
disk that will hold Arch is chosen on the other side, by ``hop install``, from
``lsblk`` output read at the moment of install. That is the whole answer to the
objection that kept hop away from partitioning: a hopfile is a snapshot of this
machine as it was, possibly days ago, and between the scan and the install a
drive can be added, a backup disk can be plugged in, and ``/dev/nvme0n1`` can
mean something else entirely. A layout computed from live data cannot be stale.
A layout computed here could be, so it is not computed here.

**One confirmation, and it has to earn its keep.** The single question this
module asks comes after the summary, the blockers, and the exact identity of the
stick about to be erased — model, size, serial. The blockers are printed in
front of the reader rather than left in a file: "you lose Premiere" is the fact
most likely to change somebody's mind, and it should not be buried in a report
they have not opened. ``assume_yes`` skips the question and says so in the
transcript, so a run nobody was asked about is visible in the paste.

**What survives a failure.** After the stick is written, a failure is not a
reason to start again: the installer and the plan are both on the stick, and the
way forward is to boot it and run ``hop install``. Every ending in this module,
including Ctrl+C, says what state the machine and the stick were left in.
Nothing here changes any disk in this machine at any point — the reboot arranges
a one-shot boot order and nothing more.

Every function that inspects hardware, and every command run for its answer
rather than for its output, goes through an injected runner. The one exception
is the scan, which is streamed rather than captured: it takes minutes, and
minutes of still screen read as a hang. See :func:`_stream_command`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, TextIO

from . import archinstall, iso, usb
from .manifest import HopfileError, Manifest, human_bytes
from .mapping import Database, DatabaseError
from .plan import Plan, Planner
from .report import render_markdown, render_summary

__all__ = ["STAGES", "GoError", "GoOptions", "Stage", "run"]

#: The stages, in order, with the heading each one prints. A caller that wants
#: to describe the run without starting it can read this.
STAGES: tuple[tuple[str, str], ...] = (
    ("preflight", "what this machine can and cannot do"),
    ("scan", "inventorying Windows"),
    ("plan", "what moves, and what does not"),
    ("confirm", "the one confirmation"),
    ("iso", "the Arch image"),
    ("medium", "erasing the stick and building the installer on it"),
    ("bootstrap", "making the live environment start hop by itself"),
    ("reboot", "restarting into the installer"),
)

#: Free space hop wants before it starts: the image, the copy of its contents
#: that goes onto the stick, and room to be wrong about both. Roughly three
#: times a 1.2 GB image, which is not tight and is not meant to be — running out
#: of disk halfway through an extraction is a slow way to learn arithmetic.
FREE_SPACE_BYTES = 4 * 1024**3

#: Where the scanner lives, relative to the repository root.
SCAN_SCRIPT = Path("windows") / "hop-scan.ps1"

#: Seconds between "hop is finished" and the machine restarting. Sixty is
#: deliberate: it is about as long as it takes to remember something you forgot
#: to back up, and ``shutdown /a`` is printed the moment the timer starts.
REBOOT_SECONDS = 60

#: The key that opens the one-time boot menu, by vendor. hop tries to set a
#: one-shot boot order itself; when the firmware ignores it — and some do — this
#: is the list the reader needs, on screen, before the machine restarts.
BOOT_MENU_KEYS: tuple[tuple[str, str], ...] = (
    ("Dell", "F12"),
    ("HP", "F9, or Esc first on some models"),
    ("Lenovo", "F12, or the small Novo button beside the power button"),
    ("Acer", "F12"),
    ("ASUS", "Esc, or F8 on older boards"),
    ("MSI", "F11"),
    ("Gigabyte", "F12"),
    ("Samsung", "Esc"),
    ("Toshiba", "F12"),
    ("Microsoft Surface", "hold volume-down while pressing power"),
)

#: Where the baggage lands on the medium, under the medium's ``hop/`` directory.
#: Named here rather than spelled out where they are used, because these are the
#: paths ``hop install`` and ``hop land`` go looking for on the other side.
BAGGAGE_HOPFILE = "hopfile.json"
BAGGAGE_PLAN = "hop-plan.json"
BAGGAGE_REPORT = "hop-report.md"
BAGGAGE_PAYLOAD = "payload"
BAGGAGE_PACKAGE = "hop"
BAGGAGE_ARCHINSTALL = "archinstall"

_WRAP = 74

#: Same invocation and same rule as ``hop/usb.py``: argv, never a shell, and not
#: one double quote in any script this module generates.
_POWERSHELL = (
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
)

#: Two facts about the machine hop is standing on, in one call: how it booted,
#: and whether this shell is allowed to format a drive.
#:
#: The firmware test is the scanner's own, in the same order, so that the two
#: agree: the environment variable Windows sets, then a registry key that exists
#: only on a machine which booted through UEFI and which is readable without
#: elevation. The scanner has two further ways to tell and hop defers to it —
#: see :meth:`_Go._build_plan`.
_PROBE = """$ErrorActionPreference = 'Stop'
$firmware = 'unknown'
$declared = $env:firmware_type
if ($declared -match 'UEFI') { $firmware = 'UEFI' }
if ($firmware -eq 'unknown' -and $declared -match 'Legacy|BIOS') { $firmware = 'BIOS' }
$key = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\SecureBoot\\State'
if ($firmware -eq 'unknown' -and (Test-Path -LiteralPath $key)) { $firmware = 'UEFI' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$administrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
[pscustomobject]@{ firmware = $firmware; administrator = $administrator } | ConvertTo-Json -Compress
"""

#: A bcdedit identifier: ``{fwbootmgr}``, ``{bootmgr}``, or a GUID in braces.
#: Read for the same reason the field names are not — see :func:`_firmware_entry`.
_BCD_IDENTIFIER = re.compile(r"\{[0-9a-fA-F-]{36}\}|\{[a-z]+\}")

#: bcdedit identifiers that are never a removable drive.
_BCD_RESERVED = ("{fwbootmgr}", "{bootmgr}", "{current}", "{default}", "{memdiag}")


class GoError(Exception):
    """``hop go`` cannot carry on. The message is the explanation for the user."""


class _Declined(Exception):
    """The user answered no to the second question, about an unsigned image."""


@dataclass
class GoOptions:
    """Everything ``hop go`` needs to know that is not a fact about the machine.

    ``hopfile`` skips the scan and uses the file you name, which is how to
    re-run after a failure without spending another eight minutes reading the
    registry. ``reboot=False`` stops once the stick is finished; that is a
    supported way to run this, not a debug flag, and it is what somebody who
    would rather restart on their own terms should use.
    """

    hopfile: Path | None = None
    out_dir: Path = Path("hop-out")
    device_id: str | None = None
    desktop: str = "plasma"
    aur_helper: str = "paru"
    prefer_flatpak: bool = False
    include_gaming: bool = True
    hostname: str | None = None
    with_secrets: bool = False
    assume_yes: bool = False
    reboot: bool = True
    keep_iso: bool = True


@dataclass
class Stage:
    """One stage of the run, and whether it finished.

    Kept after the run so that an ending can say what survived it. The stick
    being written is the line that matters: past it, the way forward is to boot
    the stick rather than to start again.
    """

    name: str
    title: str
    done: bool = False


def run(
    options: GoOptions,
    *,
    out: TextIO = sys.stdout,
    ask: Callable[[str], str] | None = None,
    runner: usb.Runner | None = None,
    platform: str | None = None,
) -> int:
    """Do the whole thing. Returns an exit code.

    ``0`` means the stick is finished. ``1`` means hop stopped because it was
    told to — the confirmation declined, or Ctrl+C — and says what was left
    behind. ``2`` means hop refused, or could not finish.

    ``ask`` is called with a question and returns what the user typed. ``runner``
    is handed to every command hop runs for its answer, and to ``hop/iso.py`` and
    ``hop/usb.py`` with it. ``platform`` exists for the same reason it does in
    ``hop/usb.py``: without it the Windows path cannot be exercised anywhere but
    Windows, and code that erases drives has to be testable somewhere other than
    the machine it would erase.
    """
    return _Go(options, out=out, ask=ask, runner=runner, platform=platform).run()


@dataclass
class _Ready:
    """What the preflight established, carried to the stages that need it."""

    drive: usb.Drive
    firmware: str


class _Go:
    """The run, as one object, so each stage can see what the last one found."""

    def __init__(
        self,
        options: GoOptions,
        *,
        out: TextIO,
        ask: Callable[[str], str] | None,
        runner: usb.Runner | None,
        platform: str | None,
    ) -> None:
        self.options = options
        self.out = out
        self.ask = ask
        self.runner = runner if runner is not None else _default_runner
        self.platform = platform if platform is not None else _this_platform()
        self.stages = [Stage(name, title) for name, title in STAGES]
        self.out_dir = Path(options.out_dir)

        # What the endings read to say what was left behind.
        self.plan_path: Path | None = None
        self.report_path: Path | None = None
        self.image: Path | None = None
        self.medium: Path | None = None
        # Resolved in the plan stage so that the confirmation can name it: the
        # reader is being asked about a stick that is going to carry the
        # contents of this directory, and "which directory" is part of that.
        self.payload_dir: Path | None = None
        # Set by hop/usb.py the moment it crosses from checking to erasing, so
        # that an ending can say which side of that line the run got to instead
        # of inferring it from which stage finished. Everything in the medium
        # stage before the erase can still refuse, and most of it does.
        self.erased = False
        self._last_progress = -1

    # --- the sequence ------------------------------------------------------

    def run(self) -> int:
        try:
            self._header()
            ready = self._preflight()
            hopfile = self._scan()
            plan = self._build_plan(hopfile, ready)
            if not self._confirm(plan, ready):
                return self._declined()
            image, label = self._image()
            medium = self._write_medium(plan, ready, hopfile, image, label)
            self._bootstrap(medium)
            return self._restart(ready)
        except _Declined:
            return self._declined()
        except KeyboardInterrupt:
            # Ctrl+C is an answer, not a crash, and it is owed the same account
            # of what state things were left in as anything else.
            self._say()
            return self._stopped("Stopped from the keyboard.", code=1)
        except (
            GoError,
            usb.UsbError,
            iso.IsoError,
            HopfileError,
            DatabaseError,
            ValueError,
            OSError,
        ) as exc:
            return self._stopped(str(exc), code=2)

    def _header(self) -> None:
        self._say("hop go")
        self._say()
        self._wrapped(
            "This ends with your Windows installation gone. Between here and there are "
            "eight stages and one question; everything before the question can be stopped "
            "with Ctrl+C and leaves this machine exactly as it is."
        )
        self._say()
        self._wrapped(
            "hop erases one USB stick. It touches no disk inside this machine. The disk that "
            "will hold Arch is chosen on the other side, in the installer, from the drive "
            "list as it is at that moment — and you will have to type that disk's device "
            "path by hand before anything happens to it."
        )
        self._say()

    # --- 1: preflight ------------------------------------------------------

    def _preflight(self) -> _Ready:
        """Everything that can refuse, asked at once, before anything is fetched.

        Reported together rather than one per run: somebody who is not an
        administrator, has no stick plugged in and has three gigabytes free
        wants to know there are three things to fix, not to discover them across
        three attempts spread over an afternoon.
        """
        self._begin("preflight")

        if self.platform != usb.WINDOWS:
            raise GoError(
                "hop go runs on the machine you are leaving, and that machine is Windows: it "
                "reads the registry through the scanner, formats the stick with Windows' own "
                "tools, and sets a one-shot boot order with bcdedit. There is nothing here for "
                "it to do — if you are already on Arch, the verb you want is 'hop land'."
            )

        problems: list[str] = []
        probe = self._probe()
        firmware = str(probe.get("firmware") or "unknown")

        if not probe.get("administrator"):
            problems.append(
                "This shell is not elevated. Erasing and formatting a drive needs "
                "Administrator, and hop would rather say so now than fail with an "
                "access-denied error after a 1.2 GB download. Close this window, right-click "
                "PowerShell, choose 'Run as administrator', and start again."
            )

        # BIOS is a definite answer and there is no point scanning for eight
        # minutes to hear it again. 'unknown' is not definite: the scanner has
        # two further ways to tell, one of which needs the elevation hop has
        # just checked for, so an unknown is deferred to the plan rather than
        # refused here.
        if firmware.upper() == "BIOS":
            refusal = usb.firmware_refusal(firmware)
            if refusal:
                problems.append(refusal)

        free = self._free_space()
        if free is not None and free < FREE_SPACE_BYTES:
            problems.append(
                f"{self.out_dir} has {human_bytes(free)} free, and hop wants "
                f"{human_bytes(FREE_SPACE_BYTES)}: the image is about 1.2 GB, and its contents "
                "are copied out of it before they go onto the stick. Empty something, or point "
                "-o at a drive with room on it."
            )

        drive: usb.Drive | None = None
        try:
            drive = self._pick_drive()
        except GoError as exc:
            problems.append(str(exc))

        if problems or drive is None:
            head = (
                "One thing stops hop before anything is touched:"
                if len(problems) == 1
                else f"{len(problems)} things stop hop before anything is touched:"
            )
            listed = "\n\n".join(
                f"{number}. {_hang(text)}" for number, text in enumerate(problems, start=1)
            )
            raise GoError(f"{head}\n\n{listed}")

        self._say(f"  firmware      {firmware}")
        self._say("  elevation     administrator")
        if free is not None:
            self._say(f"  free space    {human_bytes(free)} for {self.out_dir}")
        self._say(f"  stick         {drive.describe}")
        self._say()
        self._done("preflight")
        return _Ready(drive=drive, firmware=firmware)

    def _probe(self) -> dict:
        """Ask Windows how it booted and whether this shell can format a drive."""
        code, stdout, stderr = self._run([*_POWERSHELL, _no_quotes(_PROBE)])
        if code != 0:
            raise GoError(
                "hop could not ask Windows how it booted or whether this shell is elevated "
                f"(PowerShell exited {code}{_tail(stderr or stdout)}). Both answers decide "
                "whether the rest of this can work at all, so hop stops rather than guessing "
                "at them. Nothing has been changed."
            )
        try:
            payload = json.loads(stdout.strip() or "{}")
        except ValueError as exc:
            raise GoError(
                f"hop could not read what PowerShell said about this machine ({exc}). Nothing "
                "has been changed."
            ) from exc
        return payload if isinstance(payload, dict) else {}

    def _free_space(self) -> int | None:
        """Free bytes where the image will land, or None if that cannot be measured.

        Measured on the nearest directory that exists, because ``out_dir``
        usually does not yet.
        """
        probe = self.out_dir.resolve()
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return None

    def _pick_drive(self) -> usb.Drive:
        """The stick to erase, or a refusal naming every drive and its reason.

        Deliberately not a menu. A list with numbers beside it and a prompt
        underneath is the interface that erases the wrong disk: the number is
        cheap to type, it means nothing, and it changes when a drive is
        unplugged. Naming the drive with ``--device-id`` costs a copy and a
        paste, and cannot be done by reflex.
        """
        found = usb.drives(runner=self.runner, platform=self.platform)

        if self.options.device_id:
            wanted = self.options.device_id
            for drive in found:
                if drive.device_id.lower() == wanted.lower():
                    reason = usb.refuse_reason(drive)
                    if reason:
                        raise GoError(reason)
                    return drive
            raise GoError(
                f"No drive on this machine has the device id {wanted!r}. What hop can see:\n\n"
                + _drive_list(found)
            )

        usable = [drive for drive in found if usb.refuse_reason(drive) is None]
        if not usable:
            _no_candidate(found)
        if len(usable) > 1:
            listed = "\n".join(f"  {drive.describe}" for drive in usable)
            raise GoError(
                "More than one removable drive is plugged in, and hop will not choose between "
                f"them:\n\n{listed}\n\nUnplug the ones you want to keep, or name the one to "
                f"erase with --device-id {usable[0].device_id} — the whole id, as printed above."
            )
        return usable[0]

    # --- 2: scan -----------------------------------------------------------

    def _scan(self) -> Path:
        self._begin("scan")

        if self.options.hopfile is not None:
            path = Path(self.options.hopfile)
            if not path.is_file():
                raise GoError(f"There is no hopfile at {path}, so there is nothing to plan from.")
            self._wrapped(
                f"Skipped: you supplied {path}, so nothing on this machine is being read. If "
                "that file describes a different machine, or was written before you last "
                "installed something, the plan will describe that machine and not this one."
            )
            self._say()
            self._done("scan")
            return path

        script = _scanner_path()
        if script is None:
            raise GoError(
                "hop go could not find windows/hop-scan.ps1. It ships in the hop2arch "
                "checkout, beside the hop package, and it is the only thing that can read this "
                "machine. Run it yourself and pass the result:\n\n"
                "    powershell -NoProfile -ExecutionPolicy Bypass -File windows\\hop-scan.ps1\n"
                "    hop go --hopfile hopfile.json"
            )

        self.out_dir.mkdir(parents=True, exist_ok=True)
        hopfile = self.out_dir / "hopfile.json"
        payload = self.out_dir / "hop-payload"
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-OutFile",
            str(hopfile),
            "-PayloadDir",
            str(payload),
        ]
        if self.options.with_secrets:
            argv.append("-WithSecrets")

        self._wrapped(
            "The scanner reads this machine and writes a hopfile. It changes nothing and it "
            "sends nothing anywhere. On a full disk it takes several minutes, most of that "
            "measuring the size of your own folders."
        )
        if self.options.with_secrets:
            self._say()
            self._wrapped(
                "-WithSecrets was asked for, so your private SSH keys and your Wi-Fi passwords "
                "are copied into the payload, and the payload goes onto the stick. Treat that "
                "stick the way you treat ~/.ssh, because that is what will be on it."
            )
        self._say()

        code = _stream_command(argv, self.out)
        self._say()
        if code != 0:
            raise GoError(
                f"The scanner exited {code}; its output is above. Nothing has been changed on "
                "this machine — the scanner only reads — and nothing has been fetched or "
                "erased. Once the reason is sorted out, run hop go again."
            )
        if not hopfile.is_file():
            raise GoError(
                f"The scanner finished, but there is no hopfile at {hopfile}. Nothing has been "
                "changed. The output above says what it made of writing that file."
            )

        self._say(f"  wrote {hopfile}")
        if payload.is_dir():
            self._say(f"  wrote {payload}")
        self._say()
        self._done("scan")
        return hopfile

    # --- 3: plan -----------------------------------------------------------

    def _build_plan(self, hopfile: Path, ready: _Ready) -> Plan:
        self._begin("plan")

        manifest = Manifest.load(hopfile)
        database = Database.load()
        plan = Planner(
            manifest,
            database,
            desktop=self.options.desktop,
            prefer_flatpak=self.options.prefer_flatpak,
            aur_helper=self.options.aur_helper,
            include_gaming=self.options.include_gaming,
            hostname=self.options.hostname,
        ).build()

        # The preflight's probe has two ways to tell how the machine booted; the
        # scanner has four, one of them needing the elevation hop has already
        # insisted on. So an 'unknown' from the probe is answered here — still
        # long before anything is downloaded or erased.
        if ready.firmware.upper() not in ("UEFI", "BIOS"):
            refusal = usb.firmware_refusal(plan.system.get("firmware"))
            if refusal:
                raise GoError(refusal)

        self.out_dir.mkdir(parents=True, exist_ok=True)
        plan_path = self.out_dir / BAGGAGE_PLAN
        report_path = self.out_dir / BAGGAGE_REPORT
        _write(plan_path, json.dumps(plan.to_dict(), indent=2, ensure_ascii=False) + "\n")
        _write(report_path, render_markdown(plan))
        written = archinstall.write_config(plan, self.out_dir / BAGGAGE_ARCHINSTALL)

        self.plan_path = plan_path
        self.report_path = report_path
        self.payload_dir = self._payload_dir(plan, hopfile)
        for path in (plan_path, report_path, *written):
            self._say(f"  wrote {path}")
        self._say()
        self._done("plan")
        return plan

    # --- 4: the confirmation ----------------------------------------------

    def _confirm(self, plan: Plan, ready: _Ready) -> bool:
        """Show what is about to be lost, then ask once. False means stop."""
        self._begin("confirm")

        self._say(render_summary(plan))
        self._say()

        blockers = plan.blockers
        if blockers:
            self._wrapped(
                "These have no path across. They do not run on Linux, and hop has nothing to "
                "offer in their place:"
            )
            self._say()
            for item in blockers[:12]:
                self._say(f"  {item.source}")
                for line in textwrap.wrap(item.notes, _WRAP - 4):
                    self._say(f"    {line}")
            if len(blockers) > 12:
                self._say(f"  and {len(blockers) - 12} more, all of them in the report")
            self._say()

        self._say("If you say yes:")
        self._say()
        # What this used to say was that hop checks the signature before it uses
        # the image. It checks the checksum always and the signature only where
        # gpg is installed, which on a Windows machine it usually is not. A
        # promise made in the paragraph above the one confirmation is the last
        # place to be approximate about what is actually verified — including
        # about which of the three answers leads to a question and which to a
        # refusal, because they are not the same and reading them as the same is
        # how somebody says yes to a forged image.
        self._say("  1. hop downloads the Arch installer image, about 1.2 GB, and checks it")
        self._say("     against the checksum published beside it. It also checks Arch's own")
        self._say("     signature on the image, where gpg is installed here. If that check")
        self._say("     comes back bad, hop stops and does not ask. If it cannot be made at")
        self._say("     all — no gpg on this machine, no signature on the mirror — hop says")
        self._say("     exactly what went unanswered and asks you a second time before it")
        self._say("     builds anything from that image.")
        self._say()
        self._say("  2. This stick is erased and rebuilt as that installer:")
        self._say()
        self._say(f"       {ready.drive.describe}")
        self._say()
        self._say("     Everything on it is gone, and there is no undo. No disk inside this")
        self._say("     machine is touched by that step.")
        self._say()
        third = (
            "This machine restarts into the installer."
            if self.options.reboot
            else "The stick is finished and hop stops. You restart into it when you want to."
        )
        self._say(f"  3. {third}")
        for line in textwrap.wrap(
            "In the installer, hop reads the disks that are actually present at that moment "
            "and asks which one to erase for Arch. You will type that disk's device path by "
            "hand. Windows is still on this machine until you do.",
            _WRAP - 5,
        ):
            self._say(f"     {line}")
        self._say()

        # What goes onto the stick is every file in the payload directory, not
        # the subset of them the hopfile happens to describe: an entry written
        # without a ``mode``, or a file the scanner left there and did not
        # record, is copied all the same. So the warning is keyed on the
        # directory travelling, and the count of private entries is the extra
        # detail rather than the trigger.
        private = _private_payload(plan)
        listed = ", ".join(sorted({str(entry.get("kind") or "other") for entry in private}))
        if self.payload_dir is not None:
            self._wrapped(
                "Every file in this directory goes onto that stick, whether or not the hopfile "
                "lists it"
                + (
                    f" — including {len(private)} "
                    + ("file" if len(private) == 1 else "files")
                    + f" the scanner marked private: {listed}"
                    if private
                    else ""
                )
                + ":"
            )
            self._say()
            # On its own line rather than inside the paragraph: a path filled
            # into wrapped prose gets broken wherever the line runs out, and a
            # path broken in half is one nobody can copy or check.
            self._say(f"    {self.payload_dir}")
            self._say()
            self._wrapped(
                "The stick is FAT32, which has no file permissions at all: anybody who picks it "
                "up can read them, and they are still on it after Arch is installed. Keep it "
                "with you, and erase it once hop land has finished."
            )
            self._say()
        elif private:
            # The other direction, and worth as much: somebody who scanned with
            # --with-secrets and is now re-running from the hopfile expects
            # their keys to travel, and they are not going to.
            self._wrapped(
                f"The hopfile lists {len(private)} "
                + ("file" if len(private) == 1 else "files")
                + f" the scanner marked private — {listed} — but hop cannot find the payload "
                "directory holding them, so none of it is going onto the stick and none of it "
                "will reach the new machine. If you meant to carry them, put the payload "
                "directory back beside the hopfile and run this again."
            )
            self._say()

        data = plan.data or {}
        if data.get("total_bytes"):
            self._wrapped(
                "Your own files are no part of this. There is "
                f"{human_bytes(data['total_bytes'])} in your profile folders, and hop carries "
                "only the payload — keys, configuration, bookmarks, wallpaper. Everything else "
                "in them is on the disk the installer will erase. Copy it somewhere else first."
            )
            self._say()

        if self.report_path is not None:
            self._wrapped(
                f"Read {self.report_path} before you answer. It is the long version of the "
                "summary above, worst news first, and it is the part of this worth reading "
                "twice."
            )
            self._say()

        if self.options.assume_yes:
            self._wrapped(
                "assume_yes was set, so hop did not ask. Nobody confirmed any of the above: "
                "this run was told to carry on before it started."
            )
            self._say()
            self._done("confirm")
            return True

        answer = self._ask_line("Type yes to go on. Anything else, including y, stops here: ")
        if answer.strip().lower() != "yes":
            return False
        self._say()
        self._done("confirm")
        return True

    # --- 5: the image ------------------------------------------------------

    def _image(self) -> tuple[Path, str]:
        self._begin("iso")

        release = iso.latest_release()
        size = human_bytes(release.size_bytes) if release.size_bytes else "an unstated size"
        self._say(f"  release       {release.filename} ({size})")
        self._say(f"  from          {release.url}")
        self._say()

        self._last_progress = -1
        image = iso.download(release, self.out_dir / "iso", progress=self._progress)
        self._end_progress()
        self.image = image

        result = iso.verify(image, release, runner=self.runner)
        self._say()
        self._wrapped(result.detail)
        self._say()

        if not result.checksum_ok:
            raise GoError(
                "The image does not match its own checksum, so hop will not build a stick out "
                f"of it. The file is at {image}: delete it and run hop go again, and it will "
                "be fetched afresh. Nothing has been erased."
            )

        # A rejected signature is not an unanswered question and must not be
        # offered as one. gpg had the key, read the bytes, and said they are not
        # the bytes Arch signed — which is the exact failure the checksum cannot
        # see, because a mirror able to serve a modified image can serve a
        # matching sha256 beside it. There is no answer to "carry on anyway?"
        # that is good for the reader here, so hop does not ask it.
        if result.signature_bad:
            raise GoError(
                "Arch's own signature on that image does not match the file. hop will not "
                f"build an installer from it and there is no question to ask about it: the "
                "checksum matching means only that the file arrived intact from the mirror, "
                "and the mirror is what published the checksum. The signature is the part that "
                "says Arch made these bytes, and it says they are not.\n\n"
                f"The file is at {image}. Delete it and run hop go again — hop will fetch from "
                "the next mirror in its list. If a second mirror gives the same answer, stop "
                "and ask on the Arch forums before installing anything from it. Nothing has "
                "been erased."
            )

        if not result.trusted:
            answer = self._ask_line(
                "That question is unanswered. Build the stick from this image anyway? "
                "Type yes to go on: "
            )
            if answer.strip().lower() != "yes":
                raise _Declined
            self._say()

        label = iso.volume_label(image)
        self._say(f"  label         {label}")
        self._say()
        self._done("iso")
        return (image, label)

    # --- 6: the medium -----------------------------------------------------

    def _write_medium(
        self, plan: Plan, ready: _Ready, hopfile: Path, image: Path, label: str
    ) -> Path:
        self._begin("medium")

        contents = self.out_dir / "iso-contents"
        # Emptied rather than added to: a previous run of a different release
        # leaves its files behind, and two archisos copied onto one stick
        # produce a boot that fails in a way nobody can read. The directory is
        # inside out_dir, which hop made.
        _clean(contents)
        self._say(f"  unpacking     {image.name} into {contents}")
        iso.extract(image, contents, runner=self.runner)

        baggage = self._baggage(plan, hopfile, label)
        self._say(f"  carrying      {', '.join(sorted(baggage))}")
        self._say()
        self._wrapped(
            f"Erasing {ready.drive.device_id} now. Everything on it is being lost. Do not "
            "unplug it until hop says the stick is finished."
        )
        self._say()

        medium = usb.write_medium(
            ready.drive,
            contents,
            baggage,
            label=label,
            confirm_device_id=ready.drive.device_id,
            runner=self.runner,
            platform=self.platform,
            on_erase=self._mark_erased,
        )
        self.medium = medium
        self._say(f"  built         {medium}")
        if not self.options.keep_iso:
            # Only once the stick exists, and never before: while the stick is
            # not written, that download is the expensive thing in the room.
            _discard(image)
            _discard(Path(str(image) + ".sig"))
            _clean(contents)
            self._say("  deleted       the image and its unpacked copy, as asked")
        self._say()
        self._done("medium")
        return medium

    def _mark_erased(self) -> None:
        """hop/usb.py calls this when the stick stops being recoverable."""
        self.erased = True

    def _baggage(self, plan: Plan, hopfile: Path, label: str) -> dict[str, Path]:
        """What travels beside the installer, and where each thing lands.

        Everything here goes under ``hop/`` on the medium, so that what hop put
        on the stick can be told apart from what archiso put there. The copy of
        the hop package is not an optimisation: the live environment has no hop
        in it, and there may be no network in the room where this gets booted.
        """
        staging = self.out_dir / "medium"
        _clean(staging)
        staging.mkdir(parents=True, exist_ok=True)

        bootstrap = staging / usb.BOOTSTRAP_NAME
        _write(bootstrap, usb.bootstrap_script(label=label, command=("install",)))

        baggage: dict[str, Path] = {
            usb.BOOTSTRAP_NAME: bootstrap,
            BAGGAGE_PACKAGE: _stage_package(staging),
            BAGGAGE_HOPFILE: hopfile,
        }
        if self.plan_path is not None:
            baggage[BAGGAGE_PLAN] = self.plan_path
        if self.report_path is not None:
            baggage[BAGGAGE_REPORT] = self.report_path

        config = self.out_dir / BAGGAGE_ARCHINSTALL
        for name in ("user_configuration.json", "hop-post.sh"):
            candidate = config / name
            if candidate.is_file():
                baggage[f"{BAGGAGE_ARCHINSTALL}/{name}"] = candidate

        if self.payload_dir is not None:
            baggage[BAGGAGE_PAYLOAD] = self.payload_dir
        return baggage

    def _payload_dir(self, plan: Plan, hopfile: Path) -> Path | None:
        """Where the scanner left the payload, if it left one.

        The same order as ``hop/land.py``: what the hopfile recorded, resolved
        against the hopfile itself because that is how the scanner wrote it,
        then the directory hop go would have asked for.
        """
        # Everything in whatever this returns is copied onto the stick whole, so
        # the value decides what leaves the machine. It comes out of the hopfile,
        # and hop/land.py says why that is not a value to trust: a hopfile is a
        # file like any other, it can be edited, and it can be handed to you.
        # ``payload_dir: "C:/Users/you/.ssh"`` or ``"../../.ssh"`` would put that
        # directory on a FAT32 stick with no permissions on it. land.py refuses a
        # restore target outside your home; this is the same rule pointing the
        # other way, and the pair of them is the whole of hop's position on
        # payload paths.
        roots = [hopfile.parent.resolve(), self.out_dir.resolve()]
        candidates: list[Path] = []
        stamped = plan.hopfile.get("payload_dir")
        if stamped:
            candidates.append(hopfile.parent / str(stamped))
        candidates.append(self.out_dir / "hop-payload")
        for candidate in candidates:
            try:
                if not (candidate.is_dir() and any(candidate.iterdir())):
                    continue
                settled = candidate.resolve()
                if not any(settled == root or settled.is_relative_to(root) for root in roots):
                    self._wrapped(
                        f"The hopfile points payload_dir at {settled}, which is outside the "
                        "directory the hopfile is in. hop is not copying that onto the stick. "
                        "If you meant it, move those files next to the hopfile and run again."
                    )
                    self._say()
                    continue
                return settled
            except OSError:
                continue
        return None

    # --- 7: the bootstrap --------------------------------------------------

    def _bootstrap(self, medium: Path) -> None:
        self._begin("bootstrap")

        script = f"{usb.HOP_DIR}/{usb.BOOTSTRAP_NAME}"
        try:
            for path in usb.add_autostart(medium, script_relative=script):
                self._say(f"  wrote {path}")
        except usb.UsbError:
            # Everything the installer needs is already on the stick and the
            # ending is about to tell the reader to boot it, so it has to come
            # out of the machine safely even though this stage failed. A stick
            # pulled with its last writes still in a cache boots to a filesystem
            # error, on a machine that no longer has a Windows on it to look
            # that error up with.
            self._release(medium)
            raise
        self._say()
        self._wrapped(
            "The stick boots to a menu with two entries now: the one hop added, which runs "
            f"/{script} once the live system is up, and the plain Arch installer the ISO came "
            "with. The second is left exactly as it was, so nothing here takes the choice away."
        )
        self._say()
        self._wrapped(
            "If the automation does not start — archiso's script= parameter is a feature of "
            "the ISO rather than of the kernel, and features move — the stick still boots and "
            "everything is still on it. From the live shell:"
        )
        self._say()
        self._say(f"    /run/archiso/bootmnt/{script}")
        self._say()

        self._release(medium)
        self._say()
        self._done("bootstrap")

    def _release(self, medium: Path) -> None:
        """Flush the medium and let go of it. Never raises.

        A failure here is not a failure of the run: everything is written and
        checked, and what is left is only how to get the stick out of the
        machine. Saying that is more use than replacing whatever else went
        wrong with it.
        """
        try:
            usb.eject(str(medium), runner=self.runner, platform=self.platform)
            self._say("  the stick is flushed and safe to unplug")
        except usb.UsbError as exc:
            self._paragraphs(str(exc))

    # --- 8: the reboot -----------------------------------------------------

    def _restart(self, ready: _Ready) -> int:
        self._begin("reboot")

        if not self.options.reboot:
            self._wrapped(
                "Not asked for. The stick is finished and ejected, and nothing on this machine "
                "has been changed. When you are ready, leave the stick plugged in, restart, and "
                "pick it from the boot menu:"
            )
            self._say()
            self._boot_menu_keys()
            self._say()
            self._wrapped(
                "The live environment starts hop by itself from there. Windows stays on this "
                "machine until you tell the installer which disk to erase."
            )
            self._say()
            self._done("reboot")
            return self._finished()

        entry = self._one_shot_boot(ready)
        if entry is None:
            self._wrapped(
                "hop could not arrange a one-shot boot from the stick, so the machine will "
                "restart into whatever it usually boots. Press the boot menu key at the vendor "
                "logo and pick the stick:"
            )
        else:
            identifier, description = entry
            self._say(f"  next boot     {description}   {identifier}")
            self._say()
            self._wrapped(
                "That is a one-shot boot order: the permanent order is untouched, and a "
                "machine restarted again later boots what it always did. Some firmware "
                "ignores it — if you land back in Windows, restart and use the boot menu key:"
            )
        self._say()
        self._boot_menu_keys()
        self._say()

        code, _, stderr = self._run(["shutdown", "/r", "/t", str(REBOOT_SECONDS)])
        if code != 0:
            self._wrapped(
                f"hop could not start the restart itself (shutdown exited {code}{_tail(stderr)}). "
                "Nothing is lost: the stick is finished. Restart the machine yourself and boot "
                "from it."
            )
            self._say()
            self._done("reboot")
            return self._finished()

        self._say(f"  restarting in {REBOOT_SECONDS} seconds")
        self._say("  to stop it    shutdown /a")
        self._say()
        self._wrapped(
            "That is long enough to remember something you forgot to copy. If you use it, "
            "nothing is wasted: the stick is finished and waiting, and you can boot it whenever "
            "you like."
        )
        self._say()
        self._done("reboot")
        return self._finished()

    def _one_shot_boot(self, ready: _Ready) -> tuple[str, str] | None:
        """Ask the firmware to boot the stick once, without changing the boot order.

        ``bcdedit /set {fwbootmgr} bootsequence`` is the documented way to say
        "next time only". hop never writes ``displayorder`` or ``default``: a
        machine left permanently preferring a USB stick is a machine somebody
        has to fix later, from a boot menu, in a hurry.
        """
        code, stdout, _ = self._run(["bcdedit", "/enum", "firmware"])
        if code != 0:
            return None
        entry = _firmware_entry(stdout, ready.drive)
        if entry is None:
            return None
        identifier, description = entry
        code, _, _ = self._run(["bcdedit", "/set", "{fwbootmgr}", "bootsequence", identifier])
        if code != 0:
            return None
        return (identifier, description)

    def _boot_menu_keys(self) -> None:
        for vendor, key in BOOT_MENU_KEYS:
            self._say(f"    {vendor:<20} {key}")

    # --- endings -----------------------------------------------------------

    def _finished(self) -> int:
        self._say(_rule("done"))
        self._say()
        self._wrapped(
            "The stick is an Arch installer with your plan on it. Booting it starts hop "
            "install, which lists the disks that are there at that moment and asks you to type "
            "the device path of the one to erase. Nothing on this machine has been changed yet."
        )
        self._say()
        if self.report_path is not None:
            self._say(f"  the report    {self.report_path}")
        if self.image is not None and self.options.keep_iso:
            self._say(f"  the image     {self.image}")
        self._say()
        return 0

    def _declined(self) -> int:
        self._say()
        self._say(_rule("stopped"))
        self._say()
        self._wrapped("Stopped, because you said no. That is a complete answer.")
        self._say()
        for paragraph in self._aftermath():
            self._paragraphs(paragraph)
            self._say()
        if self.report_path is not None:
            self._wrapped(
                f"The report is still there to read: {self.report_path}. Running hop go again "
                f"with --hopfile {self.out_dir / 'hopfile.json'} skips the scan and picks up "
                "from here."
            )
            self._say()
        return 1

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
        """What state the machine and the stick were left in. Always said."""
        finished = {stage.name for stage in self.stages if stage.done}
        if "medium" in finished:
            automated = (
                "and it boots straight into hop."
                if "bootstrap" in finished
                else "though hop did not get as far as making it start by itself."
            )
            return [
                _fill(
                    "The stick was already built when this stopped, and it is finished: the "
                    f"installer, the plan, the report and the payload are all on it, {automated}"
                ),
                _fill(
                    "This is not a reason to start again. Boot the stick — the boot menu key is "
                    "in the list above, or in the machine's manual — and if the automation does "
                    "not start by itself, run:"
                )
                + f"\n\n    /run/archiso/bootmnt/{usb.HOP_DIR}/{usb.BOOTSTRAP_NAME}\n\n"
                + _fill(
                    "Nothing on this machine's disks has been changed, and Windows still boots."
                ),
            ]
        if self.erased:
            # Between the erase and the last verified copy there is no state
            # worth describing in detail: whatever is on the stick is part of an
            # installer that was never finished, and it will not boot. Saying so
            # is the whole point — the reader is holding a stick and wants to
            # know whether what was on it is gone. It is.
            return [
                _fill(
                    "The stick was erased before this failed. Everything that was on it is gone, "
                    "and what is on it now is an unfinished installer that will not boot — do not "
                    "try. Running hop go again rebuilds it from the start, which is the way "
                    "forward once whatever stopped it is sorted out."
                ),
                _fill(
                    "No disk inside this machine was touched at any point, and Windows still "
                    "boots."
                ),
            ]
        if "iso" in finished and self.image is not None:
            return [
                _fill(
                    "No drive has been erased. The image is downloaded and checked, at "
                    f"{self.image} — running hop go again will not fetch it a second time."
                )
            ]
        return [_fill("No drive has been erased and nothing on this machine has been changed.")]

    # --- transcript --------------------------------------------------------

    def _begin(self, name: str) -> None:
        for index, stage in enumerate(self.stages, start=1):
            if stage.name == name:
                self._say(_rule(f"{index}/{len(self.stages)}  {stage.title}"))
                self._say()
                return
        raise GoError(f"internal error: hop go has no stage called {name!r}")

    def _done(self, name: str) -> None:
        for stage in self.stages:
            if stage.name == name:
                stage.done = True
                return

    def _say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def _wrapped(self, text: str) -> None:
        # break_on_hyphens off. The hyphen in 'hop-report.md' or '--with-secrets'
        # is not a place to break a line, and a filename or a flag split across
        # two of them is one nobody can copy, search for, or type back.
        for line in textwrap.wrap(text, _WRAP, break_on_hyphens=False):
            self._say(line)

    def _paragraphs(self, text: str) -> None:
        """A message with its own layout: prose filled, laid-out blocks kept.

        Refusals from ``hop/usb.py`` and ``hop/iso.py`` are laid out by the
        module that raised them — lists of drives, indented commands — and
        re-wrapping those turns a list into a paragraph. Their prose is not laid
        out, though: it arrives as one sentence per paragraph, and printed as it
        stands it is a four-hundred-character line that the terminal breaks
        wherever it runs out of columns, usually in the middle of a path. So
        each block is treated the way :func:`_hang` treats one — indented means
        somebody meant it, anything else is filled.
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
        """Put the question to the user. A question nobody can hear is a no.

        ``ask`` is injected so that the tests can answer it. When there is
        nobody there — stdin closed, output redirected into a file — reading it
        fails, and hop does not answer a question about erasing a drive on the
        user's behalf.
        """
        asker = self.ask if self.ask is not None else _prompt
        try:
            return asker(question)
        except (EOFError, OSError):
            self._say()
            self._wrapped(
                "There is nobody at the keyboard to answer that: stdin is closed. hop will not "
                "take silence for a yes. Run it in a terminal, or pass --yes if you have read "
                "the report and mean it."
            )
            return ""

    def _progress(self, written: int, total: int) -> None:
        """One line, rewritten in place, so a 1.2 GB download looks alive.

        The only carriage return in hop. What survives being pasted into an
        issue is the last state of the line, which is the state a reader of that
        paste wants.
        """
        if total:
            step = written * 100 // total
            line = f"  downloading   {step:3d}%   {human_bytes(written)} of {human_bytes(total)}"
        else:
            # The mirror would not say how big it is, so there is no percentage
            # to show; move the line every 32 MB instead.
            step = written // (32 * 1024**2)
            line = f"  downloading   {human_bytes(written)} (the mirror did not give a size)"
        if step == self._last_progress:
            return
        self._last_progress = step
        self.out.write("\r" + line.ljust(_WRAP))
        _flush(self.out)

    def _end_progress(self) -> None:
        if self._last_progress >= 0:
            self.out.write("\n")

    def _run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        return self.runner(list(argv))


# --- module-level helpers -------------------------------------------------


def _this_platform() -> str:
    return usb.WINDOWS if os.name == "nt" else usb.LINUX


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run one command. A list, never a shell, never ``check=True``.

    The same conventions as ``hop/iso.py`` and ``hop/usb.py``, because ``hop
    go`` hands one runner to all three: a program that is not installed comes
    back as 127 rather than an exception, and output is decoded with
    replacement, since a Windows console in a Russian locale returns bytes that
    are not UTF-8 and losing a character beats a traceback mid-run.
    """
    try:
        done = subprocess.run(argv, check=False, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        return (127, "", str(exc))
    return (done.returncode, done.stdout or "", done.stderr or "")


def _no_quotes(script: str) -> str:
    """Check a generated script before it is handed to powershell.exe.

    The same rule as ``hop/usb.py``, for the same reason: the script arrives
    through the Windows command line, where the double quote is the one
    character whose meaning is decided twice, and none of hop's scripts need one.
    """
    if '"' in script:
        raise GoError(
            "internal error: a PowerShell script in hop/go.py contains a double quote. "
            "Nothing has been run."
        )
    return script


def _stream_command(argv: Sequence[str], out: TextIO) -> int:
    """Run a program and copy its output to ``out`` as it arrives. Returns its code.

    The one place in this module that does not go through the injected runner,
    and the reason is the scan: it takes minutes, and a runner captures output
    and hands it over at the end, so the screen would sit still for all of them.
    A still screen in the middle of a command that is about to erase a disk
    reads as a hang, and what people do about a hang is kill it.

    argv, never a shell. stderr is folded into stdout so that the transcript
    keeps the scanner's warnings where the scanner printed them.
    """
    try:
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise GoError(f"could not start {argv[0]}: {exc}") from exc

    with process:
        if process.stdout is not None:
            for line in process.stdout:
                out.write("  " + line.rstrip("\r\n") + "\n")
                _flush(out)
    return process.returncode


def _firmware_entry(text: str, drive: usb.Drive) -> tuple[str, str] | None:
    """Find the firmware boot entry for ``drive`` in ``bcdedit /enum firmware`` output.

    Matched on the drive's own words and on the shape of an identifier, never on
    bcdedit's field names: those are printed in the system language, and the
    machines hop is for are frequently not running an English Windows. A GUID in
    braces looks the same in every language.

    Returns ``(identifier, description)``, or None when nothing in the output
    looks like the stick — in which case the caller falls back to telling the
    reader which key opens the boot menu, which always works.
    """
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", drive.model or "") if len(w) > 2]
    best: tuple[int, str, str] | None = None

    for block in re.split(r"\n\s*\n", text):
        found = _BCD_IDENTIFIER.search(block)
        if not found or found.group(0) in _BCD_RESERVED:
            continue
        identifier = found.group(0)
        lowered = block.lower()
        # The model is worth more than the word USB: every removable entry says
        # USB, and only one of them says SanDisk.
        score = (2 if any(word in lowered for word in words) else 0) + int("usb" in lowered)
        if not score:
            continue
        if best is None or score > best[0]:
            best = (score, identifier, _description(block, identifier))

    return None if best is None else (best[1], best[2])


def _description(block: str, identifier: str) -> str:
    """The most useful line of a bcdedit block, for showing back to the reader.

    bcdedit lays its output out in two columns, so the value is whatever follows
    the run of spaces — which is the part hop can show, since the label beside it
    is a word in a language hop does not read.
    """
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or identifier in stripped:
            continue
        if "usb" in stripped.lower() or "uefi" in stripped.lower():
            columns = re.split(r"\s{2,}", stripped, maxsplit=1)
            return " ".join(columns[-1].split())
    return "the removable drive"


def _scanner_path() -> Path | None:
    """windows/hop-scan.ps1 in the checkout hop is running from, if it is there."""
    candidate = Path(__file__).resolve().parent.parent / SCAN_SCRIPT
    return candidate if candidate.is_file() else None


def _stage_package(staging: Path) -> Path:
    """Copy the hop package into ``staging`` so that it can travel on the stick.

    The live environment has no hop in it, and there may be no network in the
    room. ``__pycache__`` is left behind: compiled for this machine's Python,
    useless on the other side, and it makes the copy look bigger than it is.
    """
    source = Path(__file__).resolve().parent
    if not source.is_dir():
        raise GoError(
            "hop cannot find its own source files to put on the stick. That happens when hop "
            "runs out of a zipped package; install it from the checkout, or copy the hop "
            "directory onto the stick by hand afterwards."
        )
    destination = staging / BAGGAGE_PACKAGE
    try:
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            dirs_exist_ok=True,
        )
    except OSError as exc:
        raise GoError(f"could not copy the hop package into {destination}: {exc}") from exc
    return destination


def _private_payload(plan: Plan) -> list[dict]:
    """The payload entries the scanner marked as private material.

    Read off the plan rather than off ``--with-secrets``, because the flag says
    what *this* run asked for and the payload says what is actually about to be
    copied onto a filesystem with no permissions on it. ``hop go --hopfile`` on
    a hopfile scanned with secrets a week ago carries the same private keys and
    was told about none of it.

    ``mode`` is the scanner's own marker: ``0600`` is what it writes for a
    private key or an exported Wi-Fi password, ``0644`` for a bookmark file.
    """
    private: list[dict] = []
    for entry in plan.payload:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("mode") or "").strip().lstrip("0") in ("600", "400"):
            private.append(entry)
    return private


def _no_candidate(found: Sequence[usb.Drive]) -> NoReturn:
    """Always raises. Says why not one of the drives that are there is usable."""
    if not found:
        raise GoError(
            "hop cannot see any drive at all, which usually means the enumeration failed "
            "rather than that the machine has no disks in it. Plug the stick in and try again; "
            "if the list is still empty, run hop go from an elevated PowerShell."
        )
    raise GoError(
        "There is no removable drive here that hop will erase. What it found, and why each one "
        f"is not it:\n\n{_drive_list(found)}\n\n"
        "Plug in a USB stick of at least 4 GB and run hop go again."
    )


def _drive_list(found: Sequence[usb.Drive]) -> str:
    lines: list[str] = []
    for drive in found:
        lines.append(f"  {drive.describe}")
        reason = usb.refuse_reason(drive)
        if reason:
            lines.extend(f"    {line}" for line in textwrap.wrap(reason, _WRAP - 4))
        else:
            lines.append("    usable")
        lines.append("")
    return "\n".join(lines).rstrip()


def _hang(text: str) -> str:
    """Wrap a numbered item so its continuation lines sit under its first word.

    A block that is already indented was laid out by whoever wrote it — a list
    of drives, a command to type — and is carried across as it stands. Filling
    those would turn a list into a paragraph, which is the one thing a list is
    for not being.
    """
    out: list[str] = []
    for index, block in enumerate(text.split("\n\n")):
        if index:
            out.append("")
        if block.startswith((" ", "\t")):
            out.extend(block.splitlines())
        else:
            out.extend(_fill(block, width=_WRAP - 3).splitlines())
    # The indent is only applied to lines that have something on them: a blank
    # line with three spaces on it is invisible until somebody greps the paste.
    return "\n".join(out[:1] + [f"   {line}" if line else "" for line in out[1:]])


def _fill(text: str, width: int = _WRAP) -> str:
    return textwrap.fill(" ".join(text.split()), width, break_on_hyphens=False)


def _write(path: Path, text: str) -> None:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    # Unix line endings even here: every one of these files is read on the Arch
    # side, and bash will not run a script with carriage returns in it.
    path.write_text(text, encoding="utf-8", newline="\n")


def _clean(directory: Path) -> None:
    """Empty a directory hop owns, and leave a missing one missing."""
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


def _discard(path: Path) -> None:
    """Delete a file hop made. Failing to is not worth an error at this point."""
    try:
        path.unlink()
    except OSError:
        return


def _flush(out: TextIO) -> None:
    flush = getattr(out, "flush", None)
    if callable(flush):
        flush()


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
