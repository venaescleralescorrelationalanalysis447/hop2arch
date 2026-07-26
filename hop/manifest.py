"""Reading and validating a hopfile.

The scanner runs on a machine we do not control, under a PowerShell version we
did not choose, possibly without admin rights. So the rule here is: *never*
trust a key exists, never crash on a missing one, and keep every access going
through helpers that return a sane default. A hopfile that is 60% filled in is
still a useful hopfile.

See docs/HOPFILE.md for the format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_VERSIONS = (1,)


class HopfileError(Exception):
    """The file is not a hopfile we can work with."""


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    """Walk a nested dict by key path, returning ``default`` at the first miss."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        if key not in cur or cur[key] is None:
            return default
        cur = cur[key]
    return cur


def _as_list(value: Any) -> list:
    """PowerShell's ConvertTo-Json collapses one-element arrays into scalars."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@dataclass(frozen=True)
class SoftwareEntry:
    """One installed Windows program."""

    name: str
    version: str | None = None
    publisher: str | None = None
    sources: tuple[str, ...] = ()
    winget_id: str | None = None
    install_location: str | None = None
    size_bytes: int = 0
    system_component: bool = False

    @property
    def key(self) -> str:
        return f"{self.name.strip().lower()}|{(self.publisher or '').strip().lower()}"

    @property
    def executables(self) -> tuple[str, ...]:
        """Best guess at the program's exe name, from the install location."""
        if not self.install_location:
            return ()
        tail = self.install_location.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if not tail:
            return ()
        return (f"{tail.lower()}.exe",)

    @classmethod
    def from_dict(cls, raw: dict) -> SoftwareEntry:
        size = raw.get("size_bytes") or 0
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        return cls(
            name=str(raw.get("name") or "").strip(),
            version=raw.get("version"),
            publisher=raw.get("publisher"),
            sources=tuple(str(s) for s in _as_list(raw.get("sources"))),
            winget_id=raw.get("winget_id"),
            install_location=raw.get("install_location"),
            size_bytes=size,
            system_component=bool(raw.get("system_component")),
        )


@dataclass
class Manifest:
    """A parsed hopfile. Thin on purpose — the raw dict stays reachable."""

    raw: dict
    path: Path | None = None
    software: list[SoftwareEntry] = field(default_factory=list)

    # --- construction -----------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Manifest:
        p = Path(path)
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise HopfileError(f"no such hopfile: {p}") from exc
        except json.JSONDecodeError as exc:
            raise HopfileError(f"{p} is not valid JSON: {exc}") from exc
        return cls.from_dict(raw, path=p)

    @classmethod
    def from_dict(cls, raw: Any, path: Path | None = None) -> Manifest:
        if not isinstance(raw, dict):
            raise HopfileError("a hopfile must be a JSON object")
        version = raw.get("hopfile_version")
        if version is None:
            raise HopfileError("missing 'hopfile_version' — is this really a hopfile?")
        if version not in SUPPORTED_VERSIONS:
            raise HopfileError(
                f"hopfile version {version} is not supported by this hop "
                f"(understands {', '.join(str(v) for v in SUPPORTED_VERSIONS)})"
            )
        software = [
            SoftwareEntry.from_dict(item)
            for item in _as_list(raw.get("software"))
            if isinstance(item, dict) and item.get("name")
        ]
        return cls(raw=raw, path=path, software=software)

    # --- convenience accessors -------------------------------------------

    @property
    def generated_at(self) -> str | None:
        return self.raw.get("generated_at")

    @property
    def generator(self) -> str | None:
        return self.raw.get("generator")

    @property
    def hostname(self) -> str:
        return _get(self.raw, "system", "hostname", default="unknown-host")

    @property
    def username(self) -> str:
        return _get(self.raw, "user", "name", default="user")

    @property
    def locale(self) -> str | None:
        return _get(self.raw, "system", "locale")

    @property
    def timezone(self) -> str | None:
        """IANA zone if the scanner could resolve it."""
        return _get(self.raw, "system", "timezone", "iana")

    @property
    def keyboard_layouts(self) -> list[str]:
        return [str(k) for k in _as_list(_get(self.raw, "system", "keyboard_layouts"))]

    @property
    def firmware(self) -> str:
        return _get(self.raw, "system", "firmware", default="unknown")

    @property
    def secure_boot(self) -> bool | None:
        return _get(self.raw, "system", "secure_boot")

    @property
    def chassis(self) -> str:
        return _get(self.raw, "system", "chassis", default="unknown")

    @property
    def memory_gb(self) -> float | None:
        return _get(self.raw, "system", "memory_gb")

    @property
    def gpus(self) -> list[dict]:
        return [g for g in _as_list(_get(self.raw, "system", "gpus")) if isinstance(g, dict)]

    @property
    def gpu_vendors(self) -> list[str]:
        """Unique, order-preserving list of normalised GPU vendors."""
        out: list[str] = []
        for gpu in self.gpus:
            vendor = str(gpu.get("vendor") or "other").lower()
            if vendor not in out:
                out.append(vendor)
        return out

    @property
    def disks(self) -> list[dict]:
        return [d for d in _as_list(self.raw.get("disks")) if isinstance(d, dict)]

    @property
    def browsers(self) -> list[dict]:
        return [b for b in _as_list(self.raw.get("browsers")) if isinstance(b, dict)]

    @property
    def wifi_profiles(self) -> list[dict]:
        return [w for w in _as_list(_get(self.raw, "network", "wifi_profiles")) if isinstance(w, dict)]

    @property
    def payload_dir(self) -> str | None:
        return self.raw.get("payload_dir")

    @property
    def payload_entries(self) -> list[dict]:
        return [e for e in _as_list(_get(self.raw, "payload", "entries")) if isinstance(e, dict)]

    @property
    def warnings(self) -> list[str]:
        return [str(w) for w in _as_list(self.raw.get("warnings"))]

    @property
    def steam_games(self) -> list[dict]:
        return [g for g in _as_list(_get(self.raw, "gaming", "steam", "games")) if isinstance(g, dict)]

    @property
    def user_folders(self) -> list[dict]:
        """The standard profile folders, biggest first. Empty when not scanned."""
        folders = _get(self.raw, "user", "folders", default={}) or {}
        if not isinstance(folders, dict):
            return []
        out: list[dict] = []
        for name, info in folders.items():
            if not isinstance(info, dict):
                continue
            try:
                size = int(info.get("size_bytes") or 0)
            except (TypeError, ValueError):
                size = 0
            try:
                files = int(info.get("files") or 0)
            except (TypeError, ValueError):
                files = 0
            out.append({"name": str(name), "path": info.get("path"), "size_bytes": size, "files": files})
        out.sort(key=lambda f: (-f["size_bytes"], f["name"].lower()))
        return out

    @property
    def onedrive(self) -> dict | None:
        info = _get(self.raw, "user", "onedrive")
        return info if isinstance(info, dict) and info.get("present") else None

    @property
    def user_data_bytes(self) -> int:
        return sum(folder["size_bytes"] for folder in self.user_folders)

    def dev(self, *path: str, default: Any = None) -> Any:
        return _get(self.raw, "dev", *path, default=default)

    def system(self, *path: str, default: Any = None) -> Any:
        return _get(self.raw, "system", *path, default=default)

    # --- integrity --------------------------------------------------------

    def lint(self) -> list[str]:
        """Non-fatal complaints about a hopfile — surfaced by ``hop doctor``."""
        problems: list[str] = []
        if not self.software:
            problems.append("no installed software recorded (did the scanner have registry access?)")
        if not self.timezone:
            problems.append("timezone did not resolve to an IANA name; hop will fall back to UTC")
        if not self.locale:
            problems.append("no locale recorded; hop will fall back to en_US.UTF-8")
        if not self.disks:
            problems.append("no disks recorded; 'hop install-config' cannot suggest a layout")
        if self.payload_dir and not self.payload_entries:
            problems.append(f"payload_dir is {self.payload_dir!r} but the payload index is empty")
        for disk in self.disks:
            for part in _as_list(disk.get("partitions")):
                if isinstance(part, dict) and str(part.get("bitlocker", "")).lower() == "on":
                    problems.append(
                        f"BitLocker is ON for {part.get('letter') or part.get('label') or 'a volume'} — "
                        "export the recovery key and decrypt before you touch the partition table"
                    )
        return problems


def human_bytes(n: float | int | None) -> str:
    """1234567 -> '1.2 MB'. Used all over the reports."""
    if not n:
        return "0 B"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"
