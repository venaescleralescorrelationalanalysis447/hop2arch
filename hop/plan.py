"""Turning a hopfile into a plan.

A plan is the answer to "what do I actually type on the new machine". It is a
plain JSON document so you can read it, edit it, delete the three lines you
disagree with, and hand it to ``hop land``.

The planner does three things:

1. resolves every installed Windows program through the mapping database;
2. adds the packages the *machine* needs regardless of what was installed —
   GPU drivers, a desktop, audio, fonts for the user's locale, laptop bits;
3. scores the result, so the user gets an honest number before they wipe
   anything.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any

from .manifest import Manifest
from .mapping import STRATEGY_WEIGHT, AppRule, Database, MatchResult

PLAN_VERSION = 1

DESKTOPS: dict[str, dict[str, Any]] = {
    "plasma": {
        "label": "KDE Plasma",
        "why": "closest to Windows out of the box: taskbar, system tray, start menu, and a file manager laid out the way Explorer is",
        "pacman": [
            "plasma-meta", "sddm", "konsole", "dolphin", "kate", "ark", "okular",
            "spectacle", "gwenview", "kcalc", "partitionmanager", "kdeplasma-addons",
        ],
        "display_manager": "sddm",
    },
    "gnome": {
        "label": "GNOME",
        "why": "opinionated and calm; nothing like Windows, but very little breaks",
        "pacman": ["gnome", "gnome-tweaks", "gdm", "nautilus", "gnome-text-editor", "file-roller"],
        "display_manager": "gdm",
    },
    "xfce": {
        "label": "Xfce",
        "why": "light, boring, survives old hardware",
        "pacman": ["xfce4", "xfce4-goodies", "lightdm", "lightdm-gtk-greeter", "xarchiver"],
        "display_manager": "lightdm",
    },
    "cinnamon": {
        "label": "Cinnamon",
        "why": "familiar layout, gentler than Plasma about configuration",
        "pacman": ["cinnamon", "lightdm", "lightdm-gtk-greeter", "xed", "nemo-fileroller"],
        "display_manager": "lightdm",
    },
    "none": {"label": "no desktop", "why": "you know what you are doing", "pacman": [], "display_manager": None},
}

#: Things every hop gets. ntfs-3g is not optional — you will want to read the
#: old Windows partition at least once, usually at 2am, usually in a panic.
BASE_PACKAGES = [
    "base-devel", "git", "curl", "wget", "rsync", "man-db", "man-pages", "less",
    "unzip", "p7zip", "htop", "nano", "vim", "ntfs-3g", "exfatprogs", "usbutils",
    "pciutils", "networkmanager", "openssh", "reflector", "pacman-contrib",
]

AUDIO_PACKAGES = ["pipewire", "pipewire-alsa", "pipewire-pulse", "pipewire-jack", "wireplumber"]
FONT_PACKAGES = ["noto-fonts", "noto-fonts-emoji", "ttf-liberation", "ttf-dejavu"]
CJK_FONT_PACKAGES = ["noto-fonts-cjk"]
LAPTOP_PACKAGES = ["tlp", "tlp-rdw", "bluez", "bluez-utils", "brightnessctl"]
GAMING_PACKAGES = ["steam", "gamemode", "lib32-gamemode", "mangohud", "lib32-mangohud", "wine", "winetricks"]

GPU_PACKAGES: dict[str, list[str]] = {
    "nvidia": ["nvidia-dkms", "nvidia-utils", "lib32-nvidia-utils", "nvidia-settings", "linux-headers"],
    "amd": ["mesa", "lib32-mesa", "vulkan-radeon", "lib32-vulkan-radeon", "libva-mesa-driver"],
    "intel": ["mesa", "lib32-mesa", "vulkan-intel", "lib32-vulkan-intel", "intel-media-driver"],
    "other": ["mesa", "lib32-mesa"],
}

#: Arch packages that provide the same thing and cannot sensibly be installed
#: alongside each other. Position inside a group is the preference order — the
#: earliest name in the group wins and the rest are dropped, whichever part of
#: the plan asked for them.
#:
#: This matters more than it looks. pacman rejects a *transaction*, not a
#: package, so a single conflicting pair takes the other twenty-four packages in
#: the batch down with it — and the person reading the error is on a fresh
#: install with no desktop yet, which is the worst possible moment to debug
#: dependency resolution.
CONFLICT_GROUPS: list[tuple[str, ...]] = [
    # NVIDIA kernel module providers. nvidia-dkms rebuilds itself against
    # whatever kernel you end up running, which is what you want on a rolling
    # release; plain 'nvidia' is compiled against the stock kernel only and
    # leaves you without graphics the first time the kernel moves ahead of it.
    ("nvidia-dkms", "nvidia-open-dkms", "nvidia", "nvidia-open", "nvidia-lts"),
    # PipeWire ships drop-in replacements for both of these and cannot be
    # installed next to the originals.
    ("pipewire-pulse", "pulseaudio"),
    ("pipewire-jack", "jack2"),
    # 7zip is the maintained 7-Zip; p7zip is the older port it supersedes, and
    # they claim the same /usr/bin entries. Asking for both is asking pacman for
    # one tool twice.
    ("7zip", "p7zip"),
]

#: Windows keyboard layout ids (KLID) -> X11/console layout. The long tail lives
#: in /usr/share/X11/xkb; these are the ones that actually turn up.
KLID_TO_XKB = {
    "00000409": "us", "00000809": "gb", "00000419": "ru", "00000407": "de",
    "0000040c": "fr", "0000080c": "be", "00000410": "it", "0000040a": "es",
    "00000416": "br", "00000816": "pt", "00000413": "nl", "0000041d": "se",
    "00000414": "no", "00000406": "dk", "0000040b": "fi", "00000415": "pl",
    "00000405": "cz", "0000040e": "hu", "00000422": "ua", "00000423": "by",
    "0000043f": "kz", "0000041f": "tr", "00000408": "gr", "0000040d": "il",
    "00000401": "ara", "00000411": "jp", "00000412": "kr", "00000804": "cn",
    "00000404": "tw", "0000042a": "vn", "0000041e": "th", "00000418": "ro",
    "00000402": "bg", "0000041a": "hr", "00000424": "si", "0000041b": "sk",
    "00000426": "lv", "00000427": "lt", "00000425": "ee", "0000042c": "az",
    "00000437": "ge", "00000439": "in", "0000041c": "al", "0000042f": "mk",
    "0000081a": "rs", "00000c1a": "rs",
}


@dataclass
class PlanItem:
    """One Windows program and what happens to it."""

    source: str
    version: str | None
    rule_id: str | None
    title: str
    strategy: str
    install_source: str | None = None  # pacman | aur | flatpak | None
    package: str | None = None
    packages: list[str] = field(default_factory=list)
    replacement: str = ""
    notes: str = ""
    carry: list[str] = field(default_factory=list)
    confidence: str = "high"
    matched_by: str = ""
    matched_on: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def is_blocker(self) -> bool:
        return self.strategy == "none"


#: The keys an item may have, and the ones it cannot do without. Derived from
#: the dataclass so that adding a field cannot leave the reader of a hand-edited
#: plan with a stale error message.
_ITEM_KEYS = frozenset(f.name for f in fields(PlanItem))
_REQUIRED_ITEM_KEYS = frozenset(
    f.name for f in fields(PlanItem) if f.default is MISSING and f.default_factory is MISSING
)


def _section(raw: dict[str, Any], key: str, shape: type) -> Any:
    """One top-level section of a plan file, as the shape the rest of hop expects.

    A plan is JSON so that it can be edited, and blanking a section out with
    ``null`` is as ordinary an edit as deleting the key. Both have to mean the
    same thing: an empty section. Left alone, ``null`` reaches the first
    ``.get`` in ``hop land`` as an ``AttributeError`` — a traceback in front of
    somebody on a machine that has no desktop yet.

    A section of the wrong shape is a different mistake and gets a different
    answer, because there is nothing sensible to do with a list of packages
    where the three install sources belong.
    """
    value = raw.get(key)
    if value is None:
        return shape()
    if not isinstance(value, shape):
        article = "an object" if shape is dict else "a list"
        raise ValueError(
            f"the plan's {key!r} is a {type(value).__name__}, and hop needs {article} there. "
            "If you edited the plan by hand, put that section back the way it was, or set it "
            "to null to empty it."
        )
    return value


def _item_from_dict(raw: Any, position: int) -> PlanItem:
    """One item out of a plan file, with a sentence instead of a TypeError.

    A plan is JSON precisely so that it can be read and edited — deleting the
    three entries you disagree with is the supported way to change one. That
    makes a mistyped key and a deleted line ordinary events rather than
    corruption, and they have to say what happened. Left to the dataclass they
    surface as ``PlanItem.__init__() missing 1 required positional argument``
    out of the middle of ``hop land``, which tells a person on a machine with no
    desktop yet nothing they can act on.
    """
    where = f"item {position} of the plan"
    if not isinstance(raw, dict):
        raise ValueError(
            f"{where} is a {type(raw).__name__}, not an object. The 'items' list holds one "
            "object per program."
        )
    named = raw.get("source") or raw.get("title")
    if named:
        where += f" ({named})"
    unknown = sorted(set(raw) - _ITEM_KEYS)
    if unknown:
        raise ValueError(
            f"{where} has {', '.join(repr(k) for k in unknown)}, which hop does not recognise. "
            f"An item's keys are: {', '.join(sorted(_ITEM_KEYS))}."
        )
    missing = sorted(_REQUIRED_ITEM_KEYS - set(raw))
    if missing:
        raise ValueError(
            f"{where} is missing {', '.join(repr(k) for k in missing)}. If you edited the plan by "
            "hand, put the line back — or delete the whole item, which is how you drop something "
            "from a plan."
        )
    return PlanItem(**raw)


@dataclass
class Plan:
    hopfile: dict[str, Any]
    target: dict[str, Any]
    system: dict[str, Any]
    items: list[PlanItem] = field(default_factory=list)
    unknown: list[dict[str, Any]] = field(default_factory=list)
    ignored: list[dict[str, Any]] = field(default_factory=list)
    packages: dict[str, list[str]] = field(default_factory=dict)
    package_reasons: dict[str, str] = field(default_factory=dict)
    games: dict[str, Any] = field(default_factory=dict)
    payload: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    score: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": PLAN_VERSION,
            "generated_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hopfile": self.hopfile,
            "target": self.target,
            "system": self.system,
            "packages": self.packages,
            "package_reasons": self.package_reasons,
            "items": [asdict(i) for i in self.items],
            "unknown": self.unknown,
            "ignored": self.ignored,
            "games": self.games,
            "payload": self.payload,
            "data": self.data,
            "warnings": self.warnings,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Plan:
        version = raw.get("plan_version")
        if version != PLAN_VERSION:
            raise ValueError(f"plan version {version} is not supported (expected {PLAN_VERSION})")
        plan = cls(
            hopfile=_section(raw, "hopfile", dict),
            target=_section(raw, "target", dict),
            system=_section(raw, "system", dict),
            packages=_section(raw, "packages", dict),
            package_reasons=_section(raw, "package_reasons", dict),
            unknown=_section(raw, "unknown", list),
            ignored=_section(raw, "ignored", list),
            games=_section(raw, "games", dict),
            payload=_section(raw, "payload", list),
            data=_section(raw, "data", dict),
            warnings=_section(raw, "warnings", list),
            score=_section(raw, "score", dict),
        )
        plan.items = [
            _item_from_dict(item, position)
            for position, item in enumerate(_section(raw, "items", list), start=1)
        ]
        return plan

    @property
    def blockers(self) -> list[PlanItem]:
        return [i for i in self.items if i.is_blocker]

    def by_strategy(self, strategy: str) -> list[PlanItem]:
        return [i for i in self.items if i.strategy == strategy]


class Planner:
    def __init__(
        self,
        manifest: Manifest,
        db: Database,
        desktop: str = "plasma",
        prefer_flatpak: bool = False,
        aur_helper: str = "paru",
        include_gaming: bool = True,
        hostname: str | None = None,
    ) -> None:
        if desktop not in DESKTOPS:
            raise ValueError(f"unknown desktop {desktop!r}; pick one of {', '.join(DESKTOPS)}")
        self.m = manifest
        self.db = db
        self.desktop = desktop
        self.prefer_flatpak = prefer_flatpak
        self.aur_helper = aur_helper
        self.include_gaming = include_gaming
        self.hostname = hostname

    # --- entry point ------------------------------------------------------

    def build(self) -> Plan:
        plan = Plan(
            hopfile=self._hopfile_stamp(),
            target=self._target(),
            system=self._system_settings(),
        )
        self._resolve_software(plan)
        self._add_system_packages(plan)
        self._collect_packages(plan)
        self._resolve_games(plan)
        plan.payload = list(self.m.payload_entries)
        plan.data = self._user_data()
        plan.warnings = list(self.m.warnings) + self._own_warnings()
        plan.score = self._score(plan)
        return plan

    # --- pieces -----------------------------------------------------------

    def _hopfile_stamp(self) -> dict[str, Any]:
        return {
            "hostname": self.m.hostname,
            "user": self.m.username,
            "generated_at": self.m.generated_at,
            "generator": self.m.generator,
            "path": str(self.m.path) if self.m.path else None,
            "payload_dir": self.m.payload_dir,
        }

    def _target(self) -> dict[str, Any]:
        desktop = DESKTOPS[self.desktop]
        return {
            "desktop": self.desktop,
            "desktop_label": desktop["label"],
            "desktop_rationale": desktop["why"],
            "display_manager": desktop["display_manager"],
            "aur_helper": self.aur_helper,
            "prefer_flatpak": self.prefer_flatpak,
            "gaming": self.include_gaming and self._has_gaming(),
        }

    def _system_settings(self) -> dict[str, Any]:
        locale = _to_posix_locale(self.m.locale)
        layouts = _xkb_layouts(self.m.keyboard_layouts)
        return {
            "hostname": self.hostname or _sanitise_hostname(self.m.hostname),
            "username": _sanitise_username(self.m.username),
            "locale": locale,
            "locales": sorted({locale, "en_US.UTF-8"}),
            "timezone": self.m.timezone or "UTC",
            "keymap": layouts[0] if layouts else "us",
            "x11_layouts": layouts or ["us"],
            "firmware": self.m.firmware,
            "secure_boot": self.m.secure_boot,
            "chassis": self.m.chassis,
            "gpu_vendors": self.m.gpu_vendors,
            "memory_gb": self.m.memory_gb,
        }

    def _resolve_software(self, plan: Plan) -> None:
        seen: set[str] = set()
        for entry in self.m.software:
            if entry.key in seen:
                continue
            seen.add(entry.key)

            ign = self.db.ignored(entry.name)
            if ign is not None:
                plan.ignored.append({"name": entry.name, "reason": ign.reason})
                continue

            result = self.db.match(entry.name, entry.winget_id, entry.executables)
            if result is None:
                if entry.system_component:
                    plan.ignored.append({"name": entry.name, "reason": "marked as a system component"})
                else:
                    plan.unknown.append(
                        {
                            "name": entry.name,
                            "version": entry.version,
                            "publisher": entry.publisher,
                            "winget_id": entry.winget_id,
                        }
                    )
                continue

            if result.rule.strategy == "ignore":
                plan.ignored.append({"name": entry.name, "reason": result.rule.notes or "not an application"})
                continue

            plan.items.append(self._item(entry.name, entry.version, result))

        plan.items.sort(key=lambda i: (_strategy_order(i.strategy), i.title.lower()))
        plan.unknown.sort(key=lambda u: str(u.get("name", "")).lower())
        plan.ignored.sort(key=lambda u: str(u.get("name", "")).lower())

    def _item(self, source: str, version: str | None, result: MatchResult) -> PlanItem:
        rule: AppRule = result.rule
        chosen = rule.preferred(self.prefer_flatpak)
        install_source, package = chosen if chosen else (None, None)
        return PlanItem(
            source=source,
            version=version,
            rule_id=rule.id,
            title=rule.replacement or rule.name,
            strategy=rule.strategy,
            install_source=install_source,
            package=package,
            packages=list(rule.all_packages(install_source)) if install_source else [],
            replacement=rule.replacement,
            notes=rule.notes,
            carry=list(rule.carry),
            confidence=rule.confidence,
            matched_by=result.method,
            matched_on=result.matched_on,
            tags=list(rule.tags),
        )

    def _add_system_packages(self, plan: Plan) -> None:
        """Packages that come from the hardware and the locale, not from the app list."""
        add = plan.package_reasons

        for pkg in BASE_PACKAGES:
            add.setdefault(pkg, "base system and the tools you need on day one")
        for pkg in AUDIO_PACKAGES:
            add.setdefault(pkg, "audio (PipeWire replaces both the Windows mixer and PulseAudio)")
        for pkg in FONT_PACKAGES:
            add.setdefault(pkg, "fonts, so the web is not a wall of tofu boxes")

        locale = (self.m.locale or "").lower()
        if locale.split("-")[0] in ("zh", "ja", "ko"):
            for pkg in CJK_FONT_PACKAGES:
                add.setdefault(pkg, f"CJK fonts for the {self.m.locale} locale")

        for vendor in self.m.gpu_vendors or ["other"]:
            for pkg in GPU_PACKAGES.get(vendor, GPU_PACKAGES["other"]):
                add.setdefault(pkg, f"{vendor} graphics driver stack")

        for pkg in DESKTOPS[self.desktop]["pacman"]:
            add.setdefault(pkg, f"{DESKTOPS[self.desktop]['label']} desktop")

        if self.m.chassis == "laptop":
            for pkg in LAPTOP_PACKAGES:
                add.setdefault(pkg, "laptop: battery, bluetooth and backlight")

        if self.m.wifi_profiles:
            # wpa_supplicant only, not iwd as well. NetworkManager talks to one
            # Wi-Fi backend at a time and uses wpa_supplicant unless it is told
            # otherwise, so a second supplicant sits on the disk doing nothing
            # and gives the reader a package they cannot account for.
            add.setdefault("wpa_supplicant", "the Wi-Fi backend NetworkManager uses, for your saved networks")

        if plan.target["gaming"]:
            for pkg in GAMING_PACKAGES:
                add.setdefault(pkg, "gaming stack (Steam, Proton helpers, Wine)")

        if self.m.dev("wsl", "present"):
            add.setdefault("docker", "you used WSL; containers are the closest habit to keep")
            add.setdefault("docker-compose", "you used WSL")

        if self.m.dev("ssh_keys"):
            add.setdefault("openssh", "you have SSH keys worth carrying over")

    def _collect_packages(self, plan: Plan) -> None:
        buckets: dict[str, list[str]] = {"pacman": [], "aur": [], "flatpak": []}
        seen: dict[str, set[str]] = {k: set() for k in buckets}

        for pkg in plan.package_reasons:
            if pkg not in seen["pacman"]:
                seen["pacman"].add(pkg)
                buckets["pacman"].append(pkg)

        for item in plan.items:
            if not item.install_source or not item.package:
                continue
            bucket = item.install_source
            if item.package in seen[bucket]:
                continue
            seen[bucket].add(item.package)
            buckets[bucket].append(item.package)
            plan.package_reasons.setdefault(item.package, f"replaces {item.source}")

        # A package we would install from the AUR that also exists in our pacman
        # list is a mistake — drop the AUR copy.
        buckets["aur"] = [p for p in buckets["aur"] if p not in seen["pacman"]]
        self._resolve_conflicts(plan, buckets)
        plan.packages = {k: sorted(v) for k, v in buckets.items()}

    def _resolve_conflicts(self, plan: Plan, buckets: dict[str, list[str]]) -> None:
        """Keep one package out of each mutually exclusive group.

        A machine with an NVIDIA card reaches here asking for both nvidia-dkms
        (because the hardware needs a driver) and nvidia (because the mapping
        database answers 'NVIDIA GeForce Experience' with the driver package).
        Both are correct in isolation and together they are a failed install, so
        the plan has to pick — and say in package_reasons which it picked and why,
        rather than quietly shortening the list.

        The items are rewritten to name the survivor too. An item is what the
        report prints, so leaving it pointing at the dropped package would put
        'install nvidia' in the report and nvidia-dkms in every list that
        actually installs anything — the same document telling the reader two
        different things about their graphics driver.
        """
        for group in CONFLICT_GROUPS:
            for source, packages in buckets.items():
                present = [name for name in group if name in packages]
                if len(present) < 2:
                    continue
                winner, losers = present[0], present[1:]
                buckets[source] = [name for name in packages if name not in losers]
                for loser in losers:
                    plan.package_reasons.pop(loser, None)
                existing = plan.package_reasons.get(winner, "")
                plan.package_reasons[winner] = (
                    f"{existing}; " if existing else ""
                ) + (
                    f"chosen over {', '.join(losers)}, which cannot be installed alongside it"
                )
                for item in plan.items:
                    if item.install_source == source and item.package in losers:
                        item.package = winner
                        item.packages = [winner] + [
                            name for name in item.packages if name not in losers and name != winner
                        ]

    def _resolve_games(self, plan: Plan) -> None:
        games = self.m.steam_games
        if not games:
            plan.games = {"total": 0, "titles": []}
            return
        titles = []
        counts = {"works": 0, "blocked": 0, "broken": 0, "unknown": 0}
        for game in games:
            appid = int(game.get("appid") or 0)
            name = str(game.get("name") or f"appid {appid}")
            known = self.db.game_status(appid, name)
            status = known.status if known else "unknown"
            counts[status] = counts.get(status, 0) + 1
            titles.append(
                {
                    "appid": appid,
                    "name": name,
                    "status": status,
                    "reason": known.reason if known else "not in the local snapshot — check protondb.com",
                    "size_bytes": game.get("size_bytes") or 0,
                }
            )
        titles.sort(key=lambda t: ({"blocked": 0, "broken": 1, "unknown": 2, "works": 3}[t["status"]], t["name"].lower()))
        plan.games = {"total": len(titles), "counts": counts, "titles": titles}

    def _user_data(self) -> dict[str, Any]:
        """How much of the old machine is data rather than software.

        The scanner measures the standard profile folders; the report turns that
        into "your backup drive needs to be at least this big", which is the one
        number people forget until the installer is already running.
        """
        folders = self.m.user_folders
        games_bytes = sum(int(t.get("size_bytes") or 0) for t in (self.m.steam_games or []))
        onedrive = self.m.onedrive
        return {
            "folders": folders,
            "total_bytes": sum(f["size_bytes"] for f in folders),
            "onedrive": onedrive,
            "steam_bytes": games_bytes,
        }

    def _own_warnings(self) -> list[str]:
        out: list[str] = []
        if self.m.secure_boot:
            out.append(
                "Secure Boot is on. Arch will not boot until you either turn it off in firmware "
                "or set up signed boot images (sbctl). Decide which before you reboot."
            )
        if self.m.firmware == "BIOS":
            out.append("This machine boots in legacy BIOS mode — use a GPT+BIOS boot partition or MBR, not systemd-boot.")
        if not self.m.timezone:
            out.append("Timezone did not resolve; the plan falls back to UTC. Fix it with 'timedatectl set-timezone'.")
        if self.m.dev("wsl", "present"):
            out.append("You have WSL distros. Their filesystems live inside .vhdx files — export them with 'wsl --export' before wiping.")
        if "nvidia" in self.m.gpu_vendors:
            out.append("NVIDIA: the plan uses nvidia-dkms. If you keep the LTS kernel, install linux-lts-headers too.")
        data = self.m.user_data_bytes
        if data:
            out.append(f"Roughly {_gb(data)} of user data in the standard folders — make sure your backup target has room.")
        return out

    #: Names that mean "this person plays games on this machine".
    _GAMING_HINTS = ("steam", "epic games", "gog galaxy", "battle.net", "ubisoft connect",
                     "ea app", "origin", "rockstar games launcher", "riot", "minecraft")

    def _has_gaming(self) -> bool:
        gaming = self.m.raw.get("gaming") or {}
        if self.m.steam_games or (isinstance(gaming, dict) and gaming.get("steam", {}).get("present")):
            return True
        for entry in self.m.software:
            low = entry.name.lower()
            if any(hint in low for hint in self._GAMING_HINTS):
                return True
        return False

    def _score(self, plan: Plan) -> dict[str, Any]:
        """Hoppability: how much of this machine actually comes with you.

        Weighted by strategy, so an app that only runs under Wine counts for
        half. Ignored entries (redistributables and friends) do not count at
        all. Unknown apps count as zero — being unable to answer is a real cost
        to the user, and hiding it would make the number a lie.
        """
        weighted = 0.0
        counted = 0
        by_strategy: dict[str, int] = {}
        for item in plan.items:
            weight = STRATEGY_WEIGHT.get(item.strategy)
            by_strategy[item.strategy] = by_strategy.get(item.strategy, 0) + 1
            if weight is None:
                continue
            counted += 1
            weighted += weight
        unknown = len(plan.unknown)
        total = counted + unknown
        if not total:
            # Nothing resolved and nothing unknown does not mean this machine
            # hops perfectly. It means the scan came back empty — usually the
            # registry was unreadable, sometimes the hopfile was hand-written.
            # Scoring that 100% would hand the most encouraging possible answer
            # to a question hop never got to look at, and this is the number the
            # user makes the decision on.
            return {
                "hoppability": None,
                "considered": 0,
                "matched": 0,
                "unknown": 0,
                "ignored": len(plan.ignored),
                "blockers": 0,
                "by_strategy": by_strategy,
                "verdict": (
                    "no software was recorded, so there is nothing to score — re-run "
                    "'hop scan' and read the warnings it prints"
                ),
            }
        percent = round(100.0 * weighted / total, 1) if total else 100.0
        return {
            "hoppability": percent,
            "considered": total,
            "matched": counted,
            "unknown": unknown,
            "ignored": len(plan.ignored),
            "blockers": len(plan.blockers),
            "by_strategy": by_strategy,
            "verdict": _verdict(percent, len(plan.blockers)),
        }


# --- small helpers --------------------------------------------------------


def _strategy_order(strategy: str) -> int:
    return {"none": 0, "web": 1, "compat": 2, "alternative": 3, "native": 4, "builtin": 5}.get(strategy, 9)


def _verdict(percent: float, blockers: int) -> str:
    """One sentence for the top of the report and the top of the summary.

    The blocker count is consulted in every band, not only the top one. A
    blocker is a program with no Linux path at all, and it weighs zero, so a
    machine can sit at 97% with three of them: 97 programs that come across and
    three that do not. The old wording answered that with "nothing lost", four
    lines above a section headed "No Linux path. Decide what you are doing about
    these before you wipe anything." Of the two, the summary is the one people
    read, and it was the one that was wrong.
    """
    if percent >= 90 and not blockers:
        return "clean hop — nothing on this machine is holding you back"
    if percent >= 75:
        if blockers:
            return (
                f"comfortable hop apart from {blockers} blocker(s) — most of this machine "
                "comes with you, and the report starts with the part that does not"
            )
        return "comfortable hop — a couple of habits to relearn, nothing lost"
    if percent >= 55:
        if blockers:
            return "workable hop — read the blockers before you commit"
        return "workable hop — no blockers, but a fair part of this machine is unaccounted for"
    if percent >= 35:
        return "rough hop — consider dual-booting for a month first"
    return "hard hop — a lot of this machine does not come with you; dual-boot"


def _to_posix_locale(win_locale: str | None) -> str:
    if not win_locale:
        return "en_US.UTF-8"
    tag = win_locale.replace("_", "-").strip()
    parts = tag.split("-")
    if len(parts) == 1:
        return f"{parts[0].lower()}_{parts[0].upper()}.UTF-8"
    lang = parts[0].lower()
    region = parts[-1].upper()
    if len(region) != 2:  # e.g. sr-Latn-RS style tags
        region = parts[1].upper()[:2]
    return f"{lang}_{region}.UTF-8"


def _xkb_layouts(klids: list[str]) -> list[str]:
    out: list[str] = []
    for klid in klids:
        key = str(klid).strip().lower().zfill(8)
        layout = KLID_TO_XKB.get(key)
        if layout and layout not in out:
            out.append(layout)
    return out


#: What a hostname and a user name may be made of on the other side. ASCII, and
#: not because of an opinion about alphabets: str.isalnum() is true for Cyrillic
#: and CJK, Windows accepts a computer name in any of them, and systemd-hostnamed
#: and useradd accept neither. A name hop passes through unchanged here comes
#: back as a refusal from hostnamectl on a machine with no desktop yet, or from
#: 'hop install-config', which will not paste a non-ASCII name into a shell
#: script. Both failures land hours after the decision that caused them, so the
#: reduction happens here, once, where the plan can still be read.
_HOSTNAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_USERNAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


def _sanitise_hostname(name: str) -> str:
    mapped = "".join(c if c in _HOSTNAME_CHARS else "-" for c in (name or "").lower())
    # Runs of hyphens are what is left of a word hop could not transliterate;
    # collapsing them keeps 'PC-Артём-2' as 'pc-2' rather than 'pc-------2'.
    cleaned = "-".join(part for part in mapped.split("-") if part)
    return cleaned[:63].strip("-") or "arch"


def _sanitise_username(name: str) -> str:
    cleaned = "".join(c for c in (name or "").lower() if c in _USERNAME_CHARS).lstrip("-")
    if not cleaned:
        # Nothing usable survived — an account written entirely in another
        # alphabet reduces to an empty string. hop does not invent a
        # transliteration; it falls back to the name every Linux install
        # already understands, and the plan is there to be edited.
        return "user"
    if cleaned[0].isdigit():
        cleaned = f"u{cleaned}"
    return cleaned[:32]


def _gb(n: int) -> str:
    return f"{n / (1024 ** 3):.0f} GB"
