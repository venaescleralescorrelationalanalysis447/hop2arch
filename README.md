# hop2arch

Leaving Windows for Arch Linux means erasing an operating system you have lived in for years, and
the hard part is not the installer — it is knowing, before you commit, what you are giving up.
hop2arch is not a distribution and not an installer. It is a bridge: it inventories the machine you
are leaving, says in plain words which of your programs come with you, which get replaced by
something that does the same job, and which have no path at all, and then it builds the machine you
arrive on.

The command is `hop`. One verb does the whole move — `hop go` — and three do it a step at a time.

## What hop cannot do

- **It does not choose which disk to erase.** `hop go` builds the installer and starts it, but the
  disk that Arch goes onto is read from the machine in front of you at the moment of install, and
  you type its device path by hand before anything happens to it. `hop scan` and `hop plan` never
  touch a disk at all.
- **It does not move your licences.** Adobe, Affinity, Microsoft 365, Visual Studio: hop can tell
  you what the Linux answer is, but the subscription you pay for does not follow you, and neither do
  the project files. `.prproj` does not open in anything on this side.
- **It does not make anti-cheat work.** If Destiny 2 or Rainbow Six Siege is why you turn the
  machine on, hop will say so, and the game will still not run. That is the publisher's decision,
  not a configuration problem.
- **It does not back up your data.** hop measures your folders and tells you how large they are.
  Copying them off the machine is your job, and it has to happen before the installer touches the
  partition table.
- **It cannot tell you whether the trade is worth it.** It gives you a number and an itemised list.
  The decision is yours, and for some people the honest answer is to stay on Windows.

## The one command

```
hop go
```

Run it on the Windows machine. It inventories the machine, plans the Arch system, shows you the
report, and asks once. After that it fetches the current Arch image and checks it, turns a USB stick
into an installer carrying your plan and your files, and reboots into it. On the other side the
installer reads the disks that are actually present, shows you the one it means to erase and
everything on it, and waits for you to type that disk's device path in full. Then it installs, and
after the first boot your packages, keys, Wi-Fi and settings are put back.

Two moments involve a human, and they are different sizes:

1. **On Windows, one confirmation.** Everything before it can be stopped with Ctrl+C and leaves the
   machine exactly as it was. hop erases one USB stick and touches no disk inside the machine.
2. **In the installer, the device path typed by hand.** This is the point of no return. It asks for
   `/dev/nvme0n1` rather than `yes` on purpose: typing the path requires reading the line above it,
   which lists the model, the size and every partition on the disk. Typing `yes` requires nothing.

You cannot get closer to one command than that, and the reason is not caution. A machine cannot
repartition the disk it is currently running from, so a reboot is a fact of the hardware rather than
a design choice. What hop removes is everything around the reboot.

### What it needs, and what it costs

- **UEFI.** The stick is built by copying the ISO's contents onto a FAT32 filesystem, which UEFI
  boots directly. A legacy BIOS machine cannot boot it, and `hop go` refuses at the first stage
  rather than three minutes into a download.
- **A USB stick you are willing to lose.** It is erased. hop lists only removable drives, refuses
  anything holding a running system, refuses drives over 512 GB without `--allow-large` — a 2 TB
  external disk is removable, and it is far more often somebody's backup than their install stick —
  and it will not act until the drive's own identifier is handed back to it.
- **Administrator rights**, because formatting a drive needs them.
- **`gpg`, if you want the image checked properly.** hop always verifies the ISO's checksum, but a
  checksum served by the same mirror as the image proves the transfer was not corrupted, not that
  the image is the one Arch published. Only the signature proves that. Without gpg, hop says exactly
  what it did and did not establish and asks a second time. A signature gpg actively *rejects* is
  not a question — hop stops.

`hop go --no-reboot` stops once the stick is ready, which is a supported path rather than a debug
flag. If you want to drive the steps yourself, they are below.

## The three verbs it is made of

### `hop scan` — on the Windows machine you are leaving

The scanner is a PowerShell script, and `hop` does not start it for you: that machine usually has no
Python on it, and the scan takes minutes.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File windows\hop-scan.ps1
```

It writes `hopfile.json` and `hop-payload\` in the current folder. It only reads the machine, sends
nothing anywhere, and needs no administrator rights: anything that would need elevation degrades to
a warning in the hopfile rather than stopping the scan. Add `-WithSecrets` to copy private SSH keys
and Wi-Fi passwords into the payload — without it you type the Wi-Fi password by hand on the other
side, and with it that folder deserves the care you give `~/.ssh`.

### `hop plan` — on any machine, including the Windows one

```
hop plan hopfile.json --desktop plasma
```

Resolves every installed program through the mapping database, adds the packages the hardware and
the locale imply, and scores the result. Writes `hop-plan.json` (plain JSON, meant to be read and
edited) and `hop-report.md` (the honest version, for a human). It changes nothing else.

### `hop land` — on the freshly installed Arch machine

```
hop land hop-plan.json              # dry run: prints every step, changes nothing
hop land hop-plan.json --execute    # actually does it
```

The dry run is the default, and that is deliberate — you should be able to read the whole thing on
the machine you have not wiped yet. `--execute` refuses to run anywhere that is not Arch. Every step
is idempotent, so if a landing stops partway you run the same command again.

The rest exists to make those three survive contact with a real machine: `hop install-config` writes
the archinstall answer file, `hop diff` says what the plan wanted that the machine has not got yet,
`hop doctor` checks a hopfile and the database, `hop scrub` anonymises a hopfile so it can go in a
public issue, and `hop db` lints, counts and searches the mapping database.

## What it looks like

Everything below is generated from `examples/hopfile.example.json`, a synthetic machine that ships
with the repository. It is nobody's real computer, and hop has no opinion about yours until you scan
it. `hop plan` prints a one-screen summary and writes the two files; `hop report --format summary`
prints that summary again from a saved plan:

```
hoppability   65.7%   workable hop — read the blockers before you commit
programs      34 resolved, 4 unknown, 6 ignored
breakdown     14 native, 7 alternative, 4 none, 3 web, 3 compat, 3 builtin
packages      83 pacman, 3 aur
games         9 installed — 5 play, 2 blocked
user data     288.2 GB in the profile folders — back it up first
blockers      Adobe After Effects 2024, Adobe Premiere Pro 2024, Affinity Photo 2, Microsoft Visual Studio Community 2022
```

Hoppability is weighted coverage, and programs hop could not identify count as zero — being unable
to answer is a real cost to you, and hiding it behind a nicer number would be lying with arithmetic.

The report is 177 lines and ordered worst-news-first. Three sections lifted from it unedited, with
the rows in between cut:

```markdown
## Blockers (4)

*No Linux path. Decide what you are doing about these before you wipe anything.*

- **Adobe Premiere Pro 2024** — Premiere does not run on Linux and Wine does not change that. The realistic paths are DaVinci Resolve, which is professional-grade and officially supported here, or Kdenlive for lighter work. Neither opens .prproj files, so finish anything in flight before you switch.
- **Microsoft Visual Studio Community 2022** — Visual Studio proper does not exist on Linux and Wine does not run it. For C# and .NET, JetBrains Rider is the closest full IDE and the .NET SDK itself is packaged; for C++ workloads people move to CLion, Qt Creator or VS Code with clangd. If you need the real thing, keep a Windows VM.

...

## Different program, same job (7)

*You relearn a menu. You do not lose the capability.*

| On Windows | On Arch | Install | Notes |
|---|---|---|---|
| Notepad++ (64-bit x64) | Kate | `kate` (repo) | There is no Linux Notepad++. Kate is the usual landing spot: tabs, sessions, column select, macros and a built-in terminal. Notepadqq deliberately imitates the Notepad++ layout if muscle memory matters more than polish. |
| MSI Afterburner 4.6.6 | MangoHud plus CoreCtrl | `mangohud` (repo) | The two halves split up here. MangoHud is the in-game overlay and it is better than RTSS for this: FPS, frametimes, temperatures, all configurable per game. CoreCtrl or LACT handle fan curves and undervolting, with the best support on AMD; NVIDIA overclocking is possible but fiddlier than Afterburner made it. |

...

## Games (9 installed)

5 play, 1 partly broken, 2 blocked by anti-cheat, 1 unknown. This is a local snapshot — protondb.com is the live answer.

| Game | Status | Why |
|---|---|---|
| Destiny 2 | **blocked** | Bungie bans accounts detected running the game on Linux or in a VM. Do not try it, even though the game itself would run. |
| Grand Theft Auto V | partly broken | Story mode runs well under Proton. GTA Online enabled BattlEye in a configuration Rockstar has not turned on for Linux, so the online half of the game is unavailable. |
| ELDEN RING | plays | EAC is enabled for Proton, so online co-op and invasions work. Both this and Nightreign are commonly played on Steam Deck. |
```

The dry run of `hop land` on this plan is written the same way: 21 numbered steps, each showing the
command it would run and why it exists, ending with "none of them executed. Nothing on this machine
changed."

## Install

```
git clone https://github.com/Ramirmir/hop2arch
cd hop2arch
python -m hop --help        # runs straight from the checkout
pip install -e .            # or install it, and get `hop` on your PATH
```

Python 3.11 or newer. There are no dependencies and there is not going to be a requirements file:
hop2arch is standard library only, and CI installs nothing but ruff and pytest so that a stray
third-party import fails the build. The Windows side needs nothing installed at all — `hop-scan.ps1`
targets Windows PowerShell 5.1, which is already on every Windows machine.

## Adding a program to the database

`hop/data/packages.toml` is the most valuable file here, and the place where a pull request has the most
effect. It currently holds 170 application rules, 22 ignore rules, and 60 games in
`hop/data/anticheat.toml`. Each entry is flat TOML:

```toml
[[app]]
id       = "notepad-plus-plus"      # unique slug, kebab-case, stable once merged
name     = "Notepad++"              # canonical human name, not the installer's
strategy = "alternative"            # one of the seven below
tags     = ["dev", "editor"]        # free-form, used for grouping only
match    = ["notepad++"]            # lowercase substrings of the Windows display name
winget   = ["Notepad++.Notepad++"]  # winget ids, matched exactly, case-insensitively
exe      = ["notepad++.exe"]        # binaries found in the install location
pacman   = ["kate", "featherpad"]   # official repos, best first
aur      = ["notepadqq"]            # AUR, used when pacman is empty
flatpak  = []                       # flatpak ids, preferred only with --prefer-flatpak
replacement = "Kate"                # required when strategy = "alternative"
carry    = []                       # paths under the Windows profile worth copying
notes    = "There is no Linux Notepad++. Kate is the usual landing spot: tabs, sessions, column select, macros and a built-in terminal."
confidence = "high"                 # high | medium | low
```

Only `id`, `name`, `strategy` and one matcher are required. The matcher runs four passes, most
confident first, and then gives up honestly:

1. `winget` id, exact and case-insensitive. Most reliable, because it is an id and not a name.
2. `exe`, exact, against the binaries in the program's install directory.
3. `match`, substring, against the lowercased display name. Longest substring wins, so
   `visual studio code` beats `visual studio`.
4. `regex`, against the same lowercased name.
5. Nothing matched: the program lands in the report's "Not in the database" list, which is exactly
   the list contributors work from.

| `strategy` | What it means |
| --- | --- |
| `native` | The same application exists on Arch. Firefox, VLC, Steam, Blender. |
| `alternative` | A different application does the same job. Set `replacement`. |
| `builtin` | Already covered by the base system or any desktop. Nothing to install. |
| `compat` | Keep the Windows binary, run it through Wine, Proton or Bottles. |
| `web` | No desktop client; the browser version is the whole story. |
| `none` | Genuinely no path. This is a blocker, and the user has to decide. |
| `ignore` | Not an application: redistributables, drivers, updaters, SDK bits. |

Two rules decide whether a pull request gets merged.

**`notes` is written for a nervous human, and it says what is lost.** Not "use GIMP instead" — what
Photoshop does that Krita does not, which files stop opening, what has to be relearned. One or two
sentences, no marketing. Someone about to wipe their disk can tell advice from cheerleading, and
they will trust the file that tells them the bad part.

**`confidence = "low"` is a legitimate and useful answer.** The report groups by confidence, and
low-confidence rows are the ones asking a reader for a second opinion. Guessing high is worse than
admitting you are not sure.

Before opening the pull request:

```
hop db lint              # the whole database, structurally
hop db search notepad    # check nobody has already covered it
pytest -q                # 229 tests
```

A good pull request here is two lines: one program that was in the unknown list, and an entry that
tells the truth about it.

## Layout

```
hop/data/packages.toml     170 application rules and 22 ignore rules. The heart of it.
hop/data/anticheat.toml    60 games: works / blocked / broken / unknown, with reasons.
docs/HOPFILE.md        the hopfile.json v1 format, field by field.
docs/MAPPING.md        the database format and the contribution rules, in full.
examples/              the synthetic hopfile used by the tests and by this README.
hop/manifest.py        loads and validates a hopfile.
hop/mapping.py         the database and the four-pass matcher.
hop/plan.py            hopfile + database -> plan, plus the hoppability score.
hop/report.py          plan -> markdown report, one-screen summary, or shell script.
hop/land.py            carries out a plan on Arch. Dry run unless --execute.
hop/archinstall.py     the archinstall answer file and hop-post.sh.
hop/scrub.py           anonymises a hopfile so it can go in a public issue.
hop/go.py              the one command: scan, plan, stick, reboot.
hop/iso.py             fetching the Arch image and saying honestly what was verified.
hop/usb.py             choosing a removable drive and refusing every other kind.
hop/install.py         the far side: reads the live disks, asks once, installs.
hop/cli.py             the only module that prints.
tests/                 498 tests. No network, and nothing runs against your machine.
windows/hop-scan.ps1   the scanner. PowerShell 5.1, no admin, no network code.
windows/tests/         unit tests for the scanner's pure helpers, lifted out of its AST.
```

## Status

Version 0.1.0. The parts are not equally mature, and the difference matters more here than usual.

**The mapping database is the mature part.** `hop/data/packages.toml` and `hop/data/anticheat.toml` are
hand-written, linted in CI against the real example hopfile, and they are what the project is for.

**The Python is tested.** 498 tests, ruff clean, CI on Python 3.11, 3.12 and 3.13, plus a job that
installs the built wheel and uses it from outside the checkout. Writing and auditing that suite
turned up real defects rather than typos: an AUR helper named in a hand-edited plan was run as a
program, a payload entry chose the permissions its own private key was restored with, a payload path
with `..` escaped your home directory, and the verdict line could say "nothing lost" four lines above
a Blockers section.

**`hop go` and `hop install` have never been run against real hardware, and that is the sentence to
read twice.** They are unit-tested against injected fakes — canned `Get-Disk` and `lsblk` output, a
fake downloader, a fake formatter — and audited specifically for the ways they could destroy the
wrong thing. That audit found twelve defects, including one that would have erased every USB stick
under 32 GB and then failed to format it, and one where an image whose signature gpg had *rejected*
was offered as an ordinary yes/no question. All are fixed. None of that is the same as putting a
stick in a machine. The first person to run it is the first person to run it.

**`hop land` has been read carefully and never run against a fresh Arch install.** Its dry run is
checked line by line in the tests; the `--execute` path has not met a real machine. Read the dry run
before you trust it, which is what the dry run is for.

**`windows/hop-scan.ps1` has not been run end to end either.** CI parses it with the Windows
PowerShell 5.1 parser, runs PSScriptAnalyzer, unit-tests its pure helper functions against synthetic
input, and rejects PowerShell 7-only syntax. None of that is the same as reading a real machine, and
it has not read one. If it fails on yours, `hop scrub` strips the personal parts out of the output
so the hopfile can go in an issue.

Issues and pull requests: <https://github.com/Ramirmir/hop2arch>

MIT.
