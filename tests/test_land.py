"""The landing.

``hop land`` is the only part of hop that changes anything, so the tests are
mostly about it *not* changing things: a dry run that creates no file, a
transcript that never prints a password, a refusal to execute anywhere that is
not Arch. Nothing here executes a step. The one test that calls ``run`` with
``dry_run=False`` is the test that the refusal happens first, and it is skipped
on a machine where the refusal would not fire.
"""

from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest

from hop.land import PHASES, Lander, LandError, _parse_mode, diff, installed_packages
from hop.plan import Plan

WIFI_PASSWORD = "correct-horse-battery-staple"
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n"

ON_ARCH = shutil.which("pacman") is not None or Path("/etc/arch-release").exists()


def flat(text: str) -> str:
    """The transcript with its line breaks taken out.

    Titles are wrapped to 74 columns for the person reading them, which puts a
    newline in the middle of a sentence at an unpredictable place. Assertions
    about the wording go through here; assertions about a command do not, since
    a command is never wrapped.
    """
    return " ".join(text.split())


def make_payload(root: Path) -> Path:
    """A payload directory with a real secret in it, for the leak tests."""
    payload = root / "hop-payload"
    (payload / "ssh").mkdir(parents=True)
    (payload / "wifi").mkdir(parents=True)
    (payload / "ssh" / "id_ed25519").write_text(PRIVATE_KEY, encoding="utf-8")
    (payload / "wifi" / "home.json").write_text(
        json.dumps({"ssid": "MyNetwork", "psk": WIFI_PASSWORD}), encoding="utf-8"
    )
    return payload


def payload_plan() -> Plan:
    return Plan(
        hopfile={},
        target={"display_manager": "sddm", "aur_helper": "paru"},
        system={"hostname": "arch", "locale": "en_US.UTF-8", "locales": ["en_US.UTF-8"]},
        payload=[
            {"kind": "ssh", "path": "ssh/id_ed25519", "restore_to": "~/.ssh/id_ed25519", "mode": "0600"},
            {"kind": "wifi", "path": "wifi/home.json", "restore_to": None, "mode": "0600"},
        ],
    )


# --- the shape of the work -------------------------------------------------


def test_steps_cover_all_three_phases(example_plan: Plan, tmp_path: Path) -> None:
    lander = Lander(example_plan, home=tmp_path / "home", out=io.StringIO())
    steps = lander.steps()
    assert {step.phase for step in steps} == set(PHASES)
    assert [step.phase for step in steps] == sorted(
        (step.phase for step in steps), key=PHASES.index
    ), "packages before payload before settings is a dependency, not a preference"


def test_every_step_explains_itself(example_plan: Plan, tmp_path: Path) -> None:
    """A step with no sentence is a command someone is asked to trust blind."""
    for step in Lander(example_plan, home=tmp_path / "home", out=io.StringIO()).steps():
        assert step.title.strip()
        assert step.kind in ("run", "copy", "write", "note")
        if step.argv:
            assert step.command, step.title


def test_packages_are_installed_in_readable_batches(example_plan: Plan, tmp_path: Path) -> None:
    """pacman copes with two hundred names on one line. A person does not."""
    steps = Lander(example_plan, home=tmp_path / "home", out=io.StringIO(), phases=("packages",)).steps()
    installs = [s for s in steps if s.argv[:3] == ["sudo", "pacman", "-S"]]
    assert installs
    for step in installs:
        assert "--needed" in step.argv, "a phase that cannot be re-run is a phase that strands people"
        assert len(step.argv) <= 5 + 25


def test_only_filters_the_phases(example_plan: Plan, tmp_path: Path) -> None:
    lander = Lander(example_plan, home=tmp_path / "home", out=io.StringIO(), phases=("payload",))
    assert lander.phases == ("payload",)
    assert {step.phase for step in lander.steps()} == {"payload"}


def test_an_unknown_phase_is_refused(example_plan: Plan) -> None:
    with pytest.raises(LandError, match="unknown phase"):
        Lander(example_plan, phases=("packages", "wallpaper"), out=io.StringIO())


def test_phase_order_is_canonical_whatever_order_was_asked_for(example_plan: Plan) -> None:
    lander = Lander(example_plan, phases=("settings", "packages"), out=io.StringIO())
    assert lander.phases == ("packages", "settings")


def test_a_display_manager_is_only_enabled_if_there_is_one(example_plan: Plan) -> None:
    with_dm = Lander(example_plan, out=io.StringIO(), phases=("settings",)).steps()
    assert any("sddm.service" in " ".join(s.argv) for s in with_dm)

    headless = Plan(hopfile={}, target={"display_manager": None}, system={})
    without = Lander(headless, out=io.StringIO(), phases=("settings",)).steps()
    services = [arg for step in without for arg in step.argv if arg.endswith(".service")]
    assert services == ["NetworkManager.service"], "a desktopless plan enabled a display manager"


# --- the dry run -----------------------------------------------------------


def test_a_dry_run_creates_nothing(example_plan: Plan, tmp_path: Path) -> None:
    """The transcript is meant to be read on the machine you have not wiped yet."""
    home = tmp_path / "home"
    out = io.StringIO()
    lander = Lander(example_plan, home=home, payload_dir=tmp_path / "hop-payload", out=out)
    assert lander.run() == 0
    assert list(tmp_path.iterdir()) == [], "a dry run touched the disk"
    assert not home.exists()

    text = out.getvalue()
    assert "DRY RUN" in text
    assert "would run:" in text
    assert "none of them executed" in flat(text)
    assert "Nothing on this machine changed." in flat(text)


def test_the_transcript_never_prints_a_secret(tmp_path: Path) -> None:
    """Transcripts get read over shoulders and pasted into bug reports."""
    payload = make_payload(tmp_path)
    out = io.StringIO()
    lander = Lander(
        payload_plan(),
        payload_dir=payload,
        home=tmp_path / "home",
        out=out,
        phases=("payload",),
    )
    lander.run()
    text = out.getvalue()

    assert lander.payload_found
    assert WIFI_PASSWORD not in text
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in text
    assert "BEGIN OPENSSH PRIVATE KEY" not in text
    # The Wi-Fi command is shown, with the two values that matter left as
    # placeholders, so the reader still knows what is about to happen.
    assert "<password>" in text
    assert "nmcli" in text


def test_a_missing_payload_directory_is_a_note_not_a_failure(tmp_path: Path) -> None:
    out = io.StringIO()
    lander = Lander(
        payload_plan(),
        payload_dir=tmp_path / "nowhere",
        home=tmp_path / "home",
        out=out,
        phases=("payload",),
    )
    assert lander.run() == 0
    assert not lander.payload_found
    assert "This is not a failure" in flat(out.getvalue())


def test_a_restore_target_outside_home_is_left_alone(tmp_path: Path) -> None:
    """A hopfile is a file like any other. It can be edited, or handed to you."""
    payload = tmp_path / "hop-payload"
    payload.mkdir()
    (payload / "evil.conf").write_text("x", encoding="utf-8")
    plan = Plan(
        hopfile={},
        target={},
        system={},
        payload=[{"kind": "other", "path": "evil.conf", "restore_to": "/etc/sudoers.d/evil"}],
    )
    out = io.StringIO()
    Lander(plan, payload_dir=payload, home=tmp_path / "home", out=out, phases=("payload",)).run()
    text = flat(out.getvalue())
    assert "outside your home directory, so hop leaves it alone" in text
    assert not (tmp_path / "etc").exists()


@pytest.mark.parametrize(
    "path", ["a/../../outside.txt", "/etc/passwd", "C:\\Windows\\System32\\config\\SAM", "../x"]
)
def test_a_payload_path_that_leaves_the_payload_directory_is_skipped(
    tmp_path: Path, path: str
) -> None:
    """Both joins in the payload phase are pathlib joins, and pathlib is happy to
    throw the left-hand side away. The restore_to check catches the ones that
    carry a destination; this catches the rest, one step earlier."""
    payload = tmp_path / "hop-payload"
    (payload / "a").mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("not yours", encoding="utf-8")
    plan = Plan(hopfile={}, target={}, system={}, payload=[{"kind": "other", "path": path}])

    out = io.StringIO()
    Lander(plan, payload_dir=payload, home=tmp_path / "home", out=out, phases=("payload",)).run()
    text = flat(out.getvalue())
    assert "is not a path inside the payload directory" in text
    assert "would copy" not in text


def test_the_ssh_mode_is_explained_where_it_bites(tmp_path: Path) -> None:
    payload = make_payload(tmp_path)
    out = io.StringIO()
    Lander(
        payload_plan(), payload_dir=payload, home=tmp_path / "home", out=out, phases=("payload",)
    ).run()
    assert "mode 0600" in flat(out.getvalue())


def test_a_plan_with_no_packages_says_so(tmp_path: Path) -> None:
    plan = Plan(hopfile={}, target={}, system={})
    out = io.StringIO()
    Lander(plan, home=tmp_path / "home", out=out, phases=("packages",)).run()
    assert "lists no packages, so there is nothing to install" in flat(out.getvalue())


# --- the mode on a restored file -------------------------------------------


@pytest.mark.parametrize("written", ["0777", "0644", "0666", "4755", "77777", "-1"])
def test_a_secret_payload_file_is_restored_private_whatever_the_hopfile_says(
    written: str,
) -> None:
    """The hopfile says what mode to restore with, and a hopfile can be edited.

    hop cannot tell a private key from the public half of the same pair — both
    arrive as kind 'ssh' — so it has to assume the expensive one.
    """
    mode = _parse_mode(written, "ssh")
    assert not mode & 0o077, f"{written} restored an ssh payload file readable by other users"
    assert not mode & 0o7000, f"{written} put a setuid, setgid or sticky bit on a restored file"


@pytest.mark.parametrize("written", ["4755", "2755", "1777", "77777", "-1"])
def test_no_payload_file_is_restored_with_a_special_bit(written: str) -> None:
    mode = _parse_mode(written, "wallpaper")
    assert not mode & 0o7000
    assert 0 <= mode <= 0o777, "os.chmod would have refused this outright"


def test_the_mode_that_reaches_chmod_is_the_bounded_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound in the parser is worth nothing if the restore goes round it.

    This drives the real restore — the action the payload phase builds — with
    ``os.chmod`` recorded rather than replaced, because the mode bits a test can
    read back afterwards depend on the filesystem it is running on.
    """
    payload = make_payload(tmp_path)
    plan = Plan(
        hopfile={},
        target={},
        system={},
        payload=[
            {"kind": "ssh", "path": "ssh/id_ed25519", "restore_to": "~/.ssh/id_ed25519", "mode": "0777"}
        ],
    )
    asked: list[int] = []
    real_chmod = os.chmod
    monkeypatch.setattr(
        "hop.land.os.chmod", lambda path, mode: (asked.append(mode), real_chmod(path, mode))[0]
    )
    lander = Lander(
        plan,
        payload_dir=payload,
        home=tmp_path / "home",
        out=io.StringIO(),
        phases=("payload",),
    )
    action = lander._build()[0]
    assert "mode 0600" in action.step.detail, action.step.detail
    assert action.do is not None and action.do() == ("ok", "")
    assert asked, "the restore never set a mode at all"
    assert all(not mode & 0o077 for mode in asked), f"chmod was asked for {[oct(m) for m in asked]}"


# --- values the plan hands to a command or a root-owned file ---------------


def test_the_aur_helper_is_not_an_arbitrary_program(tmp_path: Path) -> None:
    """``aur_helper`` is argv[0]: hop runs whatever it names, and it comes out
    of a JSON file that the project tells people to edit by hand."""
    plan = Plan(
        hopfile={},
        target={"aur_helper": "/tmp/anything-at-all"},
        system={},
        packages={"pacman": [], "aur": ["some-package"], "flatpak": []},
    )
    with pytest.raises(LandError, match="AUR helper") as excinfo:
        Lander(plan, home=tmp_path / "home", out=io.StringIO())
    message = str(excinfo.value)
    assert "target.aur_helper" in message, "the message has to name the line to fix"
    assert "Nothing has been installed, copied or changed." in message


def test_an_ordinary_helper_name_still_works(tmp_path: Path) -> None:
    """The check is a character set, not a list of two blessed programs."""
    plan = Plan(
        hopfile={},
        target={"aur_helper": "pikaur"},
        system={},
        packages={"pacman": [], "aur": ["some-package"], "flatpak": []},
    )
    lander = Lander(plan, home=tmp_path / "home", out=io.StringIO(), phases=("packages",))
    assert lander.aur_helper == "pikaur"
    assert any(step.argv[:1] == ["pikaur"] for step in lander.steps())


@pytest.mark.parametrize(
    "system",
    [
        {"locale": "en_US.UTF-8\nLANG=C", "locales": ["en_US.UTF-8"]},
        {"locale": "en_US.UTF-8", "locales": ["en_US.UTF-8", "en_GB.UTF-8\nsomething else"]},
    ],
)
def test_a_locale_cannot_carry_a_second_line_into_etc(tmp_path: Path, system: dict) -> None:
    """The settings phase writes the locales into /etc/locale.gen and
    /etc/locale.conf, where a newline is another line of configuration."""
    plan = Plan(hopfile={}, target={}, system=system)
    with pytest.raises(LandError, match="locale") as excinfo:
        Lander(plan, home=tmp_path / "home", out=io.StringIO())
    assert "/etc/locale.conf" in str(excinfo.value)


def test_the_refusal_happens_before_the_transcript_starts(tmp_path: Path) -> None:
    """LandError promises 'raised before anything has been done'. That includes
    printing a command hop would refuse to run."""
    out = io.StringIO()
    plan = Plan(hopfile={}, target={"aur_helper": "not a helper"}, system={})
    with pytest.raises(LandError):
        Lander(plan, home=tmp_path / "home", out=out)
    assert out.getvalue() == ""


# --- what the last sentence is allowed to promise ---------------------------


def closing(plan: Plan, tmp_path: Path, **kwargs: object) -> str:
    """The summary of a landing with nothing to do, so no step can fail."""
    out = io.StringIO()
    lander = Lander(plan, home=tmp_path / "home", out=out, dry_run=False, **kwargs)
    lander._summary(0, 0, [], [])
    return flat(out.getvalue())


def test_a_payload_only_landing_does_not_promise_a_desktop(tmp_path: Path) -> None:
    """hop-post.sh tells people to finish with 'hop land --only payload', so this
    is the ordinary way to end a landing, not the odd one. It enables no display
    manager, and the closing sentence used to say the desktop starts by itself."""
    plan = Plan(hopfile={}, target={"display_manager": "sddm"}, system={})
    text = closing(plan, tmp_path, phases=("payload",))
    assert "desktop starts by itself" not in text
    assert "does not include enabling the display manager" in text


def test_a_headless_plan_is_told_it_will_get_a_text_login(tmp_path: Path) -> None:
    plan = Plan(hopfile={}, target={"display_manager": None}, system={})
    text = closing(plan, tmp_path)
    assert "desktop starts by itself" not in text
    assert "ends at a text login" in text


def test_a_full_landing_with_a_display_manager_still_says_so(tmp_path: Path) -> None:
    plan = Plan(hopfile={}, target={"display_manager": "sddm"}, system={})
    assert "desktop starts by itself" in closing(plan, tmp_path)


# --- the refusals ----------------------------------------------------------


@pytest.mark.skipif(ON_ARCH, reason="this machine really is Arch, so the refusal cannot fire")
def test_executing_off_arch_refuses_before_doing_anything(example_plan: Plan, tmp_path: Path) -> None:
    """The refusal happens in run(), before the first step, and says what to do."""
    out = io.StringIO()
    lander = Lander(example_plan, home=tmp_path / "home", dry_run=False, out=out)
    with pytest.raises(LandError, match="does not look like an Arch system") as excinfo:
        lander.run()
    message = str(excinfo.value)
    assert "Nothing has been installed, copied or changed." in message
    assert "--execute" in message, "the message has to say what to do instead"
    assert out.getvalue() == "", "nothing was printed, so nothing was started"
    assert list(tmp_path.iterdir()) == []


# --- diff ------------------------------------------------------------------


def test_diff_reports_what_is_missing() -> None:
    plan = Plan(
        hopfile={},
        target={},
        system={},
        packages={"pacman": ["firefox", "vlc"], "aur": ["spotify"], "flatpak": ["org.gimp.GIMP"]},
    )
    result = diff(plan, {"pacman": {"firefox", "linux", "base"}, "flatpak": set()})
    assert result["missing_pacman"] == ["vlc"]
    # An AUR package is built and then installed by pacman, so it turns up in
    # the same database as everything else.
    assert result["missing_aur"] == ["spotify"]
    assert result["missing_flatpak"] == ["org.gimp.GIMP"]
    assert result["extra"] == ["base", "linux"]


def test_diff_on_a_machine_that_has_everything() -> None:
    plan = Plan(hopfile={}, target={}, system={}, packages={"pacman": ["firefox"]})
    result = diff(plan, {"pacman": {"firefox"}, "flatpak": set()})
    assert result["missing_pacman"] == []
    assert result["extra"] == []


def test_diff_copes_with_an_empty_plan() -> None:
    result = diff(Plan(hopfile={}, target={}, system={}), {"pacman": set(), "flatpak": set()})
    assert result == {"missing_pacman": [], "missing_aur": [], "missing_flatpak": [], "extra": []}


@pytest.mark.skipif(
    shutil.which("pacman") is not None, reason="pacman is installed here, so it would be queried"
)
def test_installed_packages_is_empty_rather_than_angry_without_pacman() -> None:
    """hop diff is mostly run on the Windows machine, by someone checking a plan
    before they commit to it. That has to answer, not raise."""
    have = installed_packages()
    assert set(have) == {"pacman", "flatpak"}
    assert have["pacman"] == set()
    # flatpak is only asserted to be a set: a runner that happens to have it
    # installed is not a broken hop.
    assert isinstance(have["flatpak"], set)
