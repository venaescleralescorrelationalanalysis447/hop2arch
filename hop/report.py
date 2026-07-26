"""The human-facing half of a plan.

Someone is about to erase the operating system they have used for fifteen years.
The report's job is to tell them the truth about what happens to their stuff, in
an order that respects their anxiety: what breaks first, what changes, what is
identical, and only then the long boring tables.
"""

from __future__ import annotations

import shlex

from .manifest import human_bytes
from .plan import Plan

STRATEGY_HEADINGS = {
    "none": ("Blockers", "No Linux path. Decide what you are doing about these before you wipe anything."),
    "web": ("Web only", "No desktop client. The browser version is the whole story."),
    "compat": ("Runs through Wine/Proton", "Keeps working, with an extra layer underneath it. Expect the occasional rough edge."),
    # Not "you do not lose the capability". That blurb sat directly above the row
    # that swaps Photoshop for Krita and the one that swaps Lightroom for
    # darktable, whose own note says the develop settings do not transfer — the
    # section headline was answering the reader's real question before the notes
    # got to, and answering it more warmly than the data does.
    "alternative": ("Different program, same job",
                    "Something else here does the work. How close it gets is the note beside each one; read those before you count on any of them."),
    "native": ("Comes with you unchanged", "Same application, often the same version number."),
    "builtin": ("Already in the box", "Arch and your desktop cover these; nothing to install."),
}

SOURCE_LABEL = {"pacman": "repo", "aur": "AUR", "flatpak": "flatpak"}


def render_markdown(plan: Plan, show_ignored: bool = False) -> str:
    out: list[str] = []
    w = out.append
    score = plan.score
    system = plan.system

    w(f"# Hop report — {plan.hopfile.get('hostname', 'this machine')}")
    w("")
    w(f"> {score.get('verdict', '')}")
    w("")
    percent = score.get("hoppability")
    if percent is None:
        # A scan that read nothing gets no number and no progress bar. An empty
        # bar would still read as an answer, and this is not one.
        w("**Hoppability: not measured** — no installed software was recorded, "
          "so there is nothing to weigh.")
        w("")
    else:
        w(f"**Hoppability: {percent}%** "
          f"({score.get('matched', 0)} of {score.get('considered', 0)} programs resolved, "
          f"{score.get('unknown', 0)} unknown, {score.get('blockers', 0)} blockers)")
        w("")
        w(_bar(percent))
        w("")

    # --- warnings ---------------------------------------------------------
    # Above the machine description, not below it. The section is called "Read
    # these first" and it is where "your disk is encrypted, export the recovery
    # key before you repartition" lands. Anything that can cost the reader data
    # they cannot get back outranks a table about which desktop they get.
    if plan.warnings:
        w("## Read these first")
        w("")
        for warning in plan.warnings:
            w(f"- {warning}")
        w("")

    # --- the machine you are building ------------------------------------
    w("## What you are landing on")
    w("")
    rows = [
        ("Desktop", f"{plan.target.get('desktop_label', '?')} — {plan.target.get('desktop_rationale', '')}"),
        ("Locale", f"`{system.get('locale')}`"),
        ("Timezone", f"`{system.get('timezone')}`"),
        ("Keyboard", ", ".join(f"`{k}`" for k in system.get("x11_layouts") or [])),
        ("Firmware", f"{system.get('firmware')}" + (" (Secure Boot on)" if system.get("secure_boot") else "")),
        ("Graphics", ", ".join(system.get("gpu_vendors") or ["unknown"])),
        ("Hostname / user", f"`{system.get('hostname')}` / `{system.get('username')}`"),
    ]
    w("| | |")
    w("|---|---|")
    for key, value in rows:
        w(f"| **{key}** | {value} |")
    w("")
    # ``or []`` throughout this function rather than a default on .get(): a plan
    # is edited by hand, and a section emptied with null is not the same object
    # as a section that is absent. The report is the document somebody reads to
    # decide whether to wipe a disk, and it has to render from whatever the file
    # actually says.
    counts = {k: len(v or []) for k, v in plan.packages.items()}
    w(f"That is **{counts.get('pacman', 0)}** packages from the official repos, "
      f"**{counts.get('aur', 0)}** from the AUR"
      + (f" (via `{plan.target.get('aur_helper')}`)" if counts.get("aur") else "")
      + (f" and **{counts['flatpak']}** flatpaks" if counts.get("flatpak") else "")
      + ".")
    w("")

    # --- programs, worst news first --------------------------------------
    for strategy in ("none", "web", "compat", "alternative", "native", "builtin"):
        items = plan.by_strategy(strategy)
        if not items:
            continue
        heading, blurb = STRATEGY_HEADINGS[strategy]
        w(f"## {heading} ({len(items)})")
        w("")
        w(f"*{blurb}*")
        w("")
        if strategy in ("none", "web"):
            for item in items:
                # Escaped like every other cell. A newline here would end the
                # bullet and leave the rest of the sentence loose on the page,
                # in the one section that exists to be read carefully.
                line = f"- **{_esc(item.source)}** — {_esc(item.notes) or 'no Linux equivalent.'}"
                # A blocker or a web-only service can still have something
                # installable next to it — sometimes a community wrapper around
                # the same web app, sometimes a different program that does a
                # similar job. Name it without claiming which, because calling
                # LibreOffice an "unofficial Microsoft 365 client" would be a
                # small lie in the one section that exists to avoid them. The
                # note above already says what is actually lost.
                if item.package:
                    label = SOURCE_LABEL.get(item.install_source, item.install_source)
                    line += f" Closest thing hop can install: `{item.package}` ({label})."
                w(line)
            w("")
            continue
        # Only 'alternative' has a second name worth a column. For every other
        # strategy PlanItem.title falls back to the rule's canonical name, which
        # is the name of the *Windows* program: printing it under "On Arch" put
        # "Git -> Git for Windows" and "Windows Subsystem for Linux -> Windows
        # Subsystem for Linux" in front of a reader who is already unsure what
        # survives the move.
        swaps = strategy == "alternative"
        w("| On Windows | On Arch | Install | Notes |" if swaps else "| Program | Install | Notes |")
        w("|---|---|---|---|" if swaps else "|---|---|---|")
        for item in items:
            install = "—"
            if item.package:
                install = f"`{item.package}` ({SOURCE_LABEL.get(item.install_source, item.install_source)})"
            note = item.notes or ""
            if item.confidence != "high":
                note = f"({item.confidence} confidence) {note}"
            cells = [_esc(item.source)]
            if swaps:
                cells.append(_esc(item.title))
            cells += [install, _esc(note)]
            w("| " + " | ".join(cells) + " |")
        w("")

    # --- unknowns ---------------------------------------------------------
    if plan.unknown:
        w(f"## Not in the database ({len(plan.unknown)})")
        w("")
        w("hop has no opinion on these. Some are genuinely obscure; some are just missing "
          "from `hop/data/packages.toml` and would be a two-line pull request.")
        w("")
        for entry in plan.unknown:
            version = f" {entry['version']}" if entry.get("version") else ""
            publisher = f" — {entry['publisher']}" if entry.get("publisher") else ""
            w(f"- {_esc(str(entry.get('name', '')))}{_esc(version)}{_esc(publisher)}")
        w("")

    # --- games ------------------------------------------------------------
    games = plan.games or {}
    if games.get("total"):
        gcounts = games.get("counts") or {}
        w(f"## Games ({games['total']} installed)")
        w("")
        w(f"{gcounts.get('works', 0)} play, {gcounts.get('broken', 0)} partly broken, "
          f"{gcounts.get('blocked', 0)} blocked by anti-cheat, {gcounts.get('unknown', 0)} unknown. "
          "This is a local snapshot — protondb.com is the live answer.")
        w("")
        w("| Game | Status | Why |")
        w("|---|---|---|")
        for title in games.get("titles") or []:
            w(f"| {_esc(title['name'])} | {_status_badge(title['status'])} | {_esc(title.get('reason', ''))} |")
        w("")

    # --- data to carry ----------------------------------------------------
    data = plan.data or {}
    folders = data.get("folders") or []
    carried = [i for i in plan.items if i.carry]
    if carried or plan.payload or folders:
        w("## Data worth carrying")
        w("")
        if folders:
            w(f"Your profile folders hold **{human_bytes(data.get('total_bytes', 0))}**. "
              "None of it survives the installer — copy it off the machine first, to an "
              "external disk or another computer, and check you can open it there.")
            w("")
            w("| Folder | Size | Files |")
            w("|---|---:|---:|")
            for folder in folders:
                w(f"| {_esc(folder.get('name', ''))} | {human_bytes(folder.get('size_bytes', 0))} "
                  f"| {folder.get('files', 0)} |")
            w("")
            extras = []
            if data.get("steam_bytes"):
                extras.append(
                    f"Steam libraries add another {human_bytes(data['steam_bytes'])}, but those are "
                    "re-downloadable — save the saves, not the games."
                )
            onedrive = data.get("onedrive") or {}
            if onedrive.get("present"):
                extras.append(
                    "OneDrive is set up on this machine. Files marked *online-only* are not on the "
                    "disk at all: they come back from the web, and only if you still have the "
                    "account. Make sure the folder is fully downloaded before you wipe."
                )
            for line in extras:
                w(f"- {line}")
            if extras:
                w("")
        if plan.payload:
            w(f"The scanner already put {len(plan.payload)} file(s) in the payload directory "
              "— `hop land` restores them for you:")
            w("")
            for entry in plan.payload[:40]:
                dest = entry.get("restore_to") or "(imported, not copied)"
                w(f"- `{entry.get('path')}` → `{dest}`")
            if len(plan.payload) > 40:
                w(f"- …and {len(plan.payload) - 40} more")
            w("")
        if carried:
            w("Application data still sitting on the Windows partition, worth copying by hand:")
            w("")
            for item in carried:
                for path in item.carry:
                    w(f"- **{_esc(item.source)}**: `%USERPROFILE%\\{path.replace('/', chr(92))}`")
            w("")

    if show_ignored and plan.ignored:
        w(f"<details><summary>Ignored entries ({len(plan.ignored)})</summary>")
        w("")
        for entry in plan.ignored:
            w(f"- {_esc(str(entry.get('name', '')))} — {_esc(str(entry.get('reason', '')))}")
        w("")
        w("</details>")
        w("")

    w("---")
    w("")
    w("Generated by [hop2arch](https://github.com/Ramirmir/hop2arch). "
      "The mapping database lives in `hop/data/packages.toml`; if hop got something wrong about "
      "your setup, that file is where to fix it.")
    w("")
    return "\n".join(out)


def render_summary(plan: Plan) -> str:
    """The short version, for the terminal."""
    score = plan.score
    percent = score.get("hoppability")
    headline = "not measured" if percent is None else f"{percent}%"
    lines = [
        f"hoppability   {headline}   {score.get('verdict', '')}",
        f"programs      {score.get('matched', 0)} resolved, {score.get('unknown', 0)} unknown, "
        f"{score.get('ignored', 0)} ignored",
    ]
    by = score.get("by_strategy") or {}
    if by:
        parts = [f"{count} {name}" for name, count in sorted(by.items(), key=lambda kv: -kv[1])]
        lines.append("breakdown     " + ", ".join(parts))
    packages = plan.packages
    lines.append(
        "packages      "
        + ", ".join(f"{len(v)} {k}" for k, v in packages.items() if v)
    )
    games = plan.games or {}
    if games.get("total"):
        c = games.get("counts") or {}
        lines.append(f"games         {games['total']} installed — {c.get('works', 0)} play, {c.get('blocked', 0)} blocked")
    data = plan.data or {}
    if data.get("total_bytes"):
        lines.append(f"user data     {human_bytes(data['total_bytes'])} in the profile folders — back it up first")
    if plan.blockers:
        lines.append("blockers      " + ", ".join(i.source for i in plan.blockers[:5])
                     + (" …" if len(plan.blockers) > 5 else ""))
    return "\n".join(lines)


def render_shell(plan: Plan) -> str:
    """The plan as a script you could read, understand, and run by hand."""
    out = ["#!/usr/bin/env bash", "# Generated by hop2arch — read it before you run it.", "set -euo pipefail", ""]
    pacman = plan.packages.get("pacman", [])
    aur = plan.packages.get("aur", [])
    flatpak = plan.packages.get("flatpak", [])
    if pacman:
        out += ["# Official repositories", "sudo pacman -S --needed \\", *_wrap(pacman), ""]
    if aur:
        helper = plan.target.get("aur_helper", "paru")
        out += [f"# AUR (via {helper})", f"{helper} -S --needed \\", *_wrap(aur), ""]
    if flatpak:
        out += ["# Flatpak", "flatpak install -y flathub \\", *_wrap(flatpak), ""]
    return "\n".join(out)


def _wrap(packages: list[str], per_line: int = 4) -> list[str]:
    """Package names, four to a line, quoted for the shell they are going into.

    An Arch package name never needs quoting, so for a plan hop wrote this
    changes nothing you can see. It matters because ``render_shell`` reads a
    *plan file*, and a plan file is a document hop invites people to edit and
    pass around — the header two lines above these tells the reader the script
    is safe to read before running, and an unquoted name with a semicolon in it
    would make that sentence untrue. Quoted, a nonsense name is a package pacman
    cannot find, which is a message rather than an event.
    """
    quoted = [shlex.quote(str(name)) for name in packages]
    lines = []
    chunks = [quoted[i : i + per_line] for i in range(0, len(quoted), per_line)]
    for index, chunk in enumerate(chunks):
        suffix = " \\" if index < len(chunks) - 1 else ""
        lines.append("  " + " ".join(chunk) + suffix)
    return lines


def _bar(percent: float, width: int = 40) -> str:
    filled = int(round(width * max(0.0, min(100.0, percent)) / 100.0))
    return "`" + "█" * filled + "░" * (width - filled) + "`"


def _status_badge(status: str) -> str:
    return {
        "works": "plays",
        "blocked": "**blocked**",
        "broken": "partly broken",
        "unknown": "unknown",
    }.get(status, status)


def _esc(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")
