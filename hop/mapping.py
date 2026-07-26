"""The mapping database and the matcher that uses it.

Given "Mozilla Firefox (x64 ru) 128.0" from a Windows registry key, decide that
the answer is ``pacman -S firefox``. Given "Adobe Premiere Pro 2024", decide that
there is no answer and say so plainly.

Matching is deliberately boring and explainable: four passes, cheapest and most
confident first, and every result carries *how* it matched so the report can show
its work. See docs/MAPPING.md for the file format.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

STRATEGIES = ("native", "alternative", "builtin", "compat", "web", "none", "ignore")
CONFIDENCES = ("high", "medium", "low")

#: How much each strategy contributes to the hoppability score.
STRATEGY_WEIGHT = {
    "native": 1.0,
    "builtin": 1.0,
    "alternative": 0.75,
    "compat": 0.5,
    "web": 0.4,
    "none": 0.0,
    "ignore": None,  # excluded from scoring entirely
}

#: How a match was made, best first. Also the tie-break order in the resolver.
MATCH_METHODS = ("winget", "exe", "name", "regex")


class DatabaseError(Exception):
    pass


@dataclass(frozen=True)
class AppRule:
    id: str
    name: str
    strategy: str
    tags: tuple[str, ...] = ()
    match: tuple[str, ...] = ()
    regex: str | None = None
    winget: tuple[str, ...] = ()
    exe: tuple[str, ...] = ()
    pacman: tuple[str, ...] = ()
    aur: tuple[str, ...] = ()
    flatpak: tuple[str, ...] = ()
    replacement: str = ""
    carry: tuple[str, ...] = ()
    notes: str = ""
    confidence: str = "high"

    @property
    def has_packages(self) -> bool:
        return bool(self.pacman or self.aur or self.flatpak)

    def preferred(self, prefer_flatpak: bool = False) -> tuple[str, str] | None:
        """Pick one install source: ('pacman', 'firefox'). Repos beat AUR beats flatpak."""
        order = (
            [("flatpak", self.flatpak), ("pacman", self.pacman), ("aur", self.aur)]
            if prefer_flatpak
            else [("pacman", self.pacman), ("aur", self.aur), ("flatpak", self.flatpak)]
        )
        for source, packages in order:
            if packages:
                return source, packages[0]
        return None

    def all_packages(self, source: str) -> tuple[str, ...]:
        return {"pacman": self.pacman, "aur": self.aur, "flatpak": self.flatpak}.get(source, ())


@dataclass(frozen=True)
class IgnoreRule:
    reason: str
    match: tuple[str, ...] = ()
    regex: str | None = None


@dataclass(frozen=True)
class GameTitle:
    name: str
    status: str
    appid: int = 0
    reason: str = ""


@dataclass(frozen=True)
class MatchResult:
    rule: AppRule
    method: str  # one of MATCH_METHODS
    matched_on: str  # the literal token that matched, for the report


def _tuple(value, lower: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    out = [str(v).strip() for v in value if str(v).strip()]
    return tuple(v.lower() for v in out) if lower else tuple(out)


@dataclass
class Database:
    """Loaded ``packages.toml`` + ``anticheat.toml``, with lookup indexes."""

    apps: list[AppRule] = field(default_factory=list)
    ignores: list[IgnoreRule] = field(default_factory=list)
    games: list[GameTitle] = field(default_factory=list)

    _by_winget: dict[str, AppRule] = field(default_factory=dict, repr=False)
    _by_exe: dict[str, AppRule] = field(default_factory=dict, repr=False)
    _by_id: dict[str, AppRule] = field(default_factory=dict, repr=False)
    _compiled: list[tuple[re.Pattern, AppRule]] = field(default_factory=list, repr=False)
    _ignore_compiled: list[tuple[re.Pattern, IgnoreRule]] = field(default_factory=list, repr=False)
    _name_rules: list[tuple[str, AppRule]] = field(default_factory=list, repr=False)

    # --- loading ----------------------------------------------------------

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> Database:
        root = Path(data_dir) if data_dir else default_data_dir()
        db = cls()
        db._load_packages(root / "packages.toml")
        anticheat = root / "anticheat.toml"
        if anticheat.exists():
            db._load_anticheat(anticheat)
        db._index()
        return db

    def _load_packages(self, path: Path) -> None:
        raw = _read_toml(path)
        for item in raw.get("app", []):
            missing = [k for k in ("id", "name", "strategy") if not item.get(k)]
            if missing:
                raise DatabaseError(f"{path.name}: entry {item!r} is missing {', '.join(missing)}")
            if item["strategy"] not in STRATEGIES:
                raise DatabaseError(
                    f"{path.name}: {item['id']} has strategy {item['strategy']!r}; "
                    f"expected one of {', '.join(STRATEGIES)}"
                )
            confidence = item.get("confidence", "high")
            if confidence not in CONFIDENCES:
                raise DatabaseError(f"{path.name}: {item['id']} has confidence {confidence!r}")
            self.apps.append(
                AppRule(
                    id=item["id"],
                    name=item["name"],
                    strategy=item["strategy"],
                    tags=_tuple(item.get("tags")),
                    match=_tuple(item.get("match"), lower=True),
                    regex=item.get("regex"),
                    winget=_tuple(item.get("winget")),
                    exe=_tuple(item.get("exe"), lower=True),
                    pacman=_tuple(item.get("pacman")),
                    aur=_tuple(item.get("aur")),
                    flatpak=_tuple(item.get("flatpak")),
                    replacement=item.get("replacement", ""),
                    carry=_tuple(item.get("carry")),
                    notes=item.get("notes", ""),
                    confidence=confidence,
                )
            )
        for item in raw.get("ignore", []):
            self.ignores.append(
                IgnoreRule(
                    reason=item.get("reason", "not an application"),
                    match=_tuple(item.get("match"), lower=True),
                    regex=item.get("regex"),
                )
            )

    def _load_anticheat(self, path: Path) -> None:
        raw = _read_toml(path)
        for item in raw.get("title", []):
            if not item.get("name"):
                continue
            self.games.append(
                GameTitle(
                    name=item["name"],
                    status=item.get("status", "unknown"),
                    appid=int(item.get("appid") or 0),
                    reason=item.get("reason", ""),
                )
            )

    def _index(self) -> None:
        for rule in self.apps:
            self._by_id[rule.id] = rule
            for wid in rule.winget:
                self._by_winget.setdefault(wid.lower(), rule)
            for exe in rule.exe:
                self._by_exe.setdefault(exe, rule)
            if rule.regex:
                try:
                    self._compiled.append((re.compile(rule.regex, re.IGNORECASE), rule))
                except re.error as exc:
                    raise DatabaseError(f"{rule.id}: bad regex {rule.regex!r}: {exc}") from exc
        for ign in self.ignores:
            if ign.regex:
                try:
                    self._ignore_compiled.append((re.compile(ign.regex, re.IGNORECASE), ign))
                except re.error as exc:
                    raise DatabaseError(f"ignore rule {ign.reason!r}: bad regex: {exc}") from exc
        # Longest name-substrings first, so "microsoft visual studio code" wins
        # over a bare "visual studio" rule.
        self._name_rules = sorted(
            ((token, rule) for rule in self.apps for token in rule.match),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )

    # --- lookup -----------------------------------------------------------

    def by_id(self, rule_id: str) -> AppRule | None:
        return self._by_id.get(rule_id)

    def ignored(self, name: str) -> IgnoreRule | None:
        low = name.lower()
        for ign in self.ignores:
            for token in ign.match:
                if token in low:
                    return ign
        for pattern, ign in self._ignore_compiled:
            if pattern.search(low):
                return ign
        return None

    def match(
        self,
        name: str,
        winget_id: str | None = None,
        executables: tuple[str, ...] = (),
    ) -> MatchResult | None:
        """Resolve one Windows program. Returns ``None`` when nothing matches."""
        if winget_id:
            rule = self._by_winget.get(winget_id.strip().lower())
            if rule:
                return MatchResult(rule, "winget", winget_id)
        for exe in executables:
            rule = self._by_exe.get(exe.lower())
            if rule:
                return MatchResult(rule, "exe", exe)
        low = name.lower()
        for token, rule in self._name_rules:
            if token in low:
                return MatchResult(rule, "name", token)
        for pattern, rule in self._compiled:
            if pattern.search(low):
                return MatchResult(rule, "regex", pattern.pattern)
        return None

    def game_status(self, appid: int = 0, name: str = "") -> GameTitle | None:
        if appid:
            for title in self.games:
                if title.appid and title.appid == appid:
                    return title
        if name:
            low = name.strip().lower()
            for title in self.games:
                if title.name.lower() == low:
                    return title
        return None

    # --- housekeeping -----------------------------------------------------

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {"apps": len(self.apps), "ignore": len(self.ignores), "games": len(self.games)}
        for rule in self.apps:
            counts[f"strategy:{rule.strategy}"] = counts.get(f"strategy:{rule.strategy}", 0) + 1
        for rule in self.apps:
            counts[f"confidence:{rule.confidence}"] = counts.get(f"confidence:{rule.confidence}", 0) + 1
        return counts

    def lint(self) -> list[str]:
        """Problems a contributor should fix before opening the PR."""
        problems: list[str] = []
        seen: dict[str, str] = {}
        for rule in self.apps:
            if rule.id in seen:
                problems.append(f"duplicate id: {rule.id}")
            seen[rule.id] = rule.name
            if not (rule.match or rule.winget or rule.exe or rule.regex):
                problems.append(f"{rule.id}: no matcher (needs match/winget/exe/regex)")
            if rule.strategy in ("native", "alternative", "compat") and not rule.has_packages:
                problems.append(f"{rule.id}: strategy {rule.strategy} but no pacman/aur/flatpak package")
            if rule.strategy == "alternative" and not rule.replacement:
                problems.append(f"{rule.id}: strategy 'alternative' needs a 'replacement'")
            # 'web' and 'none' entries are allowed to carry packages. A service with
            # no official desktop client usually still has a community wrapper in the
            # AUR (teams-for-linux, figma-linux-bin), and a blocker can still have a
            # partial stand-in worth naming (clamav for a Windows antivirus). The
            # strategy is the honest verdict; the package is the consolation prize,
            # and the report labels it as one instead of pretending it is the answer.
            if not rule.notes:
                problems.append(f"{rule.id}: empty notes — the report will look unhelpful")
        for title in self.games:
            if title.status not in ("blocked", "broken", "works", "unknown"):
                problems.append(f"anticheat: {title.name} has status {title.status!r}")
        return problems

    def search(self, needle: str) -> list[AppRule]:
        low = needle.lower()
        return [
            rule
            for rule in self.apps
            if low in rule.id
            or low in rule.name.lower()
            or any(low in t for t in rule.match)
            or any(low in p.lower() for p in rule.pacman + rule.aur + rule.flatpak)
        ]


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise DatabaseError(f"missing database file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DatabaseError(f"{path} is not valid TOML: {exc}") from exc


@lru_cache(maxsize=1)
def default_data_dir() -> Path:
    """Find ``data/`` whether we are running from a checkout or an install."""
    here = Path(__file__).resolve()
    # hop/data/ is where the database ships, both in a checkout and in a wheel.
    # The second candidate is the old layout, kept so a checkout that predates
    # the move still runs rather than failing with a puzzle.
    for candidate in (here.parent / "data", here.parent.parent / "data"):
        if (candidate / "packages.toml").exists():
            return candidate
    raise DatabaseError(
        "cannot locate the mapping database; pass --data-dir pointing at the repo's data/ folder"
    )
