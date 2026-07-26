"""The command line.

Two things are being checked here. The exit codes, because scripts and people
both rely on the split: 0 did it, 1 did it and the answer is bad news, 2 could
not do it at all. And the promise in cli.py's own docstring — that a failure
arrives as a sentence rather than a traceback — because that is the difference
between a person who can act and a person who files an issue.

Nothing in this file passes --execute, and nothing lets hop write outside
tmp_path.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hop.cli import main
from hop.plan import Plan


@pytest.fixture
def plan_file(example_plan: Plan, tmp_path: Path) -> Path:
    path = tmp_path / "hop-plan.json"
    path.write_text(json.dumps(example_plan.to_dict()), encoding="utf-8")
    return path


# --- the parser itself -----------------------------------------------------


def test_version_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


def test_no_command_prints_help_and_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "COMMAND" in capsys.readouterr().out


def test_an_unknown_subcommand_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["teleport"])
    assert excinfo.value.code != 0
    assert "invalid choice" in capsys.readouterr().err


def test_an_unknown_desktop_is_refused(example_hopfile: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["plan", str(example_hopfile), "--desktop", "fluxbox"])
    assert excinfo.value.code != 0
    error = capsys.readouterr().err
    assert "invalid choice" in error
    assert "plasma" in error, "the message has to list the desktops that do exist"


# --- plan ------------------------------------------------------------------


def test_plan_writes_both_files(
    example_hopfile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "hop-plan.json"
    assert main(["plan", str(example_hopfile), "-o", str(out)]) == 0

    report = out.with_suffix(".md")
    assert out.exists() and report.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["plan_version"] == 1
    assert report.read_text(encoding="utf-8").startswith("# Hop report")

    printed = capsys.readouterr().out
    assert "hoppability" in printed
    assert str(out) in printed and str(report) in printed


def test_plan_no_report(example_hopfile: Path, tmp_path: Path) -> None:
    out = tmp_path / "plan.json"
    assert main(["plan", str(example_hopfile), "-o", str(out), "--no-report"]) == 0
    assert not out.with_suffix(".md").exists()


def test_plan_json_is_machine_readable(
    example_hopfile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "plan.json"
    assert main(["--json", "plan", str(example_hopfile), "-o", str(out)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan_version"] == 1
    assert payload["score"]["hoppability"] > 0


def test_plan_writes_utf8_whatever_the_console_thinks(
    example_hopfile: Path, tmp_path: Path
) -> None:
    """The report has a progress bar and arrows in it. On Windows the legacy
    code page has no room for either, and 'hop report > report.md' used to write
    an empty file and exit 2."""
    out = tmp_path / "plan.json"
    main(["plan", str(example_hopfile), "-o", str(out)])
    text = out.with_suffix(".md").read_text(encoding="utf-8")
    assert "█" in text
    assert "→" in text


# --- report ----------------------------------------------------------------


def test_report_renders_a_plan(plan_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report", str(plan_file)]) == 0
    assert capsys.readouterr().out.startswith("# Hop report")


def test_report_accepts_a_hopfile_and_says_so(
    example_hopfile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nobody remembers which of the two files this wants. The note goes to
    stderr, where it cannot end up inside a redirected report."""
    assert main(["report", str(example_hopfile)]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# Hop report")
    assert "is a hopfile" in captured.err


def test_report_formats(plan_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report", str(plan_file), "--format", "summary"]) == 0
    assert capsys.readouterr().out.startswith("hoppability")
    assert main(["report", str(plan_file), "--format", "shell"]) == 0
    assert capsys.readouterr().out.startswith("#!/usr/bin/env bash")


def test_report_to_a_file(plan_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    assert main(["report", str(plan_file), "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Hop report")


# --- land ------------------------------------------------------------------


def test_land_without_execute_stays_a_dry_run(
    plan_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default is the safe one, and the transcript says which mode it is in
    before it says anything else."""
    assert main(["land", str(plan_file)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("hop land — DRY RUN")
    assert "Nothing below is executed" in printed
    assert "would run:" in printed
    assert "none of them executed" in printed
    assert "EXECUTING" not in printed


def test_land_only_filters_phases(plan_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["land", str(plan_file), "--only", "payload"]) == 0
    printed = capsys.readouterr().out
    assert "--- payload" in printed
    assert "--- packages" not in printed


def test_land_rejects_an_unknown_phase(plan_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["land", str(plan_file), "--only", "wallpaper"]) == 2
    assert "unknown phase" in capsys.readouterr().err


def test_land_refuses_a_hopfile_and_says_what_to_run(
    example_hopfile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["land", str(example_hopfile)]) == 2
    error = capsys.readouterr().err
    assert "is a hopfile, not a plan" in error
    assert "hop plan" in error


# --- diff, doctor, install-config, scrub -----------------------------------


@pytest.mark.skipif(
    shutil.which("pacman") is not None, reason="pacman is installed here, so it would be queried"
)
def test_diff_runs_on_a_machine_with_no_pacman(
    plan_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run on the Windows machine, everything reads as missing. That is correct,
    and hop says so rather than pretending the answer means something."""
    assert main(["diff", str(plan_file)]) == 0
    printed = capsys.readouterr().out
    assert "No pacman and no flatpak on this machine" in printed
    assert "package(s) still to go" in printed


def test_doctor_reports_and_does_not_refuse(
    example_hopfile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor", str(example_hopfile)]) == 0
    printed = capsys.readouterr().out
    assert "BitLocker" in printed
    assert "doctor reports; it does not refuse" in printed


def test_install_config_writes_into_a_directory(
    plan_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "archinstall"
    assert main(["install-config", str(plan_file), "-o", str(out)]) == 0
    assert (out / "user_configuration.json").exists()
    assert (out / "hop-post.sh").exists()
    printed = capsys.readouterr().out
    assert "archinstall will still ask which disk" in printed


def test_scrub_writes_an_anonymised_copy(
    example_hopfile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "scrubbed.json"
    assert main(["scrub", str(example_hopfile), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "artem" not in text.lower()
    assert json.loads(text)["scrubbed"] is True
    assert "This is not anonymity." in capsys.readouterr().out


# --- db --------------------------------------------------------------------


def test_db_lint_passes_on_the_shipped_data(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "lint"]) == 0
    assert "No problems." in capsys.readouterr().out


def test_db_stats_and_search(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "stats"]) == 0
    assert "apps" in capsys.readouterr().out
    assert main(["db", "search", "firefox"]) == 0
    assert "firefox" in capsys.readouterr().out


def test_db_search_admits_it_found_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "search", "frobnicator"]) == 0
    assert "two-line change" in capsys.readouterr().out


def test_db_with_no_subcommand_prints_its_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db"]) == 2
    assert "lint" in capsys.readouterr().out


def test_scan_prints_the_command_it_will_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    """hop never starts the scanner: that machine usually has no Python on it."""
    assert main(["scan"]) == 0
    printed = capsys.readouterr().out
    assert "hop-scan.ps1" in printed
    assert "hop does not start it for you" in printed


# --- failures ---------------------------------------------------------------


def test_a_missing_file_is_a_sentence_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["plan", str(tmp_path / "nope.json"), "-o", str(tmp_path / "out.json")]) == 2
    error = capsys.readouterr().err
    assert error.startswith("hop: ")
    assert "no such hopfile" in error
    assert "Traceback" not in error


def test_a_missing_plan_says_where_plans_come_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["land", str(tmp_path / "nope.json")]) == 2
    error = capsys.readouterr().err
    assert error.startswith("hop: ")
    assert "hop plan" in error


def test_malformed_json_is_a_sentence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{oh no", encoding="utf-8")
    assert main(["plan", str(broken), "-o", str(tmp_path / "out.json")]) == 2
    error = capsys.readouterr().err
    assert "not valid JSON" in error
    assert "Traceback" not in error


def test_a_hand_edited_plan_is_a_sentence(
    plan_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """plan.py invites the reader to delete the entries they disagree with. Doing
    it slightly wrong has to come back as a sentence, not a stack trace."""
    raw = json.loads(plan_file.read_text(encoding="utf-8"))
    del raw["items"][0]["version"]
    plan_file.write_text(json.dumps(raw), encoding="utf-8")

    assert main(["land", str(plan_file)]) == 2
    error = capsys.readouterr().err
    assert error.startswith("hop: ")
    assert "Traceback" not in error
    assert "PlanItem" not in error, "the class name means nothing to the person reading it"
    assert "put the line back" in error


@pytest.mark.parametrize("key", ["packages", "system", "target", "payload", "warnings", "score"])
def test_a_section_emptied_with_null_still_lands(
    plan_file: Path, key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Blanking a section out with null is an ordinary hand-edit. It used to
    arrive as 'AttributeError: NoneType object has no attribute get' out of the
    middle of hop land — a bare traceback in front of somebody on a machine that
    has no desktop yet."""
    raw = json.loads(plan_file.read_text(encoding="utf-8"))
    raw[key] = None
    plan_file.write_text(json.dumps(raw), encoding="utf-8")

    assert main(["land", str(plan_file)]) == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "DRY RUN" in captured.out


def test_a_section_of_the_wrong_shape_is_a_sentence(
    plan_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = json.loads(plan_file.read_text(encoding="utf-8"))
    raw["packages"] = ["firefox", "vlc"]
    plan_file.write_text(json.dumps(raw), encoding="utf-8")

    assert main(["land", str(plan_file)]) == 2
    error = capsys.readouterr().err
    assert error.startswith("hop: ")
    assert "Traceback" not in error
    assert "'packages'" in error


def test_an_aur_helper_the_plan_invented_is_a_sentence(
    plan_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """hop land runs the AUR helper by name. The plan is a file people edit."""
    raw = json.loads(plan_file.read_text(encoding="utf-8"))
    raw["target"]["aur_helper"] = "/tmp/something-else"
    plan_file.write_text(json.dumps(raw), encoding="utf-8")

    assert main(["land", str(plan_file)]) == 2
    error = capsys.readouterr().err
    assert error.startswith("hop: ")
    assert "Traceback" not in error
    assert "target.aur_helper" in error


def test_a_missing_data_directory_is_a_sentence(
    example_hopfile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path / "not-a-database"),
                "plan",
                str(example_hopfile),
                "-o",
                str(tmp_path / "out.json"),
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert error.startswith("hop: ")
    assert "packages.toml" in error


# --- go and install: the two verbs that reach hardware ---------------------


def flat(text: str) -> str:
    """Transcript text with the line wrapping taken out.

    These commands wrap their prose to a readable width, so a phrase the test
    cares about is as likely to arrive as "the verb is 'hop\\ngo'" as on one
    line. Asserting on the wrapped form makes the test fail when somebody
    rewords a sentence by three characters, which teaches nobody anything.
    """
    return " ".join(text.split())


def test_install_off_arch_is_a_sentence_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`hop install` on the machine you have not left yet.

    This is a likely typo — the verb on Windows is `hop go` — so it has to
    arrive as prose. It regressed once already: cli.py passed out_dir=None to
    say "not given", InstallOptions has a real default there rather than an
    optional, and Path(None) raised TypeError, which is not in KNOWN_ERRORS and
    so reached the terminal as a stack trace.
    """
    code = main(["install"])
    out = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in out.out + out.err
    assert "hop go" in flat(out.out)


def test_install_does_not_reach_a_disk_when_it_refuses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["install", "--target", "/dev/nvme0n1"])
    out = capsys.readouterr()
    assert code == 2
    assert "nothing in this machine has been changed" in flat(out.out).lower()


@pytest.mark.parametrize(
    "argv",
    [
        ["go", "--desktop", "haiku"],
        ["install", "--filesystem"],
        ["go", "--out"],
    ],
)
def test_bad_flags_on_the_hardware_verbs_never_start_anything(argv: list[str]) -> None:
    """argparse rejects these before any code that could touch a drive runs."""
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code != 0


def test_the_help_says_go_is_the_one_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    # 'go' is the answer to "how do I use this", so it comes before the verbs
    # it is assembled from. A reader who stops after the first line still ends
    # up somewhere sensible.
    assert text.index("\n    go ") < text.index("\n    scan ")
