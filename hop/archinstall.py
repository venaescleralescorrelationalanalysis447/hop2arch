"""An archinstall configuration, generated from a plan.

``archinstall`` is the guided installer that ships on the Arch ISO. It can read
its answers from a JSON file instead of asking every question by hand, and that
file is what this module writes: ``user_configuration.json`` for the installer,
and ``hop-post.sh`` for the things the installer does not do — the AUR, flatpak,
the display manager, the locale.

**Nothing here writes a disk layout.** By default the emitted configuration has
no ``disk_config`` key at all, so archinstall stops and asks which disk to
install to, with the real disk list on the real screen in front of you. That is
deliberate, and it is the most important decision in this file. A hopfile is a
snapshot of a different machine, taken possibly weeks earlier; between then and
now a drive can be added, a backup disk can be plugged in, and ``/dev/nvme0n1``
can mean something else entirely. Handing archinstall a partitioning plan built
from stale information and letting it run unattended is the one mistake this
project must never make, because it is the only one that cannot be undone.

``disk_config=True`` exists so that the shape of the key is documented, and for
no other reason. It emits ``"config_type": "manual_partitioning"`` with an empty
device list and a comment telling you to fill it in; as written, archinstall has
nowhere to install and will stop, which is the intended behaviour. It will not
produce a "wipe this disk" configuration, and there is no flag that makes it.

Two more things to know before you use the output.

*No password is written anywhere.* The account name comes across with sudo
rights and nothing else. archinstall asks for the password itself, in the
installer, where you can see what you are typing. hop does not store one,
generate one, or leave a placeholder that looks like one.

*archinstall's schema moves between releases.* This config is shaped for the
3.0 series and carries a ``_hop`` block recording that. Check the keys against
the archinstall on your ISO before you rely on it — its repository keeps
example configurations, and ``archinstall --help`` will tell you the version.
If it rejects a key, delete that key: every value here can also be answered in
the installer's own menus. This file saves you twenty minutes of typing. It is
a starting point, not a guarantee, and it is better to say so than to let you
find out at 2am.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

from . import __version__
from .plan import Plan
from .report import _wrap

#: The archinstall series this configuration is shaped for.
ARCHINSTALL_TARGET = "3.0"

#: Plan desktop key -> archinstall profile name. These are archinstall's own
#: spellings, which are not always the projects' own branding: it writes
#: "Gnome" rather than "GNOME" and "Xfce4" rather than "Xfce". Copy them
#: exactly; the installer matches on the string.
DESKTOP_PROFILES: dict[str, str | None] = {
    "plasma": "KDE Plasma",
    "gnome": "Gnome",
    "xfce": "Xfce4",
    "cinnamon": "Cinnamon",
    "none": None,
}

#: Plan display manager -> archinstall greeter id. archinstall names the lightdm
#: greeters after their packages, because "lightdm" alone installs no greeter and
#: boots to a black screen with a cursor on it.
GREETERS: dict[str, str] = {
    "sddm": "sddm",
    "gdm": "gdm",
    "lightdm": "lightdm-gtk-greeter",
}

#: Packages the chosen archinstall profile installs on its own. They are removed
#: from the ``packages`` list so that archinstall and the plan are not both
#: claiming the same names: pacman would cope, but a reader would not, and if
#: archinstall changes what a profile pulls in, a name pinned here would quietly
#: start meaning something different. Everything else from the plan stays.
PROFILE_PACKAGES: dict[str, tuple[str, ...]] = {
    "plasma": ("plasma-meta", "sddm"),
    "gnome": ("gnome", "gdm"),
    "xfce": ("xfce4", "xfce4-goodies", "lightdm", "lightdm-gtk-greeter"),
    "cinnamon": ("cinnamon", "lightdm", "lightdm-gtk-greeter"),
    "none": (),
}

#: archinstall's own strings for the graphics driver choice.
GFX_DRIVERS: dict[str, str] = {
    "nvidia": "Nvidia (proprietary)",
    "amd": "AMD / ATI (open-source)",
    "intel": "Intel (open-source)",
    "open": "All open-source",
}

#: The AUR helper package to build. The ``-bin`` variants are the same programs,
#: already compiled.
AUR_HELPER_PACKAGES: dict[str, str] = {"paru": "paru-bin", "yay": "yay-bin"}

#: Arch package names, locales, keymaps and layouts are all drawn from this small
#: character set. Anything outside it does not belong in a generated shell script.
_SAFE = re.compile(r"[A-Za-z0-9@._+-]+")


def build_user_configuration(plan: Plan, *, disk_config: bool = False) -> dict:
    """The archinstall answer file for this plan, as a dict.

    Omits ``disk_config`` unless you ask for it, so the installer stops and asks
    you which disk to use. See the module docstring for why that is not
    negotiable. No password is emitted for the user account.
    """
    system: dict[str, Any] = plan.system or {}
    target: dict[str, Any] = plan.target or {}
    notes: list[str] = []

    desktop = str(target.get("desktop") or "none")
    profile_name = DESKTOP_PROFILES.get(desktop)

    gfx, gfx_note = _gfx_driver([str(v) for v in (system.get("gpu_vendors") or [])])
    if gfx_note:
        notes.append(gfx_note)

    # A legacy BIOS machine cannot boot systemd-boot at all. The plan already
    # warns about this; the config has to agree with the warning.
    firmware = str(system.get("firmware") or "unknown")
    if firmware == "UEFI":
        bootloader = "Systemd-boot"
    else:
        bootloader = "Grub"
        notes.append(
            f"Firmware reported as {firmware}, not UEFI, so the bootloader is GRUB. "
            "systemd-boot only exists on UEFI machines. If you believe this machine is "
            "really UEFI and was scanned in a legacy boot mode, fix it in firmware before "
            "installing rather than changing this line."
        )

    if profile_name:
        profile: dict[str, Any] = {
            "custom_settings": {},
            "details": [profile_name],
            "main": "Desktop",
        }
    else:
        profile = {"custom_settings": {}, "details": [], "main": "Minimal"}

    profile_config: dict[str, Any] = {"gfx_driver": gfx, "profile": profile}
    greeter = GREETERS.get(str(target.get("display_manager") or ""))
    if greeter:
        profile_config["greeter"] = greeter

    pacman = [str(p) for p in (plan.packages.get("pacman") or [])]
    provided = set(PROFILE_PACKAGES.get(desktop, ()))
    packages = [p for p in pacman if p not in provided]

    # Steam and the 32-bit halves of the driver stacks are multilib-only, and
    # multilib has to be on before the install, not after.
    repositories: list[str] = []
    if target.get("gaming") or any(p.startswith("lib32-") for p in pacman):
        repositories.append("multilib")

    sys_lang, sys_enc = _split_locale(system.get("locale"))

    notes.append(
        "No password for the user account is written here, on purpose. archinstall asks "
        "for it during the install. Depending on the release it may keep accounts in a "
        "separate user_credentials.json — if the account does not appear in the installer, "
        "add it there by hand."
    )
    notes.append(
        f"Shaped for archinstall {ARCHINSTALL_TARGET}. Check these keys against the "
        "archinstall on your ISO before relying on them; if it rejects one, delete it and "
        "answer that question in the menus."
    )

    config: dict[str, Any] = {
        "_hop": {
            "hop_version": __version__,
            "hostname": system.get("hostname"),
            "archinstall_target": ARCHINSTALL_TARGET,
            "notes": notes,
        },
        "additional-repositories": repositories,
        "audio_config": {"audio": "pipewire"},
        "bootloader": bootloader,
        "hostname": system.get("hostname") or "arch",
        "kernels": ["linux"],
        "locale_config": {
            "kb_layout": system.get("keymap") or "us",
            "sys_enc": sys_enc,
            "sys_lang": sys_lang,
        },
        "network_config": {"type": "nm"},
        "ntp": True,
        "packages": packages,
        "profile_config": profile_config,
        "swap": True,
        "timezone": system.get("timezone") or "UTC",
        "users": [
            {
                "username": system.get("username") or "user",
                "sudo": True,
                "groups": [],
            }
        ],
    }

    if disk_config:
        config["_hop"]["notes"].insert(
            0,
            "disk_config is present but empty. Fill in device_modifications yourself, "
            "looking at the disks in front of you, or delete the key and let archinstall "
            "ask.",
        )
        config["disk_config"] = {
            "_comment": (
                "hop leaves this empty on purpose. It knows what disks the old Windows "
                "machine had, which is not the same thing as knowing what disks are in "
                "the machine you are installing on today. Fill in device_modifications "
                "by hand, or delete this whole key and let archinstall ask you — that is "
                "the recommended path. As written, the installer has nowhere to install "
                "and will stop, which is the intended behaviour."
            ),
            "config_type": "manual_partitioning",
            "device_modifications": [],
        }
    else:
        config["_hop"]["notes"].insert(
            0,
            "No disk_config key: archinstall will ask you which disk to use, with the "
            "real disk list in front of you. hop never generates a partitioning plan from "
            "a scan of a different machine.",
        )

    return config


def build_post_script(plan: Plan) -> str:
    """The script to run once, as yourself, after the first boot.

    archinstall installs packages and a bootloader. It does not build an AUR
    helper, add flathub, or turn on the services you asked for. This does, in an
    order a person can follow, and it is safe to run twice.
    """
    system: dict[str, Any] = plan.system or {}
    target: dict[str, Any] = plan.target or {}

    hostname = _safe(system.get("hostname") or "arch", "hostname")
    username = _safe(system.get("username") or "user", "user name")
    helper = _safe(target.get("aur_helper") or "paru", "AUR helper")
    helper_pkg = AUR_HELPER_PACKAGES.get(helper, f"{helper}-bin")
    display_manager = target.get("display_manager")

    aur = _package_list(plan.packages.get("aur") or [])
    flatpaks = _package_list(plan.packages.get("flatpak") or [])

    lang = _safe(system.get("locale") or "en_US.UTF-8", "locale")
    locales = [_safe(loc, "locale") for loc in (system.get("locales") or [lang])]
    keymap = _safe(system.get("keymap") or "us", "keymap")
    layouts = [_safe(item, "keyboard layout") for item in (system.get("x11_layouts") or [keymap])]

    out: list[str] = []
    w = out.append

    # --- header ----------------------------------------------------------
    w("#!/usr/bin/env bash")
    w("#")
    w(f"# hop-post.sh — generated by hop2arch {__version__} for {hostname}.")
    w("#")
    w("# Run this once, as yourself, after the first boot into the new system. It does")
    w("# the parts archinstall does not: turns on the network and the display manager,")
    w("# builds an AUR helper, installs the packages archinstall cannot reach, and")
    w("# generates your locale.")
    w("#")
    w("# The packages from the official repositories are not here. archinstall installed")
    w("# them from user_configuration.json while it was building the system, which is")
    w("# earlier and faster than doing it now. If you installed Arch some other way,")
    w("# those are still missing: 'hop land hop-plan.json --only packages' fetches them.")
    w("#")
    w("# Read it before you run it. It was generated from a description of a machine you")
    w("# are no longer sitting at, and it is written to be legible line by line. Apart")
    w("# from the network, no section depends on the ones above it, so you can delete")
    w("# anything you disagree with.")
    w("#")
    w("# It does not touch disks, partitions or the bootloader, and neither does anything")
    w("# else in hop2arch. archinstall asked which disk to use with the real disk list in")
    w("# front of you, and that decision stays yours: a plan is built from a snapshot of")
    w("# another machine, and a snapshot is not a safe basis for erasing a drive.")
    w("#")
    w("# It is safe to run twice. Every step checks before it acts.")
    w("#")
    w("# It does not restore your files, keys, Wi-Fi passwords or wallpaper. That is")
    w("# 'hop land', and it is a separate step on purpose — see the end of this file.")
    w("")
    w("set -euo pipefail")
    w("")
    w("step() { printf '\\n== %s\\n' \"$1\"; }")
    w("")
    w('if [[ "$(id -u)" -eq 0 ]]; then')
    w(f'  echo "Run this as {username}, not as root." >&2')
    w('  echo "makepkg refuses to build packages as root, so the AUR step would fail." >&2')
    w('  echo "The three lines that need root call sudo themselves." >&2')
    w("  exit 1")
    w("fi")

    # --- services --------------------------------------------------------
    w("")
    w('step "Services"')
    w("")
    w("# NetworkManager is started as well as enabled: the steps below need to reach the")
    w("# internet. If you are already online through something else, expect a short blip")
    w("# while NetworkManager takes the connection over.")
    w("sudo systemctl enable NetworkManager.service")
    w("if ! systemctl is-active --quiet NetworkManager.service; then")
    w("  sudo systemctl start NetworkManager.service")
    w("fi")
    if display_manager:
        dm = _safe(display_manager, "display manager")
        w("")
        w(f"# {dm} is enabled but not started here. Starting a display manager from a")
        w("# terminal takes the console away in the middle of the script; it comes up on")
        w("# the next boot instead.")
        w(f"sudo systemctl enable {dm}.service")
    else:
        w("")
        w("# No display manager: the plan asked for no desktop, so you get a text login.")

    # --- locale ----------------------------------------------------------
    w("")
    w('step "Locale and keyboard"')
    w("")
    w("# These come from the Windows machine: the locale it was set to, plus en_US.UTF-8,")
    w("# which stays because an error message in English is the one you can search for.")
    w("for entry in " + " ".join(f'"{loc} {_encoding(loc)}"' for loc in locales) + "; do")
    w('  grep -qxF "$entry" /etc/locale.gen \\')
    w("    || printf '%s\\n' \"$entry\" | sudo tee -a /etc/locale.gen >/dev/null")
    w("done")
    w("sudo locale-gen")
    w(f'sudo localectl set-locale "LANG={lang}"')
    w(f'sudo localectl set-keymap "{keymap}"')
    if len(layouts) > 1:
        w("")
        w("# Two layouts means there has to be a key that switches between them. alt+shift")
        w("# is the chord Windows used, so it is the one your hands already know.")
        w(f'sudo localectl set-x11-keymap "{",".join(layouts)}" "" "" "grp:alt_shift_toggle"')
    else:
        w(f'sudo localectl set-x11-keymap "{layouts[0]}"')

    # --- multilib --------------------------------------------------------
    if target.get("gaming"):
        w("")
        w('step "multilib"')
        w("")
        w("# Steam and the 32-bit halves of the graphics drivers only exist in [multilib].")
        w("# archinstall should have enabled it. If it did not, this says so rather than")
        w("# editing /etc/pacman.conf behind your back.")
        w("if ! grep -q '^\\[multilib\\]' /etc/pacman.conf; then")
        w('  echo "[multilib] is not enabled in /etc/pacman.conf." >&2')
        w('  echo "Uncomment the [multilib] section and its Include line, then run:" >&2')
        w('  echo "  sudo pacman -Sy" >&2')
        w("else")
        w('  echo "[multilib] is enabled."')
        w("fi")

    # --- AUR helper ------------------------------------------------------
    w("")
    w(f'step "AUR helper ({helper})"')
    w("")
    w("# The AUR is where packages that are not in the official repositories live: build")
    w("# scripts, contributed by other users, that your machine runs.")
    w(f"# {helper_pkg} rather than {helper}: the -bin package is the same program, already")
    w("# compiled. Building it from source pulls in the whole Rust toolchain first.")
    if not aur:
        w("# Nothing in this plan comes from the AUR, but you will want the helper anyway.")
    w(f"if command -v {helper} >/dev/null 2>&1; then")
    w(f'  echo "{helper} is already installed."')
    w("else")
    w("  sudo pacman -S --needed --noconfirm base-devel git")
    w('  build_dir="$(mktemp -d)"')
    w(f'  git clone --depth 1 https://aur.archlinux.org/{helper_pkg}.git "$build_dir/{helper_pkg}"')
    w(f'  ( cd "$build_dir/{helper_pkg}" && makepkg -si --noconfirm )')
    w('  rm -rf "$build_dir"')
    w("fi")

    # --- AUR packages ----------------------------------------------------
    if aur:
        w("")
        w(f'step "AUR packages ({len(aur)})"')
        w("")
        w("# No --noconfirm here, on purpose. Each of these is a build script written by a")
        w(f"# stranger and run on your machine, and {helper} shows you every one before it")
        w("# builds. Reading them is the price of the AUR. --needed skips whatever is")
        w("# already installed, so this step is safe to repeat.")
        w(f"{helper} -S --needed \\")
        out.extend(_wrap(aur))

    # --- flatpak ---------------------------------------------------------
    if flatpaks:
        w("")
        w(f'step "Flatpak ({len(flatpaks)})"')
        w("")
        w("# Installed system-wide, so every account on the machine sees them. Put --user")
        w("# on both commands and drop the sudo if you would rather they were only yours.")
        w("sudo pacman -S --needed --noconfirm flatpak")
        w("sudo flatpak remote-add --if-not-exists flathub \\")
        w("  https://flathub.org/repo/flathub.flatpakrepo")
        w("sudo flatpak install -y flathub \\")
        out.extend(_wrap(flatpaks))

    # --- what is still missing -------------------------------------------
    w("")
    w('step "Done"')
    w("")
    w("cat <<'EOF'")
    w("")
    w("Installed: the AUR helper, the packages listed above, and the services and locale.")
    w("The repository packages came from archinstall, before this script ran.")
    w("")
    w("Not installed, and not touched by this script: your files, your SSH keys, your")
    w("Wi-Fi passwords, your bookmarks, your wallpaper. Those are in the payload")
    w("directory the scanner wrote next to your hopfile. To put them back:")
    w("")
    w("    hop land hop-plan.json --only payload --dry-run   # prints it, changes nothing")
    w("    hop land hop-plan.json --only payload --execute")
    w("")
    w("Read the dry run first. '--only payload' because the packages, the services and")
    w("the locale are the three things this script has already done; without it hop")
    w("would walk the whole plan again and you would read a long transcript of work")
    w("that is finished.")
    w("")
    w("The restore is a separate step because the permissions on a private key have to")
    w("be right the first time, and that belongs in one place rather than copied into a")
    w("generated script.")
    w("")
    w("Log out and back in, or reboot, for the display manager and the new locale.")
    w("EOF")

    return "\n".join(out) + "\n"


def write_config(plan: Plan, out_dir: str | Path, *, disk_config: bool = False) -> list[Path]:
    """Write ``user_configuration.json`` and ``hop-post.sh`` into ``out_dir``.

    Returns the paths written, in that order. The script is marked executable
    where the filesystem understands the idea, and always written with Unix line
    endings — it is usually generated on the Windows machine being left behind,
    and bash will not read a script with carriage returns in it.

    The configuration carries no disk layout and no password; the reasoning for
    both is in the module docstring, and the disk one is repeated as a comment
    header inside the script so it is in front of whoever reads that file first.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)

    config_path = directory / "user_configuration.json"
    script_path = directory / "hop-post.sh"

    config = build_user_configuration(plan, disk_config=disk_config)
    payload = json.dumps(config, indent=2, ensure_ascii=False, sort_keys=True)
    config_path.write_text(payload + "\n", encoding="utf-8", newline="\n")
    script_path.write_text(build_post_script(plan), encoding="utf-8", newline="\n")
    _make_executable(script_path)

    return [config_path, script_path]


# --- small helpers --------------------------------------------------------


def _gfx_driver(vendors: list[str]) -> tuple[str, str]:
    """Pick archinstall's graphics driver string, and explain the choice.

    Returns ``(driver, note)``; the note is empty when there is nothing worth
    saying.
    """
    found = {v.lower() for v in vendors}

    if "nvidia" in found:
        others = sorted(found - {"nvidia"})
        if others:
            return GFX_DRIVERS["nvidia"], (
                f"This machine reports {', '.join(others)} graphics as well as NVIDIA. The "
                "config picks the NVIDIA proprietary driver: mesa keeps driving the "
                "integrated GPU either way, but the discrete card is only useful with the "
                "proprietary module — nouveau cannot clock modern NVIDIA cards, so they run "
                "slow and hot. Change gfx_driver to 'All open-source' if you would rather "
                "not have an out-of-tree module in your kernel, and accept the performance."
            )
        return GFX_DRIVERS["nvidia"], (
            "NVIDIA graphics, so the config picks the proprietary driver. It is the one that "
            "performs; it is also an out-of-tree module, which means a kernel update can "
            "occasionally leave you at a text prompt. Keep a working kernel installed."
        )
    if found == {"amd"}:
        return GFX_DRIVERS["amd"], ""
    if found == {"intel"}:
        return GFX_DRIVERS["intel"], ""
    if not found:
        return GFX_DRIVERS["open"], (
            "No GPU vendor was recorded in the hopfile, so the config uses the open-source "
            "drivers. They work on everything and are the right answer when in doubt."
        )
    return GFX_DRIVERS["open"], ""


def _split_locale(locale: str | None) -> tuple[str, str]:
    """``ru_RU.UTF-8`` -> ``("ru_RU", "UTF-8")``, which is how archinstall stores it."""
    text = str(locale or "en_US.UTF-8").strip()
    if "." in text:
        lang, _, encoding = text.partition(".")
        return lang, encoding or "UTF-8"
    return text, "UTF-8"


def _encoding(locale: str) -> str:
    """The second column of a /etc/locale.gen line."""
    return _split_locale(locale)[1]


def _safe(value: Any, kind: str) -> str:
    """Check a value before it is pasted into a generated shell script.

    Everything that reaches here came out of a JSON file written on another
    machine. Package names, locales and keyboard layouts are all drawn from a
    small character set, so anything outside it is either a corrupted plan or
    someone being clever, and neither belongs in bash.
    """
    text = str(value)
    if not _SAFE.fullmatch(text):
        raise ValueError(
            f"refusing to write {text!r} into hop-post.sh: that is not a usable {kind}. "
            "Fix or remove the entry in the plan and generate the config again."
        )
    return text


def _package_list(names: list[Any]) -> list[str]:
    return [_safe(name, "package name") for name in names]


def _make_executable(path: Path) -> None:
    """chmod +x where the mode bit means something. On Windows it does not."""
    if os.name != "posix":
        return
    # A filesystem that will not take a mode bit is not a reason to fail the
    # whole write. The user can chmod it on the other side.
    with contextlib.suppress(OSError):
        path.chmod(0o755)
