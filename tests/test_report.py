"""The rendered report.

The report is the part the user actually reads before deciding whether to wipe
the disk, so the order of the sections is not cosmetic: worst news first, then
what changes, then what is identical. These tests hold that order in place, and
check the two ways markdown quietly breaks — an unescaped pipe eating a table,
and a table that is printed for a plan that has nothing to put in it.
"""

from __future__ import annotations

import shlex

import pytest

from hop.plan import Plan, PlanItem
from hop.report import render_markdown, render_shell, render_summary

SECTIONS = [
    "## Blockers",
    "## Web only",
    "## Runs through Wine/Proton",
    "## Different program, same job",
    "## Comes with you unchanged",
    "## Already in the box",
]


def plan_with(*items: PlanItem, **fields: object) -> Plan:
    plan = Plan(hopfile={"hostname": "test-machine"}, target={}, system={}, items=list(items))
    plan.packages = {"pacman": [], "aur": [], "flatpak": []}
    plan.score = {"hoppability": 50.0, "verdict": "a verdict", "matched": len(items)}
    for key, value in fields.items():
        setattr(plan, key, value)
    return plan


def section(text: str, heading: str) -> list[str]:
    """The lines of one ``## heading`` section, up to the next heading."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## ") or line.startswith("---"):
            break
        out.append(line)
    return out


def item(source: str, strategy: str, **fields: object) -> PlanItem:
    base = {
        "source": source,
        "version": "1.0",
        "rule_id": source.lower(),
        "title": source,
        "strategy": strategy,
        "install_source": "pacman",
        "package": "something",
        "notes": "a note.",
    }
    base.update(fields)
    return PlanItem(**base)


# --- the three renderers run --------------------------------------------


def test_markdown_renders_the_example(example_plan: Plan) -> None:
    text = render_markdown(example_plan)
    assert text.startswith("# Hop report — NB-ARTEM")
    assert "## Read these first" in text
    assert "Hoppability" in text
    assert text.endswith("\n")


def test_summary_renders_the_example(example_plan: Plan) -> None:
    text = render_summary(example_plan)
    assert "hoppability" in text
    assert "packages" in text
    assert "\n" in text


def test_shell_renders_the_example(example_plan: Plan) -> None:
    text = render_shell(example_plan)
    assert "sudo pacman -S --needed" in text
    assert "paru -S --needed" in text


def test_ignored_entries_are_opt_in(example_plan: Plan) -> None:
    assert "Ignored entries" not in render_markdown(example_plan)
    assert "Ignored entries" in render_markdown(example_plan, show_ignored=True)


# --- markdown that has to stay markdown -----------------------------------


def test_a_pipe_in_a_program_name_does_not_eat_the_table() -> None:
    """Windows lets you install a program called "Foo | Bar". Markdown does not."""
    plan = plan_with(
        item("Foo | Bar", "native", notes="notes with a | in them too"),
        item("Ordinary", "native"),
    )
    text = render_markdown(plan)
    assert "Foo \\| Bar" in text
    rows = [line for line in section(text, "## Comes with you unchanged") if line.startswith("|")]
    assert len(rows) == 4  # header, separator, and one row per program
    widths = {line.count("|") - line.count("\\|") for line in rows}
    assert widths == {4}, "every row of a three-column table has four unescaped pipes"


def test_a_newline_in_a_note_does_not_break_a_row() -> None:
    plan = plan_with(item("Thing", "native", notes="first line\nsecond line"))
    text = render_markdown(plan)
    assert "first line second line" in text


@pytest.mark.parametrize("strategy", ["none", "web"])
def test_the_bullet_sections_escape_like_the_tables_do(strategy: str) -> None:
    """Blockers and web-only entries are bullets rather than table rows, and were
    the one place in the report that printed the hopfile's text unescaped. A
    newline there ends the bullet and leaves the rest of the sentence loose on
    the page, in the section that most needs reading."""
    plan = plan_with(
        item("Foo | Bar", strategy, notes="first line\nsecond line", package=None)
    )
    text = render_markdown(plan)
    assert "Foo \\| Bar" in text
    bullets = [line for line in text.splitlines() if line.startswith("- ")]
    assert any("first line second line" in line for line in bullets), bullets
    assert "second line" not in [line.strip() for line in text.splitlines()]


def test_section_order_is_worst_news_first() -> None:
    plan = plan_with(
        item("Builtin thing", "builtin"),
        item("Native thing", "native"),
        item("Alternative thing", "alternative", title="Something else"),
        item("Compat thing", "compat"),
        item("Web thing", "web"),
        item("Blocked thing", "none"),
    )
    text = render_markdown(plan)
    positions = [text.index(heading) for heading in SECTIONS]
    assert positions == sorted(positions)


def test_warnings_come_before_the_machine_description() -> None:
    """"Export your BitLocker key" outranks a table about desktop environments."""
    plan = plan_with(item("Thing", "native"), warnings=["something that can cost you data"])
    text = render_markdown(plan)
    assert text.index("## Read these first") < text.index("## What you are landing on")


def test_only_the_alternative_table_has_an_on_arch_column() -> None:
    """For every other strategy the title is the Windows program's own name, and
    printing it as the Arch answer produced rows like "Git -> Git for Windows"."""
    alternative = render_markdown(plan_with(item("WinRAR", "alternative", title="Ark")))
    native = render_markdown(plan_with(item("Firefox", "native")))
    assert "| On Windows | On Arch | Install | Notes |" in alternative
    assert "| Program | Install | Notes |" in native
    assert "On Arch" not in native


def test_a_blocker_names_its_consolation_prize_without_overselling() -> None:
    plan = plan_with(item("Microsoft 365", "web", package="libreoffice-fresh"))
    text = render_markdown(plan)
    assert "Closest thing hop can install: `libreoffice-fresh` (repo)." in text


def test_low_confidence_is_stated_in_the_row() -> None:
    plan = plan_with(item("Battle.net", "compat", confidence="medium"))
    assert "(medium confidence)" in render_markdown(plan)


# --- the data section ------------------------------------------------------


def test_the_folder_table_appears_when_there_are_folders(example_plan: Plan) -> None:
    text = render_markdown(example_plan)
    assert "## Data worth carrying" in text
    assert "| Folder | Size | Files |" in text
    assert "| Videos | 200.0 GB | 412 |" in text


def test_no_folder_table_when_nothing_was_measured() -> None:
    plan = plan_with(item("Thing", "native"), data={"folders": [], "total_bytes": 0})
    text = render_markdown(plan)
    assert "| Folder | Size | Files |" not in text
    assert "profile folders hold" not in text


def test_the_payload_is_listed_with_its_destination(example_plan: Plan) -> None:
    text = render_markdown(example_plan)
    assert "`ssh/id_ed25519` → `~/.ssh/id_ed25519`" in text
    assert "(imported, not copied)" in text


# --- the shell script ------------------------------------------------------


def test_shell_starts_with_a_shebang_and_ends_cleanly(example_plan: Plan) -> None:
    text = render_shell(example_plan)
    lines = text.splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines
    body = [line for line in lines if line.strip()]
    assert not body[-1].rstrip().endswith("\\"), "the last line continues onto nothing"
    for number, line in enumerate(lines):
        if line.rstrip().endswith("\\"):
            assert number + 1 < len(lines), "a continuation with no line after it"


def test_shell_says_nothing_when_there_is_nothing_to_install() -> None:
    text = render_shell(plan_with())
    assert "pacman" not in text
    assert text.startswith("#!/usr/bin/env bash")


def test_shell_quotes_the_package_names_it_writes() -> None:
    """render_shell reads a plan file, and a plan file is a document people edit
    and pass around. The script's own header says it is safe to read before
    running it; an unquoted name would make that untrue."""
    names = ["firefox", "x; touch /tmp/pwned", "a name with spaces"]
    plan = plan_with()
    plan.packages = {"pacman": names, "aur": [], "flatpak": []}
    text = render_shell(plan)

    # The property that matters: bash reads the line back as exactly the names
    # the plan asked for, and as nothing else.
    body = [line for line in text.splitlines() if line.startswith("  ")]
    tokens = shlex.split(" ".join(line.rstrip(" \\") for line in body))
    assert tokens == names, tokens
    # An ordinary Arch package name needs no quoting and must not grow any:
    # this file is meant to be read, and a wall of quotes is harder to read.
    assert "  firefox " in text


def test_shell_leaves_real_package_names_exactly_as_they_are(example_plan: Plan) -> None:
    text = render_shell(example_plan)
    assert "'" not in text
    for name in example_plan.packages["pacman"]:
        assert name in text


# --- the empty case --------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("packages", {"pacman": ["firefox"], "aur": None, "flatpak": None}),
        ("games", {"total": 2, "counts": None, "titles": None}),
        ("system", {"x11_layouts": None, "gpu_vendors": None}),
        ("score", {"hoppability": 50.0, "verdict": "v", "by_strategy": None}),
        ("data", {"folders": None, "total_bytes": 1}),
    ],
)
def test_a_list_inside_a_section_emptied_with_null_still_renders(
    field: str, value: object
) -> None:
    """One level down from the section check in test_plan.py. A plan is edited by
    hand, and 'aur': null is the obvious way to say you want no AUR packages."""
    plan = plan_with(item("Thing", "native"), **{field: value})
    assert render_markdown(plan)
    assert render_summary(plan)
    assert render_shell(plan)


def test_an_empty_plan_still_renders() -> None:
    """A hopfile that recorded nothing is a real thing that happens: no admin
    rights, no registry access. It must produce a document, not a crash."""
    empty = Plan(hopfile={}, target={}, system={})
    text = render_markdown(empty)
    assert "Hoppability: not measured" in text
    assert "no installed software was recorded" in text
    assert "█" not in text, "an empty progress bar still reads as an answer"
    assert render_summary(empty).startswith("hoppability   not measured")
    assert render_shell(empty).startswith("#!/usr/bin/env bash")


def test_no_section_blurb_promises_more_than_the_notes_do(example_plan: Plan) -> None:
    """The 'Different program, same job' blurb used to end 'You do not lose the
    capability', four lines above the row that swaps Lightroom for darktable and
    a note saying the develop settings do not transfer. The section headline is
    read first, so it must not answer the question more warmly than the data."""
    text = render_markdown(example_plan)
    for claim in ("You do not lose the capability", "nothing is lost", "nothing lost"):
        assert claim not in text, claim
    blurb = section(text, "## Different program, same job")
    assert any("read those" in line for line in blurb), blurb


def test_the_footer_points_at_the_database() -> None:
    text = render_markdown(plan_with(item("Thing", "native")))
    assert "https://github.com/Ramirmir/hop2arch" in text
    assert "hop/data/packages.toml" in text
