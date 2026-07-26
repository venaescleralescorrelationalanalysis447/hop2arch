# The mapping database (`hop/data/packages.toml`)

This file is the heart of the project and the part that most benefits from pull
requests. It answers one question for every Windows program: **what do I run
instead, on Arch?**

`hop plan` walks the `software` array of a hopfile, matches each entry against
the rules below, and emits a plan. Rules are evaluated cheapest-first:

1. exact `winget` id match (case-insensitive) — highest confidence
2. exact `exe` match against the install location's binaries
3. `match` substring match against the lowercased display name
4. `regex` match against the lowercased display name
5. nothing matched → the app lands in the "unknown" bucket of the report

## Entry format

```toml
[[app]]
id       = "firefox"                 # unique slug, kebab-case, stable
name     = "Mozilla Firefox"         # canonical human name
strategy = "native"                  # see below
tags     = ["browser"]
match    = ["mozilla firefox"]       # lowercase substrings of the display name
winget   = ["Mozilla.Firefox"]       # winget package ids
exe      = ["firefox.exe"]           # optional
pacman   = ["firefox"]               # official repos, in preference order
aur      = []                        # AUR packages
flatpak  = ["org.mozilla.firefox"]   # flatpak app ids
replacement = ""                     # only for strategy = "alternative"
carry    = ["AppData/Roaming/Mozilla/Firefox/Profiles"]   # paths worth copying, relative to the Windows user profile
notes    = "Sign into Firefox Sync, or copy the profile folder to ~/.mozilla/firefox."
confidence = "high"                  # high | medium | low
```

Only `id`, `name`, `strategy` and at least one matcher are required.

## Strategies

| `strategy` | Meaning | Example |
| --- | --- | --- |
| `native` | The same application exists on Arch. | Firefox, VLC, Steam, Blender |
| `alternative` | A different application does the same job. Set `replacement`. | Notepad++ → Kate, Photoshop → GIMP/Krita |
| `builtin` | Already covered by the base system or any desktop environment. | Notepad, Calculator, Paint, Explorer |
| `compat` | Keep the Windows binary, run it through Wine/Proton/Bottles. | foobar2000, older games, niche utilities |
| `web` | No desktop client; use the web app. | Microsoft Teams, Photoshop Web |
| `none` | Genuinely no path on Linux — this is a blocker the user must decide about. | Adobe Premiere Pro, Vanguard-protected games |
| `ignore` | Not an application: redistributables, drivers, update helpers, SDK bits. Never shown in the report body. | Microsoft Visual C++ Redistributable |

## Ignore rules

```toml
[[ignore]]
reason = "redistributable"
match  = ["microsoft visual c++", "microsoft .net runtime"]
regex  = "^windows (sdk|driver kit)"
```

## Anti-cheat / game blockers (`hop/data/anticheat.toml`)

```toml
[[title]]
appid  = 1172470
name   = "Apex Legends"
status = "blocked"        # blocked | broken | works | unknown
reason = "Easy Anti-Cheat is not enabled for Linux by the publisher"
```

## Style rules for contributions

* One `[[app]]` per real application, not per installer variant. Language- and
  architecture-specific names (`(x64 ru)`, `(en-US)`) are handled by substring
  matching, so keep `match` short and generic.
* Prefer `pacman` over `aur`, and `aur` over `flatpak`, unless the flatpak is
  meaningfully better (proprietary apps, sandboxing).
* `notes` is written for a human who is nervous about leaving Windows. Say what
  they lose, and what to do about it. One or two sentences, no marketing.
* `confidence = "low"` is fine and useful — the report groups by confidence and
  low-confidence rows are the ones asking for a second opinion.
