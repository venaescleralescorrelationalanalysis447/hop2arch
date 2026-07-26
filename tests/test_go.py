"""The one command, driven entirely by fakes.

Nothing here downloads an image, mounts one, formats a drive, sets a boot entry
or restarts anything. Every command ``hop go`` runs goes through an injected
runner that answers from a table and records what it was asked for, the four
functions in ``hop/iso.py`` that reach the network or the ISO are replaced, and
``usb.prepare`` — the one call that erases a drive — is replaced by a function
that makes a directory in ``tmp_path`` and shouts if it was called when it
should not have been. Everything between that and the copy is the real code:
the identity check, the inventory, the FAT32 limits, the copy, the size check
afterwards, and the boot entry.

The two tests that matter most are ``test_the_whole_sequence_reaches_the_stick``
and ``test_no_at_the_confirmation_stops_before_anything_is_erased``. The first
proves the stick that comes out of a full run is bootable and carries
everything ``hop install`` needs; the second proves the word "no" is enough.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest

from hop import go, iso, usb
from hop.go import GoOptions, Stage

STICK_ID = r"\\.\PHYSICALDRIVE2"
SYSTEM_ID = r"\\.\PHYSICALDRIVE0"
STICK_BYTES = 30_765_219_840

#: bcdedit prints its field names in the system language. This is what the
#: machines hop is written for actually answer with, and the parser has to find
#: the stick in it without reading a single one of those words.
BCDEDIT_RU = """
\u0414\u0438\u0441\u043f\u0435\u0442\u0447\u0435\u0440 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438 \u043f\u0440\u043e\u0448\u0438\u0432\u043a\u0438
---------------------------
\u0438\u0434\u0435\u043d\u0442\u0438\u0444\u0438\u043a\u0430\u0442\u043e\u0440  {fwbootmgr}
displayorder            {bootmgr}
                        {b2c8f2f0-1111-2222-3333-444455556666}
timeout                 2

\u0414\u0438\u0441\u043f\u0435\u0442\u0447\u0435\u0440 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438 Windows
--------------------------
\u0438\u0434\u0435\u043d\u0442\u0438\u0444\u0438\u043a\u0430\u0442\u043e\u0440  {bootmgr}
\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435         Windows Boot Manager

\u041f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u043f\u0440\u043e\u0448\u0438\u0432\u043a\u0438 (101fffff)
------------------------------
\u0438\u0434\u0435\u043d\u0442\u0438\u0444\u0438\u043a\u0430\u0442\u043e\u0440  {b2c8f2f0-1111-2222-3333-444455556666}
\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435         UEFI: SanDisk Ultra USB 3.0, Partition 1
"""

STICK_GUID = "{b2c8f2f0-1111-2222-3333-444455556666}"


# --- the machine hop thinks it is standing on ------------------------------


def windows_payload(**overrides: object) -> dict:
    """Two disks: the one Windows is on, and the stick."""
    payload: dict = {
        "systemroot": "C:\\Windows",
        "disks": [
            {"number": 0, "model": "Samsung SSD 980 1TB", "serial": "S5GXNF0R", "size": 1.0e12,
             "bus": "NVMe", "boot": True, "system": True},
            {"number": 2, "model": "SanDisk Ultra USB 3.0", "serial": "4C530001",
             "size": float(STICK_BYTES), "bus": "USB", "boot": False, "system": False},
        ],
        "media": [
            {"number": 0, "media": "Fixed hard disk media"},
            {"number": 2, "media": "Removable Media"},
        ],
        "partitions": [
            {"number": 0, "letter": "C", "size": 9.9e11},
            {"number": 2, "letter": "F", "size": 3.07e10},
        ],
        "volumes": [
            {"letter": "C", "label": "", "fs": "NTFS", "size": 9.9e11},
            {"letter": "F", "label": "KINGSTON", "fs": "FAT32", "size": 3.07e10},
        ],
        "windows": ["C"],
    }
    payload.update(overrides)
    return payload


class FakeRunner:
    """Canned answers, matched by substring, and a record of every call.

    The same shape as ``hop.usb.Runner`` and ``hop.iso.Runner``, which is the
    point of those two agreeing: ``hop go`` hands one runner to both.
    """

    def __init__(self, answers: Sequence[tuple[str, object]] = ()) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        self.calls.append(list(argv))
        for needle, answer in self.answers:
            if needle in command:
                if isinstance(answer, tuple):
                    return answer
                if isinstance(answer, (dict, list)):
                    return (0, json.dumps(answer), "")
                return (0, str(answer), "")
        return (0, "", "")

    @property
    def commands(self) -> str:
        return "\n".join(" ".join(call) for call in self.calls)


def runner_for(
    *,
    firmware: str = "UEFI",
    administrator: bool = True,
    payload: dict | None = None,
    extra: Sequence[tuple[str, object]] = (),
) -> FakeRunner:
    return FakeRunner(
        [
            # The needle is a fragment of the probe script itself, so a change to
            # that script that stops it asking these two questions shows up here.
            ("$env:firmware_type", {"firmware": firmware, "administrator": administrator}),
            ("Get-Disk | Select-Object", payload if payload is not None else windows_payload()),
            ("Write-VolumeCache", {"flushed": True}),
            ("bcdedit /enum firmware", BCDEDIT_RU),
            *extra,
        ]
    )


# --- the ISO, as four functions that never touch the network ---------------


def release() -> iso.IsoRelease:
    return iso.IsoRelease(
        version="2026.07.01",
        filename="archlinux-2026.07.01-x86_64.iso",
        url="https://geo.mirror.pkgbuild.com/iso/latest/archlinux-2026.07.01-x86_64.iso",
        sha256="ab" * 32,
        size_bytes=1_234_567_890,
        signature_url="https://geo.mirror.pkgbuild.com/iso/latest/"
        "archlinux-2026.07.01-x86_64.iso.sig",
    )


def make_iso_tree(root: Path) -> Path:
    """The parts of an extracted archiso that hop actually looks at."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "arch" / "x86_64").mkdir(parents=True, exist_ok=True)
    (root / "arch" / "x86_64" / "airootfs.sfs").write_bytes(b"squashfs" * 64)
    (root / "arch" / "boot" / "x86_64").mkdir(parents=True, exist_ok=True)
    (root / "arch" / "boot" / "x86_64" / "vmlinuz-linux").write_bytes(b"kernel")
    (root / "loader" / "entries").mkdir(parents=True, exist_ok=True)
    (root / "loader" / "loader.conf").write_text(
        "timeout 0\ndefault 01-archiso-x86_64-linux.conf\n", encoding="utf-8"
    )
    (root / "loader" / "entries" / "01-archiso-x86_64-linux.conf").write_text(
        "title   Arch Linux install medium (x86_64, UEFI)\n"
        "linux   /arch/boot/x86_64/vmlinuz-linux\n"
        "initrd  /arch/boot/x86_64/initramfs-linux.img\n"
        "options archisobasedir=arch archisolabel=ARCH_202607\n",
        encoding="utf-8",
    )
    (root / "EFI" / "BOOT").mkdir(parents=True, exist_ok=True)
    (root / "EFI" / "BOOT" / "BOOTx64.EFI").write_bytes(b"MZ" * 32)
    return root


class Fakes:
    """Every replaced function, and a note of what it was asked to do."""

    def __init__(self) -> None:
        self.downloaded: list[str] = []
        self.formatted: list[str] = []
        self.flushed: list[str] = []
        self.ejected: list[str] = []
        self.verdict = iso.VerifyResult(
            checksum_ok=True,
            signature_checked=True,
            signature_ok=True,
            detail=(
                "The sha256 of archlinux-2026.07.01-x86_64.iso matches the checksum published "
                "beside it. gpg verified the detached signature against an Arch Linux release "
                "signing key. The image on this disk is the one Arch published."
            ),
        )


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    """Replace the four network/ISO calls and the one call that erases a drive."""
    state = Fakes()

    def latest_release(**_: object) -> iso.IsoRelease:
        return release()

    def download(rel: iso.IsoRelease, dest_dir: Path, *, opener: object = None,
                 progress: object = None) -> Path:
        state.downloaded.append(rel.filename)
        directory = Path(dest_dir)
        directory.mkdir(parents=True, exist_ok=True)
        image = directory / rel.filename
        image.write_bytes(b"not really an iso")
        if callable(progress):
            progress(600_000_000, rel.size_bytes)
            progress(rel.size_bytes, rel.size_bytes)
        return image

    def verify(_path: Path, _rel: iso.IsoRelease, *, runner: object = None) -> iso.VerifyResult:
        return state.verdict

    def extract(_image: Path, dest: Path, *, runner: object = None) -> Path:
        return make_iso_tree(Path(dest))

    def volume_label(_image: Path, *, runner: object = None) -> str:
        return "ARCH_202607"

    def prepare(drive: usb.Drive, **kwargs: object) -> str:
        # The real one erases a disk. This one records that it was reached at
        # all, which is what the refusal tests assert never happens. It calls
        # on_erase where the real one does, because that callback is how hop go
        # knows whether a later failure left the stick empty.
        state.formatted.append(drive.device_id)
        on_erase = kwargs.get("on_erase")
        if callable(on_erase):
            on_erase()
        mount = tmp_path / "stick"
        mount.mkdir(parents=True, exist_ok=True)
        return str(mount)

    monkeypatch.setattr(iso, "latest_release", latest_release)
    monkeypatch.setattr(iso, "download", download)
    monkeypatch.setattr(iso, "verify", verify)
    monkeypatch.setattr(iso, "extract", extract)
    monkeypatch.setattr(iso, "volume_label", volume_label)
    monkeypatch.setattr(usb, "prepare", prepare)

    # The last two hardware calls: getting the bytes out of the write cache and
    # letting go of the volume. tests/test_usb.py owns both, against a real
    # Windows-shaped mount like "E:".
    #
    # They have to be replaced here rather than left to run, because the fake
    # prepare above hands back a directory in tmp_path instead of a drive
    # letter. On Windows that directory happens to sit on C:, so the real code
    # finds a letter and the tests pass; on Linux there is no letter to find and
    # every one of them fails. A test whose result depends on which machine ran
    # it is worse than no test, and this one was green here and red in CI.
    def flush(_runner: object, _platform: str, mount: str) -> None:
        state.flushed.append(str(mount))

    def eject(mount: str, **_kwargs: object) -> None:
        state.ejected.append(str(mount))

    monkeypatch.setattr(usb, "_flush", flush)
    monkeypatch.setattr(usb, "eject", eject)
    return state


def options(tmp_path: Path, hopfile: Path, **overrides: object) -> GoOptions:
    settings: dict = {
        "hopfile": hopfile,
        "out_dir": tmp_path / "out",
        "device_id": STICK_ID,
        "reboot": False,
    }
    settings.update(overrides)
    return GoOptions(**settings)


def flat(text: str) -> str:
    """The transcript with its line breaks taken out.

    Everything hop prints is wrapped to 74 columns, so a sentence worth
    asserting on is usually split across two lines. Asserting against the
    unwrapped text tests what was said rather than where it happened to break.
    """
    return " ".join(text.split())


def go_run(
    opts: GoOptions,
    runner: FakeRunner,
    answers: Sequence[str] = ("yes",),
) -> tuple[int, str, list[str]]:
    """Run it with a scripted set of answers. Returns (code, transcript, asked)."""
    out = StringIO()
    asked: list[str] = []
    replies = list(answers)

    def ask(question: str) -> str:
        asked.append(question)
        return replies.pop(0) if replies else ""

    code = go.run(opts, out=out, ask=ask, runner=runner, platform="windows")
    return (code, out.getvalue(), asked)


# --- the whole thing -------------------------------------------------------


def test_the_whole_sequence_reaches_the_stick(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """One run, end to end, against a stick that is a directory in tmp_path."""
    runner = runner_for()
    code, transcript, asked = go_run(options(tmp_path, example_hopfile), runner)

    assert code == 0, transcript
    assert asked == ["Type yes to go on. Anything else, including y, stops here: "]
    assert fakes.formatted == [STICK_ID]

    medium = tmp_path / "stick"
    # The installer itself.
    assert (medium / "EFI" / "BOOT" / "BOOTx64.EFI").read_bytes() == b"MZ" * 32
    assert (medium / "arch" / "x86_64" / "airootfs.sfs").stat().st_size == 8 * 64

    # Everything hop install and hop land will look for, in one place.
    hop_dir = medium / usb.HOP_DIR
    assert json.loads((hop_dir / "hop-plan.json").read_text(encoding="utf-8"))["plan_version"] == 1
    assert (hop_dir / "hopfile.json").is_file()
    assert (hop_dir / "hop-report.md").is_file()
    assert (hop_dir / "archinstall" / "user_configuration.json").is_file()
    assert (hop_dir / "archinstall" / "hop-post.sh").is_file()

    # The copy of hop that travels, because the live environment has none and
    # there may be no network in the room.
    assert (hop_dir / "hop" / "go.py").is_file()
    assert (hop_dir / "hop" / "data" / "packages.toml").is_file()
    assert not (hop_dir / "hop" / "__pycache__").exists()

    # The script, and the boot entry that runs it.
    bootstrap = (hop_dir / usb.BOOTSTRAP_NAME).read_text(encoding="utf-8")
    assert "python3 -m hop install" in bootstrap
    assert "\r" not in bootstrap, "bash will not run a script with carriage returns in it"
    entry = (medium / "loader" / "entries" / usb.HOP_ENTRY_NAME).read_text(encoding="utf-8")
    assert "script=/hop/bootstrap.sh" in entry
    assert "archisolabel=ARCH_202607" in entry

    # The archiso entry it was copied from is left exactly as it was, so the
    # same stick still boots a plain installer.
    original = medium / "loader" / "entries" / "01-archiso-x86_64-linux.conf"
    assert "script=" not in original.read_text(encoding="utf-8")
    assert "timeout 10" in (medium / "loader" / "loader.conf").read_text(encoding="utf-8")


def test_the_transcript_says_what_is_about_to_happen(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    _, transcript, _ = go_run(options(tmp_path, example_hopfile), runner_for())

    for index, (_, title) in enumerate(go.STAGES, start=1):
        assert f"{index}/8  {title}" in transcript

    # The stick, by the name a person can match against the thing in their hand.
    assert "SanDisk Ultra USB 3.0" in transcript
    assert STICK_ID in transcript
    assert "Everything on it is gone, and there is no undo" in flat(transcript)

    # The blockers, in front of the reader rather than in a file they have not
    # opened. The example machine has Premiere on it.
    assert "Adobe Premiere Pro 2024" in transcript
    assert "hoppability" in transcript

    # And where the disk decision is really made.
    assert "type that disk's device path by hand" in flat(transcript)
    assert str(tmp_path / "out" / "hop-report.md") in transcript


def test_reboot_false_is_a_real_path_and_restarts_nothing(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    runner = runner_for()
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner)
    assert code == 0
    assert "shutdown" not in runner.commands
    assert "bcdedit" not in runner.commands
    assert "leave the stick plugged in, restart" in flat(transcript)
    assert "F12" in transcript, "the boot menu key is the way in when hop does not reboot"


# --- saying no -------------------------------------------------------------


def test_no_at_the_confirmation_stops_before_anything_is_erased(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    runner = runner_for()
    code, transcript, asked = go_run(options(tmp_path, example_hopfile), runner, answers=("no",))

    assert code == 1
    assert len(asked) == 1
    assert fakes.formatted == [], "the drive was not touched"
    assert fakes.downloaded == [], "and the image was never fetched"
    assert not (tmp_path / "stick").exists()
    assert "Stopped, because you said no" in flat(transcript)
    assert "nothing on this machine has been changed" in flat(transcript)
    # The work already done is not thrown away.
    assert "--hopfile" in transcript


def test_anything_that_is_not_yes_is_a_no(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    for answer in ("y", "Y", "", "  ", "yes please", "\u0434\u0430"):
        code, _, _ = go_run(options(tmp_path, example_hopfile), runner_for(), answers=(answer,))
        assert code == 1, f"{answer!r} was taken for a yes"
        assert fakes.formatted == []


def test_yes_is_accepted_however_it_was_capitalised(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    code, _, _ = go_run(options(tmp_path, example_hopfile), runner_for(), answers=(" YES ",))
    assert code == 0
    assert fakes.formatted == [STICK_ID]


def test_assume_yes_says_in_the_transcript_that_nobody_was_asked(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    def refuse(question: str) -> str:
        raise AssertionError(f"assume_yes still asked: {question!r}")

    out = StringIO()
    code = go.run(
        options(tmp_path, example_hopfile, assume_yes=True),
        out=out,
        ask=refuse,
        runner=runner_for(),
        platform="windows",
    )
    assert code == 0
    assert "hop did not ask" in flat(out.getvalue())
    assert "Nobody confirmed any of the above" in flat(out.getvalue())


def test_a_question_nobody_can_hear_is_a_no(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """stdin closed: hop does not answer on the user's behalf."""

    def closed(_question: str) -> str:
        raise EOFError

    out = StringIO()
    code = go.run(
        options(tmp_path, example_hopfile),
        out=out,
        ask=closed,
        runner=runner_for(),
        platform="windows",
    )
    assert code == 1
    assert fakes.formatted == []
    assert "will not take silence for a yes" in flat(out.getvalue())


# --- the preflight ---------------------------------------------------------


def test_every_failed_check_is_reported_in_one_run(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """Three things wrong should take one run to discover, not three."""
    no_stick = windows_payload()
    no_stick["disks"] = [no_stick["disks"][0]]
    no_stick["media"] = [no_stick["media"][0]]
    runner = runner_for(firmware="BIOS", administrator=False, payload=no_stick)

    code, transcript, asked = go_run(options(tmp_path, example_hopfile, device_id=None), runner)

    assert code == 2
    assert asked == [], "nothing was asked, because nothing could have happened"
    assert "3 things stop hop before anything is touched" in transcript
    assert "not elevated" in flat(transcript)
    assert "UEFI-only" in flat(transcript)
    assert "no removable drive here" in flat(transcript)
    assert fakes.downloaded == []


def test_a_bios_machine_is_refused_before_the_download(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    runner = runner_for(firmware="BIOS")
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner)
    assert code == 2
    assert "legacy BIOS mode" in flat(transcript)
    assert "Rufus" in flat(transcript), "a refusal with no way forward is not a refusal, it is a wall"
    assert fakes.downloaded == []
    assert fakes.formatted == []


def test_an_unknown_firmware_is_left_to_the_scanner(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """The probe has two ways to tell; the hopfile's scanner had four."""
    runner = runner_for(firmware="unknown")
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner)
    assert code == 0, transcript
    assert "firmware      unknown" in transcript


def test_an_unknown_firmware_the_scan_could_not_settle_either_is_refused(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    hopfile = json.loads(example_hopfile.read_text(encoding="utf-8-sig"))
    hopfile["system"]["firmware"] = "unknown"
    copied = tmp_path / "hopfile.json"
    copied.write_text(json.dumps(hopfile), encoding="utf-8")

    code, transcript, _ = go_run(options(tmp_path, copied), runner_for(firmware="unknown"))
    assert code == 2
    assert "could not tell whether this machine boots through UEFI" in flat(transcript)
    assert "msinfo32" in flat(transcript)
    assert fakes.downloaded == []


def test_the_system_disk_is_refused_by_name(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    code, transcript, _ = go_run(
        options(tmp_path, example_hopfile, device_id=SYSTEM_ID), runner_for()
    )
    assert code == 2
    assert "carries an operating system" in flat(transcript)
    assert fakes.formatted == []


def test_two_sticks_are_not_chosen_between(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    payload = windows_payload()
    payload["disks"].append(
        {"number": 3, "model": "Kingston DataTraveler", "serial": "0014", "size": 1.6e10,
         "bus": "USB", "boot": False, "system": False}
    )
    payload["media"].append({"number": 3, "media": "Removable Media"})
    payload["partitions"].append({"number": 3, "letter": "G", "size": 1.6e10})
    payload["volumes"].append({"letter": "G", "label": "DT", "fs": "FAT32", "size": 1.6e10})

    code, transcript, _ = go_run(
        options(tmp_path, example_hopfile, device_id=None), runner_for(payload=payload)
    )
    assert code == 2
    assert "will not choose between them" in flat(transcript)
    assert "--device-id" in transcript
    assert fakes.formatted == []


def test_a_device_id_that_is_not_there_lists_what_is(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    code, transcript, _ = go_run(
        options(tmp_path, example_hopfile, device_id=r"\\.\PHYSICALDRIVE9"), runner_for()
    )
    assert code == 2
    assert "PHYSICALDRIVE9" in transcript
    assert "SanDisk Ultra USB 3.0" in transcript, "it should show what is actually there"


def test_a_machine_that_will_not_say_whether_the_shell_is_elevated(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    runner = FakeRunner([("$env:firmware_type", (1, "", "Access is denied."))])
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner)
    assert code == 2
    assert "rather than guessing" in flat(transcript)
    assert fakes.downloaded == []


def test_hop_go_is_a_windows_verb(tmp_path: Path, example_hopfile: Path, fakes: Fakes) -> None:
    out = StringIO()
    code = go.run(options(tmp_path, example_hopfile), out=out, runner=runner_for(), platform="linux")
    assert code == 2
    assert "hop land" in flat(out.getvalue())


# --- the image -------------------------------------------------------------


def test_an_unsigned_image_is_worth_one_more_question(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    fakes.verdict = iso.VerifyResult(
        checksum_ok=True,
        signature_checked=False,
        signature_ok=False,
        detail=(
            "The sha256 of archlinux-2026.07.01-x86_64.iso matches the checksum published "
            "beside it, so the download is intact. That is all it establishes: the checksum "
            "came from the same mirror as the image, so anyone able to change one could "
            "change the other."
        ),
    )
    runner = runner_for()
    code, transcript, asked = go_run(
        options(tmp_path, example_hopfile), runner, answers=("yes", "no")
    )

    assert code == 1
    assert len(asked) == 2
    assert "came from the same mirror as the image" in flat(transcript)
    assert fakes.downloaded == ["archlinux-2026.07.01-x86_64.iso"]
    assert fakes.formatted == [], "the second no came before the drive was touched"


def test_a_signature_gpg_rejected_is_never_offered_as_a_question(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """The one failure the checksum cannot see must not be answerable with "yes".

    A mirror able to serve a modified image can serve a matching sha256 beside
    it; the detached signature is the only thing that catches that, and gpg
    saying BADSIG is that check catching it. Offering "build the stick from this
    image anyway?" there is offering to install a forged operating system on one
    word — and the word asked for is the same one that means "carry on" when the
    signature merely could not be checked.
    """
    fakes.verdict = iso.VerifyResult(
        checksum_ok=True,
        signature_checked=True,
        signature_ok=False,
        signature_bad=True,
        detail="gpg rejected the signature on it: the file is not what the signature says.",
    )
    code, transcript, asked = go_run(
        options(tmp_path, example_hopfile), runner_for(), answers=("yes", "yes")
    )

    assert code == 2
    assert len(asked) == 1, "the only question asked was the confirmation"
    flattened = flat(transcript)
    assert "does not match the file" in flattened
    assert "there is no question to ask about it" in flattened
    assert fakes.formatted == [], "nothing was erased"
    assert "No drive has been erased" in flattened


def test_the_second_yes_carries_on(tmp_path: Path, example_hopfile: Path, fakes: Fakes) -> None:
    fakes.verdict = iso.VerifyResult(True, False, False, "gpg is not installed here.")
    code, _, asked = go_run(options(tmp_path, example_hopfile), runner_for(), ("yes", "yes"))
    assert code == 0
    assert len(asked) == 2
    assert fakes.formatted == [STICK_ID]


def test_a_checksum_mismatch_is_not_a_question(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    fakes.verdict = iso.VerifyResult(False, False, False, "The sha256 does not match.")
    code, transcript, asked = go_run(options(tmp_path, example_hopfile), runner_for())
    assert code == 2
    assert len(asked) == 1, "hop does not offer to install from an image that is not the image"
    assert "will not build a stick out of it" in flat(transcript)
    assert fakes.formatted == []


def test_a_refusal_is_wrapped_like_everything_else_hop_says(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """The last thing somebody reads is not the one thing printed as one line."""
    fakes.verdict = iso.VerifyResult(False, False, False, "The sha256 does not match.")
    _, transcript, _ = go_run(options(tmp_path, example_hopfile), runner_for())

    ending = transcript[transcript.index("--- stopped") :]
    too_long = [line for line in ending.splitlines() if len(line) > 78]
    assert too_long == [], too_long


# --- what survives ---------------------------------------------------------


def test_a_failure_after_the_stick_was_written_says_the_stick_is_finished(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_entries(medium: Path, *, script_relative: str) -> list[Path]:
        raise usb.UsbError("loader/entries is not there, so this medium has no boot entries.")

    monkeypatch.setattr(usb, "add_autostart", no_entries)
    runner = runner_for()
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner)

    assert code == 2
    assert "The stick was already built" in flat(transcript)
    assert "not a reason to start again" in flat(transcript)
    assert "/run/archiso/bootmnt/hop/bootstrap.sh" in transcript
    assert "Windows still boots" in flat(transcript)
    # And it is safe to take out of the machine. The ending tells the reader to
    # boot the stick, which means unplugging it, and a stick unplugged with its
    # last writes still in a cache boots to a filesystem error instead.
    #
    # Asserted against the recorded fakes rather than against the PowerShell the
    # runner saw: which cmdlets that takes is tests/test_usb.py's business, and
    # pinning them here made this test pass on Windows and fail on Linux for a
    # reason that had nothing to do with hop go. What matters here is that the
    # stage ran at all before the ending claimed the stick was finished.
    assert fakes.ejected, "the medium was never ejected, so it is not safe to unplug"
    assert "flushed and safe to unplug" in flat(transcript)


def test_a_failure_before_the_stick_says_no_drive_was_erased(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def small(*_args: object, **_kwargs: object) -> Path:
        raise usb.UsbError("the stick has 1.0 GB usable and the installer comes to 1.4 GB.")

    monkeypatch.setattr(usb, "write_medium", small)
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner_for())

    assert code == 2
    assert "No drive has been erased" in flat(transcript)
    assert str(tmp_path / "out" / "iso") in transcript, "the download is kept, not repeated"


def test_keep_iso_false_deletes_the_image_only_once_the_stick_exists(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    code, transcript, _ = go_run(
        options(tmp_path, example_hopfile, keep_iso=False), runner_for()
    )
    assert code == 0
    assert fakes.formatted == [STICK_ID]
    assert not (tmp_path / "out" / "iso" / "archlinux-2026.07.01-x86_64.iso").exists()
    assert not (tmp_path / "out" / "iso-contents").exists()
    assert (tmp_path / "stick" / "EFI" / "BOOT" / "BOOTx64.EFI").is_file()
    assert "deleted" in transcript


def test_keep_iso_keeps_it(tmp_path: Path, example_hopfile: Path, fakes: Fakes) -> None:
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner_for())
    assert code == 0
    assert (tmp_path / "out" / "iso" / "archlinux-2026.07.01-x86_64.iso").is_file()
    assert "the image     " in transcript


def test_a_drive_that_could_not_be_ejected_is_not_a_failure(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything is written and checked; this is only about unplugging it."""

    def stuck(*_args: object, **_kwargs: object) -> None:
        raise usb.UsbError("E: could not be dismounted. The data is on the stick.")

    monkeypatch.setattr(usb, "eject", stuck)
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner_for())
    assert code == 0
    assert "The data is on the stick" in flat(transcript)


# --- the reboot ------------------------------------------------------------


def test_a_one_shot_boot_order_and_nothing_permanent(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    runner = runner_for()
    code, transcript, _ = go_run(options(tmp_path, example_hopfile, reboot=True), runner)

    assert code == 0
    assert ["bcdedit", "/set", "{fwbootmgr}", "bootsequence", STICK_GUID] in runner.calls
    assert ["shutdown", "/r", "/t", "60"] in runner.calls
    assert "displayorder" not in runner.commands, "the permanent boot order is not hop's to change"
    assert "default" not in runner.commands
    assert "shutdown /a" in transcript
    assert "one-shot boot order" in flat(transcript)


def test_firmware_that_will_not_be_told_gets_the_boot_menu_keys(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    runner = runner_for(extra=[("bcdedit", (1, "", "The set command specified is not valid."))])
    code, transcript, _ = go_run(options(tmp_path, example_hopfile, reboot=True), runner)

    assert code == 0
    assert "could not arrange a one-shot boot" in flat(transcript)
    assert "Lenovo" in transcript and "Novo" in transcript
    assert ["shutdown", "/r", "/t", "60"] in runner.calls


def test_a_reboot_that_will_not_start_does_not_undo_the_stick(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    runner = runner_for(extra=[("shutdown", (1, "", "Access is denied."))])
    code, transcript, _ = go_run(options(tmp_path, example_hopfile, reboot=True), runner)
    assert code == 0
    assert "Nothing is lost: the stick is finished" in flat(transcript)


def test_the_boot_entry_is_found_without_reading_a_single_field_name() -> None:
    drive = usb.Drive(STICK_ID, 2, "SanDisk Ultra USB 3.0", "4C530001", STICK_BYTES, "USB",
                      removable=True, system=False)
    found = go._firmware_entry(BCDEDIT_RU, drive)
    assert found is not None
    identifier, description = found
    assert identifier == STICK_GUID
    assert "SanDisk" in description


def test_a_machine_with_no_removable_boot_entry_gets_no_guess() -> None:
    drive = usb.Drive(STICK_ID, 2, "SanDisk Ultra USB 3.0", "4C530001", STICK_BYTES, "USB",
                      removable=True, system=False)
    plain = (
        "Windows Boot Manager\n"
        "--------------------\n"
        "identifier              {bootmgr}\n"
        "description             Windows Boot Manager\n"
    )
    assert go._firmware_entry(plain, drive) is None


# --- odds and ends ---------------------------------------------------------


def test_a_missing_hopfile_is_a_sentence(tmp_path: Path, fakes: Fakes) -> None:
    code, transcript, _ = go_run(options(tmp_path, tmp_path / "nothing.json"), runner_for())
    assert code == 2
    assert "no hopfile at" in flat(transcript)
    assert fakes.formatted == []


def test_the_stages_are_named_and_start_undone() -> None:
    assert [name for name, _ in go.STAGES] == [
        "preflight", "scan", "plan", "confirm", "iso", "medium", "bootstrap", "reboot",
    ]
    assert Stage("iso", "the Arch image").done is False


def test_no_powershell_script_in_this_module_carries_a_double_quote() -> None:
    """The rule hop/usb.py sets, checked here too: the quoting is not worth it."""
    assert '"' not in go._PROBE
    with pytest.raises(go.GoError):
        go._no_quotes('Write-Output "hello"')


# --- what the endings are allowed to claim ----------------------------------


def test_a_failure_after_the_erase_does_not_say_the_stick_is_intact(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The format succeeds and the copy fails.

    Everything hop/usb.py refuses before the erase says "Nothing has been
    erased" in as many words, so the ending used to read the stage list and say
    the same thing for a failure that happened after it. Somebody holding a
    stick that has just been wiped is the one reader who must not be told that.
    """
    def explode(_source: Path, _destination: Path) -> None:
        raise usb.UsbError("could not write to the stick: the device reports an I/O error")

    monkeypatch.setattr(usb, "_copy_file", explode)
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner_for())

    assert code == 2
    assert fakes.formatted == [STICK_ID], "the erase did happen"
    flattened = flat(transcript)
    assert "The stick was erased before this failed" in flattened
    assert "will not boot" in flattened
    assert "No disk inside this machine was touched" in flattened
    assert "No drive has been erased" not in flattened


def test_a_failure_before_the_erase_still_says_nothing_was_erased(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same rule: no hedging where hop does know."""
    def no_boot_file(_image: Path, dest: Path, *, runner: object = None) -> Path:
        tree = make_iso_tree(Path(dest))
        (tree / "EFI" / "BOOT" / "BOOTx64.EFI").unlink()
        return tree

    monkeypatch.setattr(iso, "extract", no_boot_file)
    code, transcript, _ = go_run(options(tmp_path, example_hopfile), runner_for())

    assert code == 2
    assert fakes.formatted == []
    flattened = flat(transcript)
    assert "No drive has been erased" in flattened
    assert "The stick was erased" not in flattened


def test_the_confirmation_does_not_promise_a_check_hop_may_not_make(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """gpg is usually not installed on Windows, so hop cannot promise it checked.

    The paragraph above the one confirmation is the last place to say more than
    the code delivers — and it has to keep the two failures apart, because a
    signature hop could not check and a signature gpg rejected lead to opposite
    outcomes.
    """
    _, transcript, _ = go_run(options(tmp_path, example_hopfile), runner_for(), answers=["no"])
    flattened = flat(transcript)
    assert "checks its signature before it uses it" not in flattened
    assert "where gpg is installed here" in flattened
    assert "comes back bad, hop stops and does not ask" in flattened
    assert "asks you a second time" in flattened


def test_the_payload_secrets_are_named_before_the_question_whatever_the_flag(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """--with-secrets says what this run asked for; the payload says what travels.

    Re-running with --hopfile skips the scan, and with it the only warning there
    used to be, while carrying the same private keys onto a filesystem that has
    no permissions on it.
    """
    payload = tmp_path / "out" / "hop-payload" / "ssh"
    payload.mkdir(parents=True)
    (payload / "id_ed25519").write_text("PRIVATE KEY\n", encoding="utf-8")

    _, transcript, _ = go_run(
        options(tmp_path, example_hopfile, with_secrets=False), runner_for(), answers=["no"]
    )
    flattened = flat(transcript)
    assert "marked private" in flattened
    assert "ssh, wifi" in flattened
    assert "FAT32, which has no file permissions" in flattened
    assert "erase it once hop land has finished" in flattened
    # Named, and named as the whole directory: the copy takes every file under
    # it, and the hopfile's list of entries is not what decides that.
    assert str(tmp_path / "out" / "hop-payload") in transcript
    assert "whether or not the hopfile lists it" in flattened
    # And nothing prints the bytes of anything in it.
    assert "PRIVATE KEY" not in transcript


def test_a_payload_that_will_not_travel_is_said_not_to_be_travelling(
    tmp_path: Path, example_hopfile: Path, fakes: Fakes
) -> None:
    """The hopfile lists private files and the directory holding them is gone.

    Somebody who scanned with --with-secrets and is re-running from the hopfile
    expects their keys to arrive on the other side. They are not going to, and
    the run that would have said so is the last one before the stick is built.
    """
    _, transcript, _ = go_run(
        options(tmp_path, example_hopfile), runner_for(), answers=["no"]
    )
    flattened = flat(transcript)
    assert "cannot find the payload directory" in flattened
    assert "none of it is going onto the stick" in flattened
    assert "FAT32, which has no file permissions" not in flattened


# --- where the payload is read from ----------------------------------------


def _payload_dir_for(tmp_path: Path, stamped: str) -> tuple[Path | None, str]:
    """Ask _Go where it would read the payload from, and what it said about it."""
    from hop.plan import Plan

    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    hopfile = inbox / "received.json"
    hopfile.write_text("{}", encoding="utf-8")

    outside = tmp_path / "ssh"
    outside.mkdir(exist_ok=True)
    (outside / "id_ed25519").write_text("PRIVATE KEY", encoding="utf-8")

    beside = inbox / "hop-payload"
    beside.mkdir(exist_ok=True)
    (beside / "wallpaper.jpg").write_bytes(b"x")

    driver = go._Go.__new__(go._Go)
    driver.out_dir = tmp_path / "hop-out"
    driver.out = StringIO()
    stamped = stamped.replace("{outside}", str(outside))
    plan = Plan(hopfile={"payload_dir": stamped}, target={}, system={})
    return driver._payload_dir(plan, hopfile), driver.out.getvalue()


@pytest.mark.parametrize("stamped", ["../ssh", "{outside}", "./../ssh"])
def test_the_hopfile_cannot_point_the_payload_outside_its_own_directory(
    tmp_path: Path, stamped: str
) -> None:
    """Everything in the payload directory is copied onto the stick whole, so this
    value decides what leaves the machine — onto FAT32, which has no permissions.

    hop/land.py refuses a restore target outside your home for exactly this
    reason: a hopfile is a file like any other, it can be edited, and it can be
    handed to you. This is the same rule pointing the other way.
    """
    found, said = _payload_dir_for(tmp_path, stamped)
    assert found is None
    assert "outside the directory the hopfile is in" in " ".join(said.split())


def test_a_payload_beside_the_hopfile_is_still_used(tmp_path: Path) -> None:
    found, said = _payload_dir_for(tmp_path, "hop-payload")
    assert found is not None
    assert found.name == "hop-payload"
    assert said == "", "a legitimate payload directory should not be explained away"


def test_the_refusal_is_said_out_loud_rather_than_silently_skipped(tmp_path: Path) -> None:
    """Quietly using a different directory than the hopfile named is its own
    confusion: the transcript would print a path the user never asked for."""
    _, said = _payload_dir_for(tmp_path, "{outside}")
    assert "hop is not copying that onto the stick" in " ".join(said.split())


def test_a_payload_dir_that_is_not_there_needs_no_explanation(tmp_path: Path) -> None:
    """A path that escapes and does not exist is refused by simply not being
    found. Explaining a directory the user does not have would be noise."""
    found, said = _payload_dir_for(tmp_path, "../../ssh")
    assert found is None
    assert said == ""
