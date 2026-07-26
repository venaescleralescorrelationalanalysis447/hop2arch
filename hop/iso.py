"""Getting a trustworthy Arch install image onto this machine, and its contents
into a directory.

The image is 1.2 GB and it decides what happens to the disk afterwards, so this
module is written around two questions: which file to fetch, and what is
actually known about that file once it has arrived.

**Which file.** A mirror's ``iso/latest/`` directory carries ``sha256sums.txt``,
which names the current image and gives its digest. That is the machine-readable
answer, and it is the only thing hop reads there; the directory listing beside it
is HTML written for a browser, and parsing it would tie hop to a mirror's choice
of web server. A mirror that is unreachable, serving half a checksum file, or
months behind is stepped over for the next one in :data:`DEFAULT_MIRRORS`, and if
none of them work the error says which ones were tried and what each one did.

**What is known.** :func:`verify` reports two separate facts and never merges
them. A sha256 fetched over HTTPS from the same mirror that served the image
proves the transfer was not corrupted, and nothing beyond that: anyone able to
alter the image on that mirror could alter the checksum next to it. Only the
detached GPG signature ties the bytes to a key Arch publishes. So
:class:`VerifyResult` keeps ``checksum_ok`` and ``signature_ok`` apart and adds a
``detail`` sentence written to be shown to the user word for word. Where gpg is
missing, hop names the question that went unanswered instead of calling the image
verified. Nothing in here will ever report a verified image on a checksum alone.

**Extraction is a file copy.** :func:`extract` unpacks the image into an ordinary
directory, because the stick hop builds is a FAT32 filesystem holding the ISO's
files rather than a raw image written to a device. That decision, and the limits
that come with it — no legacy BIOS boot, no file over 4 GB — belong to the USB
step; what belongs here is :func:`volume_label`, since the filesystem on the
stick has to carry the ISO's own label or archiso will not find itself at boot.

Every function that reaches the network takes an ``opener`` and every function
that runs a program takes a ``runner``. The tests need both, because none of them
may download 1.2 GB or mount anything; so does a machine behind a SOCKS proxy,
which urllib cannot use on its own.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import __version__
from .manifest import human_bytes

__all__ = [
    "ARCH_SIGNING_KEYS",
    "DEFAULT_MIRRORS",
    "IsoError",
    "IsoRelease",
    "Opener",
    "Runner",
    "VerifyResult",
    "download",
    "extract",
    "latest_release",
    "verify",
    "volume_label",
]

#: Opens a URL and returns something you can ``.read()`` bytes from, in the shape
#: :func:`urllib.request.urlopen` returns. Failure is an :class:`OSError` —
#: urllib's ``HTTPError`` and ``URLError`` are both already that, so a fake and
#: the real thing fail the same way.
Opener = Callable[[str], IO[bytes]]

#: Runs an argv list and returns ``(returncode, stdout, stderr)``. A program that
#: is not installed comes back as a non-zero return code, not an exception, so
#: "gpg is missing" is an ordinary answer rather than a crash.
Runner = Callable[[list[str]], tuple[int, str, str]]

#: Mirrors tried in order, all HTTPS. The first is Arch's own geo-routed mirror;
#: the rest are large, long-lived and on different continents, so a bad day for
#: one is rarely a bad day for the next.
DEFAULT_MIRRORS: tuple[str, ...] = (
    "https://geo.mirror.pkgbuild.com/",
    "https://mirrors.kernel.org/archlinux/",
    "https://mirror.rackspace.com/archlinux/",
    "https://ftp.halifax.rwth-aachen.de/archlinux/",
)

#: Seconds a socket may sit idle before the default opener gives up. This is per
#: read, not per download: a 1.2 GB file over a slow line is fine, a mirror that
#: has stopped talking is not.
DEFAULT_TIMEOUT = 30.0

#: Read size for the download. Large enough that the progress callback is not
#: called thousands of times a second, small enough to show movement.
CHUNK = 1 << 20

#: How far behind a mirror may be before hop treats it as stale rather than
#: quiet. Arch publishes an image every month; four months without one means the
#: mirror stopped syncing, and installing from an image that old is its own kind
#: of trouble — the archlinux-keyring inside it can be too far out of date for
#: pacman to trust the packages it is about to download.
STALE_AFTER_DAYS = 120

#: Fingerprints hop recognises as Arch Linux release signing keys, and who holds
#: them. gpg reports the fingerprint it validated against; this table only turns
#: that fingerprint into a name and decides whether hop is willing to use the
#: word "verified".
#:
#: Check these against https://archlinux.org/download/, which prints the
#: fingerprint of the key the current image is signed with. If Arch rotates its
#: signing key this table goes stale and hop refuses a genuine image while saying
#: exactly which fingerprint it saw — that is the direction this mistake is
#: allowed to point, and correcting it is a two-line change. It must never point
#: the other way, which is why an unknown fingerprint is a refusal and not a
#: shrug.
ARCH_SIGNING_KEYS: dict[str, str] = {
    "3E80CA1A8B89F69CBA57D98A76A5EF9054449A5C": "Pierre Schmitz <pierre@archlinux.de>",
    "4AA4767BBC9C4B1D18AE28B77F2D434B9741E8AC": "Pierre Schmitz <pierre@archlinux.de>",
    "0E8B644079F599DFC1DDC3973348882F6AC6A4C2": "Christian Hesse <eworm@archlinux.org>",
}

_USER_AGENT = f"hop2arch/{__version__} (+https://github.com/Ramirmir/hop2arch)"

#: ``<64 hex digits><space><space or *><filename>``, the format sha256sum writes.
_SUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")

#: The dated image. ``archlinux-x86_64.iso`` sits in the same directory and has
#: the same contents, but it carries no version, and the version is half of what
#: hop needs to know.
_ISO_NAME = re.compile(r"^archlinux-(\d{4}\.\d{2}\.\d{2})-x86_64\.iso$")

#: A checksum file is under 4 kB. Anything larger arriving from that URL is a
#: mirror answering the wrong question, and it is not going into memory.
_TEXT_LIMIT = 1 << 20

#: A detached signature is a few hundred bytes.
_SIGNATURE_LIMIT = 1 << 16

#: ISO 9660 puts its volume descriptors in 2048-byte sectors starting at sector
#: 16. The primary one is normally first, but a bootable image can put a boot
#: record ahead of it, so hop walks the set rather than assuming.
_SECTOR = 2048
_DESCRIPTOR_START = 16 * _SECTOR
_DESCRIPTOR_LIMIT = 16


class IsoError(Exception):
    """The image could not be found, fetched, checked or unpacked."""


class _StaleMirror(IsoError):
    """This mirror answered, but with an image old enough to be a problem.

    Separate from the rest only so that the caller can tell "nobody answered"
    from "everybody answered with something ancient" — the second one usually
    means the clock on this machine is wrong, not that Arch stopped shipping.
    """


@dataclass(frozen=True)
class IsoRelease:
    """One published Arch image: where it is, and what it should hash to.

    ``size_bytes`` is what the mirror said before the download started, and it is
    0 when the mirror declined to say. :func:`download` uses it to notice a
    connection that died halfway; the checksum is what actually decides whether
    the file is good.
    """

    version: str
    filename: str
    url: str
    sha256: str
    size_bytes: int
    signature_url: str | None


@dataclass
class VerifyResult:
    """What was established about an image on disk, and what was not.

    The two questions are kept apart on purpose. ``checksum_ok`` says the bytes
    survived the wire; ``signature_ok`` says Arch signed them. ``detail`` is a
    finished sentence meant to be shown to the user as it stands.

    ``signature_bad`` is the third state, and it is not the same as
    ``signature_ok`` being False. "hop could not answer the question" — no gpg,
    no ``.sig``, no copy of the key — and "hop asked the question and the answer
    was no" both leave ``signature_ok`` False, and a caller that cannot tell
    them apart ends up offering to carry on from a rejected signature the way it
    offers to carry on from a missing one. Only ``BADSIG`` sets this: gpg had the
    key, checked the bytes, and said the bytes are not what the signature covers.
    """

    checksum_ok: bool
    signature_checked: bool
    signature_ok: bool
    detail: str
    signature_bad: bool = False

    @property
    def trusted(self) -> bool:
        """True only when both questions were answered yes.

        The rule lives here so that no caller can arrive at "verified" by reading
        ``checksum_ok`` alone, which is the mistake this module exists to stop.
        """
        return self.checksum_ok and self.signature_ok


# --- finding the release ---------------------------------------------------


def latest_release(
    *,
    mirror: str | None = None,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> IsoRelease:
    """Find the current image by reading ``iso/latest/sha256sums.txt``.

    Tries ``mirror`` alone if you name one, otherwise every entry of
    :data:`DEFAULT_MIRRORS` in order, and raises :class:`IsoError` naming each
    mirror and what it did if none of them produce an answer. ``timeout`` only
    reaches the default opener; an opener you supply brings its own.
    """
    mirrors = [mirror] if mirror else list(DEFAULT_MIRRORS)
    directories = [_iso_dir(entry) for entry in mirrors]
    fetch = opener or _default_opener(timeout)

    failures: list[str] = []
    stale = 0
    for directory in directories:
        try:
            return _release_from(directory, fetch)
        except _StaleMirror as exc:
            stale += 1
            failures.append(f"  {directory}\n    {exc}")
        except IsoError as exc:
            failures.append(f"  {directory}\n    {exc}")

    tried = "\n".join(failures)
    if stale == len(failures):
        # Every mirror in the list going quiet at once is unlikely; a clock that
        # thinks it is next year makes every image look ancient, and that is the
        # first thing to check before blaming the internet.
        raise IsoError(
            "Every mirror hop tried is offering an image months old:\n\n"
            f"{tried}\n\n"
            "Mirrors do fall behind, but not all of them at once. Check this machine's "
            "date first — a clock set well ahead makes a current image look abandoned. "
            "If the date is right, pass a mirror you trust with mirror=, or download the "
            "image yourself."
        )
    raise IsoError(
        "Could not work out which Arch image to download. Every mirror hop tried either "
        f"could not be reached or did not answer with a usable checksum file:\n\n{tried}\n\n"
        "If the machine is behind a proxy that urllib cannot use, pass an opener of your "
        "own. If you already have an ISO, hop does not need to fetch one: point the USB "
        "step at the file you have."
    )


def _release_from(directory: str, fetch: Opener) -> IsoRelease:
    """Read one mirror's checksum file and turn it into a release, or explain why not."""
    text = _fetch_text(fetch, directory + "sha256sums.txt", _TEXT_LIMIT)
    matches = (_SUM_LINE.match(line) for line in text.splitlines())
    entries = {match.group(2): match.group(1).lower() for match in matches if match}
    if not entries:
        # Either the transfer was cut short, or the mirror answered a missing file
        # with a friendly HTML page and a 200. Both look the same from here, and
        # both mean the next mirror.
        raise IsoError(
            f"sha256sums.txt is there but has no checksum lines in it ({len(text)} bytes read)"
        )

    dated = sorted(
        (match.group(1), match.group(0))
        for match in (_ISO_NAME.match(name) for name in entries)
        if match
    )
    if not dated:
        raise IsoError(
            "sha256sums.txt lists no dated x86_64 image "
            f"(it has: {', '.join(sorted(entries)[:4]) or 'nothing'})"
        )

    version, filename = dated[-1]
    age = _age_days(version)
    if age is not None and age > STALE_AFTER_DAYS:
        raise _StaleMirror(
            f"the newest image here is {filename}, published {age} days ago; this mirror "
            "has stopped syncing"
        )

    url = directory + filename
    return IsoRelease(
        version=version,
        filename=filename,
        url=url,
        sha256=entries[filename],
        size_bytes=_content_length(fetch, url),
        signature_url=url + ".sig",
    )


def _iso_dir(mirror: str) -> str:
    """``https://host/archlinux`` -> ``https://host/archlinux/iso/latest/``."""
    base = mirror.strip()
    _require_https(base)
    base = base.rstrip("/")
    if not base.endswith("/iso/latest"):
        base += "/iso/latest"
    return base + "/"


def _require_https(url: str) -> None:
    """Refuse plain HTTP, and say why it is not pedantry.

    hop's only cheap check on the image is a checksum served from the same place
    as the image. Over plain HTTP anyone on the path can rewrite both, and the
    check becomes decoration. If you must use a mirror that has no TLS, download
    the image by hand and hand the file to :func:`verify` with a release you
    filled in yourself — the signature is what makes that safe, not the transport.
    """
    if url.lower().startswith("https://"):
        return
    raise IsoError(
        f"{url!r} is not an https:// URL, so hop will not fetch from it. The checksum and "
        "the image would arrive over the same unprotected connection, which makes checking "
        "one against the other worth nothing."
    )


def _age_days(version: str) -> int | None:
    """How many days ago ``2026.07.01`` was, or None if it does not parse.

    A date in the future is reported as 0 rather than as a negative number: it
    means this machine's clock is wrong, and a wrong clock is not a reason to
    reject a mirror that is otherwise answering.
    """
    try:
        published = _dt.datetime.strptime(version, "%Y.%m.%d").date()
    except ValueError:
        return None
    return max(0, (_dt.datetime.now(_dt.UTC).date() - published).days)


# --- downloading -----------------------------------------------------------


def download(
    release: IsoRelease,
    dest_dir: str | Path,
    *,
    opener: Opener | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Fetch the image into ``dest_dir`` and return the path it was written to.

    The detached signature is fetched alongside it, as ``<image>.sig``, so that
    :func:`verify` can do its work without going back to the network. A signature
    that cannot be fetched is not an error here — it is something for
    :func:`verify` to report, in the place where the user is being told what is
    known about the file.

    ``progress`` is called with ``(bytes_written, expected_total)`` after each
    chunk, where the total is 0 if the mirror never said how big the file was.

    The download goes to ``<name>.part`` and is renamed once it is complete, so
    an interrupted run never leaves something that looks like a finished image.
    A ``.part`` that does not survive is deleted: hop cannot resume, so those
    bytes have no future, and a stray 900 MB file is a puzzle for the user later.
    """
    _require_https(release.url)
    fetch = opener or _default_opener(DEFAULT_TIMEOUT)
    directory = Path(dest_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IsoError(f"could not create {directory}: {exc}") from exc

    target = directory / release.filename
    if release.size_bytes and target.is_file() and target.stat().st_size == release.size_bytes:
        # Already here, and the right length. Re-fetching 1.2 GB because a later
        # step failed is a cruel way to spend an evening, and this is not a claim
        # that the file is good — verify() still has to run, and it is the thing
        # that decides.
        _fetch_signature(fetch, release, target)
        if progress is not None:
            progress(release.size_bytes, release.size_bytes)
        return target

    partial = directory / (release.filename + ".part")
    total = release.size_bytes
    written = 0

    response = _open(fetch, release.url)
    try:
        with partial.open("wb") as handle:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if progress is not None:
                    progress(written, total)
    except OSError as exc:
        _discard(partial)
        raise IsoError(
            f"the download of {release.filename} stopped after {human_bytes(written)}: "
            f"{_why(exc)}. Nothing usable was kept; run it again."
        ) from exc
    finally:
        _close(response)

    if total and written != total:
        _discard(partial)
        raise IsoError(
            f"{release.filename} arrived as {human_bytes(written)} where the mirror said "
            f"{human_bytes(total)}. That is a truncated download, not an image; the partial "
            "file has been deleted. Try again, or fetch the release from another mirror."
        )

    try:
        partial.replace(target)
    except OSError as exc:
        raise IsoError(f"could not put {partial.name} in place as {target.name}: {exc}") from exc

    _fetch_signature(fetch, release, target)
    return target


def _fetch_signature(fetch: Opener, release: IsoRelease, image: Path) -> Path | None:
    """Put ``<image>.sig`` next to the image, if the mirror has one.

    Best effort by design. A mirror missing the signature is a fact the user
    needs, but it belongs in the sentence :func:`verify` writes, not in an
    exception thrown at the end of a long download.
    """
    if not release.signature_url:
        return None
    destination = Path(str(image) + ".sig")
    try:
        blob = _fetch_bytes(fetch, release.signature_url, _SIGNATURE_LIMIT)
        destination.write_bytes(blob)
    except (OSError, IsoError):
        return None
    return destination


# --- verification ----------------------------------------------------------


def verify(path: str | Path, release: IsoRelease, *, runner: Runner | None = None) -> VerifyResult:
    """Check the image on disk, and say plainly what that check established.

    The checksum is always computed. The signature is checked when gpg is on PATH
    and ``<image>.sig`` is next to the image; when it is not, the result says so
    and ``signature_ok`` stays False. A checksum match on its own is never
    reported as a verified image: it proves the file was not damaged in transit,
    and it cannot prove more than that, because it came from the same mirror as
    the image it describes.
    """
    image = Path(path)
    run = runner or _default_runner

    expected = (release.sha256 or "").strip().lower()
    if not expected:
        return VerifyResult(
            checksum_ok=False,
            signature_checked=False,
            signature_ok=False,
            detail=(
                f"The release description for {release.filename} carries no sha256, so there "
                "was nothing to check the file against. Fetch the release from a mirror with "
                "latest_release() rather than filling one in by hand."
            ),
        )

    try:
        digest = _sha256(image)
    except OSError as exc:
        raise IsoError(f"could not read {image} to check it: {exc}") from exc

    if digest != expected:
        return VerifyResult(
            checksum_ok=False,
            signature_checked=False,
            signature_ok=False,
            detail=(
                f"The sha256 of {image.name} does not match the one published beside it. "
                f"Expected {expected}, got {digest}. Do not install from this file. Usually "
                "the download was cut short or the mirror served an image that no longer "
                "matches its own checksum file; delete it and fetch it again. The signature "
                "was not checked — there is nothing to learn about who signed bytes that are "
                "not the bytes we asked for."
            ),
        )

    matched = f"The sha256 of {image.name} matches the checksum published beside it, so the "
    matched += "download is intact."

    signature = Path(str(image) + ".sig")
    if not signature.is_file():
        return VerifyResult(
            checksum_ok=True,
            signature_checked=False,
            signature_ok=False,
            detail=(
                f"{matched} That is all it establishes: the checksum came from the same "
                "mirror as the image, so anyone able to change one could change the other. "
                f"The detached signature, which is the part that would prove Arch published "
                f"this image, is not here — hop looked for {signature.name} next to it. You "
                "can fetch it from the mirror by hand and check it with 'gpg --verify'."
            ),
        )

    if not _have(run, ["gpg", "--version"]):
        return VerifyResult(
            checksum_ok=True,
            signature_checked=False,
            signature_ok=False,
            detail=(
                f"{matched} That is all it establishes: the checksum came from the same "
                "mirror as the image, so anyone able to change one could change the other. "
                "Only the GPG signature proves the image is the one Arch published, and gpg "
                "is not installed here, so that question is unanswered. To answer it, install "
                f"gnupg and run: gpg --verify {signature.name} {image.name}"
            ),
        )

    return _verify_signature(run, image, signature, matched)


def _verify_signature(run: Runner, image: Path, signature: Path, matched: str) -> VerifyResult:
    """Ask gpg about the detached signature and translate its answer.

    Read through ``--status-fd``, not through the human-readable output: the
    status lines are stable across gpg versions and are the same in every
    language, and the prose is neither.

    Nothing here fetches a key. Retrieving whatever key a signature names and
    then trusting it is close to circular — it shows the file was signed by
    whoever signed it. The fingerprint has to be one hop already recognises.
    """
    argv = ["gpg", "--batch", "--status-fd", "1", "--verify", str(signature), str(image)]
    try:
        code, out, err = run(argv)
    except OSError as exc:
        raise IsoError(f"could not run gpg: {exc}") from exc

    status = _status_lines(out)

    if "BADSIG" in status:
        return VerifyResult(
            checksum_ok=True,
            signature_checked=True,
            signature_ok=False,
            signature_bad=True,
            detail=(
                f"{matched} But gpg rejected the signature on it: the file is not what the "
                "signature says it is. Do not install from this image, and do not assume the "
                "matching checksum makes it safe — a mirror that can serve a modified image "
                "can serve a matching checksum next to it. Delete the file and fetch it from "
                "a different mirror."
            ),
        )

    if "NO_PUBKEY" in status:
        key = status["NO_PUBKEY"].split()[0] if status["NO_PUBKEY"] else "the signing key"
        return VerifyResult(
            checksum_ok=True,
            signature_checked=True,
            signature_ok=False,
            detail=(
                f"{matched} The signature could not be checked because this machine's gpg has "
                f"no copy of key {key}, which made it. Import Arch's release signing key and "
                f"run 'gpg --verify {signature.name} {image.name}' again. Compare the "
                "fingerprint you import against the one printed on "
                "https://archlinux.org/download/ — a key fetched because the signature asked "
                "for it proves very little on its own."
            ),
        )

    valid = status.get("VALIDSIG", "").split()
    fingerprint = valid[0].upper() if valid else ""
    owner = ARCH_SIGNING_KEYS.get(fingerprint)

    if "EXPKEYSIG" in status or "REVKEYSIG" in status:
        what = "expired" if "EXPKEYSIG" in status else "been revoked"
        return VerifyResult(
            checksum_ok=True,
            signature_checked=True,
            signature_ok=False,
            detail=(
                f"{matched} gpg says the signature itself is good, but the key that made it "
                f"has {what}, so hop will not call the image verified. Either the image is "
                "old, or the copy of the key on this machine is. Update the key from "
                "https://archlinux.org/download/ and check again before you install."
            ),
        )

    if code != 0 or not fingerprint:
        reason = _first_line(err) or _first_line(out) or f"gpg exited {code}"
        return VerifyResult(
            checksum_ok=True,
            signature_checked=True,
            signature_ok=False,
            detail=(
                f"{matched} The signature check did not come back with an answer hop can read: "
                f"{reason}. Run 'gpg --verify {signature.name} {image.name}' yourself and read "
                "what it says before installing from this image."
            ),
        )

    if owner is None:
        return VerifyResult(
            checksum_ok=True,
            signature_checked=True,
            signature_ok=False,
            detail=(
                f"{matched} gpg found a good signature made with key {fingerprint}, but that "
                "is not a fingerprint hop knows as an Arch Linux release signing key. Either "
                "Arch has changed the key it signs with and hop's list is out of date, or this "
                "image did not come from Arch. Compare that fingerprint with the one on "
                "https://archlinux.org/download/ before you install anything from it."
            ),
        )

    return VerifyResult(
        checksum_ok=True,
        signature_checked=True,
        signature_ok=True,
        detail=(
            f"{matched} gpg verified the detached signature against key {fingerprint} "
            f"({owner}), which is an Arch Linux release signing key. The image on this disk is "
            "the one Arch published."
        ),
    )


def _status_lines(out: str) -> dict[str, str]:
    """gpg's ``[GNUPG:] KEYWORD rest`` lines as ``{KEYWORD: rest}``.

    Later lines win, which matters for nothing here: gpg emits each of the
    keywords this module reads at most once per signature.
    """
    found: dict[str, str] = {}
    for line in out.splitlines():
        if not line.startswith("[GNUPG:] "):
            continue
        keyword, _, rest = line[len("[GNUPG:] ") :].partition(" ")
        if keyword:
            found[keyword] = rest.strip()
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# --- unpacking -------------------------------------------------------------


def extract(iso_path: str | Path, dest: str | Path, *, runner: Runner | None = None) -> Path:
    """Copy the image's contents into ``dest`` as ordinary files, and return ``dest``.

    Not a raw write to a device: what comes out of here is a directory that the
    USB step copies onto a FAT32 stick, which is why nothing in hop ever needs
    ``\\\\.\\PhysicalDrive`` or a device number that could be off by one.

    On Windows the image is mounted with ``Mount-DiskImage``, which has been part
    of Windows since 8, and dismounted again whatever happens — a mounted image
    left behind is something the user finds days later and cannot explain. On
    Linux ``bsdtar`` does the work, with ``7z`` as a fallback.

    The caller is the one that has to check what came out against FAT32's limits;
    a single file over 4 GB cannot go on the stick, and that check belongs where
    the stick is being written.
    """
    image = Path(iso_path)
    destination = Path(dest)
    if not image.is_file():
        raise IsoError(f"there is no image at {image}")
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IsoError(f"could not create {destination}: {exc}") from exc

    run = runner or _default_runner
    if os.name == "nt":
        return _extract_windows(image, destination, run)
    return _extract_unix(image, destination, run)


#: Mounts or dismounts an image. Written to a temporary file and run with -File
#: so that the paths arrive as arguments PowerShell binds to parameters, rather
#: than as text pasted into a command line — the same rule the rest of hop
#: follows with argv lists.
_MOUNT_PS1 = r"""param(
    [Parameter(Mandatory = $true)][ValidateSet('mount', 'dismount')][string] $Action,
    [Parameter(Mandatory = $true)][string] $ImagePath
)

$ErrorActionPreference = 'Stop'

try {
    if ($Action -eq 'dismount') {
        Dismount-DiskImage -ImagePath $ImagePath | Out-Null
        exit 0
    }

    $image = Mount-DiskImage -ImagePath $ImagePath -StorageType ISO -PassThru

    # The volume is not always there the instant the mount call returns, so give
    # Windows a few seconds to name it before deciding it failed.
    $letter = $null
    for ($i = 0; $i -lt 20; $i++) {
        $volume = $image | Get-DiskImage | Get-Volume
        if ($volume -and $volume.DriveLetter) { $letter = $volume.DriveLetter; break }
        Start-Sleep -Milliseconds 250
    }

    if (-not $letter) {
        [Console]::Error.WriteLine('The image mounted but Windows gave it no drive letter.')
        exit 1
    }

    Write-Output ($letter + ':\')
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""


def _extract_windows(image: Path, destination: Path, run: Runner) -> Path:
    script_dir = Path(tempfile.mkdtemp(prefix="hop-iso-"))
    script = script_dir / "hop-mount.ps1"
    script.write_text(_MOUNT_PS1, encoding="utf-8", newline="\r\n")

    def powershell(action: str) -> tuple[int, str, str]:
        return run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                # The script was written by hop, two lines ago, into a directory
                # hop made. Bypass is about this file, not about the machine's
                # policy: nothing is changed on disk and nothing else runs.
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Action",
                action,
                "-ImagePath",
                str(image),
            ]
        )

    try:
        code, out, err = powershell("mount")
        if code != 0:
            raise IsoError(
                f"Windows would not mount {image.name}: {_first_line(err) or f'exit {code}'}. "
                "The image may be damaged, or the account may not be allowed to mount images. "
                "You can mount it in Explorer yourself and copy its contents across by hand."
            )

        root = _drive_root(out)
        if root is None:
            raise IsoError(
                f"{image.name} was mounted but hop could not tell which drive letter it "
                f"landed on. PowerShell said: {_first_line(out) or '(nothing)'}"
            )

        dismount: tuple[int, str, str] = (0, "", "")
        try:
            _copy_tree(Path(root), destination)
        finally:
            # Always, including on the way out of a failure: an image that stays
            # mounted keeps a drive letter and a file lock, and the person who
            # finds it next week will have no idea what put it there.
            dismount = powershell("dismount")

        if dismount[0] != 0:
            raise IsoError(
                f"The image was unpacked into {destination}, but it is still mounted: "
                f"{_first_line(dismount[2]) or f'exit {dismount[0]}'}. Eject it from Explorer, "
                f"or run: Dismount-DiskImage -ImagePath '{image}'"
            )
    finally:
        shutil.rmtree(script_dir, ignore_errors=True)

    return destination


def _extract_unix(image: Path, destination: Path, run: Runner) -> Path:
    if _have(run, ["bsdtar", "--version"]):
        code, _, err = run(["bsdtar", "-x", "-f", str(image), "-C", str(destination)])
        if code != 0:
            raise IsoError(
                f"bsdtar could not unpack {image.name}: {_first_line(err) or f'exit {code}'}"
            )
        return destination

    if _have(run, ["7z", "--help"]):
        code, _, err = run(["7z", "x", "-y", f"-o{destination}", str(image)])
        if code != 0:
            raise IsoError(
                f"7z could not unpack {image.name}: {_first_line(err) or f'exit {code}'}"
            )
        return destination

    raise IsoError(
        f"Neither bsdtar nor 7z is installed, so hop cannot unpack {image.name}. bsdtar comes "
        "with libarchive ('sudo pacman -S libarchive' on Arch, 'apt install libarchive-tools' "
        "on Debian and Ubuntu); 7z comes with p7zip. bsdtar is already present on the Arch ISO."
    )


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy the mounted image across, dropping its permissions on the way.

    ``copyfile`` rather than ``copy2``: everything on an ISO is read-only, and
    carrying that bit onto the copy would leave a tree the next step cannot write
    into — and the next step's whole job is to add files to it.
    """
    try:
        shutil.copytree(source, destination, dirs_exist_ok=True, copy_function=shutil.copyfile)
    except shutil.Error as exc:
        first = exc.args[0][0] if exc.args and exc.args[0] else exc
        raise IsoError(f"could not copy the image contents into {destination}: {first}") from exc
    except OSError as exc:
        raise IsoError(f"could not copy the image contents into {destination}: {exc}") from exc


def _drive_root(out: str) -> str | None:
    """Pull ``E:\\`` out of the mount script's output."""
    for line in reversed(out.splitlines()):
        candidate = line.strip()
        if re.fullmatch(r"[A-Za-z]:\\?", candidate):
            return candidate if candidate.endswith("\\") else candidate + "\\"
    return None


# --- the label -------------------------------------------------------------


def volume_label(iso_path: str | Path, *, runner: Runner | None = None) -> str:
    """The ISO 9660 volume label, e.g. ``ARCH_202607``.

    The stick has to be formatted with this exact label. archiso finds its own
    filesystem at boot by searching for it — the kernel command line inside the
    image says ``archisolabel=ARCH_202607`` — so a stick labelled anything else
    boots as far as a rescue shell and stops there.

    Read straight out of the image's volume descriptors, which is why ``runner``
    is never used: the answer is in the file, and a value this load-bearing
    should not depend on which tools happen to be installed. The argument is kept
    so every function in this module has the same shape.
    """
    image = Path(iso_path)
    try:
        with image.open("rb") as handle:
            handle.seek(_DESCRIPTOR_START)
            descriptors = handle.read(_SECTOR * _DESCRIPTOR_LIMIT)
    except OSError as exc:
        raise IsoError(f"could not read {image}: {exc}") from exc

    for offset in range(0, len(descriptors), _SECTOR):
        block = descriptors[offset : offset + _SECTOR]
        if len(block) < 190 or block[1:6] != b"CD001":
            break
        kind = block[0]
        if kind == 255:  # the terminator: there are no more descriptors
            break
        if kind != 1:  # a boot record or a supplementary descriptor; keep looking
            continue
        label = block[40:72].decode("ascii", "replace").strip().strip("\x00").strip()
        if label:
            return label
        raise IsoError(f"{image.name} has an empty volume label, so hop cannot label the stick")

    raise IsoError(
        f"{image.name} does not look like an ISO 9660 image: hop found no primary volume "
        "descriptor where one belongs. If the file was downloaded, it may be an HTML error "
        "page saved under the name of an image."
    )


# --- the defaults the tests replace ----------------------------------------


def _default_opener(timeout: float) -> Opener:
    """urllib, with a timeout and a user agent that admits what it is.

    urllib reads ``https_proxy`` from the environment but speaks no SOCKS, which
    is the main reason every entry point here takes an opener instead of calling
    urlopen directly.
    """

    def opener(url: str) -> IO[bytes]:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        return urllib.request.urlopen(request, timeout=timeout)

    return opener


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run one command. A list, never a shell, never ``check=True``.

    A program that is not installed comes back as 127 rather than an exception,
    because "gpg is missing" is one of the answers this module expects and
    handles, not a failure. Output is decoded with replacement: a Windows console
    in a Russian locale will hand back bytes that are not UTF-8, and losing a
    character from a message is better than a traceback in the middle of a check.
    """
    try:
        done = subprocess.run(argv, check=False, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        return (127, "", str(exc))
    return (done.returncode, done.stdout or "", done.stderr or "")


def _have(run: Runner, argv: list[str]) -> bool:
    """Is this program here and willing to answer?"""
    try:
        code, _, _ = run(argv)
    except OSError:
        return False
    return code == 0


# --- small helpers ---------------------------------------------------------


def _open(fetch: Opener, url: str) -> IO[bytes]:
    try:
        return fetch(url)
    except OSError as exc:
        raise IsoError(f"{_why(exc)}") from exc


def _fetch_bytes(fetch: Opener, url: str, limit: int) -> bytes:
    """Read a small file, refusing to read a large one.

    The limit is not tidiness: these URLs are supposed to answer with a few
    kilobytes, and a mirror that answers one of them with a gigabyte is a mirror
    hop has misunderstood.
    """
    response = _open(fetch, url)
    try:
        blob = response.read(limit + 1)
    except OSError as exc:
        raise IsoError(f"{_why(exc)}") from exc
    finally:
        _close(response)
    if len(blob) > limit:
        raise IsoError(f"the answer to {url} is larger than {human_bytes(limit)}, so it was left")
    return blob


def _fetch_text(fetch: Opener, url: str, limit: int) -> str:
    return _fetch_bytes(fetch, url, limit).decode("utf-8", "replace")


def _content_length(fetch: Opener, url: str) -> int:
    """What the mirror says the image weighs, or 0 if it will not say.

    The connection is opened and closed without reading the body, so this costs
    the headers and nothing else. An unknown size is not an error: it only means
    :func:`download` cannot notice a short file, and the checksum still can.
    """
    try:
        response = _open(fetch, url)
    except IsoError:
        return 0
    try:
        headers = getattr(response, "headers", None)
        raw = headers.get("Content-Length") if hasattr(headers, "get") else None
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0
    finally:
        _close(response)


def _close(response: IO[bytes]) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        # Closing a socket that has already gone is not news.
        with contextlib.suppress(OSError):
            closer()


def _discard(path: Path) -> None:
    """Throw away a partial download. Failing to delete it is not worth an error."""
    with contextlib.suppress(OSError):
        path.unlink()


def _why(exc: OSError) -> str:
    """An HTTP failure as a person would say it."""
    code = getattr(exc, "code", None)
    if code is not None:
        return f"HTTP {code} {getattr(exc, 'reason', '')}".strip()
    reason = getattr(exc, "reason", None)
    return str(reason or exc) or exc.__class__.__name__


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""
