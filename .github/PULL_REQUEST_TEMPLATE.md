<!--
A mapping pull request needs the first section and nothing else. Delete the rest.
A code pull request needs the second. Delete the first.
-->

## Adding or changing a mapping

**Program:**
**Entry id:**

**Why this answer.** What the Arch side does that the Windows one did, and what it does not. If
something is lost — a file format, a workflow, a plugin ecosystem — say so here, because that is what
the `notes` field has to end up saying.

- [ ] `notes` says what is lost, not only what to install
- [ ] `confidence` is honest — `low` where I am guessing
- [ ] `hop db lint` clean
- [ ] `hop db search` shows nothing already covering it
- [ ] `pytest -q` clean

For a game in `hop/data/anticheat.toml`, where did you check? ProtonDB and areweanticheatyet.com are
the live sources, and the snapshot in this repository is not.

---

## Changing code

**What this fixes, and how it failed before.** A concrete input and the wrong result it produced.

- [ ] `ruff check .` clean
- [ ] `pytest -q` clean
- [ ] A regression test for every bug fixed here
- [ ] Standard library only — no new dependency
- [ ] Nothing new prints outside `hop/cli.py`
- [ ] Anything touching hardware goes through an injected runner and is tested with canned output
- [ ] No `shell=True`, and no value interpolated into a command string
- [ ] User-facing text says what is lost and does not promise what the code cannot deliver

**If this touches `hop/usb.py`, `hop/install.py` or `hop/go.py`:** say which refusal or confirmation
you changed and why it is still safe. Those three can erase a disk, and the reasoning for each guard
is in the docstrings — a change that makes one of them looser needs an argument, not just a passing
test suite.
