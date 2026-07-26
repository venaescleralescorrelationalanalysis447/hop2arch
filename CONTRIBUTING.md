# Contributing to hop2arch

The most useful thing you can send is two lines of TOML.

hop2arch's value is not its code. It is `hop/data/packages.toml`: 170 hand-written answers to the
question "what do I run instead, on Arch?", each one written for somebody who is nervous about
leaving Windows. Every program missing from that file is a line in somebody's report that says
"hop has no opinion on this". Filling one in is a real contribution and it takes ten minutes.

## Adding or fixing a mapping

1. Find something in the **"Not in the database"** section of a report — yours, or one attached to
   an issue. That list is the backlog.
2. Check nobody has covered it: `hop db search <name>`.
3. Add one `[[app]]` block to `hop/data/packages.toml`. The full field reference is in
   [`docs/MAPPING.md`](docs/MAPPING.md); the short version is in the README.
4. Run the three checks below.
5. Open a pull request. One program per pull request is ideal — it makes the entry easy to argue
   about on its own merits.

```
hop db lint              # the whole database, structurally
hop db search notepad    # nobody has already covered it
pytest -q                # nothing else broke
```

### What gets a mapping merged

**`notes` says what is lost.** This is the whole standard. Not "use GIMP instead" — what Photoshop
does that Krita does not, which files stop opening, what has to be relearned. One or two sentences.
No marketing, no "just", no exclamation marks. Somebody about to erase their disk can tell advice
from cheerleading, and they will trust the file that tells them the bad part first.

Compare:

```toml
# no
notes = "GIMP is a great free alternative to Photoshop!"

# yes
notes = "Krita is the better fit for painting and digital art, GIMP for photo retouching and general editing, and photopea.com is a browser clone that opens .psd files when you just need to fix one file. Wine and Bottles recipes for Photoshop exist but break between versions; do not build a workflow on them."
```

**`confidence = "low"` is a legitimate answer.** The report groups by confidence and low-confidence
rows are the ones asking a reader for a second opinion. Guessing `high` is worse than admitting you
are not sure.

**`strategy = "none"` is allowed and sometimes correct.** If there is no path, say so. A blocker
honestly named is worth more than an alternative that does not really substitute. Adobe Premiere is
`none`, and the entry explains why rather than pointing at Kdenlive and hoping.

**One entry per real application, not per installer variant.** Language and architecture suffixes
(`(x64 ru)`, `(en-US)`) are handled by substring matching, so keep `match` short and generic.

**Prefer `pacman` over `aur` over `flatpak`**, unless the flatpak is meaningfully better — usually
proprietary applications, or where sandboxing is the point.

## Adding a game to the anti-cheat snapshot

`hop/data/anticheat.toml` is a point-in-time snapshot, not a live feed, and the file says so at the
top. Anti-cheat support is a publisher policy switch that can be turned off in a patch — Apex Legends
worked for years and then stopped. If you add or change an entry, say in the pull request where you
checked: [ProtonDB](https://www.protondb.com/) and
[areweanticheatyet.com](https://areweanticheatyet.com/) are the live sources.

Status values are `works`, `blocked`, `broken` and `unknown`. `unknown` is a real answer for a title
that genuinely flips between patches — better than a `works` that stops being true next season.

## Contributing code

Read the module you are changing before you change it. The docstrings explain why things are the way
they are, and several of them are arguing against a change that looks obvious.

- **Python 3.11+, standard library only.** There are no runtime dependencies and there will not be
  any. CI installs nothing but ruff and pytest so that a stray third-party import fails the build.
- **`ruff check .` and `pytest -q` both clean.** Line length 100.
- **Library modules do not print.** They return strings or write to an injected stream. `hop/cli.py`
  is the only module that prints on its own initiative.
- **Anything that touches hardware takes an injected runner.** Every function that reads disks or
  runs a command accepts a callable so tests can feed it canned output. This is not only for the
  tests: it is the only way code that erases disks can be exercised anywhere other than on a machine
  it would erase. A code path that cannot be reached without real hardware is a design bug.
- **No `shell=True`, ever.** Build argv as a list. Never interpolate a value into a command string.
- **A failure is a sentence, not a traceback.** `hop/cli.py` catches a known set of exceptions and
  prints prose. If your change can raise something outside that set, add it to `KNOWN_ERRORS` or
  convert it.

### Tone

The reader is about to erase the operating system they have used for fifteen years. Everything
user-facing — help text, error messages, report prose, comments — is calm, concrete and honest.
Worst news first. No evangelism, no "just", no "simply", no emoji, no exclamation marks. If something
might not work, say so.

Comments explain *why*, not *what*. If a comment could be deleted without losing information, delete
it instead of writing it.

### Tests

Every bug fix comes with a regression test. Tests never touch the network, never run anything against
the machine they are on, and never write outside `tmp_path`. If you cannot test something without
real hardware, add the seam that makes it testable — that is usually the actual fix.

## Reporting a bug

If hop got something wrong about your machine, the hopfile is what makes it reproducible — and it
describes your computer in detail. Run it through the scrubber first:

```
hop scrub hopfile.json
```

That replaces hostnames, account names, e-mail addresses, Wi-Fi SSIDs, public keys and drive labels
with stable stand-ins, and drops the payload index entirely. It prints the replacement table so you
can see exactly what changed before you paste anything. The software list, hardware, sizes and
warnings survive, because those are what a maintainer needs.

`hop scrub` does not make you anonymous. An unusual combination of installed software, disk sizes and
game library is identifying on its own, and nothing in the tool changes that.

## Security

Please do not open a public issue for a vulnerability. [`SECURITY.md`](SECURITY.md) explains what to
do instead, and what is in scope.

## Translations

The README exists in [English](README.md) and [Russian](README.ru.md). The report itself and the
`notes` field are English only, and translating 170 hand-written entries is a real open task — one
that has to be done by a person, because the whole point of those sentences is the judgement in them.
If you want to take it on, open an issue first so the approach can be agreed before the work.

## Licence

By contributing you agree that your contribution is licensed under the MIT licence, the same as the
rest of the project.
