"""Executing a plan on the machine you have just installed.

``hop land`` is the only part of hop that changes anything, which is why it is
the part that assumes the most can go wrong. Three rules shape the module:

* A dry run is the default. ``Lander(plan).run()`` prints a transcript and
  touches nothing — no directory is created, no file is copied, no package is
  installed. That transcript is meant to be read in full before anybody passes
  ``dry_run=False``.
* It refuses to act unless it is really on Arch, and refuses to act as root.
  Both refusals explain themselves. Someone who typed the wrong command on the
  Windows box they have not wiped yet deserves a sentence, not a traceback.
* A failure is collected, not raised. Being stranded with half a desktop is a
  much worse place than having a working desktop and a list of three packages
  to retry in the morning.

Nothing here prints the contents of a payload file. Wi-Fi passwords and SSH
private keys go from disk to the command that consumes them without passing
through the transcript, because transcripts get read over shoulders and pasted
into bug reports.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TextIO

from .plan import Plan

PHASES = ("packages", "payload", "settings")

#: How many packages go into one pacman invocation. pacman itself copes with a
#: far longer list; a transcript does not, and neither does a person trying to
#: work out which of two hundred names was rejected.
CHUNK = 25

AUR_BASE = "https://aur.archlinux.org"
FLATHUB_URL = "https://flathub.org/repo/flathub.flatpakrepo"
WINDOWS_MOUNT = "/mnt/windows"

#: Directories whose whole point is that other people cannot read them.
SECRET_DIRS = (".ssh", ".gnupg")

#: What a value out of the plan may be made of before hop will run it or write
#: it. AUR helper names and locale names are both drawn from this set; anything
#: outside it is a corrupted plan or somebody being clever, and neither is
#: something to carry on carefully with.
#:
#: Only two kinds of value are checked, because only two are more than an
#: argument. ``aur_helper`` becomes argv[0] — hop runs whatever it names — and
#: the locales become the *contents* of /etc/locale.gen and /etc/locale.conf,
#: where a newline is another line of configuration. Everything else the plan
#: contributes (hostname, keymap, timezone, layouts, package names) is one
#: argument to a command that checks its own input and refuses what it does not
#: understand, and duplicating that check here would only invent new ways to
#: refuse a plan that is fine. hop/archinstall.py makes the same check for the
#: same reason before pasting a plan value into a generated shell script.
_PLAIN = re.compile(r"[A-Za-z0-9@._+-]+")

_WRAP = 74


class LandError(Exception):
    """The landing cannot safely start. Raised before anything has been done."""


@dataclass
class Step:
    """One thing the landing does, together with the sentence that explains it.

    ``argv`` is the command as a list — never a string, never through a shell.
    An empty ``argv`` means the work happens inside hop itself (a file copy, a
    note to the reader) and there is nothing to quote.
    """

    phase: str
    title: str
    argv: list[str]
    kind: str
    detail: str = ""
    optional: bool = False

    @property
    def command(self) -> str:
        """The command as you would type it, quoted safely. Empty when there is none."""
        return _fmt_argv(self.argv)


@dataclass
class _Action:
    """A step, plus how to carry it out when it is not a plain command.

    ``do`` returns ``(status, message)`` where status is ``ok``, ``skip`` or
    ``fail``. Steps with no ``do`` are executed by running ``step.argv``.
    """

    step: Step
    do: Callable[[], tuple[str, str]] | None = None


class Lander:
    """Turns a :class:`~hop.plan.Plan` into actions, and optionally performs them."""

    def __init__(
        self,
        plan: Plan,
        *,
        payload_dir: Path | None = None,
        dry_run: bool = True,
        phases: Sequence[str] = PHASES,
        aur_helper: str | None = None,
        force: bool = False,
        out: TextIO = sys.stdout,
        home: Path | None = None,
    ) -> None:
        unknown = [p for p in phases if p not in PHASES]
        if unknown:
            raise LandError(
                f"unknown phase(s) {', '.join(sorted(unknown))}; "
                f"the phases are {', '.join(PHASES)}"
            )
        self.plan = plan
        self.dry_run = dry_run
        # Keep the canonical order whatever order the caller asked in: packages
        # before payload before settings is not a preference, it is a dependency.
        self.phases = tuple(p for p in PHASES if p in phases)
        self.aur_helper = _plain(
            aur_helper or (plan.target.get("aur_helper") or "paru"),
            "AUR helper",
            "target.aur_helper",
            "runs that name as a program",
        )
        # Checked here rather than where they are used, so that the refusal is
        # true to what LandError promises: raised before anything has been done,
        # and before the transcript has printed a command hop would not run.
        for locale in _locales_in(plan):
            _plain(
                locale,
                "locale",
                "system.locale / system.locales",
                "writes that into /etc/locale.gen and /etc/locale.conf, where a newline is "
                "another line of configuration",
            )
        self.force = force
        self.out = out
        self.home = Path(home) if home is not None else Path.home()
        self.payload_dir, self.payload_found = self._resolve_payload_dir(payload_dir)

    # --- the plan of action ------------------------------------------------

    def steps(self) -> list[Step]:
        """Everything the landing would do, in order. Reads the disk; changes nothing."""
        return [action.step for action in self._build()]

    def _build(self) -> list[_Action]:
        actions: list[_Action] = []
        if "packages" in self.phases:
            actions += self._packages()
        if "payload" in self.phases:
            actions += self._payload()
        if "settings" in self.phases:
            actions += self._settings()
        return actions

    # --- running it --------------------------------------------------------

    def run(self) -> int:
        """Print the transcript and, unless this is a dry run, perform it.

        Returns 0 when nothing that mattered failed. Optional steps can fail
        without changing the exit code; they are still reported.
        """
        actions = self._build()
        if not self.dry_run:
            self._require_arch()
            self._require_non_root()

        self._header(len(actions))

        ran = 0
        skipped: list[tuple[Step, str]] = []
        failed: list[tuple[Step, str]] = []
        phase = None
        total = len(actions)

        for index, action in enumerate(actions, 1):
            if action.step.phase != phase:
                phase = action.step.phase
                self._phase_header(phase)
            self._show(index, total, action.step)
            if self.dry_run:
                continue
            status, message = self._perform(action)
            if status == "ok":
                # A note is not work. Counting the paragraphs hop printed as
                # things it did would make the summary flatter than the truth.
                if action.step.argv or action.step.kind in ("copy", "write"):
                    ran += 1
                    self._say(self._indent(total) + "done")
            elif status == "skip":
                skipped.append((action.step, message))
                self._say(self._indent(total) + f"skipped: {message}")
            else:
                failed.append((action.step, message))
                self._say(self._indent(total) + f"failed: {message}")

        return self._summary(total, ran, skipped, failed)

    def _perform(self, action: _Action) -> tuple[str, str]:
        # Anything unforeseen becomes a reported failure rather than a traceback.
        # Someone whose landing stops on line 400 of a Python file learns nothing
        # about their machine, and they are already having a difficult day.
        try:
            if action.do is not None:
                return action.do()
            if not action.step.argv:
                return ("ok", "")
            return self._exec(action.step.argv)
        except Exception as exc:  # noqa: BLE001 - deliberate: no step may abort the landing
            return ("fail", f"unexpected error: {exc.__class__.__name__}: {exc}")

    # --- refusals ----------------------------------------------------------

    def _require_arch(self) -> None:
        """Refuse to execute anywhere that is not obviously Arch."""
        if os.name == "posix" and (shutil.which("pacman") or Path("/etc/arch-release").exists()):
            return
        raise LandError(
            "This does not look like an Arch system, so hop land has fallen back to doing "
            "nothing at all: it found no pacman on PATH and no /etc/arch-release "
            f"(os.name is {os.name!r}). Nothing has been installed, copied or changed.\n\n"
            "If you are still on the machine you are leaving, this is the expected outcome — "
            "'hop plan' runs anywhere, 'hop land' only runs on the new machine. Drop the "
            "--execute flag to read the transcript here, and run the real landing after Arch "
            "has booted."
        )

    def _require_non_root(self) -> None:
        """Refuse to execute as root, and say why it is not an arbitrary rule."""
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None or geteuid() != 0:
            return
        raise LandError(
            "hop land is running as root and will not continue. Three separate reasons:\n"
            "  - the package steps already say sudo, so root buys nothing;\n"
            "  - makepkg refuses to build as root, so bootstrapping the AUR helper would "
            "stop dead there;\n"
            "  - every payload file would land owned by root — your SSH keys, your "
            "bookmarks — inside a home directory you would then not be able to write to.\n"
            "Log in as your normal user (the one with sudo rights) and run it again. "
            "Nothing has been changed."
        )

    # --- phase: packages ---------------------------------------------------

    def _packages(self) -> list[_Action]:
        pacman = sorted(self.plan.packages.get("pacman") or [])
        aur = sorted(self.plan.packages.get("aur") or [])
        flatpak = sorted(self.plan.packages.get("flatpak") or [])

        # The plan lists flatpaks by name but nothing installs the client, so add
        # it here rather than making the user work out why 'flatpak' is not found.
        if flatpak and "flatpak" not in pacman:
            pacman.append("flatpak")

        if not (pacman or aur or flatpak):
            return [
                _Action(Step("packages", "The plan lists no packages, so there is nothing to install.", [], "note"))
            ]

        out: list[_Action] = []

        if pacman:
            out.append(
                _Action(
                    Step(
                        "packages",
                        "Refresh the package database and bring the system up to date before "
                        "installing anything. Installing against a stale database is how a "
                        "machine ends up half-upgraded, and a half-upgraded Arch does not boot.",
                        ["sudo", "pacman", "-Syu", "--noconfirm"],
                        "run",
                    )
                )
            )
            # --needed is what makes this whole phase repeatable: pacman leaves
            # anything already installed alone instead of reinstalling it, so a
            # landing that died in the middle can simply be run again.
            batches = _chunks(pacman, CHUNK)
            for number, chunk in enumerate(batches, 1):
                label = f" (batch {number} of {len(batches)})" if len(batches) > 1 else ""
                out.append(
                    _Action(
                        Step(
                            "packages",
                            f"Install {len(chunk)} package(s) from the official repositories{label}.",
                            ["sudo", "pacman", "-S", "--needed", "--noconfirm", *chunk],
                            "run",
                            detail=(
                                "--needed skips what is already installed, so this is safe to "
                                "re-run; if pacman refuses the whole batch hop retries the "
                                "packages one at a time, so one wrong name does not cost you "
                                "the other " + str(len(chunk) - 1) + "."
                            ),
                        ),
                        do=partial(
                            self._install_batch,
                            ["sudo", "pacman", "-S", "--needed", "--noconfirm"],
                            list(chunk),
                        ),
                    )
                )

        out += self._aur_actions(aur)

        if flatpak:
            out.append(
                _Action(
                    Step(
                        "packages",
                        "Add the Flathub remote for your user. --if-not-exists means running "
                        "this a second time is harmless, and --user keeps it out of the "
                        "system-wide store so no polkit password prompt appears mid-landing.",
                        ["flatpak", "--user", "remote-add", "--if-not-exists", "flathub", FLATHUB_URL],
                        "run",
                    )
                )
            )
            for chunk in _chunks(flatpak, CHUNK):
                out.append(
                    _Action(
                        Step(
                            "packages",
                            f"Install {len(chunk)} flatpak(s) from Flathub. These are larger "
                            "downloads than repository packages because each one brings its "
                            "own runtime.",
                            ["flatpak", "--user", "install", "-y", "--noninteractive", "flathub", *chunk],
                            "run",
                        ),
                        do=partial(
                            self._install_batch,
                            ["flatpak", "--user", "install", "-y", "--noninteractive", "flathub"],
                            list(chunk),
                        ),
                    )
                )

        return out

    def _aur_actions(self, aur: list[str]) -> list[_Action]:
        if not aur:
            return []
        helper = self.aur_helper
        out: list[_Action] = []

        if shutil.which(helper) is None:
            build_root = self._temp_root() / f"hop-{helper}-build"
            target = build_root / f"{helper}-bin"
            out.append(
                _Action(
                    Step(
                        "packages",
                        f"{helper} is not installed, so fetch its build script first. Everything "
                        "in the AUR is submitted by other users and built on your machine from "
                        "source it downloads: reading a PKGBUILD before you install it is "
                        "ordinary practice, not paranoia.",
                        # as_posix() rather than str(): on Arch the two are the
                        # same string, and on a Windows preview str() would turn
                        # the destination into '\tmp\hop-paru-build' in a command
                        # the reader is being asked to trust.
                        ["git", "clone", "--depth", "1", f"{AUR_BASE}/{helper}-bin.git", target.as_posix()],
                        "run",
                        detail=f"the PKGBUILD lands in {target.as_posix()} if you want to read it first",
                    ),
                    do=partial(
                        self._clone_helper,
                        target,
                        ["git", "clone", "--depth", "1", f"{AUR_BASE}/{helper}-bin.git", target.as_posix()],
                    ),
                )
            )
            out.append(
                _Action(
                    Step(
                        "packages",
                        f"Build and install {helper} from that PKGBUILD. This is the step that "
                        "needs you to be a normal user with sudo rights rather than root, "
                        "because makepkg refuses to build as root.",
                        ["makepkg", "-si", "--noconfirm"],
                        "run",
                        detail=(
                            f"run inside {target.as_posix()}; the directory is removed on success "
                            "and left behind on failure so you can read the build log"
                        ),
                    ),
                    do=partial(self._makepkg, target),
                )
            )

        for chunk in _chunks(aur, CHUNK):
            out.append(
                _Action(
                    Step(
                        "packages",
                        f"Install {len(chunk)} package(s) from the AUR with {helper}. These are "
                        "built on this machine, so this step takes minutes rather than seconds, "
                        "and it is the one that will ask you to confirm PKGBUILD diffs.",
                        [helper, "-S", "--needed", "--noconfirm", *chunk],
                        "run",
                        detail="if the batch is refused hop retries them one at a time",
                    ),
                    do=partial(self._install_batch, [helper, "-S", "--needed", "--noconfirm"], list(chunk)),
                )
            )
        return out

    # --- phase: payload ----------------------------------------------------

    def _payload(self) -> list[_Action]:
        entries = list(self.plan.payload or [])
        if not entries:
            return [
                _Action(
                    Step(
                        "payload",
                        "The plan carries no payload files, so there is nothing to restore.",
                        [],
                        "note",
                    )
                )
            ]

        if not self.payload_found:
            listed = ", ".join(str(e.get("path")) for e in entries[:6])
            if len(entries) > 6:
                listed += f", and {len(entries) - 6} more"
            return [
                _Action(
                    Step(
                        "payload",
                        f"No payload directory at {self._pretty(self.payload_dir)}, so nothing is "
                        "restored. This is not a failure — the files are probably still on the "
                        "backup drive. Copy hop-payload/ across and run 'hop land --only payload' "
                        "again; nothing else in the landing depends on it.",
                        [],
                        "note",
                        detail=f"hop was expecting {len(entries)} file(s): {listed}",
                    )
                )
            ]

        out: list[_Action] = []
        bookmarks = False
        for entry in entries:
            relative = str(entry.get("path") or "").strip().replace("\\", "/")
            if not relative:
                continue
            if not _stays_inside(relative):
                out.append(
                    _Action(
                        Step(
                            "payload",
                            f"{relative} is not a path inside the payload directory, so hop "
                            "skips it. The scanner only ever writes plain relative paths; "
                            "anything else means the file was not put there by a scan of your "
                            "machine.",
                            [],
                            "note",
                            optional=True,
                        )
                    )
                )
                continue
            kind = str(entry.get("kind") or "other").lower()
            mode = _parse_mode(entry.get("mode"), kind)
            source = self.payload_dir / relative

            if not source.exists():
                out.append(
                    _Action(
                        Step(
                            "payload",
                            f"{relative} is listed in the hopfile but is not in the payload "
                            "directory, so hop skips it. Nothing else changes.",
                            [],
                            "note",
                            optional=True,
                        )
                    )
                )
                continue

            restore_to = entry.get("restore_to")
            if restore_to:
                destination = self._expand(str(restore_to))
                # The payload restores into your home directory and nowhere else.
                # A hopfile is a file like any other — it can be edited, or handed
                # to you by someone else — and nothing in the format needs hop to
                # write outside the account it is running as.
                if not self._inside_home(destination):
                    out.append(
                        _Action(
                            Step(
                                "payload",
                                f"{relative} wants to go to {destination.as_posix()}, which is "
                                "outside your home directory, so hop leaves it alone. It stays "
                                "in the payload directory; copy it yourself if you meant it.",
                                [],
                                "note",
                                optional=True,
                            )
                        )
                    )
                    continue
                out.append(self._copy_action(relative, source, destination, mode, kind))
            elif kind == "wifi":
                out.append(self._wifi_action(relative, source))
            elif kind == "bookmarks":
                out.append(self._bookmarks_action(relative, source, mode))
                bookmarks = True
            else:
                # No destination and no special handling: park it somewhere the
                # user will find it rather than guessing where it belongs.
                destination = self.home / "Documents" / "hop" / relative
                out.append(
                    self._copy_action(
                        relative,
                        source,
                        destination,
                        mode,
                        kind,
                        title=(
                            f"Copy {relative} to {self._pretty(destination)}. The hopfile does not "
                            "say where this belongs on Linux, so hop puts it somewhere obvious "
                            "instead of guessing at a config path."
                        ),
                    )
                )

        if bookmarks:
            out.append(
                _Action(
                    Step(
                        "payload",
                        "Importing those bookmarks is two menu items, and it is worth doing "
                        "before you start browsing rather than after.",
                        [],
                        "note",
                        detail=(
                            "Firefox: Bookmarks -> Manage bookmarks -> Import and Backup -> "
                            "Import Bookmarks from HTML.  Chrome and Chromium: Bookmarks -> "
                            "Bookmark manager -> the three-dot menu -> Import bookmarks."
                        ),
                    )
                )
            )
        return out

    def _copy_action(
        self,
        relative: str,
        source: Path,
        destination: Path,
        mode: int,
        kind: str,
        title: str | None = None,
    ) -> _Action:
        if title is None:
            title = f"Restore {relative} to {self._pretty(destination)}."
            # ssh does not warn about a key or a config other people can read: it
            # ignores the key and asks for a password, or says "bad permissions"
            # and stops. Both are miserable to debug at the moment you cannot push.
            if kind in ("ssh", "gpg") and not mode & 0o077:
                title += (
                    " The mode is the point here: ssh refuses to use a private key or a config "
                    "file that other users could read, and what you get is a password prompt "
                    "rather than an explanation."
                )
        return _Action(
            Step(
                "payload",
                title,
                [],
                "copy",
                detail=(
                    f"{relative}  ->  {self._pretty(destination)}   "
                    f"mode {mode:04o}, parent directory {_parent_mode(destination.parent):04o}"
                ),
            ),
            do=partial(self._copy, source, destination, mode),
        )

    def _wifi_action(self, relative: str, source: Path) -> _Action:
        # The connection is named after the payload file rather than after the
        # SSID inside it. The file is 0600 and its contents include the
        # pre-shared key; nothing read out of it reaches the transcript.
        name = Path(relative).stem
        return _Action(
            Step(
                "payload",
                f"Import the Wi-Fi profile {relative} into NetworkManager. hop builds an "
                "'nmcli connection add' from the saved profile rather than writing into "
                "/etc/NetworkManager by hand, so NetworkManager stays the only thing that "
                "owns those files.",
                ["nmcli", "connection", "add", "type", "wifi", "con-name", name,
                 "ssid", "<ssid>", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "<password>"],
                "run",
                detail=(
                    "the password is read from the payload file at the moment the command runs "
                    "and never printed; needs NetworkManager to be running, and is skipped with "
                    "a note if it is not"
                ),
                optional=True,
            ),
            do=partial(self._import_wifi, source, name),
        )

    def _bookmarks_action(self, relative: str, source: Path, mode: int) -> _Action:
        destination = self.home / "Documents" / "hop" / Path(relative).name
        return _Action(
            Step(
                "payload",
                f"Copy the bookmark export {relative} somewhere you can find it, and leave the "
                "import itself to you. hop does not write into a browser profile: bookmarks "
                "live in a database the browser keeps locked while it runs, and an import that "
                "goes wrong takes the bookmarks already in there with it.",
                [],
                "copy",
                detail=f"{relative}  ->  {self._pretty(destination)}   mode {mode:04o}",
            ),
            do=partial(self._copy, source, destination, mode),
        )

    # --- phase: settings ---------------------------------------------------

    def _settings(self) -> list[_Action]:
        system = self.plan.system
        out: list[_Action] = []

        display_manager = self.plan.target.get("display_manager")
        if display_manager:
            out.append(
                _Action(
                    Step(
                        "settings",
                        f"Enable {display_manager} so the desktop comes up at the next boot. "
                        "Enable, not start: if you are running this from an installer or a live "
                        "session, starting a display manager now would take the console away "
                        "from you mid-landing.",
                        ["sudo", "systemctl", "enable", f"{display_manager}.service"],
                        "run",
                    )
                )
            )
        out.append(
            _Action(
                Step(
                    "settings",
                    "Enable NetworkManager so the machine has a network after the next reboot. "
                    "Again enable rather than start, for the same reason.",
                    ["sudo", "systemctl", "enable", "NetworkManager.service"],
                    "run",
                )
            )
        )

        locales = [str(item) for item in (system.get("locales") or []) if item]
        locale = str(system.get("locale") or (locales[0] if locales else "en_US.UTF-8"))
        if locales:
            out.append(
                _Action(
                    Step(
                        "settings",
                        "Add your locales to /etc/locale.gen. Arch ships that file with every "
                        "locale commented out, which is why a fresh install speaks C and sorts "
                        "your files in the wrong order.",
                        ["sudo", "tee", "-a", "/etc/locale.gen"],
                        "write",
                        detail="appending: " + ", ".join(f"{item} UTF-8" for item in locales),
                    ),
                    do=partial(self._append_locales, locales),
                )
            )
            out.append(
                _Action(
                    Step(
                        "settings",
                        "Generate them. Until this runs, setting LANG to a locale simply has no "
                        "effect.",
                        ["sudo", "locale-gen"],
                        "run",
                    )
                )
            )
            out.append(
                _Action(
                    Step(
                        "settings",
                        f"Write /etc/locale.conf so the system language is {locale}.",
                        ["sudo", "tee", "/etc/locale.conf"],
                        "write",
                        detail=f"contents: LANG={locale}",
                    ),
                    do=partial(self._write_root_file, "/etc/locale.conf", f"LANG={locale}\n"),
                )
            )

        keymap = str(system.get("keymap") or "us")
        out.append(
            _Action(
                Step(
                    "settings",
                    f"Set the console keymap to {keymap}. This is the text console only — the "
                    "one you land on if the desktop ever fails to start.",
                    ["sudo", "localectl", "set-keymap", keymap],
                    "run",
                )
            )
        )
        layouts = [str(item) for item in (system.get("x11_layouts") or [keymap]) if item]
        x11 = ["sudo", "localectl", "set-x11-keymap", ",".join(layouts)]
        title = f"Set the desktop keyboard layout to {', '.join(layouts)}."
        if len(layouts) > 1:
            # Empty model and variant are positional: localectl takes
            # layout, model, variant, options in that order.
            x11 += ["", "", "grp:alt_shift_toggle"]
            title += (
                " Alt+Shift switches between them, which is the shortcut you already have in "
                "your fingers from Windows. Without this option there is no way to switch at all."
            )
        out.append(_Action(Step("settings", title, x11, "run")))

        timezone = str(system.get("timezone") or "UTC")
        out.append(
            _Action(
                Step(
                    "settings",
                    f"Set the timezone to {timezone}.",
                    ["sudo", "timedatectl", "set-timezone", timezone],
                    "run",
                )
            )
        )

        hostname = str(system.get("hostname") or "arch")
        out.append(
            _Action(
                Step(
                    "settings",
                    f"Set the hostname to {hostname}.",
                    ["sudo", "hostnamectl", "set-hostname", hostname],
                    "run",
                )
            )
        )

        out += self._windows_partition_actions()
        return out

    def _windows_partition_actions(self) -> list[_Action]:
        # Deliberately not read from the hopfile: by the time this runs the disk
        # has been repartitioned, so the only trustworthy answer is the one the
        # kernel gives right now.
        return [
            _Action(
                Step(
                    "settings",
                    "Look for the old Windows filesystem and mount the largest NTFS one "
                    f"read-only at {WINDOWS_MOUNT}. This is how you get at the files you "
                    "meant to copy and did not.",
                    ["lsblk", "-J", "-o", "NAME,PATH,FSTYPE,LABEL,SIZE,MOUNTPOINT"],
                    "run",
                    detail=(
                        f"then, for the largest NTFS filesystem found: sudo mkdir -p "
                        f"{WINDOWS_MOUNT} and sudo mount -o ro <device> {WINDOWS_MOUNT}"
                    ),
                    optional=True,
                ),
                do=self._mount_windows,
            ),
            _Action(
                Step(
                    "settings",
                    "Read-only is not timidity. Windows Fast Startup does not shut the machine "
                    "down, it hibernates it, and a hibernated NTFS volume has a dirty journal: "
                    "writing to it is exactly how people lose the data they were trying to "
                    "rescue. Copy what you need off it, do not work inside it.",
                    [],
                    "note",
                    detail=(
                        "If the mount is refused outright, boot Windows once, turn off Fast "
                        "Startup, shut down properly, and try again."
                    ),
                )
            ),
            _Action(
                Step(
                    "settings",
                    "hop does not edit /etc/fstab. If you want that partition back after every "
                    "reboot, add the line by hand — a typo in fstab is a machine that stops at "
                    "an emergency shell, and that is a bad first week.",
                    [],
                    "note",
                    detail=(
                        f"<device>  {WINDOWS_MOUNT}  ntfs3  ro,noauto,x-systemd.automount  0 0"
                        "   (use UUID=... from 'lsblk -o PATH,UUID' rather than the device name, "
                        "which moves when you add a disk)"
                    ),
                )
            ),
        ]

    # --- the actual work ---------------------------------------------------

    def _exec(self, argv: list[str], *, cwd: Path | None = None, quiet: bool = False) -> tuple[str, str]:
        """Run one command. Never a shell, never a string, never check=True."""
        try:
            done = subprocess.run(
                argv,
                check=False,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=quiet,
                text=True,
            )
        except OSError as exc:
            return ("fail", f"could not run {argv[0]}: {exc}")
        if done.returncode == 0:
            return ("ok", "")
        tail = ""
        if quiet and done.stderr:
            tail = f" — {_first_line(done.stderr)}"
        return ("fail", f"exit {done.returncode}{tail}")

    def _install_batch(self, prefix: list[str], packages: list[str]) -> tuple[str, str]:
        status, message = self._exec([*prefix, *packages])
        if status == "ok":
            return (status, message)
        # pacman refuses the entire transaction when a single name in it is
        # unknown, so one stale entry in the mapping database would otherwise
        # cost the user every other package in the batch. Retry individually and
        # report only what is genuinely missing.
        self._say(f"        the batch was refused; retrying {len(packages)} packages one at a time")
        failures = [pkg for pkg in packages if self._exec([*prefix, pkg], quiet=True)[0] != "ok"]
        if not failures:
            return ("ok", "")
        return ("fail", f"{len(failures)} of {len(packages)} could not be installed: {', '.join(failures)}")

    def _clone_helper(self, target: Path, argv: list[str]) -> tuple[str, str]:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                # Only ever a directory hop itself created under the system temp
                # directory, left behind by an earlier attempt.
                shutil.rmtree(target)
        except OSError as exc:
            return ("fail", f"could not prepare {target}: {exc}")
        return self._exec(argv)

    def _makepkg(self, target: Path) -> tuple[str, str]:
        if not target.is_dir():
            return ("fail", f"{target} is not there, so the clone must have failed")
        status, message = self._exec(["makepkg", "-si", "--noconfirm"], cwd=target)
        if status == "ok":
            shutil.rmtree(target.parent, ignore_errors=True)
        return (status, message)

    def _copy(self, source: Path, destination: Path, mode: int) -> tuple[str, str]:
        if destination.exists() and not self.force:
            return (
                "skip",
                f"{self._pretty(destination)} already exists and was left alone "
                "(pass --force to overwrite it)",
            )
        try:
            parent = destination.parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
                os.chmod(parent, _parent_mode(parent))
            # copyfile rather than copy2: the permissions on a file that came off
            # an NTFS volume mean nothing here, and the mode from the hopfile is
            # the one that matters.
            shutil.copyfile(source, destination)
            os.chmod(destination, mode)
        except OSError as exc:
            return ("fail", f"could not restore {self._pretty(destination)}: {exc}")
        return ("ok", "")

    def _import_wifi(self, source: Path, name: str) -> tuple[str, str]:
        if shutil.which("nmcli") is None:
            return ("skip", "nmcli is not installed, so the profile stays in the payload directory")
        active = self._quiet(["systemctl", "is-active", "--quiet", "NetworkManager"])
        if active != 0:
            return (
                "skip",
                "NetworkManager is not running yet, so nothing was imported. Start it with "
                "'sudo systemctl start NetworkManager' and re-run 'hop land --only payload'",
            )
        try:
            raw = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return ("fail", f"could not read the Wi-Fi profile {source.name}: {exc}")
        if not isinstance(raw, dict):
            return ("fail", f"{source.name} is not a Wi-Fi profile hop understands")

        ssid = str(raw.get("ssid") or name)
        secret = str(raw.get("psk") or raw.get("password") or raw.get("key") or "")
        if self._connection_exists(ssid) and not self.force:
            return ("skip", "a NetworkManager connection of that name already exists")

        argv = ["nmcli", "connection", "add", "type", "wifi", "con-name", ssid, "ssid", ssid]
        if secret:
            argv += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", secret]
        # Built and run without going through the transcript: this argv carries
        # the pre-shared key, and _exec would print it.
        try:
            done = subprocess.run(argv, check=False, capture_output=True, text=True)
        except OSError as exc:
            return ("fail", f"could not run nmcli: {exc}")
        if done.returncode != 0:
            return ("fail", "nmcli refused the profile: " + _redact(_first_line(done.stderr), secret))
        return ("ok", "")

    def _connection_exists(self, name: str) -> bool:
        try:
            done = subprocess.run(
                ["nmcli", "-t", "-f", "NAME", "connection", "show"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        return name in [line.strip() for line in done.stdout.splitlines()]

    def _append_locales(self, locales: list[str]) -> tuple[str, str]:
        path = Path("/etc/locale.gen")
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as exc:
            return ("fail", f"could not read {path}: {exc}")
        present = {
            line.strip() for line in existing.splitlines() if line.strip() and not line.startswith("#")
        }
        wanted = [f"{item} UTF-8" for item in locales if f"{item} UTF-8" not in present]
        if not wanted:
            return ("skip", "every locale in the plan is already enabled in /etc/locale.gen")
        body = "\n".join(["", "# added by hop2arch", *wanted, ""])
        return self._write_via_tee(["sudo", "tee", "-a", str(path)], body)

    def _write_root_file(self, path: str, body: str) -> tuple[str, str]:
        return self._write_via_tee(["sudo", "tee", path], body)

    def _write_via_tee(self, argv: list[str], body: str) -> tuple[str, str]:
        """Write a root-owned file without a shell redirect.

        ``sudo tee`` is the version of this that does not need shell=True and
        does not need hop to run as root.
        """
        try:
            done = subprocess.run(argv, check=False, input=body, capture_output=True, text=True)
        except OSError as exc:
            return ("fail", f"could not run {argv[0]}: {exc}")
        if done.returncode != 0:
            return ("fail", f"exit {done.returncode} — {_first_line(done.stderr)}")
        return ("ok", "")

    def _mount_windows(self) -> tuple[str, str]:
        try:
            done = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,PATH,FSTYPE,LABEL,SIZE,MOUNTPOINT"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            return ("skip", f"lsblk is not available here ({exc}), so no Windows partition was looked for")
        if done.returncode != 0:
            return ("skip", f"lsblk exited {done.returncode}, so no Windows partition was looked for")
        try:
            tree = json.loads(done.stdout or "{}")
        except ValueError as exc:
            return ("skip", f"could not read the lsblk output ({exc})")

        candidates = [node for node in _walk_blockdevices(tree.get("blockdevices") or [])
                      if str(node.get("fstype") or "").lower() in ("ntfs", "ntfs3")]
        if not candidates:
            return ("skip", "no NTFS filesystem on this machine, so there is no old Windows to mount")

        already = [c for c in candidates if c.get("mountpoint")]
        if already:
            return ("skip", f"{already[0].get('path')} is already mounted at {already[0].get('mountpoint')}")

        chosen = max(candidates, key=lambda node: _size_bytes(node.get("size")))
        device = str(chosen.get("path") or "")
        if not device:
            return ("skip", "lsblk reported an NTFS filesystem with no device path")

        label = f" (label {chosen.get('label')})" if chosen.get("label") else ""
        self._say(f"        largest NTFS filesystem: {device}, {chosen.get('size')}{label}")
        status, message = self._exec(["sudo", "mkdir", "-p", WINDOWS_MOUNT])
        if status != "ok":
            return (status, message)
        status, message = self._exec(["sudo", "mount", "-o", "ro", device, WINDOWS_MOUNT])
        if status != "ok":
            return (
                "fail",
                f"{message} — if it mentions hibernation, boot Windows once and shut it down "
                "with Fast Startup turned off",
            )
        self._say(f"        mounted read-only at {WINDOWS_MOUNT}; unmount it with 'sudo umount {WINDOWS_MOUNT}'")
        self._say(f"        fstab line, if you want it permanent: {device}  {WINDOWS_MOUNT}  ntfs3  ro,noauto,x-systemd.automount  0 0")
        return ("ok", "")

    def _quiet(self, argv: list[str]) -> int:
        try:
            return subprocess.run(argv, check=False, capture_output=True, text=True).returncode
        except OSError:
            return 1

    # --- transcript --------------------------------------------------------

    def _say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def _header(self, total: int) -> None:
        target = self.plan.target
        system = self.plan.system
        counts = {key: len(self.plan.packages.get(key) or []) for key in ("pacman", "aur", "flatpak")}

        if self.dry_run:
            self._say("hop land — DRY RUN")
            self._say()
            self._wrapped(
                "Nothing below is executed. No package is installed, no file is copied, no "
                "setting is changed, and no directory is created — this is a transcript of "
                "what a real landing would do, in the order it would do it. Read it through "
                "before you run it for real."
            )
        else:
            self._say("hop land — EXECUTING")
            self._say()
            self._wrapped(
                "Every command below runs for real, as you, with sudo where it says sudo. "
                "A step that fails is reported and the landing carries on: it is better to "
                "finish with a short list of things to retry than to stop halfway."
            )
        self._say()

        packages = f"{counts['pacman']} from the repos"
        if counts["aur"]:
            packages += f", {counts['aur']} from the AUR via {self.aur_helper}"
        if counts["flatpak"]:
            packages += f", {counts['flatpak']} flatpak"
        payload = (
            f"{len(self.plan.payload or [])} file(s) from {self._pretty(self.payload_dir)}"
            if self.payload_found
            else f"not found at {self._pretty(self.payload_dir)}"
        )
        rows = [
            ("target", f"{target.get('desktop_label', 'no desktop')} on "
                       f"{system.get('hostname', 'this machine')}, user {system.get('username', 'you')}"),
            ("packages", packages),
            ("payload", payload),
            ("phases", ", ".join(self.phases)),
            ("steps", str(total)),
        ]
        for key, value in rows:
            self._say(f"  {key:<10} {value}")

        geteuid = getattr(os, "geteuid", None)
        if self.dry_run and geteuid is not None and geteuid() == 0:
            self._say()
            self._wrapped(
                "You are root. A real landing would refuse to start: makepkg will not build as "
                "root, and payload files restored as root land in your home directory owned by "
                "the wrong user. Run it as yourself, with sudo rights."
            )
        self._say()

    def _phase_header(self, phase: str) -> None:
        self._say(_rule(phase))
        self._say()

    def _indent(self, total: int) -> str:
        return " " * (len(str(total)) * 2 + 4)

    def _show(self, index: int, total: int, step: Step) -> None:
        width = len(str(total))
        head = f"[{index:>{width}}/{total}]"
        indent = " " * (len(head) + 1)
        lines = textwrap.wrap(step.title, _WRAP) or [step.title]
        for number, line in enumerate(lines):
            self._say((head + " " if number == 0 else indent) + line)

        if step.argv:
            verb = "would run: " if self.dry_run else "run: "
            self._say(indent + verb + _fmt_argv(step.argv))
            if step.detail:
                self._detail(indent, step.detail)
        elif step.kind in ("copy", "write") and step.detail:
            verb = "would copy: " if self.dry_run else "copy: "
            if step.kind == "write":
                verb = "would write: " if self.dry_run else "write: "
            self._detail(indent, verb + step.detail)
        elif step.detail:
            self._detail(indent, step.detail)

        # A note cannot fail, so saying it is optional would only be noise.
        if step.optional and step.kind != "note":
            self._say(indent + "(optional — if this fails the landing carries on)")
        self._say()

    def _detail(self, indent: str, text: str) -> None:
        for line in textwrap.wrap(text, _WRAP - 2) or [text]:
            self._say(indent + "  " + line)

    def _wrapped(self, text: str) -> None:
        for line in textwrap.wrap(text, _WRAP):
            self._say(line)

    def _summary(
        self,
        total: int,
        ran: int,
        skipped: list[tuple[Step, str]],
        failed: list[tuple[Step, str]],
    ) -> int:
        self._say(_rule("summary"))
        self._say()
        if self.dry_run:
            self._say(f"{total} step(s), none of them executed. Nothing on this machine changed.")
            self._say()
            self._wrapped(
                "When you are on the Arch machine and you have read the list above, run the "
                "same command with --execute. Every step is safe to repeat, so if it stops "
                "partway you can simply run it again."
            )
            self._say()
            return 0

        hard = [pair for pair in failed if not pair[0].optional]
        self._say(f"{ran} step(s) ran, {len(skipped)} skipped, {len(failed)} failed.")
        self._say()
        if skipped:
            self._say("Skipped:")
            for _step, message in skipped:
                self._say(f"  - {message}")
            self._say()
        if failed:
            self._say("Failed:")
            for step, message in failed:
                tail = "  (optional, so the landing carried on)" if step.optional else ""
                self._say(f"  - {_short(step.title)}")
                self._say(f"    {message}{tail}")
            self._say()
        if hard:
            self._wrapped(
                f"{len(hard)} step(s) that mattered did not work. Fix what the messages above "
                "point at and run the same command again — nothing here is harmed by being "
                "run twice."
            )
            self._say()
            return 1
        # What the closing sentence may promise depends on what this run did.
        # hop-post.sh tells people to finish with 'hop land --only payload', so a
        # run that never touched the settings phase is the ordinary case rather
        # than the odd one, and it must not sign off by promising a desktop it
        # did nothing about.
        if "settings" in self.phases and self.plan.target.get("display_manager"):
            self._wrapped(
                "The landing finished. Reboot when you are ready; the desktop starts by itself "
                "from here."
            )
        elif self.plan.target.get("display_manager"):
            self._wrapped(
                f"The landing finished. This run did the {', '.join(self.phases)} phase(s), "
                "which does not include enabling the display manager, so nothing here changed "
                "what happens at the next boot."
            )
        else:
            self._wrapped(
                "The landing finished. The plan asks for no desktop, so the next boot still "
                "ends at a text login. That is what was asked for, not a step that went wrong."
            )
        self._say()
        return 0

    # --- small internals ---------------------------------------------------

    def _resolve_payload_dir(self, explicit: Path | None) -> tuple[Path, bool]:
        """Where the payload actually is, and whether it is there at all.

        Order: what the caller said, then what the hopfile said (relative to the
        hopfile itself, because that is how the scanner wrote it), then the
        directory next to wherever hop is being run.
        """
        candidates: list[Path] = []
        if explicit is not None:
            candidates.append(Path(explicit))
        stamped = self.plan.hopfile.get("payload_dir")
        if stamped:
            origin = self.plan.hopfile.get("path")
            base = Path(str(origin)).parent if origin else Path.cwd()
            candidates.append(base / str(stamped))
        candidates.append(Path.cwd() / "hop-payload")
        for candidate in candidates:
            try:
                if candidate.is_dir():
                    return (candidate, True)
            except OSError:
                continue
        return (candidates[0], False)

    def _temp_root(self) -> Path:
        """Where the AUR helper gets built, on the machine being landed on.

        ``tempfile.gettempdir()`` answers for whichever machine is running hop,
        and the common case for a dry run is reading it on the Windows box you
        have not wiped yet. Printing ``C:\\Users\\...\\Temp`` in the middle of a
        transcript of Arch commands makes the reader doubt the rest of it, so
        the preview says /tmp and only a real POSIX run consults the environment.
        """
        if os.name == "posix":
            return Path(tempfile.gettempdir())
        return Path("/tmp")

    def _expand(self, target: str) -> Path:
        text = str(target).strip().replace("\\", "/")
        if text.startswith("~"):
            return self.home / text[1:].lstrip("/")
        return Path(text)

    def _inside_home(self, path: Path) -> bool:
        """True when a restore target really is under the user's home directory."""
        try:
            home = Path(os.path.abspath(self.home))
            target = Path(os.path.abspath(path))
        except (OSError, ValueError):
            return False
        return target == home or target.is_relative_to(home)

    def _pretty(self, path: Path) -> str:
        """Paths in the transcript read better with a forward slash and a ~."""
        text = Path(path).as_posix()
        home = self.home.as_posix()
        if home and text.startswith(home):
            return "~" + text[len(home):]
        return text


# --- module-level helpers -------------------------------------------------


def installed_packages() -> dict[str, set[str]]:
    """What is installed right now, as far as this machine can tell.

    Returns empty sets rather than raising when pacman or flatpak is missing,
    because ``hop diff`` is mostly run on the Windows machine by someone
    checking a plan before they commit to it.
    """
    return {
        "pacman": _query(["pacman", "-Qq"]),
        "flatpak": _query(["flatpak", "list", "--app", "--columns=application"]),
    }


def diff(plan: Plan, installed: dict[str, set[str]] | None = None) -> dict[str, list[str]]:
    """What the plan asks for and this machine has not got.

    ``extra`` is everything installed that the plan does not mention. It is
    informative only: most of it is dependencies pulled in by packages the plan
    did ask for, so a long list there is normal and not a problem to solve.
    """
    have = installed if installed is not None else installed_packages()
    pacman = have.get("pacman") or set()
    flatpak = have.get("flatpak") or set()

    wanted_pacman = list(plan.packages.get("pacman") or [])
    # AUR packages are built and then installed by pacman, so they show up in
    # the same database as everything else.
    wanted_aur = list(plan.packages.get("aur") or [])
    wanted_flatpak = list(plan.packages.get("flatpak") or [])
    planned = set(wanted_pacman) | set(wanted_aur)

    return {
        "missing_pacman": sorted(p for p in wanted_pacman if p not in pacman),
        "missing_aur": sorted(p for p in wanted_aur if p not in pacman),
        "missing_flatpak": sorted(p for p in wanted_flatpak if p not in flatpak),
        "extra": sorted(p for p in pacman if p not in planned),
    }


def _query(argv: list[str]) -> set[str]:
    try:
        done = subprocess.run(argv, check=False, capture_output=True, text=True)
    except OSError:
        return set()
    if done.returncode != 0:
        return set()
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def _rule(label: str) -> str:
    head = f"--- {label} "
    return head + "-" * max(3, _WRAP - len(head))


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _fmt_argv(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in argv)


def _plain(value: object, kind: str, where: str, consequence: str) -> str:
    """Return ``value`` as a string, or refuse the landing and say which line.

    See :data:`_PLAIN` for which values go through here and why the rest do not.
    """
    text = str(value)
    if not _PLAIN.fullmatch(text):
        raise LandError(
            f"the plan's {kind} is {text!r}, which is not a usable {kind}. hop land {consequence}, "
            f"so it stops here rather than guess what was meant. Fix {where} in the plan and run "
            "it again. Nothing has been installed, copied or changed."
        )
    return text


def _locales_in(plan: Plan) -> list[str]:
    """Every locale name the settings phase would write, in one list."""
    system = plan.system or {}
    found = [item for item in (system.get("locales") or []) if item]
    if system.get("locale"):
        found.append(system["locale"])
    return found


def _stays_inside(relative: str) -> bool:
    """True when a payload path stays inside the payload directory.

    The scanner writes plain relative paths and nothing else. An absolute path,
    a drive letter or a ``..`` segment makes both joins in this module land
    somewhere they were not meant to: hop would read a file it was never given
    and write it outside the directory it was asked to restore into. That is the
    same reasoning as the home-directory check on ``restore_to``, applied one
    step earlier so it also covers the entries that do not carry one — and for
    the same reason, which is that a hopfile can be edited, or handed to you by
    somebody else.
    """
    if not relative or relative.startswith("/"):
        return False
    if len(relative) > 1 and relative[1] == ":":  # C:\... survives the slash swap
        return False
    return ".." not in relative.split("/")


def _parse_mode(raw: object, kind: str) -> int:
    """The mode to restore a payload file with, bounded before it is trusted.

    The number arrives in the hopfile, and a hopfile is a file like any other:
    it can be edited, or handed to you by somebody else. Two bounds, for the two
    ways a number here can hurt.

    setuid, setgid and the sticky bit have no business on a restored config file
    or a copied key, so they are dropped whatever the file says. And the kinds
    that exist to be secret are reduced to the owner's read and write: hop cannot
    tell a private key from the public half of the same pair — both arrive as
    ``kind: "ssh"`` and the mode is the only signal — and the two mistakes are
    not the same size. A public key restored 0600 costs nothing. A private key
    restored 0644 is a key you have to treat as gone.

    The mask also settles what a nonsensical value means. ``"-1"`` and
    ``"77777"`` both parse, and both used to reach ``os.chmod`` as themselves.
    """
    secret = kind in ("ssh", "gpg", "wifi")
    default = 0o600 if secret else 0o644
    if raw is None:
        return default
    try:
        mode = int(str(raw), 8)
    except ValueError:
        return default
    return mode & (0o600 if secret else 0o777)


def _parent_mode(parent: Path) -> int:
    return 0o700 if parent.name in SECRET_DIRS else 0o755


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _redact(text: str, secret: str) -> str:
    """Belt and braces: nothing a command says about itself should leak a key."""
    if secret and secret in text:
        return text.replace(secret, "<password>")
    return text


def _short(text: str, limit: int = 72) -> str:
    """The first sentence of a step title, for the list of failures.

    Titles carry their reasoning in the sentences after the first one, and a
    hard character cut leaves the reader with half an argument.
    """
    head = text.split(". ")[0].strip().rstrip(".")
    if not head or len(head) > limit:
        head = text[: limit - 1].rstrip()
        return head + "…"
    return head + "."


def _walk_blockdevices(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        out.append(node)
        out += _walk_blockdevices(node.get("children") or [])
    return out


def _size_bytes(size: object) -> float:
    """lsblk sizes look like '931.5G'. Good enough to pick the biggest one."""
    text = str(size or "").strip().upper()
    if not text:
        return 0.0
    units = {"K": 1024.0, "M": 1024.0**2, "G": 1024.0**3, "T": 1024.0**4, "P": 1024.0**5}
    factor = units.get(text[-1], 1.0)
    if text[-1] in units:
        text = text[:-1]
    try:
        return float(text) * factor
    except ValueError:
        return 0.0
