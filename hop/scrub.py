"""Anonymising a hopfile so it can be shown to someone else.

A hopfile is an inventory of your machine, and an inventory of your machine is
personal: it carries your account name, the name you gave the box, your e-mail
address, the SSIDs you connect to, the labels on your drives. `hop scrub`
rewrites those into stable stand-ins, deletes the keys that exist only to
identify hardware, and drops the payload index — the payload is where the real
SSH keys and the real Wi-Fi passwords live, and it has no business in a bug
report.

The stand-ins come from a hash rather than a random number, so the same hostname
becomes the same ``desktop-a4f1`` everywhere in the document, and scrubbing the
same file twice produces byte-identical output. Pass a ``salt`` if you would
rather your hostname were not recoverable by hashing a dictionary of likely
names.

What is deliberately left alone: software names, versions and publishers,
hardware models, disk sizes and free space, locale, timezone, keyboard layouts,
the game library, folder sizes and file counts, and every warning. A scrubbed
hopfile that no longer describes the machine is no use to the person trying to
reproduce your bug, and being useful is the entire reason to publish one.

What this does not protect you against
--------------------------------------

This is not anonymity. The exact set of programs you have installed, the sizes
of your disks and the contents of your game library are, taken together, close
to unique, and anyone who already knows what you own can recognise the file.
Nothing in this module changes that, and nothing could without throwing away the
parts a maintainer needs. What scrubbing buys you is narrower and still worth
having: a stranger reading an issue thread does not learn your name, your
machine's name or your network by accident. Read the result before you post it.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

SCRUB_VERSION = 1

#: Keys whose *name* alone says the value identifies this machine or its
#: network. The value is deleted, not replaced: a stand-in for a MAC address
#: would only invite someone to treat it as one.
_IDENTIFIER_KEYS = frozenset(
    {
        "serial",
        "serial_number",
        "uuid",
        "guid",
        "mac",
        "mac_address",
        "ip",
        "ipv4",
        "ipv6",
        "product_key",
        "hwid",
        "unique_id",
        "uniqueid",
    }
)

#: The same idea for compound names — ``volume_serial``, ``board_uuid``,
#: ``machine_guid``. Only whole trailing words count, so ``device_id`` (a PCI
#: id, which a maintainer needs to debug a driver mapping) survives.
_IDENTIFIER_SUFFIXES = (
    "_serial",
    "_serial_number",
    "_uuid",
    "_guid",
    "_mac",
    "_mac_address",
    "_ip",
    "_ipv4",
    "_ipv6",
    "_product_key",
    "_hwid",
    "_unique_id",
)

#: Profile directories that belong to Windows rather than to a person. Replacing
#: these would lose real information and hide nothing.
_SHARED_PROFILES = frozenset({"public", "default", "default user", "all users", "defaultuser0"})

#: Account names common enough to be ordinary English as well. For these the
#: final sweep matches whole words only — an account called "User" would
#: otherwise mangle ``C:\\Users\\`` in every path in the file and rewrite the
#: word "multi-user" in every warning. Paths stay covered either way, by the
#: profile rule below.
_COMMON_NAMES = frozenset(
    {
        "user",
        "users",
        "admin",
        "administrator",
        "owner",
        "guest",
        "default",
        "public",
        "home",
        "pc",
        "desktop",
        "laptop",
        "windows",
    }
)

_PREFIX = {
    "host": "desktop",
    "user": "user",
    "email": "user",
    "wifi": "wifi",
    "volume": "volume",
    "key": "key",
}

_EMAIL_SOURCE = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"

#: Whatever follows ``\Users\`` is an account name, wherever the string turns
#: up — a field the format never described, a warning quoting a path, a mapped
#: drive belonging to somebody else on the same machine. Spaces are excluded so
#: that a path mentioned mid-sentence does not swallow the rest of the sentence.
_PROFILE_SOURCE = r"(?<=[\\/]Users[\\/])(?P<profile>[^\\/:*?\"<>|\s]+)"

#: Stand-ins are parked as ``\x00<n>\x00`` while the document is rewritten, so
#: that the final substring sweep cannot chew on a replacement it made earlier.
#: NUL cannot occur in JSON text, which is what makes the marker safe.
_TOKEN_SOURCE = r"\x00[0-9]+\x00"
_TOKEN_RE = re.compile(r"\x00([0-9]+)\x00")

_SCRUB_NOTICE = (
    "This hopfile has been anonymised with 'hop scrub'. The account names, hostname, "
    "e-mail addresses, Wi-Fi SSIDs, public keys and drive labels in it are stand-ins, and the "
    "payload index has been dropped along with any reference to the files it pointed at — "
    "'hop land' cannot restore anything from this copy. Everything else — software, hardware, "
    "sizes, locale, warnings — is exactly as it was scanned."
)


@dataclass
class ScrubReport:
    """What the scrubber did, so it can be checked rather than trusted.

    ``replacements`` maps each original value to the stand-in that took its
    place, ``removed`` lists the dotted paths of keys that were dropped outright,
    and ``notes`` holds the things no program can decide for you.
    """

    replacements: dict[str, str] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def scrub(raw: dict, *, salt: str = "") -> tuple[dict, ScrubReport]:
    """Return an anonymised copy of a hopfile, and a report of what changed.

    ``raw`` is the parsed hopfile and is never modified — the copy is deep, so
    the caller can keep using the original afterwards. ``salt`` is mixed into
    every hash: leave it empty for reproducible stand-ins across machines, set
    it to anything private if you would rather nobody could confirm a guess at
    your hostname by hashing it themselves.
    """
    if not isinstance(raw, dict):
        raise TypeError(
            "scrub() expects the parsed contents of a hopfile (a JSON object), but was given "
            f"{type(raw).__name__}. Read the file with json.load() first, then pass the result."
        )
    doc = copy.deepcopy(raw)
    scrubber = _Scrubber(salt)
    scrubber.run(doc)
    return doc, scrubber.report


@dataclass(frozen=True)
class _Needle:
    """A name that must not survive anywhere in the document."""

    text: str
    standin: str
    strict: bool  # match whole words only — see _COMMON_NAMES


class _Scrubber:
    """One run over one document. Not reusable; the caches are per-document."""

    def __init__(self, salt: str) -> None:
        self.salt = salt or ""
        self.report = ScrubReport()
        self.username = ""
        self._standins: dict[tuple[str, str], str] = {}
        self._taken: dict[str, str] = {}
        self._vault: list[str] = []
        self._needles: list[_Needle] = []
        self._by_text: dict[str, str] = {}

    def run(self, doc: dict) -> None:
        # Order matters. Identifiers go first so nothing downstream wastes a
        # stand-in on a value that is about to be deleted; names are learned
        # before the fields holding them are rewritten; the blind sweep runs
        # last, over whatever the named rules did not reach.
        self._drop_identifier_keys(doc, "")
        self._drop_payload(doc)
        self._learn_names(doc)
        self._scrub_known_fields(doc)
        self._sweep(doc)
        self._finish(doc)

    # --- stand-ins ---------------------------------------------------------

    def _standin(self, kind: str, original: Any) -> str:
        """The stable stand-in for one value. Same value in, same value out."""
        text = str(original)
        key = " ".join(text.split()).casefold()
        value = self._standins.get((kind, key))
        if value is None:
            value = self._mint(kind, key)
            self._standins[(kind, key)] = value
        recorded = self.report.replacements.get(text)
        if recorded is None:
            self.report.replacements[text] = value
        elif value not in recorded.split(", "):
            # One string doing two jobs — usually a machine named after the
            # person using it. Show both stand-ins rather than hide one.
            self.report.replacements[text] = f"{recorded}, {value}"
        return value

    def _mint(self, kind: str, key: str) -> str:
        digest = hashlib.blake2b(
            key.encode("utf-8") + self.salt.encode("utf-8"), digest_size=16
        ).hexdigest()
        prefix = _PREFIX.get(kind, kind)
        candidate = ""
        for width in range(4, 33, 2):
            candidate = f"{prefix}-{digest[:width]}"
            if kind == "email":
                candidate += "@example.invalid"
            owner = self._taken.get(candidate)
            if owner is None or owner == key:
                break
            # Two different values hashed to the same short tag. Lengthen the
            # tag rather than reuse it, so the file never claims two machines
            # are one.
        self._taken[candidate] = key
        return candidate

    def _stash(self, value: str) -> str:
        """Park a finished replacement where the sweep cannot touch it."""
        self._vault.append(value)
        return f"\x00{len(self._vault) - 1}\x00"

    def _unstash(self, text: str) -> str:
        if "\x00" not in text:
            return text
        return _TOKEN_RE.sub(lambda m: self._vault[int(m.group(1))], text)

    # --- keys that are identifiers by name ---------------------------------

    def _drop_identifier_keys(self, node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key in list(node.keys()):
                here = f"{path}.{key}" if path else str(key)
                if isinstance(key, str) and _is_identifier_key(key):
                    del node[key]
                    self.report.removed.append(here)
                    continue
                self._drop_identifier_keys(node[key], here)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                self._drop_identifier_keys(item, f"{path}[{index}]")

    def _drop_payload(self, doc: dict) -> None:
        had_payload = False
        for key in ("payload", "payload_dir"):
            if key in doc and doc[key] is not None:
                doc[key] = None
                self.report.removed.append(key)
                had_payload = True
        if had_payload:
            self.report.notes.append(
                "The payload directory itself is still on your disk, untouched — this only "
                "removed the index that described it. Do not attach hop-payload/ to anything: "
                "it contains real keys, real bookmarks and possibly real Wi-Fi passwords."
            )

    # --- the names we know about -------------------------------------------

    def _learn_names(self, doc: dict) -> None:
        user = _obj(doc.get("user"))
        system = _obj(doc.get("system"))
        network = _obj(doc.get("network"))
        dev = _obj(doc.get("dev"))

        self.username = _string(user.get("name"))
        candidates: list[tuple[str, str]] = [
            ("user", self.username),
            ("host", _string(system.get("hostname"))),
            ("host", _string(network.get("hostname"))),
            ("user", _string(user.get("full_name"))),
            ("user", _string(_obj(dev.get("git")).get("user_name"))),
            ("user", _profile_tail(_string(user.get("profile_path")))),
        ]

        seen: set[str] = set()
        strict_names: list[str] = []
        for kind, text in candidates:
            key = text.casefold()
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            strict = len(key) < 5 or key in _COMMON_NAMES
            needle = _Needle(text=text, standin=self._standin(kind, text), strict=strict)
            self._needles.append(needle)
            self._by_text[key] = needle.standin
            if strict:
                strict_names.append(text)

        if not seen:
            self.report.notes.append(
                "This hopfile records neither an account name nor a hostname, so the final "
                "sweep had nothing to search for. That is unusual — check the file yourself."
            )
        for name in strict_names:
            self.report.notes.append(
                f"The name {name!r} is short or common enough to turn up inside ordinary "
                "Windows paths and program names, so it was only replaced where it stands as a "
                "word on its own. Skim the file for it before you post."
            )

    # --- fields the format tells us about ----------------------------------

    def _scrub_known_fields(self, doc: dict) -> None:
        system = _obj(doc.get("system"))
        network = _obj(doc.get("network"))
        user = _obj(doc.get("user"))
        dev = _obj(doc.get("dev"))
        personalization = _obj(doc.get("personalization"))

        self._replace_field(system, "hostname", "host")
        self._replace_field(network, "hostname", "host")

        self._replace_field(user, "name", "user")
        self._replace_field(user, "full_name", "user")
        self._replace_path(user, "profile_path")
        for folder in _members(user.get("folders")):
            self._replace_path(folder, "path")
        self._replace_path(_obj(user.get("onedrive")), "path")

        git = _obj(dev.get("git"))
        self._replace_field(git, "user_name", "user")
        self._replace_field(git, "user_email", "email")
        for entry in _members(dev.get("ssh_keys")):
            value = _string(entry.get("public_key"))
            if value:
                redacted = _redact_public_key(value, self._standin("key", value))
                entry["public_key"] = self._stash(redacted)
        gpg = _obj(dev.get("gpg"))
        key_ids = gpg.get("key_ids")
        # A GPG key id resolves to a name and an e-mail on any keyserver, and no
        # mapping bug was ever diagnosed from one.
        if isinstance(key_ids, list):
            gpg["key_ids"] = [
                self._stash(self._standin("key", k)) if isinstance(k, str) and k else k
                for k in key_ids
            ]
        elif isinstance(key_ids, str) and key_ids:
            gpg["key_ids"] = self._stash(self._standin("key", key_ids))

        for wifi in _members(network.get("wifi_profiles")):
            self._replace_field(wifi, "ssid", "wifi")

        for disk in _members(doc.get("disks")):
            self._replace_field(disk, "label", "volume")
            for partition in _members(disk.get("partitions")):
                self._replace_field(partition, "label", "volume")
                self._replace_field(partition, "volume_label", "volume")

        self._replace_path(personalization, "wallpaper")

        for entry in _members(doc.get("software")):
            self._replace_path(entry, "install_location")

        for browser in _members(doc.get("browsers")):
            for profile in _members(browser.get("profiles")):
                self._replace_path(profile, "path")

        for store in _members(_obj(doc.get("gaming"))):
            libraries = store.get("libraries")
            if isinstance(libraries, list):
                store["libraries"] = [
                    self._path(lib) if isinstance(lib, str) else lib for lib in libraries
                ]
            elif isinstance(libraries, str) and libraries:
                store["libraries"] = self._path(libraries)

    def _replace_field(self, holder: dict, key: str, kind: str) -> None:
        value = _string(holder.get(key))
        if value:
            holder[key] = self._stash(self._standin(kind, value))

    def _replace_path(self, holder: dict, key: str) -> None:
        value = _string(holder.get(key))
        if value:
            holder[key] = self._path(value)

    def _path(self, value: str) -> str:
        """Rewrite the account-specific segments of a path, keep the rest.

        ``C:\\Users\\vasya\\AppData\\Local\\Programs\\Slack`` still says Slack
        afterwards, because the program's own directory name is how the matcher
        guesses its executable, and a scrubbed file that plans differently from
        the original is worse than useless.
        """
        parts = re.split(r"([\\/])", value)
        previous = ""
        for index, part in enumerate(parts):
            if not part or part in ("\\", "/"):
                continue
            low = part.casefold()
            personal = (previous in ("users", "home") and low not in _SHARED_PROFILES) or (
                bool(self.username) and low == self.username.casefold()
            )
            if personal:
                parts[index] = self._stash(self._standin("user", part))
            previous = low
        return "".join(parts)

    # --- the blind sweep ----------------------------------------------------

    def _sweep(self, doc: dict) -> None:
        """Last pass: every remaining string, everywhere, keys included.

        The named rules above cover the format as documented. This covers the
        format as it actually arrives — a scanner field nobody anticipated, a
        warning that quotes a path, a bookmark folder named after its owner.
        """
        values = self._pattern(self._needles)
        # Keys are the skeleton of the document: `user`, `system`, `network`.
        # An account genuinely called "User" must not be allowed to rename them,
        # so only names distinctive enough to be matched without word boundaries
        # are ever swept out of a key.
        keys = self._pattern([n for n in self._needles if not n.strict])
        _walk(
            doc,
            lambda text: self._unstash(values.sub(self._sub, text)),
            lambda text: self._unstash(keys.sub(self._sub, text)),
        )

    def _pattern(self, needles: list[_Needle]) -> re.Pattern[str]:
        alternatives = [_TOKEN_SOURCE, _EMAIL_SOURCE, _PROFILE_SOURCE]
        # Longest first, so a hostname that contains the account name wins.
        for needle in sorted(needles, key=lambda n: -len(n.text)):
            escaped = re.escape(needle.text)
            if needle.strict:
                escaped = rf"(?<![\w-]){escaped}(?![\w-])"
            alternatives.append(escaped)
        return re.compile("|".join(alternatives), re.IGNORECASE)

    def _sub(self, match: re.Match[str]) -> str:
        text = match.group(0)
        if text.startswith("\x00"):
            return text  # a stand-in placed earlier in this run
        if match.group("profile") is not None:
            return text if text.casefold() in _SHARED_PROFILES else self._standin("user", text)
        known = self._by_text.get(text.casefold())
        if known is not None:
            return known
        return self._standin("email", text)

    # --- markers ------------------------------------------------------------

    def _finish(self, doc: dict) -> None:
        doc["scrubbed"] = True
        doc["scrub_version"] = SCRUB_VERSION
        warnings = doc.get("warnings")
        if isinstance(warnings, list):
            existing = warnings
        elif warnings is None:
            existing = []
        else:
            existing = [warnings]
        if existing:
            self.report.notes.append(
                "Warnings are copied through word for word, because they are the part a human "
                "most needs to read. They can name drives, paths or people that hop does not "
                "recognise as yours."
            )
        existing.append(_SCRUB_NOTICE)
        doc["warnings"] = existing


# --- small helpers --------------------------------------------------------


def _is_identifier_key(key: str) -> bool:
    name = key.strip().casefold().replace("-", "_").replace(" ", "_")
    singular = name[:-2] if name.endswith("es") else name.removesuffix("s")
    if name in _IDENTIFIER_KEYS or singular in _IDENTIFIER_KEYS:
        return True
    return any(name.endswith(suffix) for suffix in _IDENTIFIER_SUFFIXES)


def _obj(value: Any) -> dict:
    """A dict to read from either way — writes to the dummy simply go nowhere."""
    return value if isinstance(value, dict) else {}


def _members(value: Any) -> list[dict]:
    """The dict members of a list, of a dict of records, or of a lone record.

    PowerShell's ConvertTo-Json collapses one-element arrays into the element,
    and ``user.folders`` is keyed by folder name rather than being a list, so
    all three shapes turn up in real hopfiles. A dict is read as a mapping of
    records when its values are records, and as a single record otherwise.
    """
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        nested = [item for item in value.values() if isinstance(item, dict)]
        return nested or [value]
    return []


def _string(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else ""


def _profile_tail(path: str) -> str:
    """``C:\\Users\\vasya.CORP`` -> ``vasya.CORP``; the account as the disk spells it."""
    tail = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail if tail and ":" not in tail else ""


def _redact_public_key(value: str, comment_standin: str) -> str:
    """Keep the shape of an SSH public key, lose the key and the comment.

    The type matters — it tells a maintainer whether this machine was on RSA or
    ed25519, which is a real answer to a real question. The base64 body and the
    trailing ``user@host`` comment do not.
    """
    parts = value.split()
    if len(parts) >= 2 and parts[1]:
        return f"{parts[0]} <redacted:{len(parts[1])}> {comment_standin}"
    return f"<redacted public key> {comment_standin}"


def _walk(node: Any, rewrite: Any, rewrite_key: Any) -> Any:
    """Rewrite every string in the tree in place, keys and values separately."""
    if isinstance(node, dict):
        rebuilt = {}
        for key, value in node.items():
            new_key = rewrite_key(key) if isinstance(key, str) else key
            rebuilt[new_key] = _walk(value, rewrite, rewrite_key)
        node.clear()
        node.update(rebuilt)
        return node
    if isinstance(node, list):
        for index, item in enumerate(node):
            node[index] = _walk(item, rewrite, rewrite_key)
        return node
    if isinstance(node, str):
        return rewrite(node)
    return node
