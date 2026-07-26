"""The command line.

Every other module in this package returns strings and dictionaries and prints
nothing. This one is the exception: it parses arguments, decides what to write
where, and turns an exception into a sentence a worried person can act on. If a
message here ends in a traceback, that is a bug in this file.

Three conventions hold across every subcommand.

*Exit codes.* ``0`` means hop did what you asked. ``1`` means hop worked but the
answer is bad news — a landing with failed steps, a database that does not lint.
``2`` means hop could not do the thing at all: a file that is not there, a plan
from a future version, a flag that does not exist. A script can rely on that
split; so can you.

*Nothing is coloured.* hop's output gets pasted into issue threads and read over
shoulders on a machine that is mid-install, and escape codes survive neither
trip. Emphasis is done with words.

*Nothing is written that you did not name.* Every command that produces a file
takes ``-o``. Where a default path exists it is printed before the command
returns, so you always know what landed on the disk.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .archinstall import write_config
from .go import GoError, GoOptions
from .go import run as run_go
from .install import InstallError, InstallOptions
from .install import run as run_install
from .iso import IsoError
from .land import PHASES, Lander, LandError, diff, installed_packages
from .manifest import HopfileError, Manifest
from .mapping import Database, DatabaseError
from .plan import DESKTOPS, Plan, Planner
from .report import render_markdown, render_shell, render_summary
from .scrub import scrub
from .usb import UsbError

#: Anything hop raises on purpose. Caught in one place, printed as prose.
KNOWN_ERRORS = (
    HopfileError, DatabaseError, LandError, GoError, InstallError, IsoError, UsbError,
    ValueError, OSError,
)


# --- argument parsing -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hop",
        description=(
            "Move a Windows setup onto Arch Linux. Three verbs, in order: "
            "'hop scan' on the machine you are leaving, 'hop plan' anywhere, "
            "'hop land' on the machine you are building."
        ),
        epilog=(
            "hop plan never touches a disk. hop land changes nothing until you pass "
            "--execute, and --execute refuses to run anywhere that is not Arch — so "
            "the dry run is safe to read on the machine you have not wiped yet."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"hop2arch {__version__}")
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        help="the mapping database (data/ in the checkout). Found automatically when hop is run from the repo.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON on stdout instead of prose. Files are still written.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="say nothing except errors and the paths of files written.",
    )

    subs = parser.add_subparsers(dest="command", metavar="COMMAND")

    # First in the list because it is the answer to "how do I use this". The
    # verbs below it are what it is made of, and what you reach for when you
    # want to stop between the steps and look at something.
    go = subs.add_parser(
        "go",
        help="the whole move, from Windows: scan, plan, write the USB stick, reboot",
        description=(
            "Run on the Windows machine you are leaving. hop inventories it, plans "
            "the Arch system, shows you the report, and asks once. After that it "
            "fetches and verifies the Arch image, turns a USB stick into an "
            "installer carrying your plan and your files, and reboots into it. The "
            "installer asks a second question on the other side: which disk to "
            "erase. That one is the point of no return."
        ),
        epilog=(
            "Needs UEFI, administrator rights, and a USB stick you are willing to "
            "lose the contents of. Nothing on this machine's disks is touched by "
            "this command; the erase happens after the reboot, and only after you "
            "type the target disk's device path in full."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    go.add_argument("--hopfile", metavar="FILE", help="use an existing hopfile instead of scanning again")
    go.add_argument("-o", "--out", default="hop-out", metavar="DIR", help="where the hopfile, plan and report are written (default: hop-out)")
    go.add_argument("--device", metavar="ID", help="the USB stick to use, by device id. Omit it and hop lists what it found.")
    go.add_argument("--desktop", choices=sorted(DESKTOPS), default="plasma", help="which desktop to install (default: plasma, the one closest to Windows)")
    go.add_argument("--aur-helper", choices=("paru", "yay"), default="paru", help="which AUR helper the plan should use (default: paru)")
    go.add_argument("--prefer-flatpak", action="store_true", help="choose a flatpak over a repository package where both exist")
    go.add_argument("--no-gaming", action="store_true", help="leave out Steam, Proton helpers and Wine even if this machine plays games")
    go.add_argument("--hostname", metavar="NAME", help="name the new machine something else")
    go.add_argument("--with-secrets", action="store_true", help="carry private SSH keys and Wi-Fi passwords. They land unprotected on a FAT32 stick; guard it like ~/.ssh.")
    go.add_argument("--no-reboot", action="store_true", help="stop once the stick is ready and leave the reboot to you")
    go.add_argument("--yes", action="store_true", help="skip the confirmation. hop records in the transcript that nobody was asked.")
    go.set_defaults(func=cmd_go)

    inst = subs.add_parser(
        "install",
        help="install Arch onto this machine (runs inside the live ISO)",
        description=(
            "The far half of 'hop go'. Reads the disks that are present right now, "
            "shows the one it means to erase and everything on it, and requires "
            "that disk's device path typed out by hand before anything happens. "
            "The partition layout is computed from that live reading, never from "
            "the hopfile, which describes a machine as it was days ago."
        ),
        epilog=(
            "Started automatically by the stick 'hop go' wrote. Run it by hand if "
            "that did not happen; nothing about it depends on having been launched "
            "automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    inst.add_argument("--plan", metavar="FILE", help="the plan to install from (default: the one on the medium hop booted)")
    inst.add_argument("--target", metavar="DEVICE", help="the disk to erase, e.g. /dev/nvme0n1. Checked against what is actually here.")
    inst.add_argument("-o", "--out", metavar="DIR", help="where to write the generated archinstall configuration")
    inst.add_argument("--filesystem", default="ext4", help="root filesystem (default: ext4)")
    inst.add_argument("--dry-run", action="store_true", help="show the target, the layout and the commands, and stop")
    inst.add_argument("--yes", action="store_true", help="skip the confirmation. Nobody types the device path; hop says so in the transcript.")
    inst.set_defaults(func=cmd_install)

    scan = subs.add_parser(
        "scan",
        help="how to inventory the Windows machine (the scanner is a PowerShell script)",
        description=(
            "Print the exact command that inventories the machine you are leaving. "
            "hop does not run the scanner for you: it runs on Windows, where there "
            "is usually no Python at all."
        ),
    )
    scan.set_defaults(func=cmd_scan)

    plan = subs.add_parser(
        "plan",
        help="turn a hopfile into a package plan and a report",
        description=(
            "Read a hopfile, resolve every installed program through the mapping "
            "database, add the packages the hardware and locale imply, and score "
            "the result. Writes a plan and a report; changes nothing else."
        ),
    )
    plan.add_argument("hopfile", help="hopfile.json, as written by hop-scan.ps1")
    plan.add_argument("-o", "--out", default="hop-plan.json", metavar="FILE", help="where to write the plan (default: hop-plan.json)")
    plan.add_argument("--report", metavar="FILE", help="where to write the markdown report (default: alongside the plan, same name, .md)")
    plan.add_argument("--no-report", action="store_true", help="write the plan only")
    plan.add_argument("--desktop", choices=sorted(DESKTOPS), default="plasma", help="which desktop to install (default: plasma, the one closest to Windows)")
    plan.add_argument("--prefer-flatpak", action="store_true", help="choose a flatpak over a repository package where both exist")
    plan.add_argument("--aur-helper", choices=("paru", "yay"), default="paru", help="which AUR helper the plan should use (default: paru)")
    plan.add_argument("--no-gaming", action="store_true", help="leave out Steam, Proton helpers and Wine even if this machine plays games")
    plan.add_argument("--hostname", metavar="NAME", help="name the new machine something else")
    plan.add_argument("--show-ignored", action="store_true", help="include the ignored entries (redistributables, drivers) in the report")
    plan.set_defaults(func=cmd_plan)

    report = subs.add_parser(
        "report",
        help="re-render an existing plan",
        description="Render a plan you already have. Useful after you have edited one by hand.",
    )
    report.add_argument("plan", help="hop-plan.json")
    report.add_argument("--format", choices=("markdown", "summary", "shell"), default="markdown", help="markdown: the full report. summary: six lines. shell: the install commands.")
    report.add_argument("-o", "--out", metavar="FILE", help="write to a file instead of stdout")
    report.add_argument("--show-ignored", action="store_true", help="include the ignored entries (markdown only)")
    report.set_defaults(func=cmd_report)

    land = subs.add_parser(
        "land",
        help="carry out a plan on the new Arch machine",
        description=(
            "Install the packages, restore the payload, apply the settings. A dry "
            "run is the default: it prints every command and changes nothing. Read "
            "that transcript in full before you pass --execute."
        ),
        epilog=(
            "If you installed with archinstall and ran hop-post.sh, the packages and "
            "settings are already done — 'hop land hop-plan.json --only payload' "
            "restores your files and stops there."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    land.add_argument("plan", help="hop-plan.json")
    mode = land.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the transcript and change nothing (the default)")
    mode.add_argument("--execute", action="store_true", help="actually do it. Only after you have read the dry run.")
    land.add_argument("--only", metavar="PHASE[,PHASE]", help=f"run some phases only, comma-separated: {', '.join(PHASES)}")
    land.add_argument("--payload-dir", metavar="DIR", help="where the scanner's hop-payload directory is, if hop cannot find it")
    land.add_argument("--aur-helper", choices=("paru", "yay"), help="override the helper named in the plan")
    land.add_argument("--force", action="store_true", help="overwrite payload files that already exist on the new machine")
    land.set_defaults(func=cmd_land)

    install = subs.add_parser(
        "install-config",
        help="write an archinstall answer file and a post-install script",
        description=(
            "Generate user_configuration.json for archinstall plus hop-post.sh for "
            "the parts archinstall does not do. No disk layout and no password are "
            "written: the installer asks you both, with the real disks in front of you."
        ),
    )
    install.add_argument("plan", help="hop-plan.json")
    install.add_argument("-o", "--out", default="hop-archinstall", metavar="DIR", help="directory to write into (default: hop-archinstall)")
    install.add_argument("--disk-config", action="store_true", help="emit an empty disk_config block to fill in by hand. It installs nothing as written.")
    install.set_defaults(func=cmd_install_config)

    dif = subs.add_parser(
        "diff",
        help="what the plan asks for that this machine has not got",
        description=(
            "Compare a plan against the packages installed here. Run it on the Arch "
            "machine after landing; run it on the Windows machine and everything will "
            "read as missing, which is correct and not useful."
        ),
    )
    dif.add_argument("plan", help="hop-plan.json")
    dif.add_argument("--extra", action="store_true", help="also list packages installed here that the plan does not mention (mostly dependencies)")
    dif.set_defaults(func=cmd_diff)

    doctor = subs.add_parser(
        "doctor",
        help="check a hopfile and the mapping database for problems",
        description=(
            "Read a hopfile and say what is missing, wrong or dangerous about it. "
            "Everything doctor reports is advice; it exits 0 unless the file cannot "
            "be read at all."
        ),
    )
    doctor.add_argument("hopfile", nargs="?", help="hopfile.json (optional: without it, only the database is checked)")
    doctor.set_defaults(func=cmd_doctor)

    scr = subs.add_parser(
        "scrub",
        help="anonymise a hopfile so it can go in a bug report",
        description=(
            "Replace the account name, hostname, e-mail addresses, SSIDs, public keys "
            "and drive labels with stable stand-ins, and drop the payload index. "
            "Software, hardware, sizes and warnings are left exactly as scanned. "
            "Read the result before you post it."
        ),
    )
    scr.add_argument("hopfile", help="hopfile.json")
    scr.add_argument("-o", "--out", metavar="FILE", help="where to write it (default: alongside the original, .scrubbed.json)")
    scr.add_argument("--salt", default="", metavar="TEXT", help="mix a private string into the stand-ins, so nobody can confirm a guess at your hostname by hashing it")
    scr.set_defaults(func=cmd_scrub)

    db = subs.add_parser(
        "db",
        help="inspect the mapping database",
        description="The mapping database is hop/data/packages.toml. These are the tools for working on it.",
    )
    db_subs = db.add_subparsers(dest="db_command", metavar="SUBCOMMAND")
    db_lint = db_subs.add_parser("lint", help="problems a contributor should fix before opening the pull request")
    db_lint.set_defaults(func=cmd_db_lint)
    db_stats = db_subs.add_parser("stats", help="how many rules there are, by strategy and confidence")
    db_stats.set_defaults(func=cmd_db_stats)
    db_search = db_subs.add_parser("search", help="find the rule that would match a program")
    db_search.add_argument("needle", help="part of a name, an id, or a package name")
    db_search.set_defaults(func=cmd_db_search)
    db.set_defaults(func=cmd_db_help, db_parser=db)

    return parser


# --- subcommands ----------------------------------------------------------


def cmd_scan(args: argparse.Namespace, out: TextIO) -> int:
    script = Path(__file__).resolve().parent.parent / "windows" / "hop-scan.ps1"
    location = str(script) if script.exists() else "windows\\hop-scan.ps1 from the hop2arch checkout"
    if args.json:
        _dump({"scanner": str(script), "found": script.exists()}, out)
        return 0
    _say(out, "The scanner is a PowerShell script, and it runs on the machine you are leaving.")
    _say(out, "hop does not start it for you: that machine usually has no Python on it, and")
    _say(out, "the scan takes minutes, so you want its progress on your screen and not")
    _say(out, "buffered inside another program.")
    _say(out)
    _say(out, "In a PowerShell window, in the folder you want the output in:")
    _say(out)
    _say(out, f'    powershell -NoProfile -ExecutionPolicy Bypass -File "{location}"')
    _say(out)
    _say(out, "It reads and writes; it changes nothing else on the machine, and it sends")
    _say(out, "nothing anywhere. You get hopfile.json and hop-payload\\ in that folder.")
    _say(out)
    _say(out, "Add -WithSecrets to copy your private SSH keys and your Wi-Fi passwords into")
    _say(out, "the payload. Without it they are left behind, and you will be typing the")
    _say(out, "Wi-Fi password in by hand on the other side. With it, treat that folder as")
    _say(out, "you would treat ~/.ssh, because that is what is in it.")
    _say(out)
    _say(out, "Then, on any machine:  hop plan hopfile.json")
    return 0


def cmd_plan(args: argparse.Namespace, out: TextIO) -> int:
    manifest = Manifest.load(args.hopfile)
    db = _database(args)
    plan = Planner(
        manifest,
        db,
        desktop=args.desktop,
        prefer_flatpak=args.prefer_flatpak,
        aur_helper=args.aur_helper,
        include_gaming=not args.no_gaming,
        hostname=args.hostname,
    ).build()

    written: list[Path] = []
    plan_path = Path(args.out)
    _write(plan_path, json.dumps(plan.to_dict(), indent=2, ensure_ascii=False) + "\n")
    written.append(plan_path)

    if not args.no_report:
        report_path = Path(args.report) if args.report else plan_path.with_suffix(".md")
        _write(report_path, render_markdown(plan, show_ignored=args.show_ignored))
        written.append(report_path)

    if args.json:
        _dump(plan.to_dict(), out)
        return 0
    if not args.quiet:
        _say(out, render_summary(plan))
        _say(out)
    for path in written:
        _say(out, f"wrote {path}")
    if not args.quiet:
        _say(out)
        if len(written) > 1:
            _say(out, f"Read {written[1]} before you do anything else. It is the honest version.")
        _say(out, f"Then: hop land {plan_path} — a dry run by default, so it prints and changes nothing.")
    return 0


def cmd_report(args: argparse.Namespace, out: TextIO) -> int:
    plan = _plan_for_report(args)
    if args.format == "summary":
        text = render_summary(plan) + "\n"
    elif args.format == "shell":
        text = render_shell(plan)
    else:
        text = render_markdown(plan, show_ignored=args.show_ignored)
    if args.out:
        path = Path(args.out)
        _write(path, text)
        _say(out, f"wrote {path}")
        return 0
    out.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_land(args: argparse.Namespace, out: TextIO) -> int:
    plan = _load_plan(args.plan)
    phases = _phases(args.only)
    lander = Lander(
        plan,
        payload_dir=Path(args.payload_dir) if args.payload_dir else None,
        dry_run=not args.execute,
        phases=phases,
        aur_helper=args.aur_helper,
        force=args.force,
        out=out,
    )
    if args.json:
        _dump(
            {
                "dry_run": not args.execute,
                "phases": list(lander.phases),
                "payload_dir": str(lander.payload_dir),
                "payload_found": lander.payload_found,
                "steps": [
                    {
                        "phase": step.phase,
                        "title": step.title,
                        "command": step.command,
                        "kind": step.kind,
                        "detail": step.detail,
                        "optional": step.optional,
                    }
                    for step in lander.steps()
                ],
            },
            out,
        )
        return 0
    return lander.run()


def cmd_go(args: argparse.Namespace, out: TextIO) -> int:
    """The whole move. Everything it does is in hop/go.py; this only translates flags."""
    options = GoOptions(
        hopfile=Path(args.hopfile) if args.hopfile else None,
        out_dir=Path(args.out),
        device_id=args.device,
        desktop=args.desktop,
        aur_helper=args.aur_helper,
        prefer_flatpak=args.prefer_flatpak,
        include_gaming=not args.no_gaming,
        hostname=args.hostname,
        with_secrets=args.with_secrets,
        assume_yes=args.yes,
        reboot=not args.no_reboot,
    )
    return run_go(options, out=out)


def cmd_install(args: argparse.Namespace, out: TextIO) -> int:
    """Install onto this machine. Only ever run from inside the live environment."""
    # out_dir is not optional in InstallOptions — it has a real default, and
    # passing None to say "unset" replaces that default with something Path()
    # cannot take. Only override what was actually asked for.
    overrides: dict[str, Any] = {
        "plan": Path(args.plan) if args.plan else None,
        "target": args.target,
        "filesystem": args.filesystem,
        "assume_yes": args.yes,
        "dry_run": args.dry_run,
    }
    if args.out:
        overrides["out_dir"] = Path(args.out)
    return run_install(InstallOptions(**overrides), out=out)


def cmd_install_config(args: argparse.Namespace, out: TextIO) -> int:
    plan = _load_plan(args.plan)
    paths = write_config(plan, args.out, disk_config=args.disk_config)
    if args.json:
        _dump({"written": [str(p) for p in paths]}, out)
        return 0
    for path in paths:
        _say(out, f"wrote {path}")
    if args.quiet:
        return 0
    _say(out)
    _say(out, "Copy both onto the USB stick you are installing from, then:")
    _say(out)
    _say(out, f"    archinstall --config {paths[0].name}")
    _say(out)
    _say(out, "archinstall will still ask which disk to install to. That question is not")
    _say(out, "answered in the file and there is no flag that answers it: the plan was built")
    _say(out, "from a snapshot of another machine, and a snapshot is not a safe basis for")
    _say(out, "erasing a drive. It will not ask for a password either — it asks that itself,")
    _say(out, "in the installer, where you can see what you are typing.")
    _say(out)
    _say(out, f"After the first boot, as yourself: ./{paths[1].name}")
    _say(out, "That does the AUR, the flatpaks, the services and the locale. Then run")
    _say(out, "'hop land hop-plan.json --only payload' to put your own files back.")
    return 0


def cmd_diff(args: argparse.Namespace, out: TextIO) -> int:
    plan = _load_plan(args.plan)
    have = installed_packages()
    result = diff(plan, have)
    if args.json:
        _dump(result, out)
        return 0

    if not (have["pacman"] or have["flatpak"]):
        _say(out, "No pacman and no flatpak on this machine, so nothing could be queried and")
        _say(out, "the whole plan reads as missing. That is the expected answer on the machine")
        _say(out, "you are leaving; run this again once Arch has booted.")
        _say(out)

    missing = 0
    for key, label in (
        ("missing_pacman", "from the official repositories"),
        ("missing_aur", "from the AUR"),
        ("missing_flatpak", "from Flathub"),
    ):
        names = result[key]
        if not names:
            continue
        missing += len(names)
        _say(out, f"{len(names)} package(s) {label} are in the plan and not installed:")
        _say(out, _columns(names))
        _say(out)

    if not missing:
        _say(out, "Everything the plan asks for is installed.")
    else:
        _say(out, f"{missing} package(s) still to go. 'hop land {args.plan} --only packages' installs them.")
    if args.extra and result["extra"]:
        _say(out)
        _say(out, f"{len(result['extra'])} package(s) installed here that the plan does not mention.")
        _say(out, "Most of these are dependencies pulled in by packages it did ask for, so a long")
        _say(out, "list is normal and nothing to act on.")
        _say(out, _columns(result["extra"]))
    return 0


def cmd_doctor(args: argparse.Namespace, out: TextIO) -> int:
    findings: dict[str, Any] = {"hopfile": [], "database": [], "notes": []}

    db = _database(args)
    findings["database"] = db.lint()
    findings["notes"].append(
        "mapping database: " + ", ".join(f"{k} {v}" for k, v in sorted(db.stats().items()) if not k.startswith(("strategy", "confidence")))
    )

    manifest = None
    if args.hopfile:
        manifest = Manifest.load(args.hopfile)
        findings["hopfile"] = manifest.lint()
        if manifest.warnings:
            findings["notes"].append(f"the scanner itself left {len(manifest.warnings)} warning(s) in the file")

    if args.json:
        _dump(findings, out)
        return 0

    if manifest is not None:
        _say(out, f"hopfile      {args.hopfile}")
        _say(out, f"             {len(manifest.software)} program(s), generated {manifest.generated_at} by {manifest.generator}")
        _say(out)
        if findings["hopfile"]:
            _say(out, "Things about this hopfile worth knowing before you plan from it:")
            _say(out)
            for problem in findings["hopfile"]:
                _say(out, f"  - {problem}")
            _say(out)
        else:
            _say(out, "Nothing wrong with the hopfile.")
            _say(out)
        if manifest.warnings:
            # Not all of these are failures. The scanner uses this array for
            # anything a human has to look at — BitLocker being on, a size that
            # is an estimate, a payload that holds a Wi-Fi password — so the
            # heading must not tell the reader they are all read errors.
            _say(out, "Notes the scanner left on this machine:")
            _say(out)
            for warning in manifest.warnings:
                _say(out, f"  - {warning}")
            _say(out)

    if findings["database"]:
        _say(out, f"The mapping database has {len(findings['database'])} problem(s):")
        _say(out)
        for problem in findings["database"]:
            _say(out, f"  - {problem}")
        _say(out)
    else:
        _say(out, f"Mapping database is clean: {db.stats()['apps']} rules, {db.stats()['ignore']} ignore rules, {db.stats()['games']} games.")
        _say(out)

    _say(out, "None of the above stops you planning. doctor reports; it does not refuse.")
    return 0


def cmd_scrub(args: argparse.Namespace, out: TextIO) -> int:
    source = Path(args.hopfile)
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise HopfileError(f"no such hopfile: {source}") from exc
    except json.JSONDecodeError as exc:
        raise HopfileError(f"{source} is not valid JSON: {exc}") from exc

    clean, report = scrub(raw, salt=args.salt)
    destination = Path(args.out) if args.out else source.with_suffix(".scrubbed.json")
    _write(destination, json.dumps(clean, indent=2, ensure_ascii=False) + "\n")

    if args.json:
        _dump(
            {
                "written": str(destination),
                "replacements": report.replacements,
                "removed": report.removed,
                "notes": report.notes,
            },
            out,
        )
        return 0

    _say(out, f"wrote {destination}")
    if args.quiet:
        return 0
    _say(out)
    if report.replacements:
        _say(out, f"{len(report.replacements)} value(s) replaced:")
        _say(out)
        for original, standin in sorted(report.replacements.items()):
            _say(out, f"  {original}  ->  {standin}")
        _say(out)
    if report.removed:
        _say(out, f"{len(report.removed)} key(s) removed outright: " + ", ".join(report.removed[:12])
             + (" ..." if len(report.removed) > 12 else ""))
        _say(out)
    for note in report.notes:
        _say(out, f"- {note}")
    if report.notes:
        _say(out)
    _say(out, "This is not anonymity. The exact set of programs you have installed is close to")
    _say(out, "unique, and anyone who already knows what you own can recognise the file. Open")
    _say(out, "it and read it before you attach it to anything.")
    return 0


def cmd_db_lint(args: argparse.Namespace, out: TextIO) -> int:
    db = _database(args)
    problems = db.lint()
    if args.json:
        _dump({"problems": problems}, out)
        return 1 if problems else 0
    if not problems:
        stats = db.stats()
        _say(out, f"{stats['apps']} rules, {stats['ignore']} ignore rules, {stats['games']} games. No problems.")
        return 0
    _say(out, f"{len(problems)} problem(s):")
    _say(out)
    for problem in problems:
        _say(out, f"  - {problem}")
    return 1


def cmd_db_stats(args: argparse.Namespace, out: TextIO) -> int:
    db = _database(args)
    stats = db.stats()
    if args.json:
        _dump(stats, out)
        return 0
    for key in ("apps", "ignore", "games"):
        _say(out, f"{key:<24} {stats.get(key, 0)}")
    _say(out)
    for key, value in sorted(stats.items()):
        if key.startswith(("strategy:", "confidence:")):
            _say(out, f"{key:<24} {value}")
    return 0


def cmd_db_search(args: argparse.Namespace, out: TextIO) -> int:
    db = _database(args)
    rules = db.search(args.needle)
    if args.json:
        _dump(
            [
                {
                    "id": r.id,
                    "name": r.name,
                    "strategy": r.strategy,
                    "pacman": list(r.pacman),
                    "aur": list(r.aur),
                    "flatpak": list(r.flatpak),
                    "replacement": r.replacement,
                    "notes": r.notes,
                    "confidence": r.confidence,
                }
                for r in rules
            ],
            out,
        )
        return 0
    if not rules:
        _say(out, f"Nothing in the database matches {args.needle!r}.")
        _say(out, "If it is a program you use, adding it is a two-line change to hop/data/packages.toml.")
        return 0
    for rule in rules:
        chosen = rule.preferred()
        package = f"{chosen[1]} ({chosen[0]})" if chosen else "no package"
        _say(out, f"{rule.id}  [{rule.strategy}]  {package}")
        if rule.replacement:
            _say(out, f"    -> {rule.replacement}")
        if rule.notes:
            _say(out, f"    {rule.notes}")
        _say(out)
    return 0


def cmd_db_help(args: argparse.Namespace, out: TextIO) -> int:
    args.db_parser.print_help(out)
    return 2


# --- plumbing -------------------------------------------------------------


def _database(args: argparse.Namespace) -> Database:
    return Database.load(args.data_dir)


def _load_plan(path: str) -> Plan:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise HopfileError(
            f"no such plan: {p}. A plan is what 'hop plan' writes; if you have not run it yet, "
            "start there."
        ) from exc
    except json.JSONDecodeError as exc:
        raise HopfileError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise HopfileError(f"{p} is JSON but not a plan (a plan is an object, this is a {type(raw).__name__})")
    if "plan_version" not in raw and "hopfile_version" in raw:
        raise HopfileError(
            f"{p} is a hopfile, not a plan. Run 'hop plan {p}' first; that writes the plan this "
            "command wants."
        )
    return Plan.from_dict(raw)


def _plan_for_report(args: argparse.Namespace) -> Plan:
    """Take either file, because nobody remembers which of the two this wants.

    ``hop land`` is right to insist on a plan: it acts, and it should act on the
    document the user read. ``hop report`` only reads, so refusing a hopfile
    would be pedantry — it plans one on the fly with the default answers and says
    so on stderr, where the note cannot end up inside a redirected report.
    """
    path = Path(args.plan)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _load_plan(args.plan)  # let the real loader phrase the complaint
    if isinstance(raw, dict) and "plan_version" not in raw and "hopfile_version" in raw:
        manifest = Manifest.from_dict(raw, path=path)
        print(
            f"note: {path} is a hopfile, so hop planned it with the defaults "
            "(KDE Plasma, paru, gaming on). Run 'hop plan' if you want different answers.",
            file=sys.stderr,
        )
        return Planner(manifest, _database(args)).build()
    return _load_plan(args.plan)


def _phases(only: str | None) -> tuple[str, ...]:
    if not only:
        return PHASES
    asked = [part.strip().lower() for part in only.split(",") if part.strip()]
    unknown = [p for p in asked if p not in PHASES]
    if unknown:
        raise LandError(
            f"unknown phase(s) {', '.join(unknown)}; the phases are {', '.join(PHASES)}"
        )
    return tuple(asked)


def _write(path: Path, text: str) -> None:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    # Unix line endings even on Windows: these files are read on the Arch side,
    # and bash will not run a script with carriage returns in it.
    path.write_text(text, encoding="utf-8", newline="\n")


def _dump(obj: Any, out: TextIO) -> None:
    out.write(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


def _say(out: TextIO, text: str = "") -> None:
    out.write(text + "\n")


def _columns(names: list[str], width: int = 76) -> str:
    """A long list of package names, wrapped, indented, still greppable."""
    lines: list[str] = []
    current = "  "
    for name in names:
        if len(current) + len(name) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = "  "
        current += name + " "
    if current.strip():
        lines.append(current.rstrip())
    return "\n".join(lines)


def _utf8_stdout() -> None:
    """Make stdout and stderr carry the characters hop actually writes.

    On Windows, Python encodes a redirected stream with the legacy ANSI code
    page rather than UTF-8. The report contains a progress bar and arrows that
    cp1251 has no room for, so 'hop report plan.json > report.md' — an obvious
    thing to type — died with a charmap error and wrote an empty file. The
    report is the one output that has to survive being redirected, mailed and
    pasted into an issue, so it is written as UTF-8 everywhere.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # A stream that will not be reconfigured is not worth failing over; the
        # worst case is the mangled character it would have shown anyway.
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stdout)
        return 2
    # Subcommand parsers do not inherit the global flags, so anything the
    # subcommand does not define has to be filled in before the handler sees it.
    for flag in ("json", "quiet", "data_dir"):
        args.__dict__.setdefault(flag, None)

    try:
        return args.func(args, sys.stdout)
    except KNOWN_ERRORS as exc:
        sys.stdout.flush()
        print(f"hop: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nhop: stopped. Nothing was left half-done that is not named above.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
