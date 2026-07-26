"""Finding, fetching, checking and unpacking the Arch image.

Nothing in here touches the network, and nothing runs a program. Every test
drives ``hop.iso`` through a fake opener that answers from a dict and a fake
runner that answers from a table, which is the whole reason those two arguments
exist. No file larger than a few hundred bytes is written.

The tests that matter most are the ones about what ``verify`` is willing to say.
A matching checksum must never come back as a verified image, because the
checksum and the image come from the same mirror; only the signature separates
"not corrupted" from "published by Arch".
"""

from __future__ import annotations

import datetime as _dt
import email.message
import hashlib
import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

import pytest

from hop.iso import (
    ARCH_SIGNING_KEYS,
    DEFAULT_MIRRORS,
    IsoError,
    IsoRelease,
    _copy_tree,
    _drive_root,
    _extract_unix,
    _extract_windows,
    download,
    extract,
    latest_release,
    verify,
    volume_label,
)

MIRROR = DEFAULT_MIRRORS[0]
LATEST = MIRROR + "iso/latest/"

IMAGE = b"this stands in for 1.2 GB of Arch" * 8
DIGEST = hashlib.sha256(IMAGE).hexdigest()
SIGNATURE = b"-----BEGIN PGP SIGNATURE-----\nnot a real one\n-----END PGP SIGNATURE-----\n"

KNOWN_FINGERPRINT = next(iter(ARCH_SIGNING_KEYS))
KNOWN_OWNER = ARCH_SIGNING_KEYS[KNOWN_FINGERPRINT]


# --- the fakes -------------------------------------------------------------


class FakeResponse(io.BytesIO):
    """What the fake opener hands back: bytes, plus the headers urllib would."""

    def __init__(self, blob: bytes) -> None:
        super().__init__(blob)
        self.headers = {"Content-Length": str(len(blob))}


def opener_for(files: dict[str, bytes]):
    """An opener that answers from a dict and 404s on anything else."""

    def opener(url: str) -> IO[bytes]:
        if url not in files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return FakeResponse(files[url])

    return opener


def version_days_ago(days: int) -> str:
    return (_dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=days)).strftime("%Y.%m.%d")


def sums_for(version: str, *, digest: str = DIGEST) -> bytes:
    """A checksum file shaped like the one on a real mirror."""
    return (
        f"{'a' * 64}  archlinux-bootstrap-{version}-x86_64.tar.zst\n"
        f"{digest}  archlinux-{version}-x86_64.iso\n"
        f"{'b' * 64}  archlinux-x86_64.iso\n"
    ).encode()


def good_mirror(version: str | None = None, *, base: str = LATEST) -> dict[str, bytes]:
    version = version or version_days_ago(3)
    name = f"archlinux-{version}-x86_64.iso"
    return {
        base + "sha256sums.txt": sums_for(version),
        base + name: IMAGE,
        base + name + ".sig": SIGNATURE,
    }


def runner_for(table: dict[str, tuple[int, str, str]], calls: list[list[str]] | None = None):
    """A runner that answers by the first argv word, or by the whole command."""

    def run(argv: list[str]) -> tuple[int, str, str]:
        if calls is not None:
            calls.append(argv)
        key = " ".join(argv)
        for candidate in (key, argv[0]):
            if candidate in table:
                return table[candidate]
        raise AssertionError(f"the test did not expect: {key}")

    return run


def gpg_runner(status: str, *, code: int = 0, err: str = "", present: bool = True):
    table = {
        "gpg --version": (0 if present else 127, "gpg (GnuPG) 2.4.5", ""),
        "gpg": (code, status, err),
    }
    return runner_for(table)


def written_image(tmp_path: Path, *, blob: bytes = IMAGE, sig: bytes | None = SIGNATURE) -> Path:
    path = tmp_path / "archlinux-2026.07.01-x86_64.iso"
    path.write_bytes(blob)
    if sig is not None:
        Path(str(path) + ".sig").write_bytes(sig)
    return path


def release_for(path: Path, *, digest: str = DIGEST) -> IsoRelease:
    return IsoRelease(
        version="2026.07.01",
        filename=path.name,
        url=LATEST + path.name,
        sha256=digest,
        size_bytes=len(IMAGE),
        signature_url=LATEST + path.name + ".sig",
    )


# --- finding the release ---------------------------------------------------


def test_reads_the_checksum_file_and_ignores_the_undated_copy() -> None:
    version = version_days_ago(5)
    release = latest_release(opener=opener_for(good_mirror(version)))

    assert release.version == version
    assert release.filename == f"archlinux-{version}-x86_64.iso"
    assert release.url == LATEST + release.filename
    assert release.sha256 == DIGEST
    assert release.size_bytes == len(IMAGE)
    assert release.signature_url == release.url + ".sig"


def test_a_mirror_that_404s_is_stepped_over() -> None:
    """The first mirror has nothing; the second answers, and that is the answer."""
    second = DEFAULT_MIRRORS[1] + "iso/latest/"
    release = latest_release(opener=opener_for(good_mirror(base=second)))
    assert release.url.startswith(DEFAULT_MIRRORS[1])


def test_every_mirror_failing_says_which_ones_were_tried() -> None:
    with pytest.raises(IsoError) as caught:
        latest_release(opener=opener_for({}))
    message = str(caught.value)
    for mirror in DEFAULT_MIRRORS:
        assert mirror in message
    assert "404" in message


def test_a_truncated_checksum_file_is_not_an_answer() -> None:
    """Half a file arrives as a 200 with no usable line in it. Try the next one."""
    cut = sums_for(version_days_ago(2))[:100]
    files = {LATEST + "sha256sums.txt": cut}
    with pytest.raises(IsoError) as caught:
        latest_release(mirror=MIRROR, opener=opener_for(files))
    assert "no dated x86_64 image" in str(caught.value)


def test_html_where_a_checksum_file_should_be() -> None:
    files = {LATEST + "sha256sums.txt": b"<html><body>404 not found</body></html>\n"}
    with pytest.raises(IsoError) as caught:
        latest_release(mirror=MIRROR, opener=opener_for(files))
    assert "no checksum lines" in str(caught.value)


def test_a_mirror_months_behind_is_refused() -> None:
    with pytest.raises(IsoError) as caught:
        latest_release(mirror=MIRROR, opener=opener_for(good_mirror(version_days_ago(400))))
    assert "stopped syncing" in str(caught.value)


def test_every_mirror_looking_ancient_points_at_the_clock() -> None:
    """All four mirrors falling behind at once is not what has happened."""
    files: dict[str, bytes] = {}
    for mirror in DEFAULT_MIRRORS:
        files.update(good_mirror(version_days_ago(500), base=mirror + "iso/latest/"))

    with pytest.raises(IsoError) as caught:
        latest_release(opener=opener_for(files))
    assert "date" in str(caught.value)
    assert "clock set well ahead" in str(caught.value)


def test_plain_http_is_refused_before_anything_is_fetched() -> None:
    def opener(url: str) -> IO[bytes]:
        raise AssertionError("nothing should have been fetched")

    with pytest.raises(IsoError) as caught:
        latest_release(mirror="http://mirror.example/archlinux/", opener=opener)
    assert "https://" in str(caught.value)


def test_a_mirror_that_will_not_give_a_size_is_still_usable() -> None:
    class NoHeaders(io.BytesIO):
        headers: dict[str, str] = {}

    files = good_mirror(version_days_ago(1))

    def opener(url: str) -> IO[bytes]:
        if url not in files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return NoHeaders(files[url])

    assert latest_release(mirror=MIRROR, opener=opener).size_bytes == 0


# --- downloading -----------------------------------------------------------


def test_download_writes_the_image_and_the_signature(tmp_path: Path) -> None:
    files = good_mirror(version_days_ago(1))
    opener = opener_for(files)
    release = latest_release(mirror=MIRROR, opener=opener)

    seen: list[tuple[int, int]] = []
    path = download(release, tmp_path, opener=opener, progress=lambda done, total: seen.append((done, total)))

    assert path == tmp_path / release.filename
    assert path.read_bytes() == IMAGE
    assert Path(str(path) + ".sig").read_bytes() == SIGNATURE
    assert seen[-1] == (len(IMAGE), len(IMAGE))
    assert not list(tmp_path.glob("*.part"))


def test_a_truncated_download_is_deleted_not_kept(tmp_path: Path) -> None:
    """The mirror promised a size and delivered less. That is not an image."""
    files = good_mirror(version_days_ago(1))
    release = latest_release(mirror=MIRROR, opener=opener_for(files))
    files[release.url] = IMAGE[:40]

    with pytest.raises(IsoError) as caught:
        download(release, tmp_path, opener=opener_for(files))

    assert "truncated" in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_a_connection_that_dies_leaves_nothing_behind(tmp_path: Path) -> None:
    class Dies(io.BytesIO):
        headers = {"Content-Length": str(len(IMAGE))}

        def read(self, size: int = -1) -> bytes:
            raise OSError("connection reset by peer")

    files = good_mirror(version_days_ago(1))
    release = latest_release(mirror=MIRROR, opener=opener_for(files))

    def opener(url: str) -> IO[bytes]:
        return Dies(b"")

    with pytest.raises(IsoError) as caught:
        download(release, tmp_path, opener=opener)
    assert "connection reset" in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_an_image_already_there_at_the_right_size_is_not_fetched_again(tmp_path: Path) -> None:
    files = good_mirror(version_days_ago(1))
    release = latest_release(mirror=MIRROR, opener=opener_for(files))
    (tmp_path / release.filename).write_bytes(IMAGE)

    def opener(url: str) -> IO[bytes]:
        if url.endswith(".sig"):
            return FakeResponse(SIGNATURE)
        raise AssertionError("the image should not have been downloaded twice")

    assert download(release, tmp_path, opener=opener).read_bytes() == IMAGE


def test_a_missing_signature_does_not_fail_the_download(tmp_path: Path) -> None:
    """Whether the signature is there is verify()'s sentence to write, not an exception."""
    files = good_mirror(version_days_ago(1))
    release = latest_release(mirror=MIRROR, opener=opener_for(files))
    del files[release.url + ".sig"]

    path = download(release, tmp_path, opener=opener_for(files))
    assert path.is_file()
    assert not Path(str(path) + ".sig").exists()


# --- verification ----------------------------------------------------------


def test_checksum_alone_is_never_reported_as_verified(tmp_path: Path) -> None:
    """gpg is absent, so one of the two questions is unanswered, and it is named."""
    path = written_image(tmp_path)
    result = verify(path, release_for(path), runner=gpg_runner("", present=False))

    assert result.checksum_ok is True
    assert result.signature_checked is False
    assert result.signature_ok is False
    assert result.trusted is False
    assert "gpg is not installed" in result.detail
    assert "same mirror" in result.detail


def test_a_checksum_mismatch_stops_there(tmp_path: Path) -> None:
    path = written_image(tmp_path, blob=b"something else entirely")
    result = verify(path, release_for(path), runner=gpg_runner("", present=True))

    assert result.checksum_ok is False
    assert result.signature_checked is False
    assert result.trusted is False
    assert "Do not install from this file" in result.detail
    assert DIGEST in result.detail


def test_a_good_signature_from_a_key_hop_knows(tmp_path: Path) -> None:
    path = written_image(tmp_path)
    status = (
        f"[GNUPG:] GOODSIG 9741E8AC Pierre Schmitz\n"
        f"[GNUPG:] VALIDSIG {KNOWN_FINGERPRINT} 2026-07-01 1782000000 0 4 0 22 8 00\n"
    )
    result = verify(path, release_for(path), runner=gpg_runner(status))

    assert result.checksum_ok is True
    assert result.signature_checked is True
    assert result.signature_ok is True
    assert result.trusted is True
    assert KNOWN_FINGERPRINT in result.detail
    assert KNOWN_OWNER in result.detail


def test_a_bad_signature_is_a_refusal(tmp_path: Path) -> None:
    path = written_image(tmp_path)
    status = "[GNUPG:] BADSIG 9741E8AC Pierre Schmitz\n"
    result = verify(path, release_for(path), runner=gpg_runner(status, code=1))

    assert result.checksum_ok is True
    assert result.signature_checked is True
    assert result.signature_ok is False
    assert result.trusted is False
    assert "Do not install from this image" in result.detail
    # The one state a caller must be able to tell apart from the rest. Every
    # other way of not reaching signature_ok is a question hop could not answer;
    # this is the question answered no, and a caller that reads only
    # signature_ok ends up offering to carry on from a forgery.
    assert result.signature_bad is True


@pytest.mark.parametrize(
    "status,code",
    [
        ("[GNUPG:] NO_PUBKEY 76A5EF9054449A5C\n", 2),
        ("[GNUPG:] VALIDSIG 0123456789ABCDEF0123456789ABCDEF01234567 2026-07-01\n", 0),
        ("[GNUPG:] EXPKEYSIG 9741E8AC Pierre\n[GNUPG:] VALIDSIG " + KNOWN_FINGERPRINT + "\n", 0),
        ("", 2),
    ],
    ids=["no key here", "a fingerprint hop does not know", "an expired key", "no answer at all"],
)
def test_only_a_rejected_signature_is_marked_bad(tmp_path: Path, status: str, code: int) -> None:
    """Everything else is a question hop could not finish asking.

    Arch rotating its signing key, a machine with no copy of the key, a gpg that
    answered in a way hop cannot read: none of those is evidence against the
    image, and treating them as evidence would make hop refuse genuine images
    the day the key changes.
    """
    path = written_image(tmp_path)
    result = verify(path, release_for(path), runner=gpg_runner(status, code=code))
    assert result.signature_ok is False
    assert result.signature_bad is False


def test_a_key_that_is_not_in_the_keyring_says_so(tmp_path: Path) -> None:
    path = written_image(tmp_path)
    status = "[GNUPG:] NO_PUBKEY 76A5EF9054449A5C\n"
    result = verify(path, release_for(path), runner=gpg_runner(status, code=2))

    assert result.signature_checked is True
    assert result.signature_ok is False
    assert "76A5EF9054449A5C" in result.detail
    assert "archlinux.org/download" in result.detail


def test_a_good_signature_from_a_stranger_is_not_good_enough(tmp_path: Path) -> None:
    path = written_image(tmp_path)
    unknown = "1111222233334444555566667777888899990000"
    status = f"[GNUPG:] VALIDSIG {unknown} 2026-07-01 1782000000 0 4 0 22 8 00\n"
    result = verify(path, release_for(path), runner=gpg_runner(status))

    assert result.signature_checked is True
    assert result.signature_ok is False
    assert unknown in result.detail
    assert "did not come from Arch" in result.detail


def test_an_expired_key_is_reported_rather_than_waved_through(tmp_path: Path) -> None:
    path = written_image(tmp_path)
    status = (
        "[GNUPG:] EXPKEYSIG 9741E8AC Pierre Schmitz\n"
        f"[GNUPG:] VALIDSIG {KNOWN_FINGERPRINT} 2026-07-01 1782000000 0 4 0 22 8 00\n"
    )
    result = verify(path, release_for(path), runner=gpg_runner(status))

    assert result.signature_ok is False
    assert "expired" in result.detail


def test_no_signature_file_next_to_the_image(tmp_path: Path) -> None:
    path = written_image(tmp_path, sig=None)
    result = verify(path, release_for(path), runner=gpg_runner("", present=True))

    assert result.checksum_ok is True
    assert result.signature_checked is False
    assert path.name + ".sig" in result.detail


def test_gpg_answering_something_unreadable(tmp_path: Path) -> None:
    path = written_image(tmp_path)
    result = verify(path, release_for(path), runner=gpg_runner("", code=2, err="gpg: no dice\n"))

    assert result.signature_checked is True
    assert result.signature_ok is False
    assert "no dice" in result.detail


# --- the label -------------------------------------------------------------


def iso_with_label(path: Path, label: str, *, boot_record_first: bool = True) -> Path:
    """The first few sectors of an ISO 9660 image, enough to carry a label."""
    sectors = [b"\0" * 2048] * 16

    def descriptor(kind: int, body: bytes = b"") -> bytes:
        block = bytearray(b"\0" * 2048)
        block[0] = kind
        block[1:6] = b"CD001"
        block[6] = 1
        block[40 : 40 + len(body)] = body
        return bytes(block)

    if boot_record_first:
        sectors.append(descriptor(0))
    sectors.append(descriptor(1, label.ljust(32).encode("ascii")))
    sectors.append(descriptor(255))
    path.write_bytes(b"".join(sectors))
    return path


def test_the_label_comes_out_of_the_image_itself(tmp_path: Path) -> None:
    """The stick has to carry this string or archiso will not find its filesystem."""
    image = iso_with_label(tmp_path / "archlinux.iso", "ARCH_202607")
    assert volume_label(image) == "ARCH_202607"


def test_the_label_is_found_past_a_boot_record(tmp_path: Path) -> None:
    image = iso_with_label(tmp_path / "boot-first.iso", "ARCH_202512", boot_record_first=True)
    assert volume_label(image) == "ARCH_202512"


def test_something_that_is_not_an_image_says_so(tmp_path: Path) -> None:
    page = tmp_path / "archlinux.iso"
    page.write_bytes(b"<html>mirror is down</html>" + b"\0" * 40000)
    with pytest.raises(IsoError) as caught:
        volume_label(page)
    assert "ISO 9660" in str(caught.value)


# --- unpacking -------------------------------------------------------------


def test_bsdtar_is_preferred(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    run = runner_for({"bsdtar": (0, "", "")}, calls)
    image = tmp_path / "arch.iso"
    image.write_bytes(IMAGE)
    dest = tmp_path / "out"
    dest.mkdir()

    assert _extract_unix(image, dest, run) == dest
    assert calls[-1] == ["bsdtar", "-x", "-f", str(image), "-C", str(dest)]


def test_7z_is_the_fallback(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    run = runner_for({"bsdtar --version": (127, "", "not found"), "7z": (0, "", "")}, calls)
    image = tmp_path / "arch.iso"
    image.write_bytes(IMAGE)
    dest = tmp_path / "out"
    dest.mkdir()

    assert _extract_unix(image, dest, run) == dest
    assert calls[-1] == ["7z", "x", "-y", f"-o{dest}", str(image)]


def test_neither_tool_names_both(tmp_path: Path) -> None:
    run = runner_for({"bsdtar --version": (127, "", ""), "7z --help": (127, "", "")})
    image = tmp_path / "arch.iso"
    image.write_bytes(IMAGE)

    with pytest.raises(IsoError) as caught:
        _extract_unix(image, tmp_path / "out", run)
    assert "libarchive" in str(caught.value)
    assert "p7zip" in str(caught.value)


def test_extract_refuses_a_file_that_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(IsoError) as caught:
        extract(tmp_path / "missing.iso", tmp_path / "out")
    assert "no image at" in str(caught.value)


def test_the_windows_path_mounts_copies_and_dismounts(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    source = tmp_path / "mounted"
    (source / "EFI" / "BOOT").mkdir(parents=True)
    (source / "EFI" / "BOOT" / "BOOTx64.EFI").write_bytes(b"efi")

    def run(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        action = argv[argv.index("-Action") + 1]
        return (0, "E:\\\n", "") if action == "mount" else (0, "", "")

    monkeypatch.setattr("hop.iso._copy_tree", lambda root, dest: _copy_tree(source, dest))

    image = tmp_path / "arch.iso"
    image.write_bytes(IMAGE)
    dest = tmp_path / "stick"
    dest.mkdir()

    assert _extract_windows(image, dest, run) == dest
    assert (dest / "EFI" / "BOOT" / "BOOTx64.EFI").read_bytes() == b"efi"
    assert [argv[argv.index("-Action") + 1] for argv in calls] == ["mount", "dismount"]
    # The image path is an argument, never text pasted into a command line.
    assert str(image) in calls[0]


def test_the_image_is_dismounted_even_when_the_copy_fails(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return (0, "E:\\\n", "")

    def explode(root: Path, dest: Path) -> None:
        raise IsoError("the disc went away")

    monkeypatch.setattr("hop.iso._copy_tree", explode)

    image = tmp_path / "arch.iso"
    image.write_bytes(IMAGE)

    with pytest.raises(IsoError):
        _extract_windows(image, tmp_path / "stick", run)
    assert [argv[argv.index("-Action") + 1] for argv in calls] == ["mount", "dismount"]


def test_a_mount_that_fails_is_explained(tmp_path: Path) -> None:
    run = runner_for({"powershell": (1, "", "Access is denied.\n")})
    image = tmp_path / "arch.iso"
    image.write_bytes(IMAGE)

    with pytest.raises(IsoError) as caught:
        _extract_windows(image, tmp_path / "stick", run)
    assert "Access is denied" in str(caught.value)


def test_a_dismount_that_fails_is_not_swallowed(tmp_path: Path, monkeypatch) -> None:
    """A mounted image left behind is a puzzle the user finds days later."""

    def run(argv: list[str]) -> tuple[int, str, str]:
        action = argv[argv.index("-Action") + 1]
        if action == "mount":
            return (0, "E:\\\n", "")
        return (1, "", "The disk is in use.\n")

    monkeypatch.setattr("hop.iso._copy_tree", lambda root, dest: None)

    image = tmp_path / "arch.iso"
    image.write_bytes(IMAGE)

    with pytest.raises(IsoError) as caught:
        _extract_windows(image, tmp_path / "stick", run)
    assert "still mounted" in str(caught.value)
    assert "Dismount-DiskImage" in str(caught.value)


@pytest.mark.parametrize(
    ("output", "expected"),
    [("E:\\\n", "E:\\"), ("F:", "F:\\"), ("VERBOSE: mounting\nG:\\\n", "G:\\"), ("", None)],
)
def test_the_drive_letter_is_read_off_the_last_line(output: str, expected: str | None) -> None:
    assert _drive_root(output) == expected


def test_the_copy_does_not_carry_the_read_only_bit(tmp_path: Path) -> None:
    """Everything on an ISO is read-only, and the next step has to add files."""
    source = tmp_path / "src"
    source.mkdir()
    original = source / "vmlinuz-linux"
    original.write_bytes(b"kernel")
    original.chmod(0o444)

    dest = tmp_path / "dst"
    _copy_tree(source, dest)

    copied = dest / "vmlinuz-linux"
    assert copied.read_bytes() == b"kernel"
    copied.write_bytes(b"still writable")


# --- redirects: staying on TLS --------------------------------------------


def _redirect(newurl: str):
    """Drive the redirect handler the default opener installs, without a socket."""
    from hop.iso import _HttpsOnlyRedirects

    handler = _HttpsOnlyRedirects()
    request = urllib.request.Request("https://mirror.example/archlinux/iso/latest/")
    return handler.redirect_request(
        request, io.BytesIO(b""), 302, "Found", email.message.Message(), newurl
    )


@pytest.mark.parametrize(
    "newurl",
    [
        "http://mirror.example/archlinux/iso/latest/",
        "http://evil.example/archlinux/",
        "ftp://mirror.example/archlinux/",
        "HTTP://MIRROR.EXAMPLE/archlinux/",
    ],
)
def test_a_redirect_off_https_is_refused(newurl: str) -> None:
    """urllib's own policy allows a 302 from https into http or ftp, and tells the
    caller nothing about it. The checksum is only a check on the transfer, so the
    signature is the real defence — and on Windows, where hop go runs, gpg is
    usually absent and that defence is the question hop has to leave open. Losing
    TLS on the platform where the backstop is missing is not a place to relax."""
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _redirect(newurl)
    assert "not https" in str(excinfo.value)


def test_a_redirect_that_stays_on_https_is_followed() -> None:
    request = _redirect("https://mirror.example/archlinux/iso/latest/")
    assert request is not None
    assert request.full_url.startswith("https://")
